"""止损体系模块.

所有止损条件的daily维度基于21:00 day start。
daily_start_eq 应在每天21:00交易日切换时重置。

两层止损架构(2026-04-17 重构):
  1. Hard stop — tick 级,每个 tick 立即判断,无确认延时
  2. Trail stop — peak/trough 每 tick 更新,判断每分钟一次(降噪)
  3. Equity / Portfolio / Daily — bar 级,走 legacy check()

用法:
    from modules.risk import RiskManager

    rm = RiskManager(capital=1_000_000)
    rm.on_day_change(current_equity=1_000_000)  # 21:00切换时
    rm.update(equity=990_000)                    # 每bar/tick权益更新

    # 每 tick 调用:
    rm.update_peak_trough_tick(tick.last_price, net_pos)
    action, reason = rm.check_hard_stop_tick(
        tick.last_price, avg_price, net_pos, hard_stop_pct,
    )
    # 每 tick 还调:
    action2, reason2 = rm.check_trail_minutely(
        tick.last_price, datetime.now(), net_pos, trailing_pct,
    )

    # Bar 级(权益/回撤/单日止损)仍用 legacy:
    action3, reason3 = rm.check(close, avg_price, peak_price, ...)
"""
from __future__ import annotations

from datetime import datetime

# Portfolio Stops 阈值
STOP_WARNING = -0.10
STOP_REDUCE = -0.15
STOP_CIRCUIT = -0.20
STOP_DAILY = -0.05


class RiskManager:
    """集中管理风控状态.

    自动追踪:
      - peak_equity / daily_start_eq(权益维度)
      - peak_price / trough_price(价格维度,持仓期间极值,tick 级)

    策略只需在 21:00 调用 on_day_change(),其余回调由 update* 方法驱动。
    """

    def __init__(self, capital: float = 1_000_000):
        # 权益维度
        self.peak_equity = capital
        self.daily_start_eq = capital
        self._current_equity = capital
        # 价格维度(2026-04-17 新增)
        self.peak_price = 0.0       # long trail 参考
        self.trough_price = 0.0     # short trail 参考
        self._last_pos_sign = 0     # 1 / -1 / 0,用于检测反向切换
        # 分钟级 trail 门控
        self._last_trail_minute: datetime | None = None

    # ------------------------------------------------------------------ #
    # 权益追踪
    # ------------------------------------------------------------------ #

    def on_day_change(self, current_equity: float):
        """交易日切换(21:00),重置当日起始权益."""
        self.daily_start_eq = current_equity

    def update(self, equity: float):
        """每tick/bar更新权益,自动追踪peak."""
        self._current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

    # ------------------------------------------------------------------ #
    # 价格极值追踪(tick 级)
    # ------------------------------------------------------------------ #

    def update_peak_trough_tick(self, price: float, net_pos: int) -> None:
        """每 tick 更新持仓期间最高/最低价.

        net_pos 约定:做多 > 0,做空 < 0,空仓 = 0。
        做空应传绝对值的负数(例如 get_position().net_position 原值)。
        """
        if net_pos == 0:
            self.peak_price = 0.0
            self.trough_price = 0.0
            self._last_pos_sign = 0
            self._last_trail_minute = None  # 新持仓周期重新判
            return

        sign = 1 if net_pos > 0 else -1
        if sign != self._last_pos_sign:
            # 空仓→持仓,或反向切换 — 以当前价重置极值与分钟门控
            self.peak_price = price if sign > 0 else 0.0
            self.trough_price = price if sign < 0 else 0.0
            self._last_pos_sign = sign
            self._last_trail_minute = None
            return

        if sign > 0:
            if self.peak_price == 0.0 or price > self.peak_price:
                self.peak_price = price
        else:
            if self.trough_price == 0.0 or price < self.trough_price:
                self.trough_price = price

    # ------------------------------------------------------------------ #
    # Tick 级硬止损(立即触发,无确认)
    # ------------------------------------------------------------------ #

    def check_hard_stop_tick(
        self,
        price: float,
        avg_price: float,
        net_pos: int,
        hard_stop_pct: float,
    ) -> tuple[str | None, str]:
        """tick 级硬止损,阈值破即触发.

        做多:price <= avg_price * (1 - pct/100)
        做空:price >= avg_price * (1 + pct/100)
        """
        if net_pos == 0 or avg_price <= 0:
            return None, ""
        if net_pos > 0:
            line = avg_price * (1 - hard_stop_pct / 100)
            if price <= line:
                return "HARD_STOP", f"tick={price:.1f} <= 硬止损线{line:.1f}"
        else:
            line = avg_price * (1 + hard_stop_pct / 100)
            if price >= line:
                return "HARD_STOP", f"tick={price:.1f} >= 硬止损线{line:.1f}"
        return None, ""

    # ------------------------------------------------------------------ #
    # 1-min 级移动止损(降噪:每分钟判一次)
    # ------------------------------------------------------------------ #

    def check_trail_minutely(
        self,
        price: float,
        now: datetime,
        net_pos: int,
        trailing_pct: float,
    ) -> tuple[str | None, str]:
        """分钟门控的移动止损.

        同一分钟内多次调用,只有首次实际判断;下一分钟自动重新判。
        """
        if net_pos == 0:
            self._last_trail_minute = None
            return None, ""

        minute = now.replace(second=0, microsecond=0)
        if self._last_trail_minute == minute:
            return None, ""
        self._last_trail_minute = minute

        if net_pos > 0 and self.peak_price > 0:
            line = self.peak_price * (1 - trailing_pct / 100)
            if price <= line:
                return "TRAIL_STOP", f"M1 price={price:.1f} <= 移损线{line:.1f}"
        elif net_pos < 0 and self.trough_price > 0:
            line = self.trough_price * (1 + trailing_pct / 100)
            if price >= line:
                return "TRAIL_STOP", f"M1 price={price:.1f} >= 移损线{line:.1f}"
        return None, ""

    # ------------------------------------------------------------------ #
    # Bar 级综合检查(权益/回撤/单日,保留向后兼容)
    # ------------------------------------------------------------------ #

    def check(self, close, avg_price, peak_price, pos_profit, net_pos,
              hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0):
        """Legacy:bar 级全量检查.

        重构后策略应优先用 check_hard_stop_tick / check_trail_minutely;
        本方法仍服务于 equity/drawdown/daily 维度,以及做空 inline 逻辑的迁移过渡。
        """
        return check_stops(
            close=close, avg_price=avg_price, peak_price=peak_price,
            equity=self._current_equity, peak_equity=self.peak_equity,
            daily_start_eq=self.daily_start_eq, pos_profit=pos_profit,
            net_pos=net_pos, hard_stop_pct=hard_stop_pct,
            trailing_pct=trailing_pct, equity_stop_pct=equity_stop_pct,
        )

    # ------------------------------------------------------------------ #
    # 指标属性
    # ------------------------------------------------------------------ #

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self._current_equity - self.peak_equity) / self.peak_equity

    @property
    def daily_pnl_pct(self) -> float:
        if self.daily_start_eq <= 0:
            return 0.0
        return (self._current_equity - self.daily_start_eq) / self.daily_start_eq

    # ------------------------------------------------------------------ #
    # 持久化
    # ------------------------------------------------------------------ #

    def get_state(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "daily_start_eq": self.daily_start_eq,
            "peak_price": self.peak_price,
            "trough_price": self.trough_price,
            "last_pos_sign": self._last_pos_sign,
        }

    def load_state(self, state: dict):
        self.peak_equity = state.get("peak_equity", self.peak_equity)
        self.daily_start_eq = state.get("daily_start_eq", self.daily_start_eq)
        self.peak_price = state.get("peak_price", 0.0)
        self.trough_price = state.get("trough_price", 0.0)
        self._last_pos_sign = state.get("last_pos_sign", 0)


def check_stops(close, avg_price, peak_price,
                equity, peak_equity, daily_start_eq, pos_profit,
                net_pos, hard_stop_pct=0.5, trailing_pct=0.3, equity_stop_pct=2.0):
    """Bar 级全量检查(函数式,向后兼容).

    仅支持 net_pos > 0(做多)。做空逻辑由策略 inline 或走 check_hard_stop_tick/
    check_trail_minutely 处理。

    优先级: 权益止损 > 硬止损 > 移动止损 > 熔断 > 减仓 > 预警 > 单日止损

    Returns:
        (action, reason) — action 可能值:
        "EQUITY_STOP","HARD_STOP","TRAIL_STOP","CIRCUIT","REDUCE","DAILY_STOP",None
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

    # ④ Portfolio Stops(基于21:00 day start的daily计算)
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
            return "DAILY_STOP", f"当日{dp:.1%}(21:00起算)"

    return None, ""
