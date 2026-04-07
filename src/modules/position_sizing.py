"""仓位计算模块 — Vol Targeting + Carver Buffer.

用法:
    from modules.position_sizing import calc_optimal_lots, apply_buffer
    from modules.contract_info import get_multiplier
    mult = get_multiplier("i2509")  # 100
    optimal = calc_optimal_lots(forecast=7.0, atr_val=15.0, price=800.0,
                                capital=1000000, max_lots=10, multiplier=mult)
    target = apply_buffer(optimal, current_pos=3)
"""
import math
import numpy as np

from modules.contract_info import get_multiplier
TARGET_VOL = 0.15
BUFFER_FRACTION = 0.10


def calc_optimal_lots(forecast, atr_val, price, capital, max_lots,
                      multiplier=100, annual_factor=60480):
    """Vol Targeting: (f/10) × (target_vol/realized_vol) × (capital/notional).

    Args:
        multiplier: 合约乘数，建议通过 get_multiplier(instrument_id) 获取
        annual_factor: M1=60480, H1=1008, D1=252
    """
    if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or forecast == 0:
        return 0.0
    rv = (atr_val * math.sqrt(annual_factor)) / price
    if rv <= 0:
        return 0.0
    return max(0.0, min(
        (forecast / 10.0) * (TARGET_VOL / rv) * (capital / (price * multiplier)),
        float(max_lots),
    ))


def apply_buffer(optimal, current, min_buf=0.5):
    """Carver 10% buffer. 目标在buffer内不交易.

    Args:
        min_buf: buffer最小值, 测试时传0可关闭buffer让信号全部触发.
    """
    buf = max(abs(optimal) * BUFFER_FRACTION, min_buf)
    if (current - buf) <= optimal <= (current + buf):
        return current
    if optimal > current + buf:
        return max(0, math.floor(optimal - buf))
    return max(0, math.ceil(optimal + buf))
