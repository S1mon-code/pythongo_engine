"""ICT v6 D1 bias engine.

每天 00:00 (CST 商品期货新交易日 = 21:00 夜盘开始) 计算"今天的 bias":
    - bull → 只做 long
    - bear → 只做 short
    - neutral → 不开仓

Bias = direction of last MSS (Market Structure Shift) on D1.

源: ~/Desktop/ICT/ict_v3/bias.py
简化版: 不做 PD-zone gate (策略层做), 不做 dealing range (策略层处理).
       核心 = MSS detection + bias 标签.

No-lookahead: 第 t 天的 bias 只用 [0..t-1] 的 D1 数据 (yesterday's close 算).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from .structures import IntradaySwing, detect_intraday_swings, wilder_atr


# ════════════════════════════════════════════════════════════════════════════
#  Result types
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DailyBias:
    date: date
    bias: str                          # "bull" | "bear" | "neutral"
    last_mss_direction: str | None     # "bull" | "bear" | None
    last_mss_idx: int                  # 上次 MSS 在 D1 数据里的 index
    days_since_mss: int                # 至今几天
    dealing_range_high: float          # 最近 lookback 天的 swing high 高位
    dealing_range_low: float           # 最近 lookback 天的 swing low 低位
    equilibrium: float                 # (high + low) / 2
    current_close: float               # 昨天收盘价 (frozen 当天 bias 用)
    pd_zone: str                       # "premium" | "discount" | "equilibrium_band"
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MSSEvent:
    idx: int                           # D1 index
    direction: str                     # "bull" | "bear"
    broken_swing_idx: int              # 被打破的 swing index
    broken_price: float


# ════════════════════════════════════════════════════════════════════════════
#  D1 swings (fractal_n=2, 与 NQ 版一致)
# ════════════════════════════════════════════════════════════════════════════


def detect_d1_swings(
    d1_highs: np.ndarray,
    d1_lows: np.ndarray,
    fractal_n: int = 2,
) -> tuple[list[IntradaySwing], list[IntradaySwing]]:
    """D1 swing 用 2-bar fractal (intraday 用 3-bar). 复用同一个 detect 函数."""
    return detect_intraday_swings(d1_highs, d1_lows, fractal_n=fractal_n)


# ════════════════════════════════════════════════════════════════════════════
#  D1 MSS (Market Structure Shift) detection
# ════════════════════════════════════════════════════════════════════════════


def detect_d1_mss(
    d1_highs: np.ndarray,
    d1_lows: np.ndarray,
    d1_closes: np.ndarray,
    swing_highs: list[IntradaySwing],
    swing_lows: list[IntradaySwing],
    displacement_atr_mult: float = 1.0,
    atr_lookback: int = 14,
) -> list[MSSEvent]:
    """检测 D1 MSS:
       Bull MSS: D1 close > 最近 swing high 且 displacement >= atr_mult × ATR
       Bear MSS: D1 close < 最近 swing low 且 displacement >= atr_mult × ATR

    返回按时间升序的 MSS event list.
    """
    n = d1_closes.size
    if n < atr_lookback + 1:
        return []
    atr = wilder_atr(d1_highs, d1_lows, d1_closes, atr_lookback)

    events: list[MSSEvent] = []
    for t in range(atr_lookback, n):
        cur_close = d1_closes[t]
        atr_t = atr[t]
        if not np.isfinite(atr_t) or atr_t <= 0:
            continue
        prev_close = d1_closes[t - 1]
        body = abs(cur_close - prev_close)
        if body < displacement_atr_mult * atr_t:
            continue

        # Bull MSS: 当前 close > 最近 swing high
        recent_sh = [s for s in swing_highs if s.confirmed_idx < t]
        if recent_sh:
            last_sh = recent_sh[-1]
            if cur_close > last_sh.price:
                events.append(MSSEvent(
                    idx=t, direction="bull",
                    broken_swing_idx=last_sh.idx,
                    broken_price=last_sh.price,
                ))
                continue

        # Bear MSS: 当前 close < 最近 swing low
        recent_sl = [s for s in swing_lows if s.confirmed_idx < t]
        if recent_sl:
            last_sl = recent_sl[-1]
            if cur_close < last_sl.price:
                events.append(MSSEvent(
                    idx=t, direction="bear",
                    broken_swing_idx=last_sl.idx,
                    broken_price=last_sl.price,
                ))
    return events


# ════════════════════════════════════════════════════════════════════════════
#  Compute daily bias for each D1 bar
# ════════════════════════════════════════════════════════════════════════════


def compute_daily_bias(
    d1_highs: np.ndarray,
    d1_lows: np.ndarray,
    d1_closes: np.ndarray,
    d1_dates: list[date],
    fractal_n: int = 2,
    lookback_days: int = 20,
    displacement_atr_mult: float = 1.0,
    eq_band: float = 0.10,
    max_mss_age_days: int = 5,
    pd_threshold: float = 0.5,
) -> list[DailyBias]:
    """每根 D1 bar 算一个 bias. 用 [0..t-1] 历史数据 (no-lookahead).

    Returns list of DailyBias, len == d1_closes.size.
    """
    n = d1_closes.size
    if n == 0:
        return []
    swing_highs, swing_lows = detect_d1_swings(d1_highs, d1_lows, fractal_n=fractal_n)
    mss_events = detect_d1_mss(
        d1_highs, d1_lows, d1_closes,
        swing_highs, swing_lows,
        displacement_atr_mult=displacement_atr_mult,
    )

    biases: list[DailyBias] = []
    for t in range(n):
        if t < lookback_days:
            biases.append(DailyBias(
                date=d1_dates[t], bias="neutral",
                last_mss_direction=None, last_mss_idx=-1, days_since_mss=999,
                dealing_range_high=float(d1_highs[t]),
                dealing_range_low=float(d1_lows[t]),
                equilibrium=float((d1_highs[t] + d1_lows[t]) / 2),
                current_close=float(d1_closes[t]),
                pd_zone="equilibrium_band",
                notes=["warmup"],
            ))
            continue

        window_lo = max(0, t - lookback_days)
        past_mss = [m for m in mss_events if m.idx < t]
        last_mss = past_mss[-1] if past_mss else None
        last_mss_dir = last_mss.direction if last_mss else None
        last_mss_idx = last_mss.idx if last_mss else -999
        days_since = t - last_mss_idx if last_mss else 999

        # Dealing range 用 [0..t-1] 已确认的 swings
        past_sh = [s for s in swing_highs if s.confirmed_idx < t and s.idx >= window_lo]
        past_sl = [s for s in swing_lows if s.confirmed_idx < t and s.idx >= window_lo]
        if past_sh and past_sl:
            dr_high = max(s.price for s in past_sh)
            dr_low = min(s.price for s in past_sl)
        else:
            dr_high = float(d1_highs[window_lo:t].max())
            dr_low = float(d1_lows[window_lo:t].min())
        eq = (dr_high + dr_low) / 2
        cur_close = float(d1_closes[t - 1])  # frozen at "today 00:00" = yesterday's close

        if dr_high - dr_low <= 0:
            pd_zone = "equilibrium_band"
        else:
            pos = (cur_close - dr_low) / (dr_high - dr_low)
            if pd_threshold - eq_band <= pos <= pd_threshold + eq_band:
                pd_zone = "equilibrium_band"
            elif pos > pd_threshold:
                pd_zone = "premium"
            else:
                pd_zone = "discount"

        notes: list[str] = []
        bias = "neutral"
        if last_mss is None:
            notes.append("no MSS in history")
        elif pd_zone == "equilibrium_band" and days_since > max_mss_age_days:
            notes.append(f"eq band AND MSS old ({days_since}d) → neutral")
        else:
            bias = last_mss_dir or "neutral"

        biases.append(DailyBias(
            date=d1_dates[t], bias=bias,
            last_mss_direction=last_mss_dir, last_mss_idx=last_mss_idx,
            days_since_mss=days_since,
            dealing_range_high=dr_high, dealing_range_low=dr_low,
            equilibrium=eq, current_close=cur_close,
            pd_zone=pd_zone, notes=notes,
        ))
    return biases


def bias_for_date(biases: list[DailyBias], target: date) -> DailyBias | None:
    """按日期查 bias. 返 <= target 的最新一条 (frozen 在该日 00:00 早上)."""
    valid = [b for b in biases if b.date <= target]
    return valid[-1] if valid else None
