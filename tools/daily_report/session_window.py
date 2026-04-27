"""交易日时间窗口.

T 日报告范围: T-1 个交易日 21:00 → T 日 15:00.
跨周末/节假日往前回溯到上一交易日 21:00.

例: 周一 (2026-04-27) 报告 = 周五 21:00 (2026-04-24) → 周一 15:00 (2026-04-27)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


# 中国大陆主要节假日 (粗筛, 重点覆盖期货休市). 不准时只会偏 1 天, 影响很小.
_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23), date(2026, 2, 24),
        date(2026, 4, 6),
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
        date(2026, 6, 22),
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6),
        date(2026, 10, 7), date(2026, 10, 8),
    }
)


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _HOLIDAYS_2026:
        return False
    return True


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


@dataclass(frozen=True)
class SessionWindow:
    target_date: date
    start: datetime  # T-1 trading day 21:00
    end: datetime    # T 15:00

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt <= self.end


def session_window_for(target_date: date) -> SessionWindow:
    """T 日报告窗口: T-1 个交易日 21:00 ~ T 日 15:00."""
    prev = previous_trading_day(target_date)
    start = datetime.combine(prev, time(21, 0, 0))
    end = datetime.combine(target_date, time(15, 0, 0))
    return SessionWindow(target_date=target_date, start=start, end=end)
