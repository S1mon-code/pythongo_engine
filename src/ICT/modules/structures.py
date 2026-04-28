"""ICT v6 market structures — sweep / displacement / FVG / engulfing / micro-MSS.

纯 numpy 实现, 无依赖 ICT v3 框架.

源: ~/Desktop/ICT/ict_v3/structures.py + model.py 关键函数,
    严格保持数学等价 (no-lookahead, fractal swing, ATR(14) Wilder).

简化版 (MVP — 跑通版):
    ✓ Wilder ATR
    ✓ 3-bar fractal swing high/low (intraday_swings)
    ✓ Single-swing sweep detection (long: pierce + reclaim swing low)
    ✓ Displacement bar + 3-bar FVG detection
    ✓ Bullish/Bearish engulfing
    ✓ Micro-MSS in band
    ✗ EQL/EQH cluster priority sweep    (Phase 2)
    ✗ Strict MSS                          (Phase 2)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# ════════════════════════════════════════════════════════════════════════════
#  Result types
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SweepInfo:
    """检测到的 sweep 事件."""
    sweep_idx: int                 # 触发 sweep 的 bar index (in window)
    swept_level: float             # 被扫的 swing 价格
    swing_idx: int                 # swing 形成的 bar index
    source: str                    # "single_swing"


@dataclass(frozen=True)
class DisplacementInfo:
    """检测到的 displacement bar + FVG."""
    displacement_idx: int          # displacement bar index
    displacement_open: float
    displacement_close: float
    displacement_high: float       # long: 用作 OTE 100% 锚点
    displacement_low: float        # short: 用作 OTE 100% 锚点
    fvg_zone_low: float
    fvg_zone_high: float
    fvg_confirm_idx: int           # FVG 第 3 根 bar (确认 bar)
    atr_at_displacement: float     # displacement 时刻 ATR


# ════════════════════════════════════════════════════════════════════════════
#  ATR (Wilder, 与 ICT v3 一致)
# ════════════════════════════════════════════════════════════════════════════


def wilder_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder ATR — EWMA(alpha=1/n) of TR.

    返回 shape=(N,) 数组, 前 n-1 个为 NaN.
    """
    if highs.size == 0:
        return np.empty(0, dtype=float)
    prev_close = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum.reduce([
        highs - lows,
        np.abs(highs - prev_close),
        np.abs(lows - prev_close),
    ])
    out = np.full(highs.size, np.nan, dtype=float)
    if tr.size < n:
        return out
    # 前 n 根用 SMA(n) 作为 seed
    out[n - 1] = float(np.mean(tr[:n]))
    alpha = 1.0 / n
    for i in range(n, tr.size):
        out[i] = (1 - alpha) * out[i - 1] + alpha * tr[i]
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Intraday swings (3-bar fractal)
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IntradaySwing:
    idx: int                # 形成 swing 的 bar index
    confirmed_idx: int      # 确认 swing 的 bar index (idx + fractal_n)
    price: float


def detect_intraday_swings(
    highs: np.ndarray,
    lows: np.ndarray,
    fractal_n: int = 3,
) -> tuple[list[IntradaySwing], list[IntradaySwing]]:
    """3-bar fractal: 当 highs[t] 严格大于 highs[t±1..n] 即 swing high.

    返回 (swing_highs, swing_lows). 都按 confirmed_idx 升序.
    no-lookahead: caller 用时只取 confirmed_idx <= current_idx 的 swings.
    """
    n = highs.size
    swing_highs: list[IntradaySwing] = []
    swing_lows: list[IntradaySwing] = []
    if n < 2 * fractal_n + 1:
        return swing_highs, swing_lows
    for t in range(fractal_n, n - fractal_n):
        # Swing high
        is_sh = all(highs[t] > highs[t - k] for k in range(1, fractal_n + 1)) and \
                all(highs[t] > highs[t + k] for k in range(1, fractal_n + 1))
        if is_sh:
            swing_highs.append(IntradaySwing(
                idx=t, confirmed_idx=t + fractal_n, price=float(highs[t])
            ))
        # Swing low
        is_sl = all(lows[t] < lows[t - k] for k in range(1, fractal_n + 1)) and \
                all(lows[t] < lows[t + k] for k in range(1, fractal_n + 1))
        if is_sl:
            swing_lows.append(IntradaySwing(
                idx=t, confirmed_idx=t + fractal_n, price=float(lows[t])
            ))
    return swing_highs, swing_lows


# ════════════════════════════════════════════════════════════════════════════
#  Sweep detection (single-swing fractal)
# ════════════════════════════════════════════════════════════════════════════


def detect_swept_low(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    swing_lows: list[IntradaySwing],
    atr_series: np.ndarray,
    cur_idx: int,
    pierce_atr: float = 0.2,
    max_lookback_bars: int = 60,
) -> SweepInfo | None:
    """Long-bias sweep: 找一个被 pierce + reclaim 的 swing low.

    pierce: 某根 bar low < swing_low − pierce_atr × ATR
    reclaim: 当前 bar close > swing_low

    返回最新的 sweep, 或 None.
    """
    if cur_idx < 1 or cur_idx - 1 >= atr_series.size:
        return None
    atr_now = atr_series[cur_idx - 1]
    if not np.isfinite(atr_now) or atr_now <= 0:
        return None
    pierce_buf = pierce_atr * atr_now
    cur_close = closes[cur_idx]

    # 倒序遍历 swings (最新的 swing 优先匹配)
    for s in reversed(swing_lows):
        if s.confirmed_idx >= cur_idx:
            continue
        if cur_idx - s.idx > max_lookback_bars:
            break
        # 在 (s.idx, cur_idx] 范围找 pierce
        for k in range(s.idx + 1, cur_idx + 1):
            if lows[k] < s.price - pierce_buf:
                # pierced. 要求当前 bar close 收回到 swing 上方
                if cur_close > s.price:
                    return SweepInfo(
                        sweep_idx=k, swept_level=float(s.price),
                        swing_idx=s.idx, source="single_swing",
                    )
                break  # pierce 没 reclaim, 这个 swing 用不上 — 试更老的
    return None


def detect_swept_high(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    swing_highs: list[IntradaySwing],
    atr_series: np.ndarray,
    cur_idx: int,
    pierce_atr: float = 0.2,
    max_lookback_bars: int = 60,
) -> SweepInfo | None:
    """Short-bias sweep: 找一个被 pierce + reclaim 的 swing high (镜像)."""
    if cur_idx < 1 or cur_idx - 1 >= atr_series.size:
        return None
    atr_now = atr_series[cur_idx - 1]
    if not np.isfinite(atr_now) or atr_now <= 0:
        return None
    pierce_buf = pierce_atr * atr_now
    cur_close = closes[cur_idx]

    for s in reversed(swing_highs):
        if s.confirmed_idx >= cur_idx:
            continue
        if cur_idx - s.idx > max_lookback_bars:
            break
        for k in range(s.idx + 1, cur_idx + 1):
            if highs[k] > s.price + pierce_buf:
                if cur_close < s.price:
                    return SweepInfo(
                        sweep_idx=k, swept_level=float(s.price),
                        swing_idx=s.idx, source="single_swing",
                    )
                break
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Displacement bar + 3-bar FVG
# ════════════════════════════════════════════════════════════════════════════


def detect_bullish_displacement_after_sweep(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    sweep_idx: int,
    cur_idx: int,
    atr_series: np.ndarray,
    max_bars: int = 30,
    atr_mult: float = 1.0,
    fvg_min_atr_mult: float = 0.2,
    tick_size: float = 0.5,
) -> DisplacementInfo | None:
    """Sweep 之后 max_bars 内, 找一根 bullish displacement bar 留下 3-bar FVG.

    Bullish displacement:
        body = close - open  (positive on up bar)
        body >= atr_mult × ATR
        bar 是 sweep 之后

    Bullish FVG (3-bar):
        bar[t-1].high < bar[t+1].low (中间 bar 是 displacement)
        FVG 大小 = bar[t+1].low - bar[t-1].high
        FVG size >= fvg_min_atr_mult × ATR
    """
    n = closes.size
    end_search = min(cur_idx + 1, sweep_idx + 1 + max_bars)
    for t in range(sweep_idx + 1, end_search):
        if t < 1 or t + 1 >= n:
            continue
        if t - 1 >= atr_series.size:
            continue
        atr_t = atr_series[t - 1]
        if not np.isfinite(atr_t) or atr_t <= 0:
            continue
        body = closes[t] - opens[t]
        if body < atr_mult * atr_t:
            continue
        # Bullish bar
        if closes[t] <= opens[t]:
            continue
        # Bullish FVG: high[t-1] < low[t+1]
        prev_high = highs[t - 1]
        next_low = lows[t + 1]
        fvg_size = next_low - prev_high
        if fvg_size < fvg_min_atr_mult * atr_t:
            continue
        # FVG 确认 bar = t+1; 要求 cur_idx >= t+1
        if cur_idx < t + 1:
            continue
        return DisplacementInfo(
            displacement_idx=t,
            displacement_open=float(opens[t]),
            displacement_close=float(closes[t]),
            displacement_high=float(highs[t]),
            displacement_low=float(lows[t]),
            fvg_zone_low=float(prev_high),
            fvg_zone_high=float(next_low),
            fvg_confirm_idx=t + 1,
            atr_at_displacement=float(atr_t),
        )
    return None


def detect_bearish_displacement_after_sweep(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    sweep_idx: int,
    cur_idx: int,
    atr_series: np.ndarray,
    max_bars: int = 30,
    atr_mult: float = 1.0,
    fvg_min_atr_mult: float = 0.2,
    tick_size: float = 0.5,
) -> DisplacementInfo | None:
    """Sweep high 之后, 找一根 bearish displacement bar 留下 3-bar FVG (镜像)."""
    n = closes.size
    end_search = min(cur_idx + 1, sweep_idx + 1 + max_bars)
    for t in range(sweep_idx + 1, end_search):
        if t < 1 or t + 1 >= n:
            continue
        if t - 1 >= atr_series.size:
            continue
        atr_t = atr_series[t - 1]
        if not np.isfinite(atr_t) or atr_t <= 0:
            continue
        body = opens[t] - closes[t]   # short: bearish bar 的 body
        if body < atr_mult * atr_t:
            continue
        if closes[t] >= opens[t]:
            continue
        # Bearish FVG: low[t-1] > high[t+1]
        prev_low = lows[t - 1]
        next_high = highs[t + 1]
        fvg_size = prev_low - next_high
        if fvg_size < fvg_min_atr_mult * atr_t:
            continue
        if cur_idx < t + 1:
            continue
        return DisplacementInfo(
            displacement_idx=t,
            displacement_open=float(opens[t]),
            displacement_close=float(closes[t]),
            displacement_high=float(highs[t]),
            displacement_low=float(lows[t]),
            fvg_zone_low=float(next_high),
            fvg_zone_high=float(prev_low),
            fvg_confirm_idx=t + 1,
            atr_at_displacement=float(atr_t),
        )
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Engulfing + micro-MSS (reactive entry confirmation, optional)
# ════════════════════════════════════════════════════════════════════════════


def detect_bullish_engulfing(opens: np.ndarray, closes: np.ndarray, t: int) -> bool:
    """t 根 bar bullish engulfing prev bar."""
    if t < 1:
        return False
    prev_bear = closes[t - 1] < opens[t - 1]
    cur_bull = closes[t] > opens[t]
    engulfs = closes[t] > opens[t - 1]
    return bool(prev_bear and cur_bull and engulfs)


def detect_bearish_engulfing(opens: np.ndarray, closes: np.ndarray, t: int) -> bool:
    if t < 1:
        return False
    prev_bull = closes[t - 1] > opens[t - 1]
    cur_bear = closes[t] < opens[t]
    engulfs = closes[t] < opens[t - 1]
    return bool(prev_bull and cur_bear and engulfs)


def detect_micro_mss_bull(highs: np.ndarray, closes: np.ndarray, t: int, lookback: int = 5) -> bool:
    """t 根 close 突破前 lookback 根 high."""
    if t < lookback:
        return False
    prior_high = float(highs[max(0, t - lookback): t].max())
    return bool(closes[t] > prior_high)


def detect_micro_mss_bear(lows: np.ndarray, closes: np.ndarray, t: int, lookback: int = 5) -> bool:
    if t < lookback:
        return False
    prior_low = float(lows[max(0, t - lookback): t].min())
    return bool(closes[t] < prior_low)
