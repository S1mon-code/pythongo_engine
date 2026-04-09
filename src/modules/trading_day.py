"""交易日检测模块.

中国期货交易日从夜盘21:00开始，到次日15:00结束。
21:00之后的所有交易归属下一个交易日。

用法:
    from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR

    td = get_trading_day()           # "20260402"
    changed = is_new_day("20260401") # True if trading day changed
"""
from datetime import datetime, timedelta

# 交易日从21:00开始（夜盘开盘）
DAY_START_HOUR = 21


def get_trading_day(now: datetime | None = None) -> str:
    """根据当前时间推算交易日.

    规则:
    - 21:00及之后 → 归属下一个交易日（夜盘）
    - 00:00-20:59 → 归属当天交易日（日盘/凌晨夜盘尾段）
    - 周五21:00+ → 跳到下周一
    - 周六全天 → 下周一
    - 周日全天 → 下周一

    Args:
        now: 可选，用于测试。默认datetime.now()。
    """
    if now is None:
        now = datetime.now()

    if now.hour >= DAY_START_HOUR:
        # 21:00+ → 下一个自然日
        td = now + timedelta(days=1)
    else:
        # 00:00-20:59 → 当天
        td = now

    # 跳过周末
    wd = td.weekday()
    if wd == 5:       # 周六 → 周一
        td += timedelta(days=2)
    elif wd == 6:     # 周日 → 周一
        td += timedelta(days=1)

    return td.strftime("%Y%m%d")


def is_new_day(current_td: str, now: datetime | None = None) -> bool:
    """判断是否进入了新的交易日.

    Args:
        current_td: 当前记录的交易日（如 "20260402"）。
        now: 可选，用于测试。

    Returns:
        True if get_trading_day(now) != current_td。
    """
    return get_trading_day(now) != current_td


def get_day_start_time(now: datetime | None = None) -> datetime:
    """获取当前交易日的起始时间（21:00）.

    用于计算当日PnL的起点。

    Returns:
        当前交易日对应的21:00时刻。
    """
    if now is None:
        now = datetime.now()

    if now.hour >= DAY_START_HOUR:
        # 已经过了21:00 → day start就是今天21:00
        return now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    else:
        # 还没到21:00 → day start是昨天21:00
        yesterday = now - timedelta(days=1)
        return yesterday.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
