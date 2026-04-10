"""
合约信息模块 - 中国期货合约规格查询

提供期货合约的乘数、最小变动价位、交易所、交易时段等信息，
供其他模块统一调用，避免硬编码。

用法示例:
    from modules.contract_info import get_multiplier, get_tick_size, is_near_close
    mult = get_multiplier("i2509")      # 100
    tick = get_tick_size("ag2506")       # 1
    close = is_near_close("i2509", 5)   # True if within 5min of session end
"""

import re
from datetime import datetime


# ---------------------------------------------------------------------------
# 合约规格表（module-level dict）
# sessions: 每个时段用 ((start_h, start_m), (end_h, end_m)) 表示
# ---------------------------------------------------------------------------
_CONTRACT_SPECS = {
    # ══════════════════════════════════════════════════════════════
    #  中国金融期货交易所 (CFFEX)
    # ══════════════════════════════════════════════════════════════
    "if": {"exchange": "CFFEX", "multiplier": 300, "tick_size": 0.2,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 0))]},
    "ih": {"exchange": "CFFEX", "multiplier": 300, "tick_size": 0.2,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 0))]},
    "ic": {"exchange": "CFFEX", "multiplier": 200, "tick_size": 0.2,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 0))]},
    "im": {"exchange": "CFFEX", "multiplier": 200, "tick_size": 0.2,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 0))]},
    "ts": {"exchange": "CFFEX", "multiplier": 20000, "tick_size": 0.002,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 15))]},
    "tf": {"exchange": "CFFEX", "multiplier": 10000, "tick_size": 0.005,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 15))]},
    "t":  {"exchange": "CFFEX", "multiplier": 10000, "tick_size": 0.005,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 15))]},
    "tl": {"exchange": "CFFEX", "multiplier": 10000, "tick_size": 0.01,
           "sessions": [((9, 30), (11, 30)), ((13, 0), (15, 15))]},

    # ══════════════════════════════════════════════════════════════
    #  上海国际能源交易中心 (INE)
    # ══════════════════════════════════════════════════════════════
    "sc": {"exchange": "INE", "multiplier": 1000, "tick_size": 0.1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (2, 30))]},
    "nr": {"exchange": "INE", "multiplier": 10, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "lu": {"exchange": "INE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "bc": {"exchange": "INE", "multiplier": 5, "tick_size": 10,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "ec": {"exchange": "INE", "multiplier": 50, "tick_size": 0.1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},

    # ══════════════════════════════════════════════════════════════
    #  上海期货交易所 (SHFE) — 夜盘21:00-23:00
    # ══════════════════════════════════════════════════════════════
    "rb": {"exchange": "SHFE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "hc": {"exchange": "SHFE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "wr": {"exchange": "SHFE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "fu": {"exchange": "SHFE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "bu": {"exchange": "SHFE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "ru": {"exchange": "SHFE", "multiplier": 10, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "br": {"exchange": "SHFE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "sp": {"exchange": "SHFE", "multiplier": 10, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "op": {"exchange": "SHFE", "multiplier": 40, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    # SHFE — 夜盘21:00-01:00
    "cu": {"exchange": "SHFE", "multiplier": 5, "tick_size": 10,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "al": {"exchange": "SHFE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "zn": {"exchange": "SHFE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "pb": {"exchange": "SHFE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "ni": {"exchange": "SHFE", "multiplier": 1, "tick_size": 10,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "sn": {"exchange": "SHFE", "multiplier": 1, "tick_size": 10,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "ss": {"exchange": "SHFE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "ao": {"exchange": "SHFE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    "ad": {"exchange": "SHFE", "multiplier": 10, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (1, 0))]},
    # SHFE — 夜盘21:00-02:30
    "au": {"exchange": "SHFE", "multiplier": 1000, "tick_size": 0.02,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (2, 30))]},
    "ag": {"exchange": "SHFE", "multiplier": 15, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (2, 30))]},

    # ══════════════════════════════════════════════════════════════
    #  大连商品交易所 (DCE) — 夜盘21:00-23:00
    # ══════════════════════════════════════════════════════════════
    "a":  {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "b":  {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "m":  {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "y":  {"exchange": "DCE", "multiplier": 10, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "p":  {"exchange": "DCE", "multiplier": 10, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "jm": {"exchange": "DCE", "multiplier": 60, "tick_size": 0.5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "j":  {"exchange": "DCE", "multiplier": 100, "tick_size": 0.5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "i":  {"exchange": "DCE", "multiplier": 100, "tick_size": 0.5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "c":  {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "cs": {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "l":  {"exchange": "DCE", "multiplier": 5, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "v":  {"exchange": "DCE", "multiplier": 5, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pp": {"exchange": "DCE", "multiplier": 5, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "eg": {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "eb": {"exchange": "DCE", "multiplier": 5, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pg": {"exchange": "DCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "bz": {"exchange": "DCE", "multiplier": 30, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "rr": {"exchange": "DCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "lg": {"exchange": "DCE", "multiplier": 90, "tick_size": 0.5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    # DCE — 无夜盘
    "jd": {"exchange": "DCE", "multiplier": 5, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "lh": {"exchange": "DCE", "multiplier": 16, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "bb": {"exchange": "DCE", "multiplier": 500, "tick_size": 0.05,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "fb": {"exchange": "DCE", "multiplier": 10, "tick_size": 0.5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},

    # ══════════════════════════════════════════════════════════════
    #  郑州商品交易所 (CZCE) — 夜盘21:00-23:00
    # ══════════════════════════════════════════════════════════════
    "cf": {"exchange": "CZCE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "cy": {"exchange": "CZCE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "sr": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "ta": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pf": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "fg": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "ma": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "sa": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "rm": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "oi": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "px": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "sh": {"exchange": "CZCE", "multiplier": 30, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pr": {"exchange": "CZCE", "multiplier": 15, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pl": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "zc": {"exchange": "CZCE", "multiplier": 100, "tick_size": 0.2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "ur": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    "pk": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0)), ((21, 0), (23, 0))]},
    # CZCE — 无夜盘
    "sm": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "sf": {"exchange": "CZCE", "multiplier": 5, "tick_size": 2,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "ap": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "cj": {"exchange": "CZCE", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "wh": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "pm": {"exchange": "CZCE", "multiplier": 50, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "ri": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "lr": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "jr": {"exchange": "CZCE", "multiplier": 20, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "rs": {"exchange": "CZCE", "multiplier": 10, "tick_size": 1,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},

    # ══════════════════════════════════════════════════════════════
    #  广州期货交易所 (GFEX) — 无夜盘
    # ══════════════════════════════════════════════════════════════
    "si": {"exchange": "GFEX", "multiplier": 5, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "lc": {"exchange": "GFEX", "multiplier": 1, "tick_size": 20,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "ps": {"exchange": "GFEX", "multiplier": 3, "tick_size": 5,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "pt": {"exchange": "GFEX", "multiplier": 1000, "tick_size": 0.05,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
    "pd": {"exchange": "GFEX", "multiplier": 1000, "tick_size": 0.05,
           "sessions": [((9, 0), (11, 30)), ((13, 30), (15, 0))]},
}

# 未知品种的默认值
_DEFAULT_SPEC = {
    "exchange": "DCE",
    "multiplier": 100,
    "tick_size": 0.5,
    "sessions": [
        ((9, 0), (11, 30)),
        ((13, 30), (15, 0)),
        ((21, 0), (23, 0)),
    ],
}


def _extract_product(instrument_id: str) -> str:
    """从合约代码中提取品种代码，如 'i2509' -> 'i', 'ag2506' -> 'ag'"""
    match = re.match(r"^([a-zA-Z]+)", instrument_id)
    if match:
        return match.group(1).lower()
    return instrument_id.lower()


def get_contract(instrument_id: str) -> dict:
    """
    获取合约规格信息

    Args:
        instrument_id: 合约代码，如 "i2509", "ag2506"

    Returns:
        dict with keys: product, exchange, multiplier, tick_size, sessions
    """
    product = _extract_product(instrument_id)
    spec = _CONTRACT_SPECS.get(product, _DEFAULT_SPEC)
    return {
        "product": product,
        "exchange": spec["exchange"],
        "multiplier": spec["multiplier"],
        "tick_size": spec["tick_size"],
        "sessions": list(spec["sessions"]),
    }


def get_multiplier(instrument_id: str) -> int:
    """获取合约乘数"""
    return get_contract(instrument_id)["multiplier"]


def get_tick_size(instrument_id: str) -> float:
    """获取最小变动价位"""
    return get_contract(instrument_id)["tick_size"]


def get_exchange(instrument_id: str) -> str:
    """获取交易所代码"""
    return get_contract(instrument_id)["exchange"]


def get_sessions(instrument_id: str) -> list:
    """获取交易时段列表，每个时段为 ((start_h, start_m), (end_h, end_m))"""
    return get_contract(instrument_id)["sessions"]


def _time_to_minutes(h: int, m: int) -> int:
    """将时分转为当天分钟数"""
    return h * 60 + m


def _is_time_in_session(now_h: int, now_m: int, start: tuple, end: tuple) -> bool:
    """
    判断当前时间是否在某个交易时段内

    处理跨午夜的时段（如 21:00-02:30）
    """
    now_min = _time_to_minutes(now_h, now_m)
    start_min = _time_to_minutes(start[0], start[1])
    end_min = _time_to_minutes(end[0], end[1])

    if start_min <= end_min:
        # 不跨午夜：如 09:00-11:30
        return start_min <= now_min < end_min
    else:
        # 跨午夜：如 21:00-02:30
        return now_min >= start_min or now_min < end_min


def is_in_session(instrument_id: str) -> bool:
    """
    判断当前时间是否在该合约的交易时段内

    Args:
        instrument_id: 合约代码

    Returns:
        True 表示当前处于交易时段
    """
    now = datetime.now()
    sessions = get_sessions(instrument_id)
    for start, end in sessions:
        if _is_time_in_session(now.hour, now.minute, start, end):
            return True
    return False


def _minutes_to_session_end(now_h: int, now_m: int, start: tuple, end: tuple) -> int:
    """
    计算距离时段结束的分钟数

    仅在当前时间处于该时段内时有意义，否则返回 -1
    """
    if not _is_time_in_session(now_h, now_m, start, end):
        return -1

    now_min = _time_to_minutes(now_h, now_m)
    end_min = _time_to_minutes(end[0], end[1])
    start_min = _time_to_minutes(start[0], start[1])

    if start_min <= end_min:
        # 不跨午夜
        return end_min - now_min
    else:
        # 跨午夜
        if now_min >= start_min:
            return (24 * 60 - now_min) + end_min
        else:
            return end_min - now_min


def is_near_close(instrument_id: str, minutes: int = 5) -> bool:
    """
    判断当前时间是否接近某个交易时段的收盘

    Args:
        instrument_id: 合约代码
        minutes: 距离收盘的分钟数阈值，默认5分钟

    Returns:
        True 表示当前处于某个时段结束前 N 分钟内
    """
    now = datetime.now()
    sessions = get_sessions(instrument_id)
    for start, end in sessions:
        remaining = _minutes_to_session_end(now.hour, now.minute, start, end)
        if 0 <= remaining <= minutes:
            return True
    return False
