"""ICT v6 时区 helpers.

PythonGO 实盘 tick.datetime 已经是 CST (中国标准时间, 上海时区, UTC+8).
所有 kill zone / lunch break / hard cutoff 判断直接用 tick.datetime.time().

不需要 ET/CT 转换 (NQ 版本需要, CN 版本不需要).
"""
from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from typing import Any

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


def to_python_datetime(dt: Any) -> datetime | None:
    """把 PythonGO 任意 datetime 表达 (datetime / numpy datetime64 / pandas Timestamp /
    str / None) 转成 Python stdlib datetime.

    返 None 表示无法转换 (caller 决定怎么处理).

    PythonGO V2 实测: TickData.datetime / KLineData.datetime 是 datetime.datetime
    (经源码审计 docs/pythongo/classdef.md 确认), 但仍兜底防御.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt
    # pandas Timestamp 有 to_pydatetime
    if hasattr(dt, "to_pydatetime"):
        try:
            return dt.to_pydatetime()
        except Exception:
            pass
    # numpy datetime64 → 用 pandas / 手工转
    if hasattr(dt, "astype"):
        try:
            import pandas as pd
            return pd.Timestamp(dt).to_pydatetime()
        except Exception:
            pass
    # str fallback
    if isinstance(dt, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y%m%d %H:%M:%S"):
            try:
                return datetime.strptime(dt, fmt)
            except ValueError:
                continue
    return None
