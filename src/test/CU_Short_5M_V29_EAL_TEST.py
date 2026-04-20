"""
================================================================================
  CU_Short_5M_V29_EAL_TEST — MFI + RSI + EMA 做空 + EAL执行 (铜 M5)
================================================================================

  ⚠️ 模拟盘测试！信号bar=M5, 执行bar=M1(EAL子bar).
  EAL将大单拆批次，在M1子bar上用多因子评分择时执行。

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
from modules.error_handler import throttle_on_error
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
from modules.eal import EALManager, EALConfig


STRATEGY_NAME = "CU_Short_5M_V29_EAL_TEST"

MFI_PERIOD = 20
RSI_PERIOD = 20
EMA_PERIOD = 60
MFI_THRESHOLD = 65
WARMUP = 60

CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.5

FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * 120


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
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(period, n - 1):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _mfi(highs, lows, closes, volumes, period=14):
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tp = (highs + lows + closes) / 3.0
    raw_mf = tp * volumes
    for i in range(period, n):
        pf = nf = 0.0
        for j in range(i - period + 1, i + 1):
            if j > 0 and tp[j] > tp[j - 1]:
                pf += raw_mf[j]
            elif j > 0 and tp[j] < tp[j - 1]:
                nf += raw_mf[j]
        out[i] = 100.0 if nf == 0 else 100.0 - 100.0 / (1 + pf / nf)
    return out


def atr(highs, lows, closes, period=14):
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1:period + 1])
    a = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1 - a) + tr[i] * a
    return out


def generate_signal(closes, highs, lows, volumes, bar_idx):
    if bar_idx < WARMUP:
        return 0.0
    m_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
    r_arr = _rsi(closes, RSI_PERIOD)
    e_arr = _ema(closes, EMA_PERIOD)
    m, r, e, c = m_arr[bar_idx], r_arr[bar_idx], e_arr[bar_idx], closes[bar_idx]
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
    if c < e:
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


class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="cu2605", title="合约代码")
    kline_style: str = Field(default="M5", title="信号K线周期")
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
    eal_status: str = Field(default="空闲", title="EAL状态")
    last_action: str = Field(default="---", title="上次操作")
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")


class CU_Short_5M_V29_EAL_TEST(BaseStrategy):
    """测试版 铜M5做空 V29 MFI+RSI+EMA + EAL执行"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None
        self.kline_generator_exec = None
        self.avg_price = 0.0
        self.trough_price = 0.0
        self.order_id = set()
        self._investor_id = ""
        self._risk = None
        self._current_td = ""
        self._daily_review_sent = False
        self._today_trades = []
        self._guard = None
        self._slip = None
        self._hb = None
        self._om = OrderMonitor()
        self._perf = None
        self._multiplier = 5
        self._eal = None

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
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes, sim_24h=p.sim_24h, open_grace_sec=30)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)
        self.kline_generator = KLineGenerator(
            callback=self.callback, real_time_callback=self.real_time_callback,
            exchange=p.exchange, instrument_id=p.instrument_id, style=p.kline_style,
        )
        self.kline_generator.push_history_data()
        self.kline_generator_exec = KLineGenerator(
            callback=self.callback_exec, real_time_callback=self.real_time_callback_exec,
            exchange=p.exchange, instrument_id=p.instrument_id, style="M1",
        )
        self.kline_generator_exec.push_history_data()
        self._eal = EALManager(EALConfig(
            max_batch_size=3, execution_window_bars=5, force_final_batch=True,
        ))
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
                self._risk.on_day_change(acct.balance, acct.position_profit)
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
        self.output(
            f"EAL TEST {STRATEGY_NAME} | {p.instrument_id} "
            f"{p.kline_style}+M1(EAL) | mult={self._multiplier} | pos={actual}"
        )

    def on_stop(self):
        if self._eal and self._eal.is_active():
            self._eal.cancel()
        self._save()
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        self.kline_generator_exec.tick_to_kline(tick)
        try:
            td = get_trading_day()
            if td != self._current_td and self._current_td:
                acct = self._get_account()
                if acct:
                    self._risk.on_day_change(acct.balance, acct.position_profit)
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

    # ── M5 信号回调 ──

    def callback(self, kline: KLineData):
        try:
            self._on_bar_signal(kline)
        except Exception as e:
            self.output(f"[M5异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _on_bar_signal(self, kline):
        p = self.params_map
        if not self.trading:
            return
        if self._eal and self._eal.is_active():
            self.state_map.eal_status = self._eal.get_status()
            self._push_widget(kline)
            return

        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP + 2:
            self._push_widget(kline)
            return
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        bar_idx = len(closes) - 1
        close = float(closes[-1])

        m_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
        r_arr = _rsi(closes, RSI_PERIOD)
        e_arr = _ema(closes, EMA_PERIOD)
        mv = m_arr[bar_idx] if not np.isnan(m_arr[bar_idx]) else 0
        rv = r_arr[bar_idx] if not np.isnan(r_arr[bar_idx]) else 0
        ev = e_arr[bar_idx] if not np.isnan(e_arr[bar_idx]) else 0
        self.output(f"[IND] MFI={mv:.1f} RSI={rv:.1f} EMA60={ev:.1f} close={close:.1f}")

        raw = generate_signal(closes, highs, lows, volumes, bar_idx)
        forecast = min(FORECAST_CAP, max(0.0, abs(raw) * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self.output(f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f}")

        atr_arr = atr(highs, lows, closes)
        optimal = round(calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        ))
        net_pos = abs(self.get_position(p.instrument_id).net_position)
        target = min(apply_buffer(optimal, net_pos), p.max_lots)

        # forecast=0 → 强制退出 (信号消失不走buffer)
        if forecast == 0 and net_pos > 0:
            target = 0
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
        if acct:
            self._risk.update(acct.balance)
            self.state_map.equity = round(acct.balance, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        if not self._guard.should_trade():
            self._push_widget(kline)
            return

        # 止损 → 立即执行
        if net_pos > 0:
            if self.avg_price > 0 and close >= self.avg_price * (1 + p.hard_stop_pct / 100):
                self.output(f"[HARD_STOP] close={close:.1f}")
                self._immediate_close(kline, net_pos, "HARD_STOP")
                return
            if self.trough_price > 0 and close >= self.trough_price * (1 + p.trailing_pct / 100):
                self.output(f"[TRAIL_STOP] close={close:.1f}")
                self._immediate_close(kline, net_pos, "TRAIL_STOP")
                return
            ch_atr = atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_short(lows, closes, ch_atr, bar_idx):
                self.output("[CHANDELIER] Exit")
                self._immediate_close(kline, net_pos, "CLOSE")
                return

        # 正常信号 → EAL
        if target != net_pos:
            if target > net_pos and net_pos >= p.max_lots:
                self.output(f"[SKIP] 已达最大持仓 net_pos={net_pos} >= max_lots={p.max_lots}")
            else:
                direction = "sell" if target > net_pos else "buy"
                self._eal.submit(target, net_pos, direction)
                self.output(f"[EAL提交] target={target} current={net_pos} dir={direction}")

        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline)
        self.update_status_bar()

    # ── M1 EAL执行回调 ──

    def callback_exec(self, kline: KLineData):
        if not self.trading or not self._eal or not self._eal.is_active():
            return
        action = self._eal.on_bar(kline)
        if action is None:
            self.state_map.eal_status = self._eal.get_status()
            return

        vol = action['volume']
        direction = action['direction']
        p = self.params_map
        price = kline.close

        # 执行前检查总持仓，防止超限
        actual = abs(self.get_position(p.instrument_id).net_position)
        if direction == "sell" and actual >= p.max_lots:
            self.output(f"[EAL取消] 持仓已达上限 {actual}>={p.max_lots}")
            self._eal.cancel()
            self.state_map.eal_status = self._eal.get_status()
            return

        self.output(
            f"[EAL] batch{action['batch_idx']} {vol}手 {direction} "
            f"@{price:.1f} score={action['score']:.2f} timeout={action['timeout']}"
        )

        self._slip.set_signal_price(price)
        if direction == "sell":
            vol = min(vol, p.max_lots - actual)
            if vol <= 0:
                self._eal.cancel()
                return
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price, order_direction="sell",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            if self.avg_price == 0:
                self.avg_price = price
                self.trough_price = price
            else:
                total = actual + vol
                self.avg_price = (self.avg_price * actual + price * vol) / total if total > 0 else price
            self._rec("EAL开空", vol, "卖", price, actual, actual + vol)
        else:
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=price, order_direction="buy",
            )
            if oid is not None:
                self.order_id.add(oid)
                self._om.on_send(oid, vol, price)
            remaining = max(0, actual - vol)
            if remaining == 0:
                self._perf.on_close(self.avg_price, price, actual, direction="short")
                self.avg_price = 0.0
                self.trough_price = 0.0
            self._rec("EAL平空", vol, "买", price, actual, remaining)

        if not self._eal.is_active():
            r = self._eal.get_result()
            self.output(
                f"[EAL完成] {r['total_filled']}手 VWAP={r['vwap_fill']:.1f} "
                f"bars={r['bars_used']} timeout={r['timeout_count']}"
            )
            self.state_map.last_action = f"EAL {r['total_filled']}手@{r['vwap_fill']:.1f}"

        self.state_map.eal_status = self._eal.get_status()
        self._save()
        self.update_status_bar()

    def real_time_callback_exec(self, kline: KLineData):
        pass

    # ── 止损立即执行 ──

    def _immediate_close(self, kline, actual, action):
        labels = {
            "CLOSE": "信号平仓", "HARD_STOP": "硬止损",
            "TRAIL_STOP": "移动止损", "FLATTEN": "即将收盘清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close
        if self._eal and self._eal.is_active():
            self._eal.cancel()
            self.output("[EAL取消] 止损优先")
        if actual <= 0:
            return
        self._slip.set_signal_price(price)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=actual, price=price, order_direction="buy",
        )
        if oid is not None:
            self.order_id.add(oid)
            self._om.on_send(oid, actual, price)
        pnl_pct = (self.avg_price - price) / self.avg_price * 100 if self.avg_price > 0 else 0
        self._perf.on_close(self.avg_price, price, actual, direction="short")
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, actual, "买", price, actual, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {actual}手 @{price:,.1f} pnl={pnl_pct:+.2f}%")
        self.avg_price = 0.0
        self.trough_price = 0.0
        self._save()

    # ── 辅助 ──

    def _rec(self, action, lots, side, price, before, after):
        self._today_trades.append({
            "time": time.strftime("%H:%M:%S"), "action": action,
            "lots": lots, "side": side, "price": round(price, 1),
            "before": before, "after": after,
        })

    def _save(self):
        state = {
            "trough_price": self.trough_price, "avg_price": self.avg_price,
            "trading_day": self._current_td, "today_trades": self._today_trades[-50:],
        }
        state.update(self._risk.get_state() if self._risk is not None else {})
        save_state(state, name=STRATEGY_NAME)

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
        self._slip.on_fill(
            trade.price, trade.volume,
            ("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell"),
        )
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        actual = abs(pos.net_position) if pos else 0
        direction = ("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell")
        if direction == "sell" and actual > 0:
            old_pos = max(0, actual - trade.volume)
            if old_pos > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old_pos + trade.price * trade.volume) / actual
            else:
                self.avg_price = trade.price
        elif direction == "buy" and actual == 0:
            self.avg_price = 0.0
            self.trough_price = 0.0
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
