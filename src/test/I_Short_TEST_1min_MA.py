"""I_Short_TEST_1min_MA — 1分钟双MA做空测试

⚠️  仅用于测试开仓/加仓/减仓/平仓流程！
逻辑: MA3 > MA10 → 做空(信号翻转频繁), MA3 < MA10 → 平仓
基于 TestFullModule 模式编写，确保兼容 PythonGO。
"""
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier
from modules.error_handler import throttle_on_error
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day
from modules.order_monitor import OrderMonitor


STRATEGY_NAME = "I_Short_TEST_1min_MA"
MA_FAST = 3
MA_SLOW = 10


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所")
    instrument_id: str = Field(default="i2609", title="合约")
    kline_style: str = Field(default="M1", title="K线周期")
    max_position: int = Field(default=10, title="最大手数")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=True, title="模拟盘")


class State(BaseState):
    ma_fast: float = Field(default=0.0, title="MA3")
    ma_slow: float = Field(default=0.0, title="MA10")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="持仓")
    avg_price: float = Field(default=0.0, title="均价")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="操作")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════

class I_Short_TEST_1min_MA(BaseStrategy):
    """⚠️ 测试版 — 1分钟双MA做空, 验证开仓/加仓/减仓/平仓"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()

        self.avg_price = 0.0
        self._om = OrderMonitor()
        self._guard = None
        self._multiplier = 100

    @property
    def main_indicator_data(self):
        return {"MA3": self.state_map.ma_fast, "MA10": self.state_map.ma_slow}

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map
        self._multiplier = get_multiplier(p.instrument_id)
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        saved = load_state(STRATEGY_NAME)
        if saved:
            self.avg_price = saved.get("avg_price", 0.0)

        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual == 0:
            self.avg_price = 0.0

        super().on_start()
        self.output(f"⚠️ TEST {STRATEGY_NAME} 启动 | {p.instrument_id} {p.kline_style} | 持仓={actual}")
        feishu("start", p.instrument_id,
               f"**⚠️ 测试启动** {STRATEGY_NAME}\n合约: {p.instrument_id}\n持仓: {actual}手")

    def on_stop(self):
        save_state({"avg_price": self.avg_price}, name=STRATEGY_NAME)
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        if not hasattr(self, '_tick_count'):
            self._tick_count = 0
        self._tick_count += 1
        if self._tick_count <= 5 or self._tick_count % 200 == 0:
            self.output(f"[TICK#{self._tick_count}] {tick.instrument_id} @ {tick.last_price}")
        self.kline_generator.tick_to_kline(tick)

    # ══════════════════════════════════════════════════════════════════════
    #  K线回调 (和 TestFullModule 完全一致的命名)
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar(self, kline: KLineData):
        p = self.params_map

        # 撤挂单
        for oid in list(self.order_id):
            self.cancel_order(oid)
        for oid in self._om.check_timeouts(self.cancel_order):
            self.output(f"[超时撤单] {oid}")

        # 历史回放
        if not self.trading:
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline)
            return

        # 执行pending (next-bar规则)
        if self._pending is not None:
            self._execute(kline)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline)
            self.update_status_bar()
            return

        # 数据
        producer = self.kline_generator.producer
        if len(producer.close) < MA_SLOW + 2:
            self._push_widget(kline)
            return

        closes = np.array(producer.close, dtype=np.float64)
        close = float(closes[-1])
        fast_val = float(np.mean(closes[-MA_FAST:]))
        slow_val = float(np.mean(closes[-MA_SLOW:]))
        self.state_map.ma_fast = round(fast_val, 1)
        self.state_map.ma_slow = round(slow_val, 1)

        # 持仓
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        self.state_map.net_pos = current

        # 盘前清仓已禁用 — 完全靠信号和止损管理
        # # ── 盘前清仓 ──
        # if self._guard.should_flatten() and current > 0:
        #     self._pending = "FLATTEN"
        #     self._pending_target = 0
        #     self._pending_reason = "盘前清仓"
        #     self._push_widget(kline)
        #     return

        # ── 非交易时段 ──
        if not self._guard.should_trade():
            self._push_widget(kline)
            return

        # ── 信号 → 目标手数 ──
        diff_pct = (fast_val - slow_val) / slow_val * 100
        self.output(f"[信号] MA3={fast_val:.1f} MA10={slow_val:.1f} diff={diff_pct:+.4f}% 持仓={current}")

        if diff_pct < -0.01:
            # MA3 < MA10 → 做空
            if diff_pct < -0.05:
                target = p.max_position
            else:
                target = max(1, p.max_position // 2)
        elif diff_pct > 0.01:
            # MA3 > MA10 → 平仓
            target = 0
        else:
            target = current

        # ── pending ──
        if target != current:
            if current == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and current > 0:
                self._pending = "CLOSE"
            elif target > current:
                self._pending = "ADD"
            elif target < current:
                self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = (
                f"MA3={fast_val:.1f} MA10={slow_val:.1f} "
                f"diff={diff_pct:+.4f}% target={target}"
            )

        self.state_map.target_lots = target

        # ── 当前bar立即处理pending (不等下一根bar) ──
        if self._pending is not None:
            self._execute(kline)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self._push_widget(kline)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (SHORT: open=sell, close=buy)
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData):
        action = self._pending
        target = self._pending_target if self._pending_target is not None else 0
        reason = self._pending_reason
        p = self.params_map
        price = kline.close
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        diff = target - current

        if diff == 0:
            return

        if diff > 0:
            # 开空 / 加空
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=diff, price=price, order_direction="sell", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, diff, price)
            if action == "OPEN":
                self.avg_price = price
            elif current > 0:
                self.avg_price = (self.avg_price * current + price * diff) / (current + diff)
        else:
            # 减仓 / 平仓
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price, order_direction="buy", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, abs(diff), price)
            if target == 0:
                self.avg_price = 0.0

        label = {
            "OPEN": "开空", "ADD": "加空", "REDUCE": "减空",
            "CLOSE": "平空", "FLATTEN": "清仓",
        }.get(action, action)

        self.output(f"[{label}] {abs(diff)}手 @ {price:.1f} | {current}→{target} | {reason}")
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {abs(diff)}手 @ {price:,.1f}\n"
               f"{reason}\n持仓: {current} → {target}")
        self.state_map.last_action = label
        save_state({"avg_price": self.avg_price}, name=STRATEGY_NAME)

    # ══════════════════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════════════════

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({
                "kline": kline, "signal_price": sp, **self.main_indicator_data,
            })
        except Exception:
            pass

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)
        self.state_map.net_pos = self.get_position(
            self.params_map.instrument_id).net_position
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        throttle_on_error(self, error)
