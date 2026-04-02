"""交易日检测模块.

用法:
    from modules.trading_day import get_trading_day
    td = get_trading_day()  # "20260402"
"""
from datetime import datetime, timedelta


def get_trading_day():
    """当前时间+4小时推算交易日. 夜盘自动归下一天, 跳过周末."""
    s = datetime.now() + timedelta(hours=4)
    wd = s.weekday()
    if wd == 5:
        s += timedelta(days=2)
    elif wd == 6:
        s += timedelta(days=1)
    return s.strftime("%Y%m%d")
