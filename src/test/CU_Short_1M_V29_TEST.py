"""
================================================================================
  CU_Short_1M_V29_TEST — MFI + RSI + EMA 做空测试版 (铜 M1)
================================================================================

  ⚠️ 仅用于模拟盘测试！M1级别信号频繁。
  信号逻辑与V29完全一致，仅时间框架改为1分钟。

================================================================================
"""
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier, get_tick_size
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR
from modules.risk import check_stops, RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer


STRATEGY_NAME = "CU_Short_1M_V29_TEST"

# 策略指标参数
MFI_PERIOD = 20
RSI_PERIOD = 20
EMA_PERIOD = 60
MFI_THRESHOLD = 65
WARMUP = 60

# Chandelier Exit (Short)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.5

# Vol Targeting (M1: 铜~600 bars/day)
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 600

DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def _ema(arr, period):
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period:
        return out
    out[period - 1] = np.mean(arr[:period])
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(closes, period=14):
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        out[period] = 100.0
    else:
        out[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            out[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _mfi(highs, lows, closes, volumes, period=14):
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tp = (highs + lows + closes) / 3.0
    raw_mf = tp * volumes
    for i in range(period, n):
        pos_flow = neg_flow = 0.0
        for j in range(i - period + 1, i + 1):
            if j > 0 and tp[j] > tp[j - 1]:
                pos_flow += raw_mf[j]
            elif j > 0 and tp[j] < tp[j - 1]:
                neg_flow += raw_mf[j]
        if neg_flow == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1 + pos_flow / neg_flow)
    return out


def atr(highs, lows, closes, period=14):
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1:period + 1])
    a = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1 - a) + tr[i] * a
    return out


def generate_signal(closes, highs, lows, volumes, bar_idx):
    if bar_idx < WARMUP:
        return 0.0
    mfi_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
    rsi_arr = _rsi(closes, RSI_PERIOD)
    ema_arr = _ema(closes, EMA_PERIOD)
    m = mfi_arr[bar_idx]
    r = rsi_arr[bar_idx]
    e = ema_arr[bar_idx]
    close = closes[bar_idx]
    if np.isnan(m) or np.isnan(r) or np.isnan(e):
        return 0.0
    score = 0.0
    if m > MFI_THRESHOLD:
        score += 1.0
    elif m > 50:
        score += 0.3
    if r > 60:
        score += 1.0
    elif r < 40:
        score += 0.5
    if close < e:
        score += 1.0
    if score >= 2.0:
        return -0.9 * min(1.0, score / 3.0)
    return 0.0


def chandelier_short(lows, closes, atr_arr, bar_idx):
    if bar_idx < CHANDELIER_PERIOD:
        return False
    a = atr_arr[bar_idx]
    if np.isnan(a):
        return False
    ll = np.min(lows[bar_idx - CHANDELIER_PERIOD + 1:bar_idx + 1])
    return bool(closes[bar_idx] > ll + CHANDELIER_MULT * a)


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="cu2605", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    max_lots: int = Field(default=5, title="最大持仓")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=True, title="24H模拟盘模式")


class State(BaseState):
    signal: float = Field(default=0.0, title="信号")
    forecast: float = Field(default=0.0, title="预测")
    target_lots: int = Field(default=0, title="目标手")
    net_pos: int = Field(default=0, title="净持仓")
    avg_price: float = Field(default=0.0, title="均价")
    trough_price: float = Field(default=0.0, title="谷价(空)")
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    daily_pnl: str = Field(default="---", title="当日盈亏")
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="上次操作")
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")


class CU_Short_1M_V29_TEST(BaseStrategy):
    """⚠️ 测试版 — 铜M1做空 V29 MFI + RSI + EMA"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None
        self.avg_price = 0.0
        self.trough_price = 0.0
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()
        self._investor_id = ""
        self._risk = None
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._multiplier = 5

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def on_start(self):
        p = self.params_map
        self._multiplier = get_multiplier(p.instrument_id)
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)
        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange, instrument_id=p.instrument_id, style=p.kline_style,
        )
        self.kline_generator.push_history_data()
        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id
        self._risk = RiskManager(capital=p.capital)
        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self.trough_price = saved.get("trough_price", 0.0)
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance)
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        self.state_map.net_pos = -actual
        if actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td
        super().on_start()
        self.output(f"⚠️ TEST {STRATEGY_NAME} 启动 | {p.instrument_id} {p.kline_style} | 乘数={self._multiplier} | 持仓={actual}")

    def on_stop(self):
        self._save()
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        try:
            td = get_trading_day()
            if td != self._current_td and self._current_td:
                acct = self._get_account()
                if acct:
                    self._risk.on_day_change(acct.balance)
                self._perf.on_day_change()
                self._today_trades = []
                self._current_td = td
                self.state_map.trading_day = td
                self._daily_review_sent = False
                self._save()
            if not self._current_td:
                self._current_td = td
                self.state_map.trading_day = td
            self.state_map.session = self._guard.get_status()
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")

    def callback(self, kline: KLineData):
        try:
            self._on_bar(kline)
        except Exception as e:
            self.output(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar(self, kline: KLineData):
        signal_price = 0.0
        p = self.params_map

        for oid in list(self.order_id):
            self.cancel_order(oid)
        for oid in self._om.check_timeouts(self.cancel_order):
            self.output(f"[超时撤单] {oid}")

        if not self.trading:
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline)
            return

        if self._pending is not None:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP + 2:
            self._push_widget(kline, signal_price)
            return

        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        bar_idx = len(closes) - 1
        close = float(closes[-1])

        self.output(f"[DEBUG] bar_idx={bar_idx} closes={len(closes)}")

        # debug: 打印指标值
        mfi_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
        rsi_arr = _rsi(closes, RSI_PERIOD)
        ema_arr = _ema(closes, EMA_PERIOD)
        m_val = mfi_arr[bar_idx] if not np.isnan(mfi_arr[bar_idx]) else -1
        r_val = rsi_arr[bar_idx] if not np.isnan(rsi_arr[bar_idx]) else -1
        e_val = ema_arr[bar_idx] if not np.isnan(ema_arr[bar_idx]) else -1
        self.output(f"[IND] MFI={m_val:.1f} RSI={r_val:.1f} EMA60={e_val:.1f} close={closes[bar_idx]:.1f}")

        raw = generate_signal(closes, highs, lows, volumes, bar_idx)
        forecast = min(FORECAST_CAP, max(0.0, abs(raw) * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        atr_arr = atr(highs, lows, closes)
        optimal_raw = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        )
        optimal = round(optimal_raw)
        net_pos = abs(self.get_position(p.instrument_id).net_position)
        target = apply_buffer(optimal, net_pos)
        target = min(target, p.max_lots)
        self.state_map.net_pos = -net_pos
        self.state_map.target_lots = -target

        if net_pos == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
        elif self.trough_price == 0.0 or close < self.trough_price:
            self.trough_price = close
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.trough_price = round(self.trough_price, 1)

        acct = self._get_account()
        equity = pos_profit = 0.0
        if acct:
            equity = acct.balance
            pos_profit = acct.position_profit
            self._risk.update(equity)
            self.state_map.equity = round(equity, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        # 盘前清仓已禁用 — 完全靠信号和止损管理
        # if self._guard.should_flatten() and net_pos > 0:
        #     self._pending_reason = "盘前清仓"
        #     self._exec_close(kline, net_pos, "FLATTEN")
        #     self._push_widget(kline, kline.close)
        #     self.update_status_bar()
        #     return

        if not self._guard.should_trade():
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 止损
        if net_pos > 0:
            if self.avg_price > 0 and close >= self.avg_price * (1 + p.hard_stop_pct / 100):
                self._pending = "HARD_STOP"
                self._pending_reason = f"硬止损(空) close={close:.1f}"
                self.output(f"[HARD_STOP] {self._pending_reason}")
            if self.trough_price > 0 and close >= self.trough_price * (1 + p.trailing_pct / 100):
                self._pending = "TRAIL_STOP"
                self._pending_reason = f"移动止损(空) close={close:.1f}"
                self.output(f"[TRAIL_STOP] {self._pending_reason}")
            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.avg_price,
                pos_profit=pos_profit, net_pos=net_pos,
                hard_stop_pct=999.0, trailing_pct=999.0, equity_stop_pct=p.equity_stop_pct,
            )
            if action and action not in ("WARNING", "HARD_STOP", "TRAIL_STOP"):
                self._pending = action
                self._pending_reason = reason

        # Chandelier Exit
        if self._pending is None and net_pos > 0:
            ch_atr = atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_short(lows, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_reason = "Chandelier Exit (Short)"

        # 正常信号
        if self._pending is None and target != net_pos:
            if net_pos == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and net_pos > 0:
                self._pending = "CLOSE"
            elif target > net_pos:
                self._pending = "ADD"
            elif target < net_pos:
                self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = f"signal={raw:.2f} forecast={forecast:.1f} optimal={optimal} target={target}"

        # ── 当前bar立即处理pending (不等下一根bar) ──
        if self._pending is not None:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行 (SHORT: open=sell, close=buy)
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData, action: str) -> float:
        price = kline.close
        p = self.params_map
        actual = abs(self.get_position(p.instrument_id).net_position)

        if action == "OPEN":
            target = self._pending_target or 1
            vol = max(1, target)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[保证金不足]")
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(exchange=p.exchange, instrument_id=p.instrument_id,
                                  volume=vol, price=price, order_direction="sell", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = price
            self.trough_price = price
            self.state_map.last_action = f"开空{vol}手"
            self._rec("开空", vol, "卖", price, actual, actual + vol)
            feishu("open", p.instrument_id, f"**开空** {vol}手 @ {price:,.1f}\n{self._pending_reason}")
            self._save()
            return -price

        elif action == "ADD":
            target = self._pending_target or (actual + 1)
            vol = max(1, target - actual)
            acct = self._get_account()
            if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
                self.output("[加仓保证金不足]")
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.send_order(exchange=p.exchange, instrument_id=p.instrument_id,
                                  volume=vol, price=price, order_direction="sell", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.avg_price = (self.avg_price * actual + price * vol) / (actual + vol) if actual > 0 else price
            self.state_map.last_action = f"加空{vol}手"
            self._rec("加空", vol, "卖", price, actual, actual + vol)
            feishu("add", p.instrument_id, f"**加空** {vol}手 @ {price:,.1f}\n{self._pending_reason}")
            self._save()
            return -price

        elif action == "REDUCE":
            vol = max(1, actual // 2)
            if actual <= 0:
                return 0.0
            self._slip.set_signal_price(price)
            oid = self.auto_close_position(exchange=p.exchange, instrument_id=p.instrument_id,
                                           volume=vol, price=price, order_direction="buy", market=True)
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            self.state_map.last_action = f"减空{vol}手"
            self._rec("减空", vol, "买", price, actual, actual - vol)
            feishu("reduce", p.instrument_id, f"**减空** {vol}手 @ {price:,.1f}\n{self._pending_reason}")
            self._save()
            return price

        elif action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                         "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, actual, action)
        return 0.0

    def _exec_close(self, kline: KLineData, actual: int, action: str) -> float:
        labels = {"CLOSE": "信号平仓", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
                  "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损", "FLATTEN": "即将收盘清仓"}
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close
        if actual <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        oid = self.auto_close_position(exchange=p.exchange, instrument_id=p.instrument_id,
                                       volume=actual, price=price, order_direction="buy", market=True)
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, price)
        pnl_pct = (self.avg_price - price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, actual, direction="short")
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, actual, "买", price, actual, 0)
        feishu(action.lower(), p.instrument_id, f"**{label}** {actual}手 @ {price:,.1f}\n盈亏: {pnl_pct:+.2f}%")
        self.avg_price = 0.0
        self.trough_price = 0.0
        self._save()
        return price

    def _rec(self, action, lots, side, price, before, after):
        self._today_trades.append({"time": time.strftime("%H:%M:%S"), "action": action,
                                   "lots": lots, "side": side, "price": round(price, 1),
                                   "before": before, "after": after})

    def _save(self):
        state = {"trough_price": self.trough_price, "avg_price": self.avg_price,
                 "trading_day": self._current_td, "today_trades": self._today_trades[-50:]}
        state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({"kline": kline, "signal_price": sp, **self.main_indicator_data})
        except Exception:
            pass

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_id.discard(trade.order_id)
        self._om.on_fill(trade.order_id)
        self._slip.on_fill(trade.price, trade.volume, "buy" if "买" in str(trade.direction) else "sell")
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        direction = "buy" if "买" in str(trade.direction) else "sell"
        if direction == "sell" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
        elif direction == "buy" and actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
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
