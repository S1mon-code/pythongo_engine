"""I_Long_4H_V27 — McGinley Dynamic + Force Index (铁矿石4H做多)

QBase_v2 策略: mild_trend_long_I_4h_v27
信号逻辑:
  Close > McGinley(20) AND ForceIndex(13) > 0 → signal = +1.0
  Close < McGinley(20) AND ForceIndex(13) < 0 → signal = -1.0
  Close > McGinley(20) AND ForceIndex(13) <= 0 → signal = +0.3
  Close < McGinley(20) AND ForceIndex(13) >= 0 → signal = -0.3
  else → 0.0
  LONG ONLY: clamp to [0, 1]

仓位: Vol Targeting 每bar重算 + Carver 10% buffer
止损: 移动止损 + 2%权益硬止损 + Chandelier Exit + Portfolio Stops

部署: 复制到 Windows 无限易 pyStrategy/self_strategy/
"""
import math
import os
import json
import time
from datetime import datetime, timedelta

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.feishu import feishu, LABELS


# ═══════════════════════════════════════════════════════════════
# Section 1: CONFIG
# ═══════════════════════════════════════════════════════════════

# 合约参数 (铁矿石 DCE)
MULTIPLIER = 100               # 合约乘数
TICK_SIZE = 0.5                # 最小变动价位

# 策略指标参数 (QBase mild_trend_long_I_4h_v27)
MCGINLEY_PERIOD = 20           # McGinley Dynamic 周期
FORCE_INDEX_PERIOD = 13        # Force Index EMA 平滑周期
WARMUP = 53                    # 热身bar数

# Vol Targeting
TARGET_VOL = 0.15              # 目标年化波动率 15%
VOL_ATR_PERIOD = 14            # ATR周期 (仓位用)
# DCE铁矿石交易时段: 9:00-11:30(2.5h) + 13:30-15:00(1.5h) + 21:00-23:00(2h) = 6h
# 6h / 4h = 1.5 bars/day → 252 × 1.5 = 378
ANNUAL_FACTOR = 378            # 4H bars per year (铁矿石)

# Forecast (单策略, 无blending)
FORECAST_SCALAR = 10.0         # raw signal [0,1] × 10 → forecast [0,10]
FORECAST_CAP = 20.0

# Carver Buffer
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

# 止损
TRAILING_PCT = 2.0             # 移动止损 2%
HARD_STOP_EQUITY_PCT = 0.02    # 账户权益 2% 硬止损
STOP_WARNING = -0.10           # -10% 预警
STOP_REDUCE = -0.15            # -15% 减仓
STOP_CIRCUIT = -0.20           # -20% 熔断
STOP_DAILY = -0.05             # -5% 单日止损

# Chandelier Exit
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# 换月提醒 (天数)
ROLLOVER_WARN_DAYS = 15
ROLLOVER_URGENT_DAYS = 5

# 状态文件路径
STATE_DIR = "./state"
STRATEGY_NAME = "I_Long_4H_V27_McGinley_ForceIndex"


# ═══════════════════════════════════════════════════════════════
# Section 2: INDICATORS (纯numpy实现, 自包含)
# ═══════════════════════════════════════════════════════════════

def ema(data, period):
    """EMA seeded with data[0], no NaN. (QBase trend/ema.py)"""
    n = len(data)
    if n == 0:
        return np.array([], dtype=np.float64)
    alpha = 2.0 / (period + 1)
    out = np.empty(n, dtype=np.float64)
    out[0] = data[0]
    for i in range(1, n):
        out[i] = alpha * data[i] + (1.0 - alpha) * out[i - 1]
    return out


def mcginley_dynamic(closes, period=20):
    """McGinley Dynamic: 根据市场速度自适应调整的移动平均线.

    公式: MD[i] = MD[i-1] + (Close - MD[i-1]) / (k × (Close/MD[i-1])^4)
    其中 k = period (McGinley常数)
    种子值: 前 period+1 根bar的SMA
    """
    n = len(closes)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    # 用SMA作为种子值
    out[period] = np.mean(closes[:period + 1])
    k = period  # McGinley常数
    for i in range(period + 1, n):
        prev = out[i - 1]
        ratio = closes[i] / prev if prev != 0 else 1.0
        denominator = k * (ratio ** 4)
        if denominator == 0:
            out[i] = prev
        else:
            out[i] = prev + (closes[i] - prev) / denominator
    return out


def force_index(closes, volumes, period=13):
    """Force Index: (close - prev_close) × volume, 经EMA平滑.

    衡量价格变动的力量, 综合价格变化和成交量.
    """
    n = len(closes)
    if n < 2:
        return np.full(n, np.nan)
    raw = np.zeros(n, dtype=np.float64)
    raw[1:] = (closes[1:] - closes[:-1]) * volumes[1:]
    return ema(raw, period)


def atr(highs, lows, closes, period=14):
    """ATR with Wilder RMA smoothing. (QBase volatility/atr.py)"""
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)
    out = np.full(n, np.nan)
    out[period] = np.mean(tr[1: period + 1])
    alpha_w = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1.0 - alpha_w) + tr[i] * alpha_w
    return out


# ═══════════════════════════════════════════════════════════════
# Section 3: SIGNAL (QBase v27 McGinley + ForceIndex 信号逻辑)
# ═══════════════════════════════════════════════════════════════

def signal_v27(closes, mcginley_arr, fi_arr, bar_idx):
    """McGinley Dynamic + Force Index 信号.

    逻辑:
      Close > McGinley AND ForceIndex > 0 → +1.0 (强多)
      Close < McGinley AND ForceIndex < 0 → -1.0 (强空)
      Close > McGinley AND ForceIndex <= 0 → +0.3 (弱多)
      Close < McGinley AND ForceIndex >= 0 → -0.3 (弱空)
      else → 0.0

    LONG ONLY: clamp to [0, 1]
    """
    mc_val = mcginley_arr[bar_idx]
    fi_val = fi_arr[bar_idx]
    close = closes[bar_idx]

    if np.isnan(mc_val) or np.isnan(fi_val):
        return 0.0

    above_mc = close > mc_val
    below_mc = close < mc_val
    fi_positive = fi_val > 0
    fi_negative = fi_val < 0

    if above_mc and fi_positive:
        raw = 1.0
    elif below_mc and fi_negative:
        raw = -1.0
    elif above_mc and not fi_positive:
        raw = 0.3
    elif below_mc and not fi_negative:
        raw = -0.3
    else:
        raw = 0.0

    # LONG ONLY: clamp to [0, 1]
    return max(0.0, min(1.0, raw))


# ═══════════════════════════════════════════════════════════════
# Section 4: POSITION SIZING — Vol Targeting + Carver Buffer
# ═══════════════════════════════════════════════════════════════

def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    """Vol-targeted continuous lots (fractional).

    公式: raw = (forecast/10) × (target_vol/realized_vol) × (capital/notional)
    """
    if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or forecast == 0:
        return 0.0
    realized_vol = (atr_val * math.sqrt(ANNUAL_FACTOR)) / price
    if realized_vol <= 0:
        return 0.0
    vol_scalar = TARGET_VOL / realized_vol
    notional = price * MULTIPLIER
    raw = (forecast / 10.0) * vol_scalar * (capital / notional)
    return max(0.0, min(raw, float(max_lots)))


def apply_buffer(optimal, current):
    """Carver 10% buffer: 只在optimal超出buffer区间时交易."""
    buffer = max(abs(optimal) * BUFFER_FRACTION, 0.5)
    if (current - buffer) <= optimal <= (current + buffer):
        return current
    if optimal > current + buffer:
        target = math.floor(optimal - buffer)
    else:
        target = math.ceil(optimal + buffer)
    if abs(target - current) < MIN_TRADE_SIZE:
        return current
    return max(0, target)


# ═══════════════════════════════════════════════════════════════
# Section 5: RISK — 移动止损 + 硬止损 + Chandelier Exit + Portfolio Stops
# ═══════════════════════════════════════════════════════════════

def chandelier_exit_triggered(highs, closes, atr_arr, bar_idx):
    """Chandelier Exit: close < highest_high(period) - mult × ATR."""
    if bar_idx < CHANDELIER_PERIOD:
        return False
    atr_val = atr_arr[bar_idx]
    if np.isnan(atr_val):
        return False
    start = bar_idx - CHANDELIER_PERIOD + 1
    highest = np.max(highs[start:bar_idx + 1])
    return bool(closes[bar_idx] < highest - CHANDELIER_MULT * atr_val)


def check_portfolio_stops(equity, peak_equity, daily_start_equity):
    """Portfolio级别止损检查.

    Returns (action, value).
    action: circuit / reduce / warning / daily_stop / ok
    """
    dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
    daily_pnl = (
        (equity - daily_start_equity) / daily_start_equity
        if daily_start_equity > 0 else 0.0
    )
    if dd <= STOP_CIRCUIT:
        return ("circuit", dd)
    if dd <= STOP_REDUCE:
        return ("reduce", dd)
    if dd <= STOP_WARNING:
        return ("warning", dd)
    if daily_pnl <= STOP_DAILY:
        return ("daily_stop", daily_pnl)  # daily PnL from 21:00 day start (21:00起算)
    return ("ok", dd)


# ═══════════════════════════════════════════════════════════════
# Section 6: OPERATIONS — 状态持久化 + 交易日检测
# ═══════════════════════════════════════════════════════════════

# 交易日从21:00开始（夜盘开盘）
DAY_START_HOUR = 21

def get_trading_day():
    """根据当前时间推算交易日. 21:00起算为下一交易日."""
    now = datetime.now()
    if now.hour >= DAY_START_HOUR:
        td = now + timedelta(days=1)
    else:
        td = now
    wd = td.weekday()
    if wd == 5:
        td += timedelta(days=2)
    elif wd == 6:
        td += timedelta(days=1)
    return td.strftime("%Y%m%d")


def save_state(state_dict, strategy_name=STRATEGY_NAME):
    """原子写JSON: temp → fsync → rename."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{strategy_name}_state.json")
    tmp = path + ".tmp"
    state_dict["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(tmp, "w") as f:
        json.dump(state_dict, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        bak = path + ".bak"
        try:
            os.replace(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)


def load_state(strategy_name=STRATEGY_NAME):
    """读主文件, 失败读备份."""
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{strategy_name}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ═══════════════════════════════════════════════════════════════
# Section 7: PYTHONGO STRATEGY CLASS
# ═══════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="H4", title="K线周期")
    max_position: int = Field(default=10, title="最大持仓手数")
    capital: float = Field(default=1_000_000, title="配置资金")


class State(BaseState):
    forecast: float = Field(default=0.0, title="信号强度")
    target_lots: int = Field(default=0, title="目标手数")
    net_pos: int = Field(default=0, title="当前持仓")
    peak_price: float = Field(default=0.0, title="持仓最高价")
    trading_day: str = Field(default="", title="交易日")
    last_action: str = Field(default="—", title="上次操作")
    mcginley_val: float = Field(default=0.0, title="McGinley值")
    force_index_val: float = Field(default=0.0, title="ForceIndex值")


class I_Long_4H_V27_McGinley_ForceIndex(BaseStrategy):
    """铁矿石4H做多 — McGinley Dynamic + Force Index

    QBase_v2: mild_trend_long_I_4h_v27
    方向: LONG ONLY
    周期: 4小时K线
    指标: McGinley Dynamic(20) + Force Index(13)
    """

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_gen = None

        # Next-bar pending (当前bar信号 → 下一bar执行)
        self._pending = None          # "OPEN"/"ADD"/"REDUCE"/"CLOSE"/"TRAIL_STOP"/"HARD_STOP"/"CIRCUIT"
        self._pending_target = None   # int: 目标手数

        # 持仓追踪
        self.peak_price = 0.0         # 移动止损用: 持仓期间最高价
        self.entry_equity = 0.0       # 开仓时权益 (硬止损参考)

        # 权益追踪
        self.peak_equity = 0.0        # 历史最高权益 (portfolio stops)
        self.daily_start_equity = 0.0 # 当日起始权益 (单日止损)
        self._current_trading_day = ""

        # 委托追踪
        self.order_ids = set()

        # 账户ID (on_start中初始化)
        self._investor_id = ""

    @property
    def main_indicator_data(self):
        return {
            "forecast": self.state_map.forecast,
            "mcginley": self.state_map.mcginley_val,
            "force_index": self.state_map.force_index_val,
        }

    # ── Lifecycle ──────────────────────────────────────────────

    def on_start(self):
        p = self.params_map

        # 账户ID (不能传空字符串给get_account_fund_data, 必须在push_history_data之前)
        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        # 恢复持久化状态 (必须在push_history_data之前, 历史bar回调需要这些值)
        saved = load_state()
        if saved:
            self.peak_equity = saved.get("peak_equity", 0.0)
            self.daily_start_equity = saved.get("daily_start_equity", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self._current_trading_day = saved.get("trading_day", "")
            self.output(
                f"恢复状态: peak_eq={self.peak_equity:.0f} "
                f"peak_px={self.peak_price:.1f}"
            )

        # 初始化权益
        if self._investor_id:
            account = self.get_account_fund_data(self._investor_id)
            if account:
                eq = account.balance
                if self.peak_equity == 0.0:
                    self.peak_equity = eq
                if self.daily_start_equity == 0.0:
                    self.daily_start_equity = eq

        # 查实际持仓 (重启恢复: 信任broker)
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual > 0 and self.peak_price == 0.0:
            self.output(f"重启检测到持仓 {actual}手, peak_price未知")

        # 初始化K线生成器 (必须在investor_id和状态恢复之后)
        self.kline_gen = KLineGenerator(
            callback=self._on_bar_complete,
            real_time_callback=self._on_bar_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        # 加载历史K线 (必须在 super().on_start() 之前)
        self.kline_gen.push_history_data()

        super().on_start()
        self.output(
            f"V27 McGinley+ForceIndex 启动 | {p.instrument_id}@{p.exchange} | "
            f"H4 | max={p.max_position} capital={p.capital:,.0f} | 持仓={actual}"
        )
        feishu(
            "start", p.instrument_id,
            f"**策略启动**: V27 McGinley+ForceIndex\n"
            f"**合约**: {p.instrument_id}\n"
            f"**周期**: H4\n"
            f"**持仓**: {actual}手"
        )

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen.tick_to_kline(tick)

        # 交易日切换检测 (夜盘→日盘)
        td = get_trading_day()
        if td != self._current_trading_day and self._current_trading_day:
            account = (self.get_account_fund_data(self._investor_id)
                       if self._investor_id else None)
            if account:
                self.daily_start_equity = account.balance
            self._current_trading_day = td
            self.state_map.trading_day = td
            self.output(
                f"新交易日: {td}, "
                f"daily_start_equity={self.daily_start_equity:.0f}"
            )

        if not self._current_trading_day:
            self._current_trading_day = td
            self.state_map.trading_day = td

    def on_stop(self):
        self._save_state()
        super().on_stop()
        self.output("策略停止, 状态已保存")

    # ── K线回调 ────────────────────────────────────────────────

    def _on_bar_update(self, kline: KLineData):
        """实时K线更新 → 推送图表."""
        self._push_widget(kline)

    def _on_bar_complete(self, kline: KLineData):
        """4H K线完成 — 主交易逻辑."""
        p = self.params_map
        signal_price = 0.0

        # ── 1. 撤挂单 ──
        for oid in list(self.order_ids):
            self.cancel_order(oid)

        # ── 2. 执行pending (next-bar规则: 上根bar的信号在本bar执行) ──
        if self._pending is not None:
            signal_price = self._execute_pending(kline)

        # ── 3. 数据准备 ──
        producer = self.kline_gen.producer
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)
        bar_idx = len(closes) - 1

        if bar_idx < WARMUP:
            self._push_widget(kline, signal_price)
            return

        # ── 4. 指标计算 ──
        mc_arr = mcginley_dynamic(closes, MCGINLEY_PERIOD)
        fi_arr = force_index(closes, volumes, FORCE_INDEX_PERIOD)
        atr_arr = atr(highs, lows, closes, VOL_ATR_PERIOD)

        # 更新状态显示
        mc_val = mc_arr[bar_idx]
        fi_val = fi_arr[bar_idx]
        self.state_map.mcginley_val = (
            round(mc_val, 2) if not np.isnan(mc_val) else 0.0
        )
        self.state_map.force_index_val = (
            round(fi_val, 0) if not np.isnan(fi_val) else 0.0
        )

        # ── 5. 信号 (LONG ONLY, 自动clamp到[0,1]) ──
        raw = signal_v27(closes, mc_arr, fi_arr, bar_idx)
        forecast = min(FORECAST_CAP, raw * FORECAST_SCALAR)
        forecast = max(0.0, forecast)  # LONG ONLY: 不做空
        self.state_map.forecast = forecast

        # ── 6. 仓位计算 ──
        optimal = calc_optimal_lots(
            forecast, atr_arr[bar_idx], closes[bar_idx],
            p.capital, p.max_position
        )
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        target = apply_buffer(optimal, current)
        self.state_map.net_pos = current

        # ── 7. 止损检查 (优先级: 硬止损 > 移动止损 > Chandelier > Portfolio) ──

        # 硬止损: 浮亏 > 2% 账户权益
        account = (self.get_account_fund_data(self._investor_id)
                   if self._investor_id else None)
        if account and current > 0:
            equity = account.balance
            position_profit = (
                account.position_profit
                if hasattr(account, 'position_profit') else 0
            )
            if (position_profit < 0
                    and abs(position_profit) > equity * HARD_STOP_EQUITY_PCT):
                self._pending = "HARD_STOP"
                self._pending_target = 0
                self.output(
                    f"硬止损! 浮亏{position_profit:.0f} > "
                    f"权益{equity:.0f}×{HARD_STOP_EQUITY_PCT:.0%}"
                )
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # 移动止损: close跌破 peak × (1 - trailing%)
        close = closes[bar_idx]
        if current > 0:
            if close > self.peak_price:
                self.peak_price = close
            if (self.peak_price > 0
                    and close <= self.peak_price * (1 - TRAILING_PCT / 100)):
                self._pending = "TRAIL_STOP"
                self._pending_target = 0
                self.output(
                    f"移动止损! close={close:.1f} < "
                    f"peak={self.peak_price:.1f}×"
                    f"{1 - TRAILING_PCT / 100:.3f}"
                )
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # Chandelier Exit: close < HH(22) - 2.5×ATR
        atr_ch = atr(highs, lows, closes, CHANDELIER_PERIOD)
        if current > 0 and chandelier_exit_triggered(
            highs, closes, atr_ch, bar_idx
        ):
            self._pending = "CLOSE"
            self._pending_target = 0
            self.output("Chandelier Exit 触发")
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # Portfolio Stops: 总权益级别止损
        if account:
            equity = account.balance
            if equity > self.peak_equity:
                self.peak_equity = equity
            stop_action, val = check_portfolio_stops(
                equity, self.peak_equity, self.daily_start_equity
            )
            if stop_action == "circuit":
                self._pending = "CIRCUIT"
                self._pending_target = 0
                self.output(f"熔断! {val:.1%}")
                self._push_widget(kline, signal_price)
                return
            elif stop_action == "reduce":
                target = max(0, target // 2)
                self.output(f"减仓! {val:.1%}")
            elif stop_action == "daily_stop":
                self._pending = "CLOSE"
                self._pending_target = 0
                self.output(f"单日止损! {val:.1%}")
                self._push_widget(kline, signal_price)
                return
            elif stop_action == "warning":
                self.output(f"预警 {val:.1%}")

        # ── 8. 生成pending (下一bar执行) ──
        if target != current:
            if current == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and current > 0:
                self._pending = "CLOSE"
            elif target > current:
                self._pending = "ADD"
            else:
                self._pending = "REDUCE"
            self._pending_target = target

        self.state_map.target_lots = target
        self.state_map.last_action = self._pending or "HOLD"

        self.output(
            f"[BAR] MC={mc_val:.1f} FI={fi_val:.0f} f={forecast:.1f} "
            f"optimal={optimal:.1f} target={target} current={current} "
            f"pending={self._pending or '—'}"
        )
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ── 执行 ───────────────────────────────────────────────────

    def _execute_pending(self, kline: KLineData) -> float:
        """执行pending信号, 返回signal_price供图表标记."""
        action = self._pending
        target = (
            self._pending_target if self._pending_target is not None else 0
        )
        self._pending = None
        self._pending_target = None

        p = self.params_map
        price = kline.close
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        diff = target - current

        if diff == 0:
            return 0.0

        # 保证金检查 (买入时)
        if diff > 0 and self._investor_id:
            account = self.get_account_fund_data(self._investor_id)
            if account:
                needed = price * MULTIPLIER * abs(diff) * 0.15  # 保守估算
                if needed > account.available * 0.6:
                    self.output(
                        f"保证金不足! 需要{needed:.0f} > "
                        f"可用{account.available:.0f}×60%"
                    )
                    feishu(
                        "error", p.instrument_id,
                        f"**保证金不足**\n"
                        f"需要: {needed:,.0f}\n可用: {account.available:,.0f}"
                    )
                    return 0.0

        # 下单
        if diff > 0:
            # 买入: send_order
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if action == "OPEN":
                self.peak_price = price
                account = (self.get_account_fund_data(self._investor_id)
                           if self._investor_id else None)
                if account:
                    self.entry_equity = account.balance
        else:
            # 卖出/平仓: auto_close_position
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if target == 0:
                self.peak_price = 0.0

        # 飞书通知
        action_lower = action.lower() if action else "info"
        feishu(
            action_lower, p.instrument_id,
            f"**操作**: {LABELS.get(action_lower, action)}\n"
            f"**手数**: {abs(diff)}手 ({'买' if diff > 0 else '卖'})\n"
            f"**价格**: {price:,.1f}\n"
            f"**持仓**: {current} → {target}\n"
            f"**信号**: {self.state_map.forecast:.1f}/20"
        )

        self.output(
            f"[执行{action}] {p.instrument_id} "
            f"{'买' if diff > 0 else '卖'}{abs(diff)}手 @ {price:.1f} "
            f"({current}→{target})"
        )
        self.state_map.last_action = action
        self._save_state()
        return price if diff > 0 else -price

    # ── 辅助 ───────────────────────────────────────────────────

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        """推送图表数据到无限易界面."""
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    def _save_state(self):
        """持久化关键状态到JSON文件."""
        save_state({
            "peak_equity": self.peak_equity,
            "daily_start_equity": self.daily_start_equity,
            "peak_price": self.peak_price,
            "trading_day": self._current_trading_day,
            "net_pos": self.state_map.net_pos,
            "forecast": self.state_map.forecast,
        })

    # ── 回调 ───────────────────────────────────────────────────

    def on_trade(self, trade: TradeData, log=True):
        """成交回报: 更新持仓显示."""
        super().on_trade(trade, log=True)
        self.order_ids.discard(trade.order_id)
        pos = self.get_position(self.params_map.instrument_id)
        self.state_map.net_pos = pos.net_position if pos else 0
        self.output(
            f"[成交] {trade.direction} {trade.volume}手 @ {trade.price}"
        )
        self._save_state()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_ids.discard(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu(
            "error", self.params_map.instrument_id,
            f"**异常**: {error}"
        )
