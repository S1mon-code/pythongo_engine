"""QBase_v2 Iron Ore Long Portfolio — PythonGO V2 实盘策略

铁矿石1H多头组合: v6(RSI+EMA) + v7(BB+OBV) + v8(ATR+CMF) + v9(MACD+Vol)
Carver Signal Blending → Vol Targeting → Chandelier Exit → 飞书通知

部署: 复制此文件到 Windows 无限易 pyStrategy/self_strategy/ 目录
"""
import math
import time
import hashlib
import base64
import hmac

import numpy as np
import requests

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator


# ═══════════════════════════════════════════════════════════════
# Section 1: CONFIG
# ═══════════════════════════════════════════════════════════════

# --- Instrument Parameters (Iron Ore) ---
MULTIPLIER = 100               # 合约乘数
TICK_SIZE = 0.5                # 最小变动价位
MARGIN_RATIO = 0.12            # 保证金率

# --- Strategy Weights (equal weight across v6/v7/v8/v9) ---
STRATEGY_WEIGHTS = {
    "v6": 0.25,
    "v7": 0.25,
    "v8": 0.25,
    "v9": 0.25,
}

# --- Signal Blending (Carver-style forecast combination) ---
FORECAST_CAP = 20.0
FORECAST_TARGET_ABS = 10.0
FDM = 1.35                     # Forecast Diversification Multiplier

# --- Vol Targeting ---
TARGET_VOL = 0.15              # 目标年化波动率 15%
VOL_ATR_PERIOD = 20
ANNUAL_FACTOR = 252 * 4        # H1 bars per year

# --- Chandelier Exit ---
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.5

# --- Portfolio Stops ---
STOP_WARNING = -0.10           # -10% 预警
STOP_REDUCE = -0.15            # -15% 减仓
STOP_CIRCUIT = -0.20           # -20% 熔断
STOP_DAILY = -0.05             # -5% 单日止损

# --- Feishu Notification ---
FEISHU_WEBHOOK_URL = ""        # 填入你的 webhook URL
FEISHU_SECRET = ""             # 填入你的 secret
FEISHU_ENABLED = False         # 上线前改为 True

# --- Per-Strategy Indicator Parameters (QBase optimized) ---

# v6: RSI Momentum + EMA Trend Filter
V6_RSI_PERIOD = 20
V6_EMA_PERIOD = 40

# v7: Bollinger Breakout + OBV Confirmation
V7_BB_PERIOD = 30
V7_BB_STD = 2.0
V7_OBV_EMA_PERIOD = 20

# v8: ATR Channel Breakout + CMF
V8_EMA_PERIOD = 30
V8_ATR_PERIOD = 20
V8_ATR_MULT = 1.5
V8_CMF_PERIOD = 25

# v9: MACD Histogram + Volume Surge
V9_FAST_PERIOD = 20
V9_SLOW_PERIOD = 50
V9_SIGNAL_PERIOD = 12
V9_VOL_PERIOD = 30

# --- Warmup Bars ---
WARMUP = 70


# ═══════════════════════════════════════════════════════════════
# Section 2: INDICATORS (从QBase_v2原样移植, 纯numpy)
# ═══════════════════════════════════════════════════════════════

def _ema(arr, period):
    """EMA with SMA seed. Returns NaN before period-1. (QBase _utils.py)"""
    result = np.full(arr.size, np.nan)
    if arr.size < period:
        return result
    result[period - 1] = arr[:period].mean()
    k = 2.0 / (period + 1.0)
    for i in range(period, arr.size):
        result[i] = arr[i] * k + result[i - 1] * (1.0 - k)
    return result


def ema(data, period):
    """EMA seeded with data[0], no NaN output. (QBase trend/ema.py)"""
    n = len(data)
    if n == 0:
        return np.array([], dtype=np.float64)
    alpha = 2.0 / (period + 1)
    out = np.empty(n, dtype=np.float64)
    out[0] = data[0]
    for i in range(1, n):
        out[i] = alpha * data[i] + (1.0 - alpha) * out[i - 1]
    return out


def sma(data, period):
    """Simple Moving Average."""
    n = len(data)
    result = np.full(n, np.nan)
    if n < period:
        return result
    cumsum = np.cumsum(data)
    result[period - 1:] = (
        cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])
    ) / period
    return result


def rsi(closes, period=14):
    """RSI with Wilder smoothing. (QBase momentum/rsi.py)"""
    if closes.size == 0:
        return np.array([], dtype=float)
    if closes.size <= period:
        return np.full(closes.size, np.nan)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    result = np.full(closes.size, np.nan)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    if avg_loss == 0:
        result[period] = 100.0
    else:
        result[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    alpha = 1.0 / period
    for i in range(period, len(deltas)):
        avg_gain = avg_gain * (1.0 - alpha) + gains[i] * alpha
        avg_loss = avg_loss * (1.0 - alpha) + losses[i] * alpha
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            result[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def bollinger_bands(closes, period=20, num_std=2.0):
    """Bollinger Bands with ddof=1 sample std. (QBase)"""
    n = len(closes)
    upper = np.full(n, np.nan)
    middle = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if n < period:
        return upper, middle, lower
    for i in range(period - 1, n):
        window = closes[i - period + 1: i + 1]
        sma_val = np.mean(window)
        std = np.std(window, ddof=1)
        middle[i] = sma_val
        upper[i] = sma_val + num_std * std
        lower[i] = sma_val - num_std * std
    return upper, middle, lower


def atr(highs, lows, closes, period=14):
    """ATR with Wilder RMA smoothing. (QBase)"""
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
    alpha = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1.0 - alpha) + tr[i] * alpha
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """MACD using _ema (SMA-seeded). (QBase)"""
    n = closes.size
    empty = np.array([], dtype=float)
    if n == 0:
        return empty, empty, empty
    nans = np.full(n, np.nan)
    if n < slow:
        return nans.copy(), nans.copy(), nans.copy()
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    macd_line = fast_ema - slow_ema
    valid_start = slow - 1
    macd_valid = macd_line[valid_start:]
    sig_ema = _ema(macd_valid, signal)
    signal_line = np.full(n, np.nan)
    signal_line[valid_start:] = sig_ema
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def obv(closes, volumes):
    """On-Balance Volume. (QBase)"""
    if closes.size == 0:
        return np.array([], dtype=np.float64)
    closes = closes.astype(np.float64)
    volumes = volumes.astype(np.float64)
    direction = np.zeros(len(closes), dtype=np.float64)
    direction[1:] = np.sign(np.diff(closes))
    directed_volume = direction * volumes
    directed_volume[0] = volumes[0]
    return np.cumsum(directed_volume)


def cmf(highs, lows, closes, volumes, period=20):
    """Chaikin Money Flow. (QBase)"""
    n = len(closes)
    if n == 0:
        return np.array([], dtype=np.float64)
    highs = highs.astype(np.float64)
    lows = lows.astype(np.float64)
    closes = closes.astype(np.float64)
    volumes = volumes.astype(np.float64)
    hl_range = highs - lows
    hl_safe = np.where(hl_range != 0.0, hl_range, 1.0)
    clv = np.where(
        hl_range != 0.0,
        ((closes - lows) - (highs - closes)) / hl_safe,
        0.0,
    )
    mf_volume = clv * volumes
    result = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return result
    mfv_sum = np.sum(mf_volume[:period])
    vol_sum = np.sum(volumes[:period])
    result[period - 1] = mfv_sum / vol_sum if vol_sum != 0.0 else 0.0
    for i in range(period, n):
        mfv_sum += mf_volume[i] - mf_volume[i - period]
        vol_sum += volumes[i] - volumes[i - period]
        result[i] = mfv_sum / vol_sum if vol_sum != 0.0 else 0.0
    return result


# ═══════════════════════════════════════════════════════════════
# Section 3: SIGNAL STRATEGIES (QBase v6/v7/v8/v9 信号逻辑)
# ═══════════════════════════════════════════════════════════════

def signal_v6(closes, rsi_arr, ema_arr, bar_idx):
    """v6: RSI Momentum + EMA Trend Filter."""
    close = closes[bar_idx]
    rsi_val = rsi_arr[bar_idx]
    ema_val = ema_arr[bar_idx]
    if np.isnan(rsi_val) or np.isnan(ema_val):
        return 0.0
    if close > ema_val and rsi_val > 55:
        return min(1.0, (rsi_val - 50) / 30)
    if close > ema_val and rsi_val > 40:
        return 0.3
    return 0.0


def signal_v7(closes, bb_upper, bb_middle, obv_arr, obv_ema_arr, bar_idx):
    """v7: Bollinger Breakout + OBV Confirmation."""
    close = closes[bar_idx]
    if np.isnan(bb_upper[bar_idx]) or np.isnan(obv_ema_arr[bar_idx]):
        return 0.0
    if close > bb_upper[bar_idx] and obv_arr[bar_idx] > obv_ema_arr[bar_idx]:
        return 0.9
    if close > bb_middle[bar_idx] and obv_arr[bar_idx] > obv_ema_arr[bar_idx]:
        return 0.5
    return 0.0


def signal_v8(closes, ema_arr, atr_arr, cmf_arr, atr_mult, bar_idx):
    """v8: ATR Channel Breakout + CMF."""
    close = closes[bar_idx]
    ema_val = ema_arr[bar_idx]
    atr_val = atr_arr[bar_idx]
    cmf_val = cmf_arr[bar_idx]
    if np.isnan(ema_val) or np.isnan(atr_val) or np.isnan(cmf_val):
        return 0.0
    breakout = ema_val + atr_mult * atr_val
    if close > breakout and cmf_val > 0:
        return min(1.0, 0.6 + cmf_val)
    if close > ema_val and cmf_val > 0.1:
        return 0.4
    return 0.0


def signal_v9(closes, volumes, macd_hist, macd_line_arr, vol_mean, bar_idx):
    """v9: MACD Histogram + Volume Surge."""
    if bar_idx < 1:
        return 0.0
    hist = macd_hist[bar_idx]
    prev_hist = macd_hist[bar_idx - 1]
    ml = macd_line_arr[bar_idx]
    vol = volumes[bar_idx]
    vm = vol_mean[bar_idx]
    if np.isnan(hist) or np.isnan(prev_hist) or np.isnan(vm):
        return 0.0
    vol_ratio = vol / vm if vm > 0 else 0.0
    if hist > 0 and hist > prev_hist and vol_ratio > 1.2:
        return min(1.0, 0.4 + vol_ratio / 3.0)
    if hist > 0 and ml > 0:
        return 0.3
    return 0.0


# ═══════════════════════════════════════════════════════════════
# Section 4: SIGNAL BLENDER — Carver / Man AHL Standard
# ═══════════════════════════════════════════════════════════════

def blend_forecasts(raw_signals, weights, fdm,
                    forecast_cap=FORECAST_CAP,
                    forecast_target_abs=FORECAST_TARGET_ABS):
    """Carver Signal Blending: Scale → Cap → Weighted Combine → FDM → Re-Cap → Direction Filter."""
    active_names = []
    for name, sig in raw_signals.items():
        if name not in weights:
            continue
        if sig is None or (isinstance(sig, float) and np.isnan(sig)):
            continue
        active_names.append(name)

    if not active_names:
        return 0.0

    total_weight = sum(weights[n] for n in active_names)
    if total_weight <= 0.0:
        return 0.0

    weighted_sum = 0.0
    for name in active_names:
        scaled = raw_signals[name] * forecast_target_abs
        capped = float(np.clip(scaled, -forecast_cap, forecast_cap))
        norm_w = weights[name] / total_weight
        weighted_sum += norm_w * capped

    combined = weighted_sum * fdm
    combined = float(np.clip(combined, -forecast_cap, forecast_cap))
    combined = max(0.0, combined)  # LONG_ONLY
    return combined


# ═══════════════════════════════════════════════════════════════
# Section 5: POSITION SIZING — Vol Targeting
# ═══════════════════════════════════════════════════════════════

def calc_target_lots(forecast, atr_val, price, capital, max_lots,
                     target_vol=TARGET_VOL,
                     instrument_multiplier=MULTIPLIER,
                     annual_factor=ANNUAL_FACTOR):
    """Vol-targeted position sizing: (f/10) × (target_vol/realized_vol) × (capital/notional)."""
    if price is None or atr_val is None or forecast is None:
        return 0
    if np.isnan(price) or np.isnan(atr_val) or np.isnan(forecast):
        return 0
    if price <= 0.0 or atr_val <= 0.0 or forecast == 0.0:
        return 0

    realized_vol = (atr_val * math.sqrt(annual_factor)) / price
    if realized_vol <= 0.0:
        return 0

    vol_scalar = target_vol / realized_vol
    notional = price * instrument_multiplier
    if notional <= 0.0:
        return 0

    raw = (forecast / 10.0) * vol_scalar * (capital / notional)
    target = int(round(raw))
    return max(0, min(target, max_lots))


# ═══════════════════════════════════════════════════════════════
# Section 6: RISK MANAGER — Chandelier Exit + Portfolio Stops
# ═══════════════════════════════════════════════════════════════

def chandelier_exit_triggered(highs, lows, closes, atr_arr, bar_idx,
                               chandelier_period=CHANDELIER_PERIOD,
                               chandelier_mult=CHANDELIER_MULT):
    """Chandelier Exit: close < highest_high(period) - mult × ATR → 平仓."""
    if bar_idx < chandelier_period:
        return False
    atr_val = atr_arr[bar_idx]
    if np.isnan(atr_val):
        return False
    start = bar_idx - chandelier_period + 1
    highest_high = np.max(highs[start:bar_idx + 1])
    stop_level = highest_high - chandelier_mult * atr_val
    return bool(closes[bar_idx] < stop_level)


def check_portfolio_stops(equity, peak_equity, daily_start_equity,
                          stop_warning=STOP_WARNING,
                          stop_reduce=STOP_REDUCE,
                          stop_circuit=STOP_CIRCUIT,
                          stop_daily=STOP_DAILY):
    """Portfolio-level risk checks. Returns (action, value)."""
    drawdown = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
    daily_pnl = (equity - daily_start_equity) / daily_start_equity if daily_start_equity > 0 else 0.0

    if drawdown <= stop_circuit:
        return ("circuit", drawdown)
    if drawdown <= stop_reduce:
        return ("reduce", drawdown)
    if drawdown <= stop_warning:
        return ("warning", drawdown)
    if daily_pnl <= stop_daily:
        return ("daily_stop", daily_pnl)
    return ("ok", drawdown)


# ═══════════════════════════════════════════════════════════════
# Section 7: FEISHU NOTIFIER — 飞书交易提醒
# ═══════════════════════════════════════════════════════════════

_FEISHU_COLORS = {
    "open": "green", "add": "blue", "reduce": "orange",
    "close": "red", "error": "carmine", "daily_summary": "purple",
}
_FEISHU_LABELS = {
    "open": "开仓", "add": "加仓", "reduce": "减仓",
    "close": "平仓", "error": "异常", "daily_summary": "每日汇总",
}


def _feishu_sign(secret):
    """Generate timestamp + HMAC-SHA256 signature."""
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def _feishu_post(card):
    """Send card payload to Feishu webhook."""
    if FEISHU_SECRET:
        ts, sign = _feishu_sign(FEISHU_SECRET)
        card["timestamp"] = ts
        card["sign"] = sign
    try:
        requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=5)
    except Exception:
        pass  # 通知失败不影响交易


def feishu_send(action, symbol, price, lots, forecast, signals_detail, extra=""):
    """发送交易信号卡片到飞书."""
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK_URL:
        return
    color = _FEISHU_COLORS.get(action, "grey")
    label = _FEISHU_LABELS.get(action, action)

    lines = [
        f"**品种**: {symbol}",
        f"**操作**: {label}",
        f"**价格**: {price:,.1f}" if isinstance(price, (int, float)) and price > 0 else "",
        f"**手数**: {lots}" if lots > 0 else "",
        f"**综合信号**: {forecast:.1f}/20" if isinstance(forecast, (int, float)) else "",
        "",
        f"**信号明细**:",
        signals_detail,
    ]
    if extra:
        lines.append(f"\n{extra}")
    lines.append(f"\n---\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*")

    content = "\n".join(line for line in lines if line is not None)
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"铁矿石策略 | {label} | {symbol}"},
                "template": color,
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }
    _feishu_post(card)


def feishu_send_daily(symbol, equity, pnl_pct, peak_dd_pct, positions):
    """发送每日汇总卡片."""
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK_URL:
        return
    color = "green" if pnl_pct >= 0 else "red"
    content = "\n".join([
        f"**品种**: {symbol}",
        f"**当日盈亏**: {pnl_pct:+.2%}",
        f"**持仓**: {positions} 手",
        f"**累计回撤**: {peak_dd_pct:.2%}",
        f"**权益**: {equity:,.0f}",
        f"\n---\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*",
    ])
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"每日汇总 | {symbol}"},
                "template": color,
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }
    _feishu_post(card)


# ═══════════════════════════════════════════════════════════════
# Section 8: PYTHONGO MAIN STRATEGY
# ═══════════════════════════════════════════════════════════════

class Params(BaseParams):
    """策略参数 — 在无限易UI中可调."""
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2409", title="合约代码")
    kline_style: str = Field(default="H1", title="K线周期")
    max_position: int = Field(default=10, title="最大持仓手数")
    capital: float = Field(default=1_000_000, title="配置资金")


class State(BaseState):
    """持久状态 — 策略重启后恢复."""
    last_forecast: float = Field(default=0.0, title="最新信号")
    last_target: int = Field(default=0, title="目标手数")
    peak_equity: float = Field(default=0.0, title="权益峰值")
    daily_start_equity: float = Field(default=0.0, title="当日起始权益")


class PortfolioIronLong(BaseStrategy):
    """铁矿石1H多头组合策略 — QBase_v2 Portfolio

    v6(RSI+EMA) + v7(BB+OBV) + v8(ATR+CMF) + v9(MACD+Vol)
    Carver Signal Blending → Vol Targeting → Chandelier Exit

    遵循 next-bar 规则: 信号在当前bar产生, 下一bar开头执行
    """

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_gen = None

        # next-bar 规则: 待执行的目标手数 (当前bar计算, 下一bar执行)
        self._pending_target = None   # int or None
        self._pending_signals = None  # dict for feishu detail
        self._pending_forecast = 0.0

        # 委托 ID 追踪
        self.order_ids = set()

    # ── 主图指标 (显示在K线图上) ────────────────────────────────

    @property
    def main_indicator_data(self):
        return {
            "forecast": self.state_map.last_forecast,
        }

    # ── Lifecycle ──────────────────────────────────────────────

    def on_start(self):
        p = self.params_map

        # KLineGenerator 必须在 super().on_start() 之前初始化
        self.kline_gen = KLineGenerator(
            callback=self._on_bar_complete,
            real_time_callback=self._on_bar_update,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        # push_history_data 在 super().on_start() 之前, 避免历史回放阶段下单
        self.kline_gen.push_history_data()

        # 初始化权益追踪
        account = self.get_account_fund_data("")
        if account:
            eq = account.balance
            if self.state_map.peak_equity == 0.0:
                self.state_map.peak_equity = eq
            if self.state_map.daily_start_equity == 0.0:
                self.state_map.daily_start_equity = eq

        super().on_start()
        self.output(
            f"策略启动: 铁矿石1H多头组合 | "
            f"{p.instrument_id}@{p.exchange} | K线={p.kline_style} | "
            f"max_pos={p.max_position} capital={p.capital:,.0f}"
        )

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_gen.tick_to_kline(tick)

    def on_stop(self):
        super().on_stop()
        self.output("策略停止")

    # ── K线回调 ────────────────────────────────────────────────

    def _on_bar_update(self, kline: KLineData):
        """实时tick更新 — 仅更新图表."""
        self._push_widget(kline)

    def _on_bar_complete(self, kline: KLineData):
        """H1 K线完成 — 主交易逻辑.

        执行顺序:
          1. 撤销所有未成交挂单
          2. 执行上一bar产生的pending信号 (next-bar规则)
          3. 计算指标 + 生成信号 + 合并 + sizing + 风控
          4. 存入pending, 等下一bar执行
        """
        p = self.params_map

        # ── 1. 撤销所有未成交挂单 ──
        for oid in list(self.order_ids):
            self.cancel_order(oid)

        # ── 2. 执行上一bar的pending信号 (next-bar规则) ──
        if self._pending_target is not None:
            self._execute(
                self._pending_target, kline.close,
                self._pending_signals, self._pending_forecast,
            )
            self._pending_target = None
            self._pending_signals = None

        # ── 3. 计算指标和信号 ──
        producer = self.kline_gen.producer
        closes = np.array(producer.close, dtype=np.float64)
        highs = np.array(producer.high, dtype=np.float64)
        lows = np.array(producer.low, dtype=np.float64)
        volumes = np.array(producer.volume, dtype=np.float64)

        bar_idx = len(closes) - 1
        if bar_idx < WARMUP:
            self._push_widget(kline)
            return

        # 计算指标
        rsi_arr = rsi(closes, V6_RSI_PERIOD)
        ema_v6 = ema(closes, V6_EMA_PERIOD)

        bb_upper, bb_middle, _ = bollinger_bands(closes, V7_BB_PERIOD, V7_BB_STD)
        obv_arr = obv(closes, volumes)
        obv_ema_arr = ema(obv_arr, V7_OBV_EMA_PERIOD)

        ema_v8 = ema(closes, V8_EMA_PERIOD)
        atr_v8 = atr(highs, lows, closes, V8_ATR_PERIOD)
        cmf_arr = cmf(highs, lows, closes, volumes, V8_CMF_PERIOD)

        macd_line_arr, _, macd_hist = macd(
            closes, V9_FAST_PERIOD, V9_SLOW_PERIOD, V9_SIGNAL_PERIOD,
        )
        vol_mean = sma(volumes, V9_VOL_PERIOD)
        atr_vol = atr(highs, lows, closes, VOL_ATR_PERIOD)

        # 生成信号
        s6 = signal_v6(closes, rsi_arr, ema_v6, bar_idx)
        s7 = signal_v7(closes, bb_upper, bb_middle, obv_arr, obv_ema_arr, bar_idx)
        s8 = signal_v8(closes, ema_v8, atr_v8, cmf_arr, V8_ATR_MULT, bar_idx)
        s9 = signal_v9(closes, volumes, macd_hist, macd_line_arr, vol_mean, bar_idx)
        raw_signals = {"v6": s6, "v7": s7, "v8": s8, "v9": s9}

        # Signal Blending
        forecast = blend_forecasts(raw_signals, STRATEGY_WEIGHTS, FDM)

        # Position Sizing
        target = calc_target_lots(
            forecast, atr_vol[bar_idx], closes[bar_idx],
            p.capital, p.max_position,
        )

        # Chandelier Exit
        atr_chandelier = atr(highs, lows, closes, CHANDELIER_PERIOD)
        if chandelier_exit_triggered(highs, lows, closes, atr_chandelier, bar_idx):
            target = 0
            self.output("Chandelier Exit 触发")

        # Portfolio Stops
        account = self.get_account_fund_data("")
        if account:
            equity = account.balance
            if equity > self.state_map.peak_equity:
                self.state_map.peak_equity = equity

            stop_action, val = check_portfolio_stops(
                equity, self.state_map.peak_equity, self.state_map.daily_start_equity,
            )
            if stop_action == "circuit":
                target = 0
                self.output(f"熔断! 回撤{val:.1%}")
            elif stop_action == "reduce":
                target = max(0, target // 2)
                self.output(f"减仓! 回撤{val:.1%}")
            elif stop_action == "daily_stop":
                target = 0
                self.output(f"单日止损! {val:.1%}")
            elif stop_action == "warning":
                self.output(f"预警! 回撤{val:.1%}")

        # ── 4. 存入pending, 下一bar执行 ──
        self.state_map.last_forecast = forecast
        self.state_map.last_target = target
        self._pending_target = target
        self._pending_signals = raw_signals
        self._pending_forecast = forecast

        # Log
        self.output(
            f"[BAR] f={forecast:.1f} target={target} "
            f"| v6={s6:.2f} v7={s7:.2f} v8={s8:.2f} v9={s9:.2f}"
        )
        self._push_widget(kline)
        self.update_status_bar()

    # ── 执行 ───────────────────────────────────────────────────

    def _execute(self, target_lots, price, raw_signals, forecast):
        """比较目标仓位与当前仓位, 市价下单."""
        p = self.params_map
        pos = self.get_position(p.instrument_id)
        current = pos.net_position if pos else 0
        diff = target_lots - current

        if diff == 0:
            return

        # 分类操作
        if current == 0 and target_lots > 0:
            action = "open"
        elif target_lots == 0 and current > 0:
            action = "close"
        elif diff > 0:
            action = "add"
        else:
            action = "reduce"

        # 市价下单 (price仅用于显示, market=True实际以市价成交)
        if diff > 0:
            oid = self.send_order(
                exchange=p.exchange,
                instrument_id=p.instrument_id,
                volume=abs(diff),
                price=price,
                order_direction="buy",
                market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)
        else:
            oid = self.auto_close_position(
                exchange=p.exchange,
                instrument_id=p.instrument_id,
                volume=abs(diff),
                price=price,
                order_direction="sell",
                market=True,
            )
            if oid is not None:
                self.order_ids.add(oid)

        # 飞书通知
        detail = " | ".join(f"{k}={v:.2f}" for k, v in (raw_signals or {}).items())
        feishu_send(action, p.instrument_id, price, abs(diff), forecast, detail)

        self.output(
            f"[{action.upper()}] {p.instrument_id} "
            f"{'买' if diff > 0 else '卖'}{abs(diff)}手 @ {price:.1f} "
            f"({current}→{target_lots})"
        )

    # ── 图表 ───────────────────────────────────────────────────

    def _push_widget(self, kline: KLineData, signal_price: float = 0.0):
        """更新K线图表."""
        try:
            self.widget.recv_kline({
                "kline": kline,
                "signal_price": signal_price,
                **self.main_indicator_data,
            })
        except Exception:
            pass

    # ── 回调 ───────────────────────────────────────────────────

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)
        self.order_ids.discard(trade.order_id)
        self.output(f"[成交] {trade.direction} {trade.volume}手 @ {trade.price}")
        self.update_status_bar()

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)
        self.order_ids.discard(order.order_id)

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu_send("error", self.params_map.instrument_id, 0, 0, 0, str(error))
