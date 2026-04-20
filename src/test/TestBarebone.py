"""
TestBarebone — 最小化测试: 零module依赖, 只测K线回调+开平仓
确认基础链路: on_start -> on_tick -> callback -> send_order
"""
import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator


class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2609", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    fast_period: int = Field(default=3, title="快线周期", ge=2)
    slow_period: int = Field(default=7, title="慢线周期", ge=2)


class State(BaseState):
    fast_ma: float = Field(default=0.0, title="快均线")
    slow_ma: float = Field(default=0.0, title="慢均线")
    net_pos: int = Field(default=0, title="净持仓")
    bar_count: int = Field(default=0, title="K线数")
    last_action: str = Field(default="---", title="上次操作")


class TestBarebone(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None
        self._pending = None
        self.order_id = set()
        self._bar_count = 0

    @property
    def main_indicator_data(self):
        return {
            "MA_fast": self.state_map.fast_ma,
            "MA_slow": self.state_map.slow_ma,
        }

    def on_start(self):
        self.output("=== TestBarebone on_start ===")
        p = self.params_map
        try:
            self.kline_generator = KLineGenerator(
                callback=self.callback,
                real_time_callback=self.real_time_callback,
                exchange=p.exchange,
                instrument_id=p.instrument_id,
                style=p.kline_style,
            )
            self.output("KLineGenerator OK")
            self.kline_generator.push_history_data()
            self.output("push_history_data OK, bars=" + str(len(self.kline_generator.producer.close)))
        except Exception as e:
            self.output("KLineGenerator FAIL: " + str(e))

        super().on_start()
        self.output("=== on_start DONE ===")

    def on_stop(self):
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        pos = self.get_position(self.params_map.instrument_id)
        self.state_map.net_pos = pos.net_position if pos else 0
        self.output("[TRADE] vol=" + str(trade.volume) + " pos=" + str(self.state_map.net_pos))
        self.update_status_bar()

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output("[CALLBACK ERROR] " + str(type(e).__name__) + ": " + str(e))

    def _on_bar(self, kline: KLineData):
        p = self.params_map
        signal_price = 0.0
        self._bar_count += 1

        # 撤挂单
        for oid in list(self.order_id):
            self.cancel_order(oid)

        # 执行pending
        if self._pending is not None:
            signal_price = self._do_exec(kline, self._pending)
            self._pending = None
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 数据检查
        producer = self.kline_generator.producer
        n = len(producer.close)
        if n < p.slow_period + 2:
            self.output("[WARMUP] " + str(n) + "/" + str(p.slow_period + 2))
            self._push_widget(kline, signal_price)
            return

        closes = np.array(producer.close, dtype=np.float64)
        close = float(closes[-1])
        fast_ma = float(np.mean(closes[-p.fast_period:]))
        slow_ma = float(np.mean(closes[-p.slow_period:]))
        self.state_map.fast_ma = round(fast_ma, 2)
        self.state_map.slow_ma = round(slow_ma, 2)
        self.state_map.bar_count = self._bar_count

        net_pos = self.get_position(p.instrument_id).net_position
        self.state_map.net_pos = net_pos
        bullish = fast_ma > slow_ma

        # 金叉开, 死叉平
        if net_pos == 0 and bullish:
            self._pending = "OPEN"
        elif net_pos > 0 and not bullish:
            self._pending = "CLOSE"

        self.output(
            "[BAR " + str(self._bar_count) + "] "
            "c=" + str(round(close, 1)) + " "
            "f=" + str(round(fast_ma, 1)) + " "
            "s=" + str(round(slow_ma, 1)) + " "
            "pos=" + str(net_pos) + " "
            "p=" + str(self._pending or "---")
        )
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    def _do_exec(self, kline, action):
        p = self.params_map
        price = kline.close
        actual = self.get_position(p.instrument_id).net_position

        if action == "OPEN":
            self.output("[OPEN] buy 1 @ " + str(price))
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=1, price=price,
                order_direction="buy", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self.output("[OPEN OK] oid=" + str(oid))
            else:
                self.output("[OPEN FAIL] oid=None")
            self.state_map.last_action = "OPEN"
            return price

        elif action == "CLOSE" and actual > 0:
            self.output("[CLOSE] sell " + str(actual) + " @ " + str(price))
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=actual, price=price,
                order_direction="sell", market=False,
            )
            if oid is not None:
                self.order_id.add(oid)
                self.output("[CLOSE OK] oid=" + str(oid))
            else:
                self.output("[CLOSE FAIL] oid=None")
            self.state_map.last_action = "CLOSE"
            return -price

        return 0.0

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({"kline": kline, "signal_price": sp, **self.main_indicator_data})
        except Exception:
            pass
