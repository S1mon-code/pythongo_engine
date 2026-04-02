"""
================================================================================
  TestFullModule — M1 双均线 + 全模块 (模块化版)
================================================================================

  所有运维模块通过 modules/ 目录导入, 主文件只有策略逻辑.

  部署: 将 src/ 下所有内容复制到无限易 pyStrategy/self_strategy/
        self_strategy/
          ├── TestFullModule.py      # 本文件
          └── modules/               # 模块目录
              ├── __init__.py
              ├── feishu.py
              ├── persistence.py
              ├── trading_day.py
              ├── risk.py
              ├── slippage.py
              ├── heartbeat.py
              ├── order_monitor.py
              ├── performance.py
              ├── rollover.py
              └── position_sizing.py

================================================================================
"""
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

# ── 模块导入 ──
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day
from modules.risk import check_stops
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

MULTIPLIER = 100
VOL_ATR_PERIOD = 14
ANNUAL_FACTOR = 252 * 240
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATOR
# ══════════════════════════════════════════════════════════════════════════════

def atr(highs, lows, closes, period=14):
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1:period+1])
    a = 1.0 / period
    for i in range(period+1, n):
        out[i] = out[i-1]*(1-a) + tr[i]*a
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    fast_period: int = Field(default=3, title="快线周期", ge=2)
    slow_period: int = Field(default=7, title="慢线周期", ge=2)
    unit_volume: int = Field(default=1, title="每次手数", ge=1)
    max_lots: int = Field(default=3, title="最大持仓", ge=1)
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")


class State(BaseState):
    fast_ma: float = Field(default=0.0, title="快均线")
    slow_ma: float = Field(default=0.0, title="慢均线")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="最高价")
    hard_line: float = Field(default=0.0, title="止损线")
    trail_line: float = Field(default=0.0, title="移损线")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="—", title="回撤")
    daily_pnl: str = Field(default="—", title="当日盈亏")
    trading_day: str = Field(default="", title="交易日")
    pending: str = Field(default="—", title="待执行")
    last_action: str = Field(default="—", title="上次操作")
    slippage: str = Field(default="—", title="滑点")
    perf: str = Field(default="—", title="绩效")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class TestFullModule(BaseStrategy):
    """M1 双均线 — 全模块集成 (模块化版)"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        self.avg_price = 0.0
        self.peak_price = 0.0
        self._pending = None
        self._pending_target = None
        self.order_id = set()

        self._investor_id = ""
        self._peak_equity = 0.0
        self._daily_start_eq = 0.0
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []

        # 模块实例
        self._slip = SlippageTracker()
        self._hb = HeartbeatMonitor()
        self._om = OrderMonitor()
        self._perf = PerformanceTracker()

    @property
    def main_indicator_data(self):
        return {f"MA{self.params_map.fast_period}": self.state_map.fast_ma,
                f"MA{self.params_map.slow_period}": self.state_map.slow_ma}

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map

        self.kline_generator = KLineGenerator(
            callback=self.callback, real_time_callback=self.real_time_callback,
            exchange=p.exchange, instrument_id=p.instrument_id, style=p.kline_style)
        self.kline_generator.push_history_data()

        # 账户ID
        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        # 恢复状态
        saved = load_state("TestFullModule")
        if saved:
            self._peak_equity = saved.get("peak_equity", 0.0)
            self._daily_start_eq = saved.get("daily_start_eq", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self.output(f"[恢复] peak_eq={self._peak_equity:.0f} avg={self.avg_price:.1f}")

        # 权益初始化
        acct = self._get_account()
        if acct:
            if self._peak_equity == 0:
                self._peak_equity = acct.balance
            if self._daily_start_eq == 0:
                self._daily_start_eq = acct.balance

        # 信任broker
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        # 换月检查
        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月提醒**: 距交割月**{days}天**")

        super().on_start()
        self.output(f"启动 | {p.instrument_id} M1 | 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**策略启动**\n合约: {p.instrument_id}\n持仓: {actual}手")

    def on_stop(self):
        self._save()
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止**\n持仓: {self.state_map.net_pos}手\n{self._slip.format_report()}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        p = self.params_map

        # 交易日切换
        td = get_trading_day()
        if td != self._current_td and self._current_td:
            acct = self._get_account()
            if acct:
                self._daily_start_eq = acct.balance
            self._today_trades = []
            self._current_td = td
            self.state_map.trading_day = td
            self._daily_review_sent = False
            self._rollover_checked = False
            self._save()
            self.output(f"[新交易日] {td}")
        if not self._current_td:
            self._current_td = td
            self.state_map.trading_day = td

        # 换月 (每天一次)
        if not self._rollover_checked:
            level, days = check_rollover(p.instrument_id)
            if level:
                feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")
            self._rollover_checked = True

        # 心跳
        for atype, msg in self._hb.check(p.instrument_id):
            if atype == "no_tick":
                feishu("no_tick", p.instrument_id, msg)

        # 每日回顾
        now = datetime.now()
        if (not self._daily_review_sent
                and now.hour == DAILY_REVIEW_HOUR
                and DAILY_REVIEW_MINUTE <= now.minute < DAILY_REVIEW_MINUTE + 5):
            self._send_review()
            self._daily_review_sent = True

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        # 撤挂单 + 超时检查
        for oid in list(self.order_id):
            self.cancel_order(oid)
        for oid in self._om.check_timeouts(self.cancel_order):
            self.output(f"[超时撤单] {oid}")

        # 执行pending
        if self._pending:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 指标
        producer = self.kline_generator.producer
        if len(producer.close) < p.slow_period + 2:
            self._push_widget(kline, signal_price)
            return

        close = float(producer.close[-1])
        fast_ma = float(producer.sma(p.fast_period))
        slow_ma = float(producer.sma(p.slow_period))
        self.state_map.fast_ma = round(fast_ma, 2)
        self.state_map.slow_ma = round(slow_ma, 2)

        # 持仓
        net_pos = self.get_position(p.instrument_id).net_position
        self.state_map.net_pos = net_pos
        if net_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
        elif close > self.peak_price:
            self.peak_price = close
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self.state_map.hard_line = round(self.avg_price*(1-p.hard_stop_pct/100), 1) if net_pos > 0 else 0.0
        self.state_map.trail_line = round(self.peak_price*(1-p.trailing_pct/100), 1) if net_pos > 0 else 0.0

        # 权益
        acct = self._get_account()
        equity = pos_profit = 0.0
        if acct:
            equity = acct.balance
            pos_profit = acct.position_profit
            if equity > self._peak_equity:
                self._peak_equity = equity
            self.state_map.equity = round(equity, 0)
            dd = (equity-self._peak_equity)/self._peak_equity if self._peak_equity > 0 else 0
            dp = (equity-self._daily_start_eq)/self._daily_start_eq if self._daily_start_eq > 0 else 0
            self.state_map.drawdown = f"{dd:.2%}"
            self.state_map.daily_pnl = f"{dp:+.2%}"

        # ── 止损检查 (调用risk模块) ──
        action, reason = check_stops(
            close=close, avg_price=self.avg_price, peak_price=self.peak_price,
            equity=equity, peak_equity=self._peak_equity,
            daily_start_eq=self._daily_start_eq, pos_profit=pos_profit,
            net_pos=net_pos, hard_stop_pct=p.hard_stop_pct,
            trailing_pct=p.trailing_pct, equity_stop_pct=p.equity_stop_pct,
        )
        if action and action != "WARNING":
            self._pending = action
            self.output(f"[{action}] {reason}")
        elif action == "WARNING":
            self.output(f"[预警] {reason}")
            feishu("warning", p.instrument_id, f"**回撤预警**: {reason}")

        # ── 正常信号 ──
        if self._pending is None:
            spread = (fast_ma-slow_ma)/slow_ma if slow_ma > 0 else 0
            raw = min(1.0, max(0.0, spread*100)) if fast_ma > slow_ma else 0.0
            forecast = raw * 10.0

            hi = np.array(producer.high, dtype=np.float64)
            lo = np.array(producer.low, dtype=np.float64)
            cl = np.array(producer.close, dtype=np.float64)
            atr_val = atr(hi, lo, cl, VOL_ATR_PERIOD)[-1]

            optimal = calc_optimal_lots(forecast, atr_val, close, p.capital, p.max_lots,
                                        annual_factor=ANNUAL_FACTOR)
            target = apply_buffer(optimal, net_pos)

            if target != net_pos:
                if net_pos == 0 and target > 0:
                    self._pending = "OPEN"
                elif target == 0 and net_pos > 0:
                    self._pending = "CLOSE"
                elif target > net_pos:
                    self._pending = "ADD"
                else:
                    self._pending = "REDUCE_SIG"
                self._pending_target = target

        self.state_map.pending = self._pending or "—"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
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
        actual = self.get_position(p.instrument_id).net_position

        if action == "OPEN":
            target = self._pending_target or p.unit_volume
            vol = max(1, target)
            acct = self._get_account()
            if acct and price*MULTIPLIER*vol*0.15 > acct.available*0.6:
                self.output("[保证金不足]")
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(exchange=p.exchange, instrument_id=p.instrument_id,
                                  volume=vol, price=price, order_direction="buy", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = price
            self.peak_price = price
            self.state_map.last_action = f"建仓{vol}手"
            self._rec("建仓", vol, "买", price, actual, actual+vol)
            feishu("open", p.instrument_id, f"**建仓** {vol}手 @ {price:,.1f}")
            self._save()
            return price

        elif action == "ADD":
            target = self._pending_target or (actual + p.unit_volume)
            vol = max(1, target - actual)
            acct = self._get_account()
            if acct and price*MULTIPLIER*vol*0.15 > acct.available*0.6:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(exchange=p.exchange, instrument_id=p.instrument_id,
                                  volume=vol, price=price, order_direction="buy", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = (self.avg_price*actual + price*vol)/(actual+vol) if actual > 0 else price
            self.state_map.last_action = f"加仓{vol}手"
            self._rec("加仓", vol, "买", price, actual, actual+vol)
            feishu("add", p.instrument_id, f"**加仓** {vol}手 @ {price:,.1f}\n均价: {self.avg_price:.1f}")
            self._save()
            return price

        elif action == "REDUCE_SIG":
            target = self._pending_target or max(0, actual-1)
            vol = actual - target
            if vol <= 0 or actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.auto_close_position(exchange=p.exchange, instrument_id=p.instrument_id,
                                           volume=vol, price=price, order_direction="sell", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.state_map.last_action = f"减仓{vol}手"
            self._rec("减仓", vol, "卖", price, actual, actual-vol)
            feishu("reduce", p.instrument_id, f"**减仓** {vol}手 @ {price:,.1f}")
            self._save()
            return -price

        elif action == "REDUCE":
            vol = max(1, actual//2)
            if actual <= 0:
                return 0.0
            oid = self.auto_close_position(exchange=p.exchange, instrument_id=p.instrument_id,
                                           volume=vol, price=price, order_direction="sell", market=True)
            if oid is not None:
                self.order_id.add(oid)
            self.state_map.last_action = f"回撤减仓{vol}手"
            self._rec("回撤减仓", vol, "卖", price, actual, actual-vol)
            feishu("reduce", p.instrument_id, f"**回撤减仓** {vol}手")
            self._save()
            return -price

        elif action in ("CLOSE","HARD_STOP","TRAIL_STOP","EQUITY_STOP","CIRCUIT","DAILY_STOP"):
            labels = {"CLOSE":"趋势出场","HARD_STOP":"硬止损","TRAIL_STOP":"移动止损",
                      "EQUITY_STOP":"权益止损","CIRCUIT":"熔断","DAILY_STOP":"单日止损"}
            label = labels.get(action, action)
            if actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.auto_close_position(exchange=p.exchange, instrument_id=p.instrument_id,
                                           volume=actual, price=price, order_direction="sell", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, actual, price)
            pnl_pct = (price-self.avg_price)/self.avg_price*100 if self.avg_price > 0 else 0
            abs_pnl = self._perf.on_close(self.avg_price, price, actual)
            self.state_map.last_action = f"★{label}★ {pnl_pct:+.2f}%"
            self._rec(label, actual, "卖", price, actual, 0)
            feishu(action.lower(), p.instrument_id,
                   f"**★{label}★** {actual}手 @ {price:,.1f}\n盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})")
            self.avg_price = 0.0
            self.peak_price = 0.0
            self._save()
            return -price

        return 0.0

    # ══════════════════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════════════════

    def _rec(self, action, lots, side, price, before, after):
        self._today_trades.append({"time": time.strftime("%H:%M:%S"), "action": action,
                                   "lots": lots, "side": side, "price": round(price,1),
                                   "before": before, "after": after})

    def _save(self):
        save_state({"peak_equity": self._peak_equity, "daily_start_eq": self._daily_start_eq,
                     "peak_price": self.peak_price, "avg_price": self.avg_price,
                     "trading_day": self._current_td, "today_trades": self._today_trades[-50:]},
                    name="TestFullModule")

    def _send_review(self):
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        net = pos.net_position if pos else 0
        acct = self._get_account()
        eq = acct.balance if acct else 0
        dd = ((eq-self._peak_equity)/self._peak_equity*100) if self._peak_equity > 0 else 0
        tbl = ""
        if self._today_trades:
            tbl = "| 时间 | 操作 | 手数 | 价格 | 持仓 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-10:]:
                tbl += f"| {t['time']} | {t['action']} | {t['lots']}({t['side']}) | {t['price']} | {t['before']}→{t['after']} |\n"
        else:
            tbl = "无交易"
        feishu("daily_review", p.instrument_id,
               f"**📊 每日回顾**\n权益: {eq:,.0f} 回撤: {dd:.2f}%\n"
               f"持仓: {net}手\n\n{tbl}\n\n{self._slip.format_report()}\n{self._perf.format_report(p.instrument_id)}")

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({"kline": kline, "signal_price": sp, **self.main_indicator_data})
        except Exception:
            pass

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)
        slip = self._slip.on_fill(trade.price, trade.volume,
                                   "buy" if "买" in str(trade.direction) else "sell")
        if slip != 0:
            self.output(f"[滑点] {slip:.1f}ticks")
        self.state_map.net_pos = self.get_position(self.params_map.instrument_id).net_position
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
