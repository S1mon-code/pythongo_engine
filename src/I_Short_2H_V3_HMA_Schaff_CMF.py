"""I Short 2H V3 — HMA + Schaff Trend Cycle + CMF (铁矿石2H做空)

QBase_v2 策略: mild_trend_short_I_2h_v3
信号逻辑:
  HMA(40) 下降 → 基础信号 -(0.25 + strength×0.25)
    strength = min(1.0, slope×50.0), slope = (hma_prev - hma_cur) / hma_prev
  Schaff(50,30,50) 下降 AND < 75 → signal -= 0.2
  CMF(45) < 0 → signal -= 0.2
  Clamp to [-1, 0]
仓位: Vol Targeting + Carver 10% buffer
止损: 移动止损(空头反向) + 2%权益硬止损 + Chandelier Exit(空头) + Portfolio Stops

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

# 合约参数 (铁矿石 I — DCE)
MULTIPLIER = 100
TICK_SIZE = 0.5

# 策略指标参数
HMA_PERIOD = 40
SCHAFF_PERIOD = 50
SCHAFF_FAST = 30
SCHAFF_SLOW = 50
CMF_PERIOD = 45
WARMUP = 65  # 保证所有指标预热完成

# Vol Targeting
TARGET_VOL = 0.15
VOL_ATR_PERIOD = 14
ANNUAL_FACTOR = 252 * 3  # 2H bars per year (DCE铁矿石 ~6h交易/天 → 3根bar/天)

# Forecast (单策略, 无blending)
FORECAST_SCALAR = 10.0   # raw signal [-1,0] × 10 → forecast [-10,0]
FORECAST_CAP = 20.0

# Carver Buffer
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

# 止损 (空头: 反向逻辑)
TRAILING_PCT = 2.0             # 移动止损: 价格从最低点反弹 2%
HARD_STOP_EQUITY_PCT = 0.02    # 账户权益 2% 硬止损
STOP_WARNING = -0.10           # -10% 预警
STOP_REDUCE = -0.15            # -15% 减仓
STOP_CIRCUIT = -0.20           # -20% 熔断
STOP_DAILY = -0.05             # -5% 单日止损

# Chandelier Exit (空头)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# 换月提醒 (天数)
ROLLOVER_WARN_DAYS = 15
ROLLOVER_URGENT_DAYS = 5

# 状态文件路径
STATE_DIR = "./state"


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


def wma(data, period):
    """Weighted Moving Average — HMA 所需的加权移动平均."""
    n = len(data)
    out = np.full(n, np.nan)
    weights = np.arange(1, period + 1, dtype=np.float64)
    w_sum = weights.sum()
    for i in range(period - 1, n):
        out[i] = np.sum(data[i - period + 1:i + 1] * weights) / w_sum
    return out


def hma(data, period):
    """Hull Moving Average — 更灵敏的趋势指标.

    HMA = WMA( 2×WMA(half) - WMA(full), sqrt(period) )
    """
    half = max(period // 2, 1)
    sqrt_p = max(int(np.sqrt(period)), 1)
    wma_half = wma(data, half)
    wma_full = wma(data, period)
    n = len(data)
    diff = np.full(n, np.nan)
    for i in range(n):
        if not np.isnan(wma_half[i]) and not np.isnan(wma_full[i]):
            diff[i] = 2.0 * wma_half[i] - wma_full[i]
    # Replace NaN with 0 for WMA calc
    diff_clean = np.where(np.isnan(diff), 0.0, diff)
    result = wma(diff_clean, sqrt_p)
    # Set early values to NaN
    for i in range(min(period + sqrt_p - 2, n)):
        result[i] = np.nan
    return result


def schaff_trend_cycle(closes, period=50, fast=30, slow=50):
    """Schaff Trend Cycle: MACD smoothed through double stochastic.

    步骤:
    1. 计算 MACD 线 (EMA fast - EMA slow)
    2. 对 MACD 做第一次 stochastic
    3. EMA(3) 平滑
    4. 对平滑结果做第二次 stochastic
    5. EMA(3) 平滑 → 最终 STC 值
    """
    n = len(closes)
    if n < slow + period:
        return np.full(n, np.nan)

    # Step 1: MACD line
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd = ema_fast - ema_slow

    # Step 2: First stochastic of MACD
    stoch1 = np.full(n, np.nan)
    for i in range(period - 1, n):
        window = macd[i - period + 1:i + 1]
        hi = np.max(window)
        lo = np.min(window)
        if hi == lo:
            stoch1[i] = 50.0
        else:
            stoch1[i] = 100.0 * (macd[i] - lo) / (hi - lo)

    # Step 3: EMA smooth stoch1
    s1_clean = np.where(np.isnan(stoch1), 50.0, stoch1)
    smooth1 = ema(s1_clean, 3)

    # Step 4: Second stochastic
    stoch2 = np.full(n, np.nan)
    for i in range(period - 1, n):
        window = smooth1[max(0, i - period + 1):i + 1]
        hi = np.max(window)
        lo = np.min(window)
        if hi == lo:
            stoch2[i] = 50.0
        else:
            stoch2[i] = 100.0 * (smooth1[i] - lo) / (hi - lo)

    # Step 5: EMA smooth stoch2
    s2_clean = np.where(np.isnan(stoch2), 50.0, stoch2)
    result = ema(s2_clean, 3)

    # Mask early values
    for i in range(min(slow + period, n)):
        result[i] = np.nan

    return result


def cmf(highs, lows, closes, volumes, period=45):
    """Chaikin Money Flow — 资金流量指标.

    MFM = ((close - low) - (high - close)) / (high - low)
    MFV = MFM × volume
    CMF = SUM(MFV, period) / SUM(volume, period)
    """
    n = len(closes)
    out = np.full(n, np.nan)
    hl_range = highs - lows
    mfm = np.where(
        hl_range != 0,
        ((closes - lows) - (highs - closes)) / hl_range,
        0.0,
    )
    mfv = mfm * volumes
    for i in range(period - 1, n):
        vol_sum = np.sum(volumes[i - period + 1:i + 1])
        if vol_sum == 0:
            out[i] = 0.0
        else:
            out[i] = np.sum(mfv[i - period + 1:i + 1]) / vol_sum
    return out


def atr(highs, lows, closes, period=14):
    """ATR with Wilder RMA smoothing. (QBase volatility/atr.py)"""
    n = len(closes)
    if n == 0 or n < period + 1:
        return np.full(n, np.nan)
    tr = np.empty(n)
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
# Section 3: SIGNAL (QBase v3 信号逻辑 — SHORT ONLY)
# ═══════════════════════════════════════════════════════════════

def compute_raw_signal(hma_arr, schaff_arr, cmf_arr, bar_idx):
    """HMA slope + Schaff + CMF → 空头信号 [-1, 0].

    QBase mild_trend_short_I_2h_v3 信号逻辑:
    1. HMA 必须下降 (hma_cur < hma_prev), 否则 signal = 0
    2. 基础信号: -(0.25 + strength × 0.25)
       strength = min(1.0, slope × 50.0)
       slope = (hma_prev - hma_cur) / hma_prev
    3. Schaff 下降 AND < 75: signal -= 0.2
    4. CMF < 0: signal -= 0.2
    5. Clamp to [-1, 0]
    """
    if bar_idx < 1:
        return 0.0

    hma_cur = hma_arr[bar_idx]
    hma_prev = hma_arr[bar_idx - 1]

    if np.isnan(hma_cur) or np.isnan(hma_prev):
        return 0.0

    # 条件1: HMA 必须下降
    if hma_cur >= hma_prev:
        return 0.0

    # 基础信号: 根据HMA下降斜率计算强度
    slope = (hma_prev - hma_cur) / hma_prev if hma_prev != 0 else 0.0
    strength = min(1.0, slope * 50.0)
    signal = -(0.25 + strength * 0.25)

    # Schaff Trend Cycle 加强
    schaff_cur = schaff_arr[bar_idx]
    schaff_prev = schaff_arr[bar_idx - 1] if bar_idx >= 1 else np.nan
    if (
        not np.isnan(schaff_cur)
        and not np.isnan(schaff_prev)
        and schaff_cur < schaff_prev
        and schaff_cur < 75
    ):
        signal -= 0.2

    # CMF 加强
    cmf_val = cmf_arr[bar_idx]
    if not np.isnan(cmf_val) and cmf_val < 0:
        signal -= 0.2

    # Clamp to [-1, 0]
    return max(-1.0, min(0.0, signal))


# ═══════════════════════════════════════════════════════════════
# Section 4: POSITION SIZING — Vol Targeting + Carver Buffer
# ═══════════════════════════════════════════════════════════════

def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    """Vol-targeted 仓位计算 (空头: forecast为负, 取绝对值计算手数).

    返回值: 正数, 代表做空手数.
    """
    if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or forecast == 0:
        return 0.0
    realized_vol = (atr_val * math.sqrt(ANNUAL_FACTOR)) / price
    if realized_vol <= 0:
        return 0.0
    vol_scalar = TARGET_VOL / realized_vol
    notional = price * MULTIPLIER
    # 用 forecast 绝对值计算仓位大小
    abs_forecast = min(FORECAST_CAP, abs(forecast) * FORECAST_SCALAR)
    raw = (abs_forecast / 10.0) * vol_scalar * (capital / notional)
    return max(0.0, min(raw, float(max_lots)))


def apply_buffer(optimal, current):
    """Carver 10% buffer: 仅当 optimal 超出 buffer 区间才交易.

    optimal / current: 均为正数 (空头手数的绝对值).
    """
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
# Section 5: RISK — 移动止损(空头) + 硬止损 + Chandelier(空头) + Portfolio Stops
# ═══════════════════════════════════════════════════════════════

def chandelier_exit_short_triggered(lows, closes, atr_arr, bar_idx):
    """Chandelier Exit (空头): close > lowest_low(period) + mult x ATR.

    空头止损: 价格上穿最低价 + ATR 通道时触发.
    """
    if bar_idx < CHANDELIER_PERIOD:
        return False
    atr_val = atr_arr[bar_idx]
    if np.isnan(atr_val):
        return False
    start = bar_idx - CHANDELIER_PERIOD + 1
    lowest = np.min(lows[start:bar_idx + 1])
    return bool(closes[bar_idx] > lowest + CHANDELIER_MULT * atr_val)


def check_portfolio_stops(equity, peak_equity, daily_start_equity):
    """Portfolio 级别止损检查. Returns (action, value)."""
    dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
    daily_pnl = (
        (equity - daily_start_equity) / daily_start_equity
        if daily_start_equity > 0
        else 0.0
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

STRATEGY_NAME = "I_Short_2H_V3_HMA_Schaff_CMF"


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


def save_state(state_dict):
    """原子写JSON: temp -> fsync -> rename."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{STRATEGY_NAME}_state.json")
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


def load_state():
    """读主文件, 失败读备份."""
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{STRATEGY_NAME}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None


# ═══════════════════════════════════════════════════════════════
# Section 8: PYTHONGO STRATEGY CLASS
# ═══════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2509", title="合约代码")
    kline_style: str = Field(default="H2", title="K线周期")
    max_position: int = Field(default=10, title="最大持仓手数")
    capital: float = Field(default=1_000_000, title="配置资金")


class State(BaseState):
    forecast: float = Field(default=0.0, title="信号强度")
    target_lots: int = Field(default=0, title="目标手数")
    net_pos: int = Field(default=0, title="当前持仓(负=空)")
    trough_price: float = Field(default=0.0, title="持仓最低价")
    trading_day: str = Field(default="", title="交易日")
    last_action: str = Field(default="—", title="上次操作")


class I_Short_2H_V3_HMA_Schaff_CMF(BaseStrategy):
    """铁矿石2H做空 — HMA + Schaff Trend Cycle + CMF

    QBase_v2: mild_trend_short_I_2h_v3
    方向: SHORT ONLY
    指标: HMA(40) slope + Schaff(50,30,50) + CMF(45)
    """

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_gen = None

        # Next-bar pending (信号当前bar产生, 下一bar执行)
        self._pending = None          # "OPEN"/"ADD"/"REDUCE"/"CLOSE"/"TRAIL_STOP"/"HARD_STOP"/"CIRCUIT"
        self._pending_target = None   # int (空头手数, 正数)

        # 空头持仓追踪: 记录最低价 (trough)
        self.trough_price = 0.0       # 持仓期间的最低价
        self.entry_equity = 0.0       # 开仓时权益 (硬止损参考)

        # 权益追踪
        self.peak_equity = 0.0
        self.daily_start_equity = 0.0
        self._current_trading_day = ""

        # 委托追踪
        self.order_ids = set()

        # 账户ID (on_start中初始化)
        self._investor_id = ""

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

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
            self.trough_price = saved.get("trough_price", 0.0)
            self._current_trading_day = saved.get("trading_day", "")
            self.output(
                f"恢复状态: peak_eq={self.peak_equity:.0f} "
                f"trough_px={self.trough_price:.1f}"
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
        # 空头: net_position 为负数
        pos = self.get_position(p.instrument_id)
        actual = pos.net_position if pos else 0
        self.state_map.net_pos = actual
        if actual < 0 and self.trough_price == 0.0:
            self.output(f"重启检测到空头持仓 {actual}手, trough_price未知")

        # KLineGenerator 初始化 + 历史数据加载 (必须在investor_id和状态恢复之后)
        self.kline_gen = KLineGenerator(
            callback=self._on_bar_complete,
            real_time_callback=self._on_bar_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_gen.push_history_data()

        super().on_start()
        self.output(
            f"{STRATEGY_NAME} 启动 | {p.instrument_id}@{p.exchange} | "
            f"H2 SHORT | max={p.max_position} capital={p.capital:,.0f} | "
            f"持仓={actual}"
        )
        feishu(
            "start", p.instrument_id,
            f"**策略启动**: {STRATEGY_NAME}\n"
            f"**方向**: 做空\n**持仓**: {actual}手"
        )

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen.tick_to_kline(tick)

        # 交易日切换检测
        td = get_trading_day()
        if td != self._current_trading_day and self._current_trading_day:
            account = (self.get_account_fund_data(self._investor_id)
                       if self._investor_id else None)
            if account:
                self.daily_start_equity = account.balance
            self._current_trading_day = td
            self.state_map.trading_day = td
            self.output(
                f"新交易日: {td}, daily_start_equity={self.daily_start_equity:.0f}"
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
        """实时K线更新 -> 推送图表."""
        self._push_widget(kline)

    def _on_bar_complete(self, kline: KLineData):
        """2H K线完成 — 主交易逻辑 (SHORT ONLY)."""
        p = self.params_map
        signal_price = 0.0

        # ── 1. 撤挂单 ──
        for oid in list(self.order_ids):
            self.cancel_order(oid)

        # ── 2. 执行pending (next-bar规则) ──
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
        hma_arr = hma(closes, HMA_PERIOD)
        schaff_arr = schaff_trend_cycle(
            closes, SCHAFF_PERIOD, SCHAFF_FAST, SCHAFF_SLOW
        )
        cmf_arr = cmf(highs, lows, closes, volumes, CMF_PERIOD)
        atr_arr = atr(highs, lows, closes, VOL_ATR_PERIOD)

        # ── 5. 信号计算 (SHORT ONLY) ──
        raw_signal = compute_raw_signal(
            hma_arr, schaff_arr, cmf_arr, bar_idx
        )
        # forecast: 负值代表做空强度, 范围 [-FORECAST_CAP, 0]
        forecast = max(-FORECAST_CAP, raw_signal * FORECAST_SCALAR)
        forecast = min(0.0, forecast)  # SHORT ONLY: 确保 <= 0
        self.state_map.forecast = forecast

        # ── 6. 仓位计算 ──
        optimal = calc_optimal_lots(
            forecast, atr_arr[bar_idx], closes[bar_idx],
            p.capital, p.max_position,
        )
        # 获取当前空头手数 (绝对值)
        pos = self.get_position(p.instrument_id)
        current_net = pos.net_position if pos else 0  # 负数=空头
        current_abs = abs(current_net)                # 空头手数绝对值
        target = apply_buffer(optimal, current_abs)
        self.state_map.net_pos = current_net

        # ── 7. 止损检查 (空头反向逻辑) ──

        # 硬止损: 浮亏 > 2% 账户权益
        account = (self.get_account_fund_data(self._investor_id)
                   if self._investor_id else None)
        if account and current_abs > 0:
            equity = account.balance
            position_profit = (
                account.position_profit
                if hasattr(account, "position_profit")
                else 0
            )
            if (
                position_profit < 0
                and abs(position_profit) > equity * HARD_STOP_EQUITY_PCT
            ):
                self._pending = "HARD_STOP"
                self._pending_target = 0
                self.output(
                    f"硬止损! 浮亏{position_profit:.0f} > "
                    f"权益{equity:.0f}x{HARD_STOP_EQUITY_PCT:.0%}"
                )
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # 移动止损 (空头: 追踪最低价, 价格反弹超过阈值时止损)
        close = closes[bar_idx]
        if current_abs > 0:
            # 更新trough (最低价)
            if close < self.trough_price or self.trough_price == 0:
                self.trough_price = close
            # 空头止损: 价格从最低点反弹超过 TRAILING_PCT%
            if (
                self.trough_price > 0
                and close >= self.trough_price * (1 + TRAILING_PCT / 100)
            ):
                self._pending = "TRAIL_STOP"
                self._pending_target = 0
                self.output(
                    f"移动止损(空)! close={close:.1f} >= "
                    f"trough={self.trough_price:.1f}x"
                    f"{1 + TRAILING_PCT / 100:.3f}"
                )
                self._push_widget(kline, signal_price)
                self.update_status_bar()
                return

        # Chandelier Exit (空头: close > lowest_low + mult x ATR)
        atr_ch = atr(highs, lows, closes, CHANDELIER_PERIOD)
        if current_abs > 0 and chandelier_exit_short_triggered(
            lows, closes, atr_ch, bar_idx
        ):
            self._pending = "CLOSE"
            self._pending_target = 0
            self.output("Chandelier Exit (空头) 触发")
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # Portfolio Stops
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

        # ── 8. 生成pending (空头交易) ──
        if target != current_abs:
            if current_abs == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and current_abs > 0:
                self._pending = "CLOSE"
            elif target > current_abs:
                self._pending = "ADD"
            else:
                self._pending = "REDUCE"
            self._pending_target = target

        self.state_map.target_lots = target
        self.state_map.last_action = self._pending or "HOLD"

        # 日志: HMA slope + Schaff + CMF
        hma_slope = 0.0
        if (
            bar_idx > 0
            and not np.isnan(hma_arr[bar_idx])
            and not np.isnan(hma_arr[bar_idx - 1])
        ):
            hma_slope = hma_arr[bar_idx] - hma_arr[bar_idx - 1]
        schaff_val = (
            schaff_arr[bar_idx]
            if not np.isnan(schaff_arr[bar_idx])
            else 0.0
        )
        cmf_val = (
            cmf_arr[bar_idx]
            if not np.isnan(cmf_arr[bar_idx])
            else 0.0
        )
        self.output(
            f"[BAR] HMA_slope={hma_slope:.2f} Schaff={schaff_val:.1f} "
            f"CMF={cmf_val:.4f} f={forecast:.1f} optimal={optimal:.1f} "
            f"target={target} current={current_abs}(空) "
            f"pending={self._pending or '—'}"
        )
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ── 执行 ───────────────────────────────────────────────────

    def _execute_pending(self, kline: KLineData) -> float:
        """执行pending信号 (空头操作), 返回signal_price供图表标记."""
        action = self._pending
        target = self._pending_target if self._pending_target is not None else 0
        self._pending = None
        self._pending_target = None

        p = self.params_map
        price = kline.close
        pos = self.get_position(p.instrument_id)
        current_net = pos.net_position if pos else 0  # 负数=空头
        current_abs = abs(current_net)
        diff = target - current_abs  # 正=加空, 负=减空

        if diff == 0:
            return 0.0

        # 保证金检查 (加空时)
        if diff > 0 and self._investor_id:
            account = self.get_account_fund_data(self._investor_id)
            if account:
                needed = price * MULTIPLIER * abs(diff) * 0.15  # 保守估算
                if needed > account.available * 0.6:
                    self.output(
                        f"保证金不足! 需要{needed:.0f} > "
                        f"可用{account.available:.0f}x60%"
                    )
                    feishu(
                        "error", p.instrument_id,
                        f"**保证金不足**\n需要: {needed:,.0f}\n"
                        f"可用: {account.available:,.0f}"
                    )
                    return 0.0

        # 下单 (空头操作)
        if diff > 0:
            # 开空 / 加空: sell to open
            oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="sell", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if action == "OPEN":
                self.trough_price = price  # 初始化最低价追踪
                account = (self.get_account_fund_data(self._investor_id)
                           if self._investor_id else None)
                if account:
                    self.entry_equity = account.balance
        else:
            # 平空 / 减空: buy to close
            oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=abs(diff), price=price,
                order_direction="buy", market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
            if target == 0:
                self.trough_price = 0.0  # 全平: 重置追踪

        # 飞书通知
        action_lower = action.lower() if action else "info"
        feishu(
            action_lower, p.instrument_id,
            f"**操作**: {LABELS.get(action_lower, action)}\n"
            f"**手数**: {abs(diff)}手 ({'卖开' if diff > 0 else '买平'})\n"
            f"**价格**: {price:,.1f}\n"
            f"**持仓**: {current_abs}空 -> {target}空\n"
            f"**信号**: {self.state_map.forecast:.1f}/20"
        )

        self.output(
            f"[执行{action}] {p.instrument_id} "
            f"{'卖开' if diff > 0 else '买平'}{abs(diff)}手 @ {price:.1f} "
            f"({current_abs}空->{target}空)"
        )
        self.state_map.last_action = action
        self._save_state()
        # 空头: 卖开返回负价格标记, 买平返回正价格标记
        return -price if diff > 0 else price

    # ── 辅助 ───────────────────────────────────────────────────

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        """推送图表更新."""
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    def _save_state(self):
        """持久化策略状态."""
        save_state({
            "peak_equity": self.peak_equity,
            "daily_start_equity": self.daily_start_equity,
            "trough_price": self.trough_price,
            "trading_day": self._current_trading_day,
            "net_pos": self.state_map.net_pos,
            "forecast": self.state_map.forecast,
        })

    # ── 回调 ───────────────────────────────────────────────────

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_ids.discard(trade.order_id)
        # 更新持仓显示
        pos = self.get_position(self.params_map.instrument_id)
        self.state_map.net_pos = pos.net_position if pos else 0
        self.output(f"[成交] {trade.direction} {trade.volume}手 @ {trade.price}")
        self._save_state()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_ids.discard(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
