"""换月提醒模块.

用法:
    from modules.rollover import check_rollover
    level, days = check_rollover("i2509")
    # level: None | "warn"(15天内) | "urgent"(5天内)
    # days: 距交割月天数
"""
from datetime import date


def check_rollover(instrument_id):
    """检查合约是否临近交割月.

    Args:
        instrument_id: 如 "i2509"

    Returns:
        (level, days) — level: None/"warn"/"urgent", days: 距交割月1日天数
    """
    digits = "".join(c for c in instrument_id if c.isdigit())
    if len(digits) < 4:
        return None, -1
    yy = int(digits[-4:-2])
    mm = int(digits[-2:])
    if mm < 1 or mm > 12:
        return None, -1

    delivery = date(2000 + yy, mm, 1)
    days = (delivery - date.today()).days

    if days <= 0:
        return "urgent", 0
    if days <= 5:
        return "urgent", days
    if days <= 15:
        return "warn", days
    return None, days
