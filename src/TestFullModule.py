"""
================================================================================
  TestFullModule — M1 双均线 + 完整止损体系
================================================================================

  交易: MA3/MA7, 开仓/加仓/平仓
  止损体系:
    1. 硬止损 — 跌破均价 × (1 - 0.5%)
    2. 移动止损 — 跌破peak × (1 - 0.3%)
    3. 硬止损(权益) — 浮亏 > 账户权益 × 2%
    4. Portfolio Stops — 回撤 -10%预警/-15%减仓/-20%熔断
    5. 单日止损 — 当日亏 ≥ 5%
    6. 保证金检查 — 下单前检查可用资金

  get_account_fund_data 用正确的 investor_id 调用

================================================================================
"""
import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator


class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    fast_period: int = Field(default=3, title="快线周期", ge=2)
    slow_period: int = Field(default=7, title="慢线周期", ge=2)
    unit_volume: int = Field(default=1, title="每次手数", ge=1)
    max_lots: int = Field(default=3, title="最大持仓", ge=1)
    hard_stop_pct: float = Field(default=0.5, title="硬止损-价格(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="硬止损-权益(%)")


class State(BaseState):
    fast_ma: float = Field(default=0.0, title="快均线")
    slow_ma: float = Field(default=0.0, title="慢均线")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="最高价")
    hard_stop_line: float = Field(default=0.0, title="价格止损线")
    trail_stop_line: float = Field(default=0.0, title="移动止损线")
    equity: float = Field(default=0.0, title="权益")
    peak_equity: float = Field(default=0.0, title="峰值权益")
    drawdown: str = Field(default="0.00%", title="回撤")
    daily_pnl: str = Field(default="0.00%", title="当日盈亏")
    pos_profit: float = Field(default=0.0, title="持仓盈亏")
    pending: str = Field(default="—", title="待执行")
    last_action: str = Field(default="—", title="上次操作")


class TestFullModule(BaseStrategy):
    """M1 双均线 + 完整止损体系"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        self.avg_price = 0.0
        self.peak_price = 0.0
        self._pending = None
        self.order_id = set()

        # 账户
        self._investor_id = ""
        self._peak_equity = 0.0
        self._daily_start_eq = 0.0

    @property
    def main_indicator_data(self):
        return {
            f"MA{self.params_map.fast_period}": self.state_map.fast_ma,
            f"MA{self.params_map.slow_period}": self.state_map.slow_ma,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        # 获取investor_id (正确的API调用方式)
        investor = self.get_investor_data(1)
        if investor:
            self._investor_id = investor.investor_id
            self.output(f"[账户] investor_id={self._investor_id}")

            # 初始化权益基准
            account = self.get_account_fund_data(self._investor_id)
            if account:
                self._peak_equity = account.balance
                self._daily_start_eq = account.balance
                self.state_map.equity = round(account.balance, 0)
                self.state_map.peak_equity = round(account.balance, 0)
                self.output(f"[账户] balance={account.balance:.0f} available={account.available:.0f}")
        else:
            self.output("[账户] get_investor_data返回None, 权益止损不可用")

        super().on_start()
        self.output(
            f"TestFullModule 启动 | {p.instrument_id} M1 | "
            f"止损: 价格{p.hard_stop_pct}% 移动{p.trailing_pct}% 权益{p.equity_stop_pct}%"
        )

    def on_stop(self):
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self.state_map.net_pos = self.get_position(
            self.params_map.instrument_id
        ).net_position
        self.update_status_bar()

    def on_error(self, error):
        self.output(f"[错误] {error}")

    # ══════════════════════════════════════════════════════════════════════
    #  获取账户数据 (安全封装)
    # ══════════════════════════════════════════════════════════════════════

    def _get_account(self):
        """返回AccountData或None."""
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # ── 1. 撤挂单 ──
        for oid in list(self.order_id):
            self.cancel_order(oid)

        # ── 2. 执行pending ──
        if self._pending:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # ── 3. 指标 ──
        producer = self.kline_generator.producer
        if len(producer.close) < p.slow_period + 2:
            self._push_widget(kline, signal_price)
            return

        close = float(producer.close[-1])
        fast_ma = float(producer.sma(p.fast_period))
        slow_ma = float(producer.sma(p.slow_period))
        self.state_map.fast_ma = round(fast_ma, 2)
        self.state_map.slow_ma = round(slow_ma, 2)

        # ── 4. 持仓 ──
        net_pos = self.get_position(p.instrument_id).net_position
        self.state_map.net_pos = net_pos

        if net_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
            self.state_map.hard_stop_line = 0.0
            self.state_map.trail_stop_line = 0.0
        else:
            if close > self.peak_price:
                self.peak_price = close
            self.state_map.hard_stop_line = round(self.avg_price * (1 - p.hard_stop_pct / 100), 1)
            self.state_map.trail_stop_line = round(self.peak_price * (1 - p.trailing_pct / 100), 1)

        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)

        # ── 5. 更新权益状态 ──
        account = self._get_account()
        equity = 0.0
        pos_profit = 0.0
        available = 0.0
        if account:
            equity = account.balance
            pos_profit = account.position_profit
            available = account.available
            if equity > self._peak_equity:
                self._peak_equity = equity
            self.state_map.equity = round(equity, 0)
            self.state_map.peak_equity = round(self._peak_equity, 0)
            self.state_map.pos_profit = round(pos_profit, 0)

            dd = (equity - self._peak_equity) / self._peak_equity if self._peak_equity > 0 else 0
            daily_pnl = (equity - self._daily_start_eq) / self._daily_start_eq if self._daily_start_eq > 0 else 0
            self.state_map.drawdown = f"{dd:.2%}"
            self.state_map.daily_pnl = f"{daily_pnl:+.2%}"

        # ══════════════════════════════════════════════════════════
        # 6. 止损信号 (优先级: 权益止损 > 硬止损 > 移动止损 > Portfolio > 正常)
        # ══════════════════════════════════════════════════════════

        # ① 权益硬止损: 浮亏 > 权益 × equity_stop_pct%
        if (net_pos > 0 and account
                and pos_profit < 0
                and abs(pos_profit) > equity * (p.equity_stop_pct / 100)):
            self._pending = "EQUITY_STOP"
            self.output(
                f"[权益止损] 浮亏={pos_profit:.0f} > 权益{equity:.0f}×{p.equity_stop_pct}% "
                f"= {equity * p.equity_stop_pct / 100:.0f}"
            )

        # ② 硬止损: 价格跌破均价
        elif (net_pos > 0 and self.avg_price > 0
              and close <= self.avg_price * (1 - p.hard_stop_pct / 100)):
            self._pending = "HARD_STOP"
            pnl = (close - self.avg_price) / self.avg_price * 100
            self.output(
                f"[硬止损] close={close:.1f} <= 止损线{self.state_map.hard_stop_line} | 浮亏={pnl:.2f}%"
            )

        # ③ 移动止损: 从最高价回落
        elif (net_pos > 0 and self.peak_price > 0
              and close <= self.peak_price * (1 - p.trailing_pct / 100)):
            self._pending = "TRAIL_STOP"
            self.output(
                f"[移动止损] close={close:.1f} <= 移损线{self.state_map.trail_stop_line} | "
                f"peak={self.peak_price:.1f}"
            )

        # ④ Portfolio Stops: 回撤检查
        elif net_pos > 0 and account and self._peak_equity > 0:
            dd = (equity - self._peak_equity) / self._peak_equity
            daily_pnl = (equity - self._daily_start_eq) / self._daily_start_eq if self._daily_start_eq > 0 else 0

            if dd <= -0.20:
                self._pending = "CIRCUIT"
                self.output(f"[熔断] 回撤{dd:.1%}, 全部平仓")
            elif dd <= -0.15:
                self._pending = "REDUCE"
                self.output(f"[减仓止损] 回撤{dd:.1%}")
            elif dd <= -0.10:
                self.output(f"[预警] 回撤{dd:.1%}")
            elif daily_pnl <= -0.05:
                self._pending = "DAILY_STOP"
                self.output(f"[单日止损] 当日{daily_pnl:.1%}")

        # ── 正常信号 (没有触发任何止损时) ──
        if self._pending is None:
            if net_pos > 0 and fast_ma < slow_ma:
                self._pending = "CLOSE"
                self.output(f"[信号] 趋势出场")
            elif 0 < net_pos < p.max_lots and fast_ma > slow_ma:
                self._pending = "ADD"
            elif net_pos == 0 and fast_ma > slow_ma:
                self._pending = "OPEN"

        self.state_map.pending = self._pending or "—"
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    # ══════════════════════════════════════════════════════════════════════
    #  执行
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        actual_pos = self.get_position(p.instrument_id).net_position

        # ── 开仓 ──
        if action == "OPEN":
            vol = p.unit_volume

            # 保证金检查
            account = self._get_account()
            if account:
                needed = price * 100 * vol * 0.15
                self.output(f"[保证金] 需要={needed:.0f} 可用={account.available:.0f}")
                if needed > account.available * 0.6:
                    self.output(f"[保证金不足] 放弃开仓")
                    return 0.0

            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            self.avg_price = price
            self.peak_price = price
            self.state_map.last_action = f"建仓 {vol}手@{price:.1f}"
            self.output(
                f"[执行] 建仓 {vol}手 @ {price:.1f} | "
                f"止损线={price*(1-p.hard_stop_pct/100):.1f} "
                f"移损线={price*(1-p.trailing_pct/100):.1f}"
            )
            return price

        # ── 加仓 ──
        elif action == "ADD":
            vol = p.unit_volume

            account = self._get_account()
            if account:
                needed = price * 100 * vol * 0.15
                if needed > account.available * 0.6:
                    self.output(f"[保证金不足] 放弃加仓")
                    return 0.0

            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            if actual_pos > 0:
                self.avg_price = (self.avg_price * actual_pos + price * vol) / (actual_pos + vol)
            else:
                self.avg_price = price
            self.state_map.last_action = f"加仓 {vol}手@{price:.1f} 均={self.avg_price:.1f}"
            self.output(f"[执行] 加仓 {vol}手 @ {price:.1f} | 均价={self.avg_price:.1f}")
            return price

        # ── 减仓 (Portfolio -15%) ──
        elif action == "REDUCE":
            vol = max(1, actual_pos // 2)
            if actual_pos <= 0:
                return 0.0
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            pnl = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
            self.state_map.last_action = f"减仓(回撤) {vol}手 {pnl:+.2f}%"
            self.output(f"[执行] ★减仓止损★ {vol}手 @ {price:.1f} | 剩余≈{actual_pos-vol}手")
            return -price

        # ── 全平 (所有止损和趋势出场) ──
        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP", "CIRCUIT", "DAILY_STOP"):
            labels = {
                "CLOSE": "趋势出场",
                "HARD_STOP": "★硬止损★",
                "TRAIL_STOP": "★移动止损★",
                "EQUITY_STOP": "★权益止损★",
                "CIRCUIT": "★熔断★",
                "DAILY_STOP": "★单日止损★",
            }
            label = labels.get(action, action)
            if actual_pos <= 0:
                return 0.0
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=actual_pos, price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_id.add(oid)
            pnl = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
            self.state_map.last_action = f"{label} {actual_pos}手 {pnl:+.2f}%"
            self.output(
                f"[执行] {label} {actual_pos}手 @ {price:.1f} | "
                f"均价={self.avg_price:.1f} 盈亏={pnl:+.2f}%"
            )
            self.avg_price = 0.0
            self.peak_price = 0.0
            return -price

        return 0.0

    # ══════════════════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════════════════

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass
