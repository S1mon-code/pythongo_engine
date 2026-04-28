"""ICT v6 时区 helpers.

PythonGO 实盘 tick.datetime 已经是 CST (中国标准时间, 上海时区, UTC+8).
所有 kill zone / lunch break / hard cutoff 判断直接用 tick.datetime.time().

不需要 ET/CT 转换 (NQ 版本需要, CN 版本不需要).
"""
from __future__ import annotations

from datetime import datetime, time, timezone, timedelta

# CST = UTC+8
CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    """当前 CST 时间."""
    return datetime.now(CST).replace(tzinfo=None)


def time_in_window(t: time, start: time, end: time) -> bool:
    """t 是否在 [start, end) 区间内 (不跨午夜).

    标准 [start, end) 半开区间 — 跟 SessionGuard 一致.
    """
    return start <= t < end
