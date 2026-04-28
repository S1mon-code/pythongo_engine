"""QExp robust signals — 移植自 QExp overlay/signals/.

来源: QExp audit-2026-04-26 / 2026-04-27 通过严格 robust 测试 (ex-best-year Δ < 0.20)
的 4 个生产策略信号. 翻译为纯 numpy 实现, 无 QExp 框架依赖, 直接供 PythonGO 策略使用.

| 信号 | 品种 | 周期 | 方向 | 8y Sharpe | ex-best Δ |
|------|------|------|------|-----------|-----------|
| MomentumContinuationSignal       | AG | 5min  | long  | +0.908 | -0.09 |
| VolSqueezeBreakoutLongV2Signal   | AG | 5min  | long  | +0.470 | -0.14 |
| PullbackStrongTrendSignal        | I  | 15min | long  | +0.374 | -0.15 |
| HighVolBreakdownShortSignal      | HC | 30min | short | +0.544 | -0.13 |

每个 signal 暴露 `compute(opens, highs, lows, closes, bar_idx)` →
    SignalResult(fires: bool, entry_price: float, atr: float, metadata: dict)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  Result types
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SignalResult:
    """信号 compute 输出.

    fires: 是否触发开仓 (long 信号: 买入触发; short 信号: 卖出触发)
    entry_price: 触发时建议的成交参考价 (策略 close_raw)
    atr: 当前 ATR (供 strategy 算 profit_target = entry ± 2 × ATR)
    metadata: debug 字段, log 用
    """
    fires: bool
    entry_price: float = 0.0
    atr: float = 0.0
    metadata: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
#  Indicator helpers (纯 numpy, 与 QExp 实现严格对齐)
# ══════════════════════════════════════════════════════════════════════════════


def true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """TR = max(H-L, |H-prev_C|, |L-prev_C|).

    第一根 bar 没有 prev_close, 用 closes[0] 自身 (TR=H-L).
    """
    if highs.size == 0:
        return np.empty(0, dtype=float)
    prev = np.concatenate([[closes[0]], closes[:-1]])
    return np.maximum.reduce([
        highs - lows,
        np.abs(highs - prev),
        np.abs(lows - prev),
    ])


def atr_window(tr: np.ndarray, lookback: int) -> float:
    """ATR = mean(TR[-lookback:]). 不够 lookback 返 NaN."""
    if tr.size < lookback:
        return float("nan")
    return float(np.mean(tr[-lookback:]))


def rolling_atr_history(tr: np.ndarray, atr_lookback: int, history_lookback: int) -> list[float]:
    """生成 history_lookback 个连续的 ATR 值 (供算 hist_atr_mean).

    每个 ATR 是 atr_lookback 长度的 mean, 滑窗 history_lookback 次 (剔除最新一窗).
    返回 history_lookback 个 float, 不够则 raise.
    """
    if tr.size < atr_lookback + history_lookback + 1:
        raise ValueError("insufficient TR history")
    out: list[float] = []
    for i in range(history_lookback):
        offset = -(atr_lookback + history_lookback - i)
        out.append(float(np.mean(tr[offset:offset + atr_lookback])))
    return out


def trend_zscore(closes: np.ndarray, return_lookback: int, z_lookback: int) -> float:
    """趋势 z-score: 当前 N-bar log-return vs 过去 z_lookback 期分布的标准化值.

    return_lookback: log-return 计算窗口 (默认 60)
    z_lookback: 用多少历史 returns 估均值/方差 (默认 240)
    """
    if closes.size < z_lookback + return_lookback + 1:
        return float("nan")
    log_close = np.log(np.maximum(closes, 1e-9))
    r = log_close[return_lookback:] - log_close[:-return_lookback]
    if r.size < z_lookback + 1:
        return float("nan")
    win = r[-(z_lookback + 1):-1]
    cur_r = float(r[-1])
    sigma = float(np.std(win, ddof=1))
    if sigma <= 0:
        return float("nan")
    return (cur_r - float(np.mean(win))) / sigma


# ══════════════════════════════════════════════════════════════════════════════
#  1. MomentumContinuationSignal — AG 5min long (Sharpe +0.908)
# ══════════════════════════════════════════════════════════════════════════════


class MomentumContinuationSignal:
    """强阳线连续 (long-bias swing).

    思路: 单根 bar 的 body > 1.5×ATR + 收阳 + close 在 range top 80% 是
        显著的方向信号, 趋势跟踪资金会在下一 bar 入场. 我们在当前 bar
        close 时建仓, front-run 1 根 bar.

    入场:
        body = close - open (positive on up bar)
        range = high - low
        body_to_range = body / range
        atr = ATR(20)
        FIRES iff:
            close > open
            AND body > body_atr_mult × atr           (1.5×)
            AND body_to_range >= body_to_range_min   (0.6)
            AND cooldown_bars 已过 (3 bars)
    """

    name = "momentum_continuation"
    bias = "long"

    def __init__(
        self,
        atr_lookback: int = 20,
        body_atr_mult: float = 1.5,
        body_to_range_min: float = 0.6,
        cooldown_bars: int = 3,
    ):
        self.atr_n = int(atr_lookback)
        self.body_atr = float(body_atr_mult)
        self.body_range_min = float(body_to_range_min)
        self.cooldown = int(cooldown_bars)
        self._last_fired_idx = -10**9

    @property
    def warmup(self) -> int:
        return self.atr_n + 1

    def reset(self) -> None:
        self._last_fired_idx = -10**9

    def compute(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        bar_idx: int,
    ) -> SignalResult:
        n = closes.size
        if n < self.warmup:
            return SignalResult(False, metadata={"state": "insufficient_history"})

        tr = true_range(highs, lows, closes)
        atr = atr_window(tr, self.atr_n)
        if not np.isfinite(atr) or atr <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})

        op, hi, lo, cl = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
        bar_range = hi - lo
        body = cl - op
        body_to_range = (body / bar_range) if bar_range > 0 else 0.0
        is_up = cl > op

        new_window = (bar_idx - self._last_fired_idx) >= self.cooldown
        fires = bool(
            is_up
            and new_window
            and body > self.body_atr * atr
            and body_to_range >= self.body_range_min
        )
        if fires:
            self._last_fired_idx = bar_idx

        return SignalResult(
            fires=fires,
            entry_price=cl,
            atr=atr,
            metadata={
                "state": "fires" if fires else "idle",
                "atr": atr,
                "body": body,
                "body_to_range": body_to_range,
                "is_up": is_up,
                "cooldown_ok": new_window,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  2. VolSqueezeBreakoutLongV2Signal — AG 5min long (Sharpe +0.470)
# ══════════════════════════════════════════════════════════════════════════════


class VolSqueezeBreakoutLongV2Signal:
    """ATR squeeze + breakout + trend filter.

    入场 (5 个全满足):
        1. trend z_60 > +0.5  (uptrend)
        2. cur_ATR / mean(60 个 hist ATR) <= 0.8  (squeeze)
        3. close > 20-bar high (excl current)  (breakout)
        4. body_to_range >= 0.5  (强收盘)
        5. cooldown_bars 已过 (5 bars)
    """

    name = "vol_squeeze_breakout_long_v2"
    bias = "long"

    def __init__(
        self,
        atr_lookback: int = 20,
        atr_history_lookback: int = 60,
        squeeze_ratio: float = 0.8,
        breakout_lookback: int = 20,
        body_to_range_min: float = 0.5,
        trend_return_lookback: int = 60,
        trend_z_lookback: int = 240,
        trend_z_min: float = 0.5,
        cooldown_bars: int = 5,
    ):
        self.atr_n = int(atr_lookback)
        self.atr_hist = int(atr_history_lookback)
        self.squeeze_r = float(squeeze_ratio)
        self.brk_n = int(breakout_lookback)
        self.body_range_min = float(body_to_range_min)
        self.tr_n = int(trend_return_lookback)
        self.tz_n = int(trend_z_lookback)
        self.tz_min = float(trend_z_min)
        self.cooldown = int(cooldown_bars)
        self._last_fired_idx = -10**9

    @property
    def warmup(self) -> int:
        return max(
            self.atr_n + self.atr_hist,
            self.brk_n,
            self.tz_n + self.tr_n,
        ) + 1

    def reset(self) -> None:
        self._last_fired_idx = -10**9

    def compute(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        bar_idx: int,
    ) -> SignalResult:
        if closes.size < self.warmup:
            return SignalResult(False, metadata={"state": "insufficient_history"})

        z_trend = trend_zscore(closes, self.tr_n, self.tz_n)
        if not np.isfinite(z_trend):
            return SignalResult(False, metadata={"state": "z_trend_nan"})
        if z_trend < self.tz_min:
            return SignalResult(False, metadata={"state": "trend_filter_off", "z_trend": z_trend})

        tr = true_range(highs, lows, closes)
        cur_atr = atr_window(tr, self.atr_n)
        if not np.isfinite(cur_atr) or cur_atr <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})
        try:
            atr_hist = rolling_atr_history(tr, self.atr_n, self.atr_hist)
        except ValueError:
            return SignalResult(False, metadata={"state": "insufficient_history"})
        hist_atr_mean = float(np.mean(atr_hist))
        if hist_atr_mean <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})
        squeeze = cur_atr / hist_atr_mean

        if highs.size < self.brk_n + 1:
            return SignalResult(False, metadata={"state": "insufficient_history"})
        breakout_level = float(highs[-(self.brk_n + 1):-1].max())

        op, hi, lo, cl = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
        bar_range = hi - lo
        body = cl - op
        body_to_range = (body / bar_range) if bar_range > 0 else 0.0

        new_window = (bar_idx - self._last_fired_idx) >= self.cooldown
        is_squeeze = squeeze <= self.squeeze_r
        is_breakout = cl > breakout_level
        is_strong_close = body_to_range >= self.body_range_min

        fires = bool(new_window and is_squeeze and is_breakout and is_strong_close)
        if fires:
            self._last_fired_idx = bar_idx

        return SignalResult(
            fires=fires,
            entry_price=cl,
            atr=cur_atr,
            metadata={
                "state": "fires" if fires else "idle",
                "z_trend": z_trend,
                "squeeze_ratio": squeeze,
                "breakout_level": breakout_level,
                "atr": cur_atr,
                "body_to_range": body_to_range,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  3. PullbackStrongTrendSignal — I 15min long (Sharpe +0.374)
# ══════════════════════════════════════════════════════════════════════════════


class PullbackStrongTrendSignal:
    """强趋势中的回撤入场.

    入场 (4 条件):
        1. trend z_60 > +1.0 (强 uptrend, 比 v2 +0.5 更严)
        2. 20-bar rolling max 在最近 8 bar 内 (最近创新高)
        3. (rolling_max - cur_close) / atr >= 1.5 (深度回撤至少 1.5 ATR)
        4. cooldown_bars 已过 (5 bars)
    """

    name = "pullback_strong_trend"
    bias = "long"

    def __init__(
        self,
        lookback: int = 20,
        atr_lookback: int = 20,
        k_atr: float = 1.5,
        recent_high_lookback: int = 8,
        cooldown_bars: int = 5,
        trend_return_lookback: int = 60,
        trend_z_lookback: int = 240,
        trend_z_min: float = 1.0,
    ):
        self.lb = int(lookback)
        self.atr_n = int(atr_lookback)
        self.k_atr = float(k_atr)
        self.recent_n = int(recent_high_lookback)
        self.cooldown = int(cooldown_bars)
        self.tr_n = int(trend_return_lookback)
        self.tz_n = int(trend_z_lookback)
        self.tz_min = float(trend_z_min)
        self._last_fired_idx = -10**9

    @property
    def warmup(self) -> int:
        return max(self.lb, self.atr_n, self.tz_n + self.tr_n) + 1

    def reset(self) -> None:
        self._last_fired_idx = -10**9

    def compute(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        bar_idx: int,
    ) -> SignalResult:
        if closes.size < self.warmup:
            return SignalResult(False, metadata={"state": "insufficient_history"})

        z_trend = trend_zscore(closes, self.tr_n, self.tz_n)
        if not np.isfinite(z_trend):
            return SignalResult(False, metadata={"state": "z_trend_nan"})
        if z_trend < self.tz_min:
            return SignalResult(False, metadata={"state": "trend_filter_off", "z_trend": z_trend})

        if highs.size < self.lb + 1:
            return SignalResult(False, metadata={"state": "insufficient_history"})
        recent_window = highs[-(self.lb + 1):-1]
        rolling_max = float(recent_window.max())
        # bars_since_max: 距 rolling_max 还有几 bar (越小越近)
        bars_since_max = (self.lb - 1) - int(np.argmax(recent_window))

        tr = true_range(highs, lows, closes)
        atr = atr_window(tr, self.atr_n)
        if not np.isfinite(atr) or atr <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})

        cl = float(closes[-1])
        pullback_atr_units = (rolling_max - cl) / atr if atr > 0 else 0.0

        new_window = (bar_idx - self._last_fired_idx) >= self.cooldown
        recent_high = bars_since_max <= self.recent_n
        deep_enough = pullback_atr_units >= self.k_atr

        fires = bool(new_window and recent_high and deep_enough)
        if fires:
            self._last_fired_idx = bar_idx

        return SignalResult(
            fires=fires,
            entry_price=cl,
            atr=atr,
            metadata={
                "state": "fires" if fires else "idle",
                "z_trend": z_trend,
                "rolling_max": rolling_max,
                "pullback_atr_units": pullback_atr_units,
                "bars_since_max": bars_since_max,
                "atr": atr,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  4. HighVolBreakdownShortSignal — HC 30min short (Sharpe +0.544)
# ══════════════════════════════════════════════════════════════════════════════


class HighVolBreakdownShortSignal:
    """波动扩张 + 跌破 = panic-selling/regime-change SHORT.

    思路: 不靠日线 SMA 下方判断"已确认下跌", 而是用**波动放大**直接捕捉 regime change.

    入场 (4 条件):
        1. cur_ATR / mean(60 hist ATR) >= 1.3 (vol expansion)
        2. cur_close < 20-bar low (excl current) (breakdown)
        3. cur_close < cur_open AND body_to_range >= 0.5 (强阴线, 不是 wick)
        4. cooldown_bars 已过 (5 bars)
    """

    name = "high_vol_breakdown_short"
    bias = "short"

    def __init__(
        self,
        atr_lookback: int = 20,
        atr_history_lookback: int = 60,
        vol_expansion_min: float = 1.3,
        breakdown_lookback: int = 20,
        body_to_range_min: float = 0.5,
        cooldown_bars: int = 5,
    ):
        self.atr_n = int(atr_lookback)
        self.atr_hist = int(atr_history_lookback)
        self.vol_min = float(vol_expansion_min)
        self.brk_n = int(breakdown_lookback)
        self.body_range_min = float(body_to_range_min)
        self.cooldown = int(cooldown_bars)
        self._last_fired_idx = -10**9

    @property
    def warmup(self) -> int:
        return max(self.atr_n + self.atr_hist, self.brk_n) + 1

    def reset(self) -> None:
        self._last_fired_idx = -10**9

    def compute(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        bar_idx: int,
    ) -> SignalResult:
        if closes.size < self.warmup:
            return SignalResult(False, metadata={"state": "insufficient_history"})

        tr = true_range(highs, lows, closes)
        cur_atr = atr_window(tr, self.atr_n)
        if not np.isfinite(cur_atr) or cur_atr <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})
        try:
            atr_hist = rolling_atr_history(tr, self.atr_n, self.atr_hist)
        except ValueError:
            return SignalResult(False, metadata={"state": "insufficient_history"})
        hist_atr_mean = float(np.mean(atr_hist))
        if hist_atr_mean <= 0:
            return SignalResult(False, metadata={"state": "degenerate"})
        vol_ratio = cur_atr / hist_atr_mean
        if vol_ratio < self.vol_min:
            return SignalResult(False, metadata={"state": "no_vol_expansion", "vol_ratio": vol_ratio})

        if lows.size < self.brk_n + 1:
            return SignalResult(False, metadata={"state": "insufficient_history"})
        breakdown_level = float(lows[-(self.brk_n + 1):-1].min())

        op, hi, lo, cl = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
        bar_range = hi - lo
        body = op - cl  # short: body = open - close (positive on bear bar)
        body_to_range = (body / bar_range) if bar_range > 0 else 0.0

        new_window = (bar_idx - self._last_fired_idx) >= self.cooldown
        is_breakdown = cl < breakdown_level
        is_strong_bear = (cl < op) and (body_to_range >= self.body_range_min)

        fires = bool(new_window and is_breakdown and is_strong_bear)
        if fires:
            self._last_fired_idx = bar_idx

        return SignalResult(
            fires=fires,
            entry_price=cl,
            atr=cur_atr,
            metadata={
                "state": "fires" if fires else "idle",
                "vol_ratio": vol_ratio,
                "breakdown_level": breakdown_level,
                "atr": cur_atr,
                "body_to_range": body_to_range,
            },
        )
