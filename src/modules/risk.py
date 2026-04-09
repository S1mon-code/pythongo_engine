"""止损体系模块.

所有止损条件的daily维度基于21:00 day start。
daily_start_eq 应在每天21:00交易日切换时重置。

用法:
    from modules.risk import check_stops, RiskManager

    # 函数式（向后兼容）
    result = check_stops(
        close=800.0, avg_price=810.0, peak_price=820.0,
        equity=990000, peak_equity=1000000, daily_start_eq=995000,
        pos_profit=-5000, net_pos=3,
    )

    # 类式（推荐，自动管理daily reset）
    rm = RiskManager(capital=1_000_000)
    rm.on_day_change(current_equity=1_000_000)  # 21:00切换时调用
    rm.update(equity=990000)                     # 每tick更新
    action, reason = rm.check(close=800.0, avg_price=810.0, ...)
"""

# Portfolio Stops 阈值
STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05


class RiskManager:
    """集中管理日级别风控状态.

    自动追踪peak_equity和daily_start_eq，策略只需在21:00调用on_day_change()。
    """

    def __init__(self, capital: float = 1_000_000):
        self.peak_equity = capital
        self.daily_start_eq = capital
        self._current_equity = capital

    def on_day_change(self, current_equity: float):
        """交易日切换（21:00），重置当日起始权益."""
        self.daily_start_eq = current_equity
        # peak_equity不重置，持续追踪历史最高

    def update(self, equity: float):
        """每tick/bar更新权益，自动追踪peak."""
        self._current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

    def check(self, close, avg_price, peak_price, pos_profit, net_pos,
              hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0):
        """检查全部止损条件. 使用内部管理的equity状态."""
        return check_stops(
            close=close, avg_price=avg_price, peak_price=peak_price,
            equity=self._current_equity, peak_equity=self.peak_equity,
            daily_start_eq=self.daily_start_eq, pos_profit=pos_profit,
            net_pos=net_pos, hard_stop_pct=hard_stop_pct,
            trailing_pct=trailing_pct, equity_stop_pct=equity_stop_pct,
        )

    @property
    def drawdown_pct(self) -> float:
        """当前回撤百分比（从peak）."""
        if self.peak_equity <= 0:
            return 0.0
        return (self._current_equity - self.peak_equity) / self.peak_equity

    @property
    def daily_pnl_pct(self) -> float:
        """当日PnL百分比（从21:00 day start）."""
        if self.daily_start_eq <= 0:
            return 0.0
        return (self._current_equity - self.daily_start_eq) / self.daily_start_eq

    def get_state(self) -> dict:
        """导出状态用于持久化."""
        return {
            "peak_equity": self.peak_equity,
            "daily_start_eq": self.daily_start_eq,
        }

    def load_state(self, state: dict):
        """从持久化恢复状态."""
        self.peak_equity = state.get("peak_equity", self.peak_equity)
        self.daily_start_eq = state.get("daily_start_eq", self.daily_start_eq)


def check_stops(close, avg_price, peak_price,
                equity, peak_equity, daily_start_eq, pos_profit,
                net_pos, hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0):
    """检查全部止损条件（函数式，向后兼容）.

    daily_start_eq 必须在21:00交易日切换时重置为当时的equity。

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

    # ④ Portfolio Stops（基于21:00 day start的daily计算）
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
            return "DAILY_STOP", f"当日{dp:.1%}（21:00起算）"

    return None, ""
