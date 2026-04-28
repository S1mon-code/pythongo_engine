"""ICT v6 CN futures session structure.

替代 NQ 版本的 ET kill zones, 适配中国期货市场 CST 时区.

理论映射 (ICT 哲学不变 — 只在高流动性窗口交易):

    NQ kill zones (ET)           →  CN futures (CST)
    ────────────────────────────────────────────────
    London_KZ (02:00-05:00)      →  NIGHT_OPEN_KZ (21:00-22:00)
    London_SB (03:00-04:00)      →  NIGHT_OPEN_SB (21:00-21:30)
    NY_AM_KZ  (07:00-10:00)      →  DAY_OPEN_KZ   (09:00-10:00)
    NY_AM_SB  (10:00-11:00)      →  DAY_OPEN_SB   (09:00-09:30)
    NY_PM_KZ  (13:30-16:00)      →  AFTERNOON_KZ  (13:30-14:30)
    NY_PM_SB  (14:00-15:00)      →  AFTERNOON_SB  (13:30-14:00)

中国期货特殊:
    - 早盘 10:15-10:30 茶歇 (无成交, broker 拒单)
    - 午盘 11:30-13:30 lunch break (无成交)
    - 日盘 14:50 hard cutoff (10 min 提前强平, 避开收盘流动性恶化)
    - 夜盘视品种而定 (I/铁矿: 21:00-23:00, 部分品种到 02:30)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from .timezones import time_in_window


# ════════════════════════════════════════════════════════════════════════════
#  CN 期货 kill zone 定义 (CST)
# ════════════════════════════════════════════════════════════════════════════

CN_KILL_ZONES: dict[str, tuple[time, time]] = {
    "DAY_OPEN_KZ":     (time(9, 0),  time(10, 0)),    # 日盘开盘 1 小时
    "DAY_OPEN_SB":     (time(9, 0),  time(9, 30)),    # 日盘 Silver Bullet 30min
    "MORNING_KZ":      (time(10, 30), time(11, 30)),  # 早盘后段
    "AFTERNOON_KZ":    (time(13, 30), time(14, 30)),  # 午盘恢复
    "AFTERNOON_SB":    (time(13, 30), time(14, 0)),   # 午盘 SB
    "DAY_CLOSE_KZ":    (time(14, 30), time(15, 0)),   # 日盘收盘
    "NIGHT_OPEN_KZ":   (time(21, 0),  time(22, 0)),   # 夜盘开盘
    "NIGHT_OPEN_SB":   (time(21, 0),  time(21, 30)),  # 夜盘 SB
}

# I (铁矿) 默认 allowed kill zones (高流动性窗口) — DCE 21:00-23:00 夜盘
DEFAULT_CN_ALLOWED_KZS: tuple[str, ...] = (
    "DAY_OPEN_KZ", "DAY_OPEN_SB",
    "AFTERNOON_KZ", "AFTERNOON_SB",
    "NIGHT_OPEN_KZ", "NIGHT_OPEN_SB",
)

# 茶歇 (DCE/SHFE/INE/CZCE 商品期货早盘 10:15-10:30 不成交, broker 会拒单)
TEA_BREAK = (time(10, 15), time(10, 30))

# 午餐 (11:30-13:30 全市场 lunch break)
LUNCH_BREAK = (time(11, 30), time(13, 30))

# Hard cutoff (强平窗口 — 避开收盘流动性恶化)
DAY_HARD_CUTOFF = (time(14, 50), time(15, 0))    # 日盘最后 10 min
NIGHT_HARD_CUTOFF_DCE = (time(22, 50), time(23, 0))  # DCE 夜盘最后 10 min (I 适用)


@dataclass(frozen=True)
class SessionRange:
    """某一时段的 high/low (供 ICT 当作 liquidity pool 参考)."""
    high: float
    low: float
    start_ts: datetime
    end_ts: datetime


# ════════════════════════════════════════════════════════════════════════════
#  Kill zone 判断
# ════════════════════════════════════════════════════════════════════════════


def get_active_kill_zone(ts: datetime) -> str | None:
    """返回当前时刻所在的 kill zone 名 (CST). 不在任何 KZ 时返 None."""
    t = ts.time()
    # 注意: SB 是 KZ 子集. 优先匹配更窄的 SB (有更高 priority).
    sb_first = ["DAY_OPEN_SB", "AFTERNOON_SB", "NIGHT_OPEN_SB"]
    others = [k for k in CN_KILL_ZONES if k not in sb_first]
    for name in sb_first + others:
        start, end = CN_KILL_ZONES[name]
        if time_in_window(t, start, end):
            return name
    return None


def in_any_kill_zone(ts: datetime) -> bool:
    return get_active_kill_zone(ts) is not None


def in_lunch_break(ts: datetime) -> bool:
    """11:30-13:30 lunch break OR 10:15-10:30 茶歇."""
    t = ts.time()
    return time_in_window(t, *LUNCH_BREAK) or time_in_window(t, *TEA_BREAK)


def past_hard_cutoff(ts: datetime, include_night: bool = True) -> bool:
    """是否进入 hard cutoff 窗口 (强平期).

    日盘: 14:50-15:00
    夜盘 (DCE): 22:50-23:00 (I 铁矿适用)
    """
    t = ts.time()
    if time_in_window(t, *DAY_HARD_CUTOFF):
        return True
    if include_night and time_in_window(t, *NIGHT_HARD_CUTOFF_DCE):
        return True
    return False


def can_trade(ts: datetime, allowed_kzs: tuple[str, ...] = DEFAULT_CN_ALLOWED_KZS) -> bool:
    """综合判断: 在 allowed KZ + 不在 lunch break + 不在 hard cutoff."""
    if in_lunch_break(ts):
        return False
    if past_hard_cutoff(ts):
        return False
    kz = get_active_kill_zone(ts)
    return kz in allowed_kzs
