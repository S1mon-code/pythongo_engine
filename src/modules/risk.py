"""止损体系模块.

用法:
    from modules.risk import check_stops

    result = check_stops(
        close=800.0, avg_price=810.0, peak_price=820.0,
        equity=990000, peak_equity=1000000, daily_start_eq=995000,
        pos_profit=-5000, net_pos=3,
        hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0,
    )
    # result: ("TRAIL_STOP", "close=800.0 <= 移损线817.5") 或 (None, "")
"""

STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05


def check_stops(close, avg_price, peak_price,
                equity, peak_equity, daily_start_eq, pos_profit,
                net_pos, hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0):
    """检查全部止损条件.

    优先级: 权益止损 > 硬止损 > 移动止损 > 熔断 > 减仓 > 预警 > 单日止损

    Returns:
        (action, reason) — action: str或None, reason: 描述
        action可能值: "EQUITY_STOP","HARD_STOP","TRAIL_STOP","CIRCUIT","REDUCE","DAILY_STOP",None
    """
    if net_pos <= 0:
        return None, ""

    # ① 权益止损
    if equity > 0 and pos_profit < 0 and abs(pos_profit) > equity * (equity_stop_pct / 100):
        return "EQUITY_STOP", f"浮亏{pos_profit:.0f} > 权益{equity:.0f}×{equity_stop_pct}%"

    # ② 硬止损
    if avg_price > 0 and close <= avg_price * (1 - hard_stop_pct / 100):
        line = avg_price * (1 - hard_stop_pct / 100)
        return "HARD_STOP", f"close={close:.1f} <= 止损线{line:.1f}"

    # ③ 移动止损
    if peak_price > 0 and close <= peak_price * (1 - trailing_pct / 100):
        line = peak_price * (1 - trailing_pct / 100)
        return "TRAIL_STOP", f"close={close:.1f} <= 移损线{line:.1f}"

    # ④ Portfolio Stops
    if peak_equity > 0:
        dd = (equity - peak_equity) / peak_equity
        dp = (equity - daily_start_eq) / daily_start_eq if daily_start_eq > 0 else 0

        if dd <= STOP_CIRCUIT:
            return "CIRCUIT", f"回撤{dd:.1%}"
        if dd <= STOP_REDUCE:
            return "REDUCE", f"回撤{dd:.1%}"
        if dd <= STOP_WARNING:
            return "WARNING", f"回撤{dd:.1%}"
        if dp <= STOP_DAILY:
            return "DAILY_STOP", f"当日{dp:.1%}"

    return None, ""
