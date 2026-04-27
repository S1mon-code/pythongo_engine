"""
================================================================================
  JM_Long_1H_V13 — Donchian + MFI, H1触发, 最多5手, 自管持仓 (白银做多, 新标准)
================================================================================

  信号: Donchian 全通道位置 + MFI 量价确认 → [0, 1]
    移植自 QBase_v3 strategies/candidates/AGLongV13DonchianMfi
    don_sig: close 在通道上半段 → clip((pos-0.5)*3, 0, 1) (pos=0.5 中轴)
    mfi_sig: MFI > floor 50 时 → clip((mfi-50)/30, 0, 1)
    combined: 0.6 × don_sig + 0.4 × mfi_sig

  频率: H1 (1小时K线)
  执行: 直接限价单 (AggressivePricer 穿盘口) + escalator 自动升级 urgency
  图表:
    主图: Donchian上沿(DC_U) + 中轴(DC_M) + 下沿(DC_L) + Chandelier止损线
    副图: MFI (0-100, 含 50 参考线提示 floor)

  新标准 (2026-04-24 统一):
    1. 自管持仓 — self._own_pos + self._my_oids 过滤, 不碰 broker 其他仓
    2. max_lots = 5 (硬上限, 4 道保险)
    3. UI 实时字段 — heartbeat / last_price / bid1 / ask1 / spread_tick /
       last_tick_time / status_bar_updates / ui_push_count
    4. _log() 双写 — self.output() → StraLog.txt + print(flush=True) → stdout
    5. 所有关键路径都有日志 (除 [TICK #N] 不打)
    6. 周期 status_bar 刷新 (每 10 tick)
    7. type-safe widget push (NaN→0, 防止图表 Y 轴被污染)
    8. 完整风控: Chandelier Exit + Vol Targeting (Carver) + 三层止损

  保留: session_guard, 滑点, 绩效, 换月, 飞书, 每日复盘, 订单 escalator
================================================================================
"""
import sys
import time
from datetime import datetime

import numpy as np

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.feishu import feishu
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.persistence import load_state, save_state
from modules.position_sizing import apply_buffer, calc_optimal_lots
from modules.pricing import AggressivePricer
from modules.risk import RiskManager
from modules.rollover import check_rollover
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "JM_Long_1H_V13"

# Donchian + MFI 信号参数 (from QBase_v3 AGLongV13DonchianMfi)
DC_PERIOD = 50
MFI_PERIOD = 20
MFI_FLOOR = 50.0
BREAKOUT_WEIGHT = 0.6     # Donchian 信号权重 (MFI 为 1 - 0.6 = 0.4)
WARMUP = 70               # DC_PERIOD 50 + 缓冲

# Chandelier Exit (AG V13: mult=3.0, 比 AL V8 的 2.58 宽)
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 3.0

# Vol Targeting (Carver)
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
# DCE 品种 H1 日 bar 数: 日盘 3h45 + 夜盘 2h (21:00-23:00) ≈ 5h45 → ~6 bars/day
ANNUAL_FACTOR = 252 * 6

# 日报时间
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15

# 硬上限 (max_lots 的代码级保险)
MAX_LOTS = 5


def _freq_to_sec(kline_style) -> int:
    mapping = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800,
               "H1": 3600, "H4": 14400, "D1": 86400}
    for getter in (lambda x: str(x), lambda x: getattr(x, "value", None),
                   lambda x: getattr(x, "name", None)):
        try:
            raw = getter(kline_style)
            if raw is None:
                continue
            key = str(raw).upper()
            if "." in key:
                key = key.rsplit(".", 1)[-1]
            if key in mapping:
                return mapping[key]
        except Exception:
            continue
    return 3600


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS (纯numpy, 与 V8 原始/QBase 一致)
# ══════════════════════════════════════════════════════════════════════════════

def _atr(highs, lows, closes, period=14):
    """ATR — Wilder RMA."""
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


def _donchian(highs, lows, period=20):
    """Donchian Channel — returns (upper, lower, mid). Excludes current bar."""
    n = len(highs)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    mid = np.full(n, np.nan)
    for i in range(period, n):
        upper[i] = np.max(highs[i - period:i])
        lower[i] = np.min(lows[i - period:i])
        mid[i] = (upper[i] + lower[i]) / 2.0
    return upper, lower, mid


def _mfi(highs, lows, closes, volumes, period=20):
    """Money Flow Index (0-100) — 移植自 QBase_v3 indicators/volume/mfi.py."""
    n = len(closes)
    if n == 0:
        return np.array([], dtype=np.float64)

    highs = highs.astype(np.float64)
    lows = lows.astype(np.float64)
    closes = closes.astype(np.float64)
    volumes = volumes.astype(np.float64)

    tp = (highs + lows + closes) / 3.0
    raw_mf = tp * volumes
    tp_diff = np.zeros(n, dtype=np.float64)
    tp_diff[1:] = np.diff(tp)

    pos_flow = np.where(tp_diff > 0, raw_mf, 0.0)
    neg_flow = np.where(tp_diff < 0, raw_mf, 0.0)

    result = np.full(n, np.nan, dtype=np.float64)
    pos_sum = np.sum(pos_flow[1 : period + 1])
    neg_sum = np.sum(neg_flow[1 : period + 1])

    if n > period:
        result[period] = 100.0 if neg_sum == 0.0 else 100.0 - 100.0 / (1.0 + pos_sum / neg_sum)

    for i in range(period + 1, n):
        pos_sum += pos_flow[i] - pos_flow[i - period]
        neg_sum += neg_flow[i] - neg_flow[i - period]
        result[i] = 100.0 if neg_sum == 0.0 else 100.0 - 100.0 / (1.0 + pos_sum / neg_sum)

    return result


def generate_signal(closes, highs, lows, volumes, bar_idx):
    """Donchian 全通道位置 + MFI 量价确认 → [0, 1]. Long only.

    移植自 QBase_v3 AGLongV13DonchianMfi._generate_signal:
      pos = (close - lower) / (upper - lower)
      don_sig = clip((pos - 0.5) * 3, 0, 1) if pos > 0.5 else 0
      mfi_sig = clip((mfi - floor) / 30, 0, 1) if mfi > floor else 0
      combined = 0.6 * don_sig + 0.4 * mfi_sig
    """
    if bar_idx < WARMUP:
        return 0.0
    dc_upper, dc_lower, _ = _donchian(highs, lows, DC_PERIOD)
    mfi_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
    close = closes[bar_idx]
    upper = dc_upper[bar_idx]
    lower = dc_lower[bar_idx]
    mfi_val = mfi_arr[bar_idx]
    if np.isnan(upper) or np.isnan(lower) or np.isnan(mfi_val):
        return 0.0
    chan_width = upper - lower
    if chan_width <= 0:
        return 0.0

    pos = (close - lower) / chan_width
    don_sig = float(np.clip((pos - 0.5) * 3.0, 0.0, 1.0)) if pos > 0.5 else 0.0

    mfi_sig = 0.0
    if mfi_val > MFI_FLOOR:
        mfi_sig = float(np.clip((mfi_val - MFI_FLOOR) / 30.0, 0.0, 1.0))

    combined = BREAKOUT_WEIGHT * don_sig + (1.0 - BREAKOUT_WEIGHT) * mfi_sig
    return float(np.clip(combined, 0.0, 1.0))


def chandelier_long(highs, closes, atr_arr, bar_idx):
    """Long Chandelier: close < highest_high(period) - mult x ATR."""
    if bar_idx < CHANDELIER_PERIOD:
        return False
    a = atr_arr[bar_idx]
    if np.isnan(a):
        return False
    hh = np.max(highs[bar_idx - CHANDELIER_PERIOD + 1:bar_idx + 1])
    return bool(closes[bar_idx] < hh - CHANDELIER_MULT * a)


def _nz_last(arr, idx: int, fallback: float) -> float:
    """安全读 arr[idx], 越界/NaN 返回 fallback."""
    if idx < 0 or idx >= len(arr):
        return float(fallback)
    v = arr[idx]
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return float(fallback)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float(fallback)
    return float(fallback) if np.isnan(v) else v


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE (新标准 — 40+ 字段, 含所有信号数据和 UI 实时字段)
# ══════════════════════════════════════════════════════════════════════════════

class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="jm2605", title="合约代码")
    kline_style: str = Field(default="H1", title="K线周期")
    max_lots: int = Field(default=MAX_LOTS, title="最大持仓(硬上限5)")
    capital: float = Field(default=1_000_000, title="配置资金")
    hard_stop_pct: float = Field(default=0.5, title="硬止损(%)")
    trailing_pct: float = Field(default=0.3, title="移动止损(%)")
    equity_stop_pct: float = Field(default=2.0, title="权益止损(%)")
    flatten_minutes: int = Field(default=5, title="即将收盘提示(分钟)")
    sim_24h: bool = Field(default=False, title="24H模拟盘模式")
    # takeover_lots: 启动时手动指定接管手数 (默认 0=按 state 恢复; >0=手动接管, 覆盖 state)
    takeover_lots: int = Field(default=0, title="启动接管手数")


class State(BaseState):
    # 信号相关
    signal: float = Field(default=0.0, title="信号(0-1)")
    forecast: float = Field(default=0.0, title="预测(forecast)")
    optimal: int = Field(default=0, title="Optimal手数")
    target_lots: int = Field(default=0, title="目标手")
    # 持仓
    own_pos: int = Field(default=0, title="自管持仓")
    broker_pos: int = Field(default=0, title="账户总持仓")
    my_oids_n: int = Field(default=0, title="已发单累计")
    # UI 实时字段 (每 tick 更新)
    last_price: float = Field(default=0.0, title="最新价")
    last_bid1: float = Field(default=0.0, title="买一价")
    last_ask1: float = Field(default=0.0, title="卖一价")
    spread_tick: int = Field(default=0, title="盘口价差(tick)")
    last_tick_time: str = Field(default="---", title="最后tick时间")
    heartbeat: int = Field(default=0, title="心跳(每tick+1)")
    tick_count: int = Field(default=0, title="tick计数")
    bar_count: int = Field(default=0, title="bar计数")
    ui_push_count: int = Field(default=0, title="UI推送次数")
    status_bar_updates: int = Field(default=0, title="状态栏刷新次数")
    # 信号指标值 (Donchian + MFI + Chandelier — V13 的各种信号数据)
    dc_upper: float = Field(default=0.0, title="DC上沿")
    dc_mid: float = Field(default=0.0, title="DC中轴")
    dc_lower: float = Field(default=0.0, title="DC下沿")
    chandelier: float = Field(default=0.0, title="Chandelier止损")
    mfi_value: float = Field(default=0.0, title="MFI")
    don_sig: float = Field(default=0.0, title="Donchian子信号")
    mfi_sig: float = Field(default=0.0, title="MFI子信号")
    atr: float = Field(default=0.0, title="ATR")
    # 持仓追踪
    avg_price: float = Field(default=0.0, title="均价")
    peak_price: float = Field(default=0.0, title="峰价")
    hard_line: float = Field(default=0.0, title="硬止损线")
    trail_line: float = Field(default=0.0, title="移损线")
    # 账户
    equity: float = Field(default=0.0, title="权益")
    drawdown: str = Field(default="---", title="回撤")
    daily_pnl: str = Field(default="---", title="当日盈亏")
    # 状态
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="交易时段")
    pending: str = Field(default="---", title="待执行")
    last_action: str = Field(default="---", title="上次操作")
    last_direction: str = Field(default="---", title="上次direction(DIAG)")
    # 辅助统计
    slippage: str = Field(default="---", title="滑点")
    perf: str = Field(default="---", title="绩效")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class JM_Long_1H_V13_Donchian_MFI(BaseStrategy):
    """焦煤 H1 做多 — Donchian + MFI (QBase_v3 V13), 新标准版."""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # 自管理持仓 (核心: 不从 broker 继承历史仓位)
        self._own_pos: int = 0
        self._my_oids: set = set()
        self.avg_price = 0.0
        self.peak_price = 0.0
        # 启动接管模式: takeover_lots > 0 时, 首 tick 用 last_price 兜底 avg_price/peak_price
        self._takeover_pending = False

        # pending / 挂单
        self._pending = None
        self._pending_target = None
        self._pending_reason = ""
        self.order_id = set()

        # 账户 / 风控
        self._investor_id = ""
        self._risk: RiskManager | None = None
        self._current_td = ""
        self._daily_review_sent = False
        self._rollover_checked = False
        self._today_trades = []

        # 模块
        self._guard: SessionGuard | None = None
        self._slip: SlippageTracker | None = None
        self._hb: HeartbeatMonitor | None = None
        self._om = OrderMonitor()
        self._perf: PerformanceTracker | None = None
        self._pricer: AggressivePricer | None = None
        self._multiplier = 5

        # 指标缓存 (供 main/sub_indicator_data 读)
        self._ind_dc_upper = 0.0
        self._ind_dc_mid = 0.0
        self._ind_dc_lower = 0.0
        self._ind_chandelier = 0.0
        self._ind_mfi = 50.0        # 初值 50 (无偏向)
        self._ind_mfi_floor = MFI_FLOOR   # 副图显示一条参考线
        self._ind_atr = 0.0

        # 诊断计数器
        self._tick_count = 0
        self._bar_count = 0               # callback 总次数 (含历史)
        self._realtime_cb_count = 0
        self._last_session_state = None
        self._widget_err_count = 0
        self._widget_ok_count = 0

    # ══════════════════════════════════════════════════════════════════════
    #  日志封装 — 双写, 实时落盘
    # ══════════════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        """self.output() → StraLog.txt + print() → stdout."""
        self.output(msg)
        try:
            print(f"[{STRATEGY_NAME}] {msg}", flush=True)
            sys.stdout.flush()
        except Exception:
            pass

    @property
    def main_indicator_data(self):
        """主图 (价格尺度): Donchian 三线 + Chandelier."""
        return {
            "DC_U": self._ind_dc_upper,
            "DC_M": self._ind_dc_mid,
            "DC_L": self._ind_dc_lower,
            "Chandelier": self._ind_chandelier,
        }

    @property
    def sub_indicator_data(self):
        """副图 (0-100 尺度): MFI + floor 参考线 (50)."""
        return {
            "MFI": self._ind_mfi,
            "MFI_Floor": self._ind_mfi_floor,
        }

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def _current_bar_ts(self) -> int:
        sec = _freq_to_sec(self.params_map.kline_style)
        return int(time.time() // sec * sec)

    # ══════════════════════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════════════════════

    def on_start(self):
        p = self.params_map
        self._log(f"[ON_START] 开始初始化 {p.instrument_id} {p.kline_style}")

        if p.max_lots > MAX_LOTS:
            self._log(f"[max_lots] 参数 {p.max_lots} > {MAX_LOTS}, 强制拉回")
            p.max_lots = MAX_LOTS

        self._multiplier = get_multiplier(p.instrument_id)
        self._pricer = AggressivePricer(tick_size=get_tick_size(p.instrument_id))
        self._guard = SessionGuard(p.instrument_id, p.flatten_minutes,
                                   sim_24h=p.sim_24h, open_grace_sec=30)
        self._slip = SlippageTracker(p.instrument_id)
        self._hb = HeartbeatMonitor(p.instrument_id)
        self._perf = PerformanceTracker(p.instrument_id)
        self._log(
            f"[ON_START] 模块初始化 multiplier={self._multiplier} "
            f"tick_size={get_tick_size(p.instrument_id)} sim_24h={p.sim_24h}"
        )

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self._log(f"[ON_START] KLineGenerator 已创建, push_history_data...")
        self.kline_generator.push_history_data()
        producer = self.kline_generator.producer
        self._log(
            f"[ON_START] push_history 完成 producer_bars={len(producer.close)} "
            f"(预热需 {WARMUP} 根)"
        )

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id
        self._log(f"[ON_START] investor_id={self._investor_id}")

        self._risk = RiskManager(capital=p.capital)

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._risk.load_state(saved)
            self._own_pos = int(saved.get("own_pos", 0))
            self._my_oids = set(saved.get("my_oids", []))
            self.avg_price = saved.get("avg_price", 0.0)
            self.peak_price = saved.get("peak_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._today_trades = saved.get("today_trades", [])
            self._log(
                f"[ON_START 恢复] own_pos={self._own_pos} avg={self.avg_price:.1f} "
                f"peak={self.peak_price:.1f} my_oids={len(self._my_oids)}"
            )

        acct = self._get_account()
        if acct:
            if self._risk.peak_equity == p.capital:
                self._risk.update(acct.balance)
            if self._risk.daily_start_eq == p.capital:
                self._risk.on_day_change(acct.balance, acct.position_profit)
            self._log(
                f"[ON_START 账户] balance={acct.balance:.0f} "
                f"available={acct.available:.0f} position_profit={acct.position_profit:.0f}"
            )

        pos = self.get_position(p.instrument_id)
        broker_pos = pos.net_position if pos else 0
        self.state_map.own_pos = self._own_pos
        self.state_map.broker_pos = broker_pos
        self._log(f"[ON_START 持仓] own_pos={self._own_pos} broker_pos={broker_pos}")

        # Takeover override: 启动时手动指定接管手数, 优先级最高 (覆盖 state 恢复值).
        # 用途: 18:00 强制清算后, 21:00 重开时承接下午策略已开仓位的部分,
        # broker 端"底仓"不被触动 (策略只管 _own_pos 这部分).
        if p.takeover_lots > 0:
            self._own_pos = int(p.takeover_lots)
            self._my_oids = set()           # 历史 oid 失效
            self._takeover_pending = True   # 首 tick 兜底 avg_price/peak_price
            self.avg_price = 0.0
            self.peak_price = 0.0
            self.state_map.own_pos = self._own_pos
            self._log(
                f"[ON_START TAKEOVER] 手动接管 {self._own_pos} 手 "
                f"(覆盖 state, broker_pos={broker_pos}, 底仓={broker_pos - self._own_pos} 手不动). "
                f"avg/peak 将在首 tick 用 last_price 兜底."
            )
            feishu("start", p.instrument_id,
                   f"**TAKEOVER 启动** {STRATEGY_NAME}\n"
                   f"接管 {self._own_pos} 手 / broker {broker_pos} 手\n"
                   f"底仓 {broker_pos - self._own_pos} 手不归策略管")

        if self._own_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        level, days = check_rollover(p.instrument_id)
        if level:
            feishu("rollover", p.instrument_id, f"**换月提醒**: 距交割月**{days}天**")

        self._log(
            f"[ON_START] 调用 super().on_start() "
            f"(trading=True + sub_market_data + load_data_signal)"
        )
        super().on_start()

        self._log(
            f"=== 启动完成 === | {p.instrument_id} {p.kline_style} | "
            f"max_lots={p.max_lots} | own_pos={self._own_pos} broker_pos={broker_pos} | "
            f"session={self._guard.get_status()} should_trade={self._guard.should_trade()}"
        )
        self._log(
            "=== UI 测试清单 === (请对照检查):\n"
            "  主图: DC_U / DC_M / DC_L / Chandelier (4 条价格线)\n"
            "  副图: MFI + MFI_Floor (50, 参考线)\n"
            "  状态栏: heartbeat/last_price/mfi_value/don_sig/mfi_sig 等实时字段\n"
            "  箭头: OPEN/ADD/REDUCE/CLOSE 会在 K 线上标记"
        )
        feishu("start", p.instrument_id,
               f"**策略启动** {STRATEGY_NAME} (新标准)\n"
               f"合约 {p.instrument_id} {p.kline_style}\n"
               f"自管持仓: {self._own_pos}手 (账户总: {broker_pos}手)\n"
               f"max_lots: {p.max_lots}\n"
               f"止损 hard={p.hard_stop_pct}% trail={p.trailing_pct}% "
               f"equity={p.equity_stop_pct}%")

    def on_stop(self):
        self._save()
        self._log(
            f"[ON_STOP] tick_count={self._tick_count} bar_count={self._bar_count} "
            f"own_pos={self._own_pos} my_oids={len(self._my_oids)} "
            f"ui_push={self._widget_ok_count}"
        )
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止** {STRATEGY_NAME}\n"
               f"自管持仓: {self._own_pos}手\n"
               f"{self._slip.format_report() if self._slip else ''}")
        super().on_stop()

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)

        # Takeover 兜底: 接管模式下首个有效 tick 用 last_price 初始化 avg_price / peak_price.
        # _risk.peak_price 由 _on_tick_stops 内的 update_peak_trough_tick 自动初始化 (sign 切换分支).
        if self._takeover_pending and tick.last_price > 0:
            self.avg_price = float(tick.last_price)
            self.peak_price = float(tick.last_price)
            self._takeover_pending = False
            self._log(
                f"[TAKEOVER FIRST TICK] avg_price=peak_price={tick.last_price:.2f} "
                f"own_pos={self._own_pos}"
            )

        self._tick_count += 1
        self.state_map.tick_count = self._tick_count
        self.state_map.heartbeat = self._tick_count % 1000   # 肉眼观察 UI 跳动

        # ── UI 实时字段 (每 tick) ──
        self.state_map.last_price = float(tick.last_price)
        self.state_map.last_bid1 = float(tick.bid_price1)
        self.state_map.last_ask1 = float(tick.ask_price1)
        try:
            ts = get_tick_size(self.params_map.instrument_id) or 0.01
            if tick.ask_price1 > 0 and tick.bid_price1 > 0:
                self.state_map.spread_tick = int(round(
                    (tick.ask_price1 - tick.bid_price1) / ts
                ))
        except Exception:
            pass
        try:
            self.state_map.last_tick_time = tick.datetime.strftime("%H:%M:%S")
        except Exception:
            self.state_map.last_tick_time = str(tick.datetime)

        # 每 10 tick 主动刷新状态栏
        if self._tick_count % 10 == 0:
            try:
                self.update_status_bar()
                self.state_map.status_bar_updates = self.state_map.status_bar_updates + 1
            except Exception as e:
                self._log(f"[UI_STATUS 异常] {type(e).__name__}: {e}")

        self.kline_generator.tick_to_kline(tick)

        if self._pricer is not None:
            try:
                self._pricer.update(tick)
            except Exception as e:
                self._log(f"[pricer异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_stops(tick)
        except Exception as e:
            self._log(f"[stops异常] {type(e).__name__}: {e}")

        try:
            self._on_tick_aux(tick)
        except Exception as e:
            self._log(f"[on_tick_aux异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**on_tick异常**\n{type(e).__name__}: {e}")

    def _on_tick_stops(self, tick: TickData):
        """Tick 级止损 — 只基于 own_pos."""
        if not self.trading:
            return
        if self._guard is not None and not self._guard.should_trade():
            return
        if self._pending is not None:
            return
        if self._own_pos <= 0:
            return

        p = self.params_map
        price = tick.last_price

        self._risk.update_peak_trough_tick(price, self._own_pos)
        self.peak_price = self._risk.peak_price

        action, reason = self._risk.check_hard_stop_tick(
            price=price, avg_price=self.avg_price,
            net_pos=self._own_pos, hard_stop_pct=p.hard_stop_pct,
        )
        if action:
            self._log(f"[{action}][TICK] {reason}")
            self._exec_stop_at_tick(price, action, reason)
            return

        action, reason = self._risk.check_trail_minutely(
            price=price, now=datetime.now(),
            net_pos=self._own_pos, trailing_pct=p.trailing_pct,
        )
        if action:
            self._log(f"[{action}][M1] {reason}")
            self._exec_stop_at_tick(price, action, reason)

    def _on_tick_aux(self, tick: TickData):
        p = self.params_map

        # Session 切换日志
        if self._guard is not None:
            cur = self._guard.should_trade()
            if cur != self._last_session_state:
                self._log(
                    f"[SESSION_CHANGE] should_trade "
                    f"{self._last_session_state} → {cur} | "
                    f"status={self._guard.get_status()}"
                )
                self._last_session_state = cur

        # 订单 urgency 升级
        if (self._guard is not None and self._guard.should_trade()
                and self._pricer is not None):
            to_escalate = self._om.check_escalation()
            for oid, next_urgency, info in to_escalate:
                self._resubmit_escalated(oid, next_urgency, info)

        # 交易日切换
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
            self._rollover_checked = False
            self._save()
            self._log(f"[新交易日] {td}")
        if not self._current_td:
            self._current_td = td
            self.state_map.trading_day = td

        if not self._rollover_checked:
            level, days = check_rollover(p.instrument_id)
            if level:
                feishu("rollover", p.instrument_id, f"**换月**: 距交割月{days}天")
            self._rollover_checked = True

        for atype, msg in self._hb.check(p.instrument_id):
            if atype == "no_tick":
                feishu("no_tick", p.instrument_id, msg)

        self.state_map.session = self._guard.get_status()

        # 每日复盘
        now = datetime.now()
        if (not self._daily_review_sent
                and now.hour == DAILY_REVIEW_HOUR
                and DAILY_REVIEW_MINUTE <= now.minute < DAILY_REVIEW_MINUTE + 5):
            self._send_review()
            self._daily_review_sent = True

    # ══════════════════════════════════════════════════════════════════════
    #  定价 / escalator
    # ══════════════════════════════════════════════════════════════════════

    def _aggressive_price(self, price, direction, urgency: str = "normal"):
        if self._pricer is None or self._pricer.last == 0:
            return price
        return self._pricer.price(direction, urgency)

    def _resubmit_escalated(self, old_oid, next_urgency: str, info: dict) -> None:
        direction = info.get("direction")
        kind = info.get("kind")
        vol = info.get("vol", 0)
        if not direction or not kind or vol <= 0:
            return
        if self._pricer is None or self._pricer.last == 0:
            return
        p = self.params_map

        self.cancel_order(old_oid)
        self._om.on_cancel(old_oid)
        self.order_id.discard(old_oid)

        new_price = self._pricer.price(direction, next_urgency)
        if kind == "open":
            new_oid = self.send_order(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=new_price, order_direction=direction,
            )
        else:
            new_oid = self.auto_close_position(
                exchange=p.exchange, instrument_id=p.instrument_id,
                volume=vol, price=new_price, order_direction=direction,
            )
        if new_oid is None:
            self._log(f"[ESCALATE] oid={old_oid} → {next_urgency}: 重发 None")
            return
        self.order_id.add(new_oid)
        self._my_oids.add(new_oid)
        self._om.on_send(new_oid, vol, new_price,
                         urgency=next_urgency, direction=direction, kind=kind)
        self._log(
            f"[ESCALATE] {old_oid} → {new_oid} | {direction} {vol}手 "
            f"@ {new_price:.1f} | {info.get('urgency')} → {next_urgency}"
        )

    # ══════════════════════════════════════════════════════════════════════
    #  K线 + 指标
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData):
        self._bar_count += 1
        if self._bar_count <= 3 or self._bar_count % 20 == 0:
            producer = self.kline_generator.producer if self.kline_generator else None
            n = len(producer.close) if producer else 0
            self._log(
                f"[BAR #{self._bar_count}] dt={kline.datetime} "
                f"O={kline.open} H={kline.high} L={kline.low} C={kline.close} "
                f"V={kline.volume} | producer_n={n} trading={self.trading}"
            )
        try:
            self._on_bar(kline)
        except Exception as e:
            self._log(f"[callback异常] {type(e).__name__}: {e}")

    def real_time_callback(self, kline: KLineData):
        self._realtime_cb_count += 1
        if self._realtime_cb_count == 1 or self._realtime_cb_count % 200 == 0:
            self._log(
                f"[RT_CB #{self._realtime_cb_count}] "
                f"last_close={kline.close} dt={kline.datetime}"
            )
        self._push_widget(kline)

    def _refresh_indicators(self) -> None:
        """每 bar 闭合刷新指标缓存 (供 UI 和 state_map)."""
        producer = self.kline_generator.producer
        n = len(producer.close)
        if n < 2:
            return

        closes = np.asarray(producer.close, dtype=np.float64)
        highs = np.asarray(producer.high, dtype=np.float64)
        lows = np.asarray(producer.low, dtype=np.float64)
        bar_idx = n - 1
        cur = float(closes[-1])

        # Donchian
        if n >= DC_PERIOD + 1:
            dc_u, dc_l, dc_m = _donchian(highs, lows, DC_PERIOD)
            self._ind_dc_upper = _nz_last(dc_u, bar_idx, cur)
            self._ind_dc_mid = _nz_last(dc_m, bar_idx, cur)
            self._ind_dc_lower = _nz_last(dc_l, bar_idx, cur)
        else:
            self._ind_dc_upper = self._ind_dc_mid = self._ind_dc_lower = cur

        # MFI
        volumes = np.asarray(producer.volume, dtype=np.float64)
        if n >= MFI_PERIOD + 2:
            mfi_arr = _mfi(highs, lows, closes, volumes, MFI_PERIOD)
            self._ind_mfi = _nz_last(mfi_arr, bar_idx, 50.0)

        # ATR + Chandelier
        if n >= CHANDELIER_PERIOD + 2:
            ch_atr = _atr(highs, lows, closes, CHANDELIER_PERIOD)
            a = ch_atr[bar_idx]
            self._ind_atr = _nz_last(ch_atr, bar_idx, 0.0)
            if not np.isnan(a):
                hh = float(np.max(highs[bar_idx - CHANDELIER_PERIOD + 1:bar_idx + 1]))
                self._ind_chandelier = hh - CHANDELIER_MULT * float(a)
            else:
                self._ind_chandelier = cur
        else:
            self._ind_chandelier = cur

        # 同步到 state_map
        self.state_map.dc_upper = round(self._ind_dc_upper, 1)
        self.state_map.dc_mid = round(self._ind_dc_mid, 1)
        self.state_map.dc_lower = round(self._ind_dc_lower, 1)
        self.state_map.chandelier = round(self._ind_chandelier, 1)
        self.state_map.mfi_value = round(self._ind_mfi, 2)
        self.state_map.atr = round(self._ind_atr, 2)

    def _on_bar(self, kline: KLineData):
        p = self.params_map
        signal_price = 0.0

        try:
            self._refresh_indicators()
        except Exception as e:
            self._log(f"[refresh_indicators 异常] {type(e).__name__}: {e}")

        # 历史回放
        if not self.trading:
            if self._bar_count <= 3 or self._bar_count % 20 == 0:
                self._log(f"[ON_BAR 历史] bar#{self._bar_count} trading=False")
            self._push_widget(kline)
            return

        # 非交易时段
        if self._guard is not None and not self._guard.should_trade():
            self._log(
                f"[ON_BAR 非交易时段] bar#{self._bar_count} "
                f"session={self._guard.get_status()}"
            )
            self.state_map.session = self._guard.get_status()
            self._push_widget(kline)
            self.update_status_bar()
            return

        self._log(
            f"[ON_BAR 实盘] bar#{self._bar_count} dt={kline.datetime} "
            f"close={kline.close} own_pos={self._own_pos} pending={self._pending}"
        )

        # 撤挂单
        n_cancel = 0
        for oid in list(self.order_id):
            self.cancel_order(oid)
            n_cancel += 1
        if n_cancel > 0:
            self._log(f"[ON_BAR] bar 开头撤 {n_cancel} 个挂单")
        for oid in self._om.check_timeouts(self.cancel_order):
            self._log(f"[超时撤单] {oid}")

        # 残留 pending
        if self._pending is not None:
            self._log(f"[ON_BAR] 执行残留 pending={self._pending} reason={self._pending_reason}")
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""
            self._push_widget(kline, signal_price)
            self.update_status_bar()
            return

        # 预热检查
        producer = self.kline_generator.producer
        if len(producer.close) < WARMUP + 2:
            self._log(
                f"[ON_BAR 预热] producer_bars={len(producer.close)} "
                f"需要 {WARMUP + 2} 根, 还差 {WARMUP + 2 - len(producer.close)}"
            )
            self._push_widget(kline)
            return

        closes = np.asarray(producer.close, dtype=np.float64)
        highs = np.asarray(producer.high, dtype=np.float64)
        lows = np.asarray(producer.low, dtype=np.float64)
        bar_idx = len(closes) - 1
        close = float(closes[-1])

        # 指标 debug log
        self._log(
            f"[IND] DC_U={self._ind_dc_upper:.1f} DC_M={self._ind_dc_mid:.1f} "
            f"DC_L={self._ind_dc_lower:.1f} Chandelier={self._ind_chandelier:.1f} | "
            f"MFI={self._ind_mfi:.1f} (floor={MFI_FLOOR}) | "
            f"ATR={self._ind_atr:.1f} close={close:.1f}"
        )

        # ── 信号 (Donchian + MFI 组合) ──
        volumes = np.asarray(producer.volume, dtype=np.float64)
        raw = generate_signal(closes, highs, lows, volumes, bar_idx)

        # 子信号也算一次供 state_map 展示
        chan_w = self._ind_dc_upper - self._ind_dc_lower
        if chan_w > 0:
            pos = (close - self._ind_dc_lower) / chan_w
            don_sig = float(np.clip((pos - 0.5) * 3.0, 0.0, 1.0)) if pos > 0.5 else 0.0
        else:
            don_sig = 0.0
        mfi_sig = float(np.clip((self._ind_mfi - MFI_FLOOR) / 30.0, 0.0, 1.0)) \
                  if self._ind_mfi > MFI_FLOOR else 0.0
        self.state_map.don_sig = round(don_sig, 3)
        self.state_map.mfi_sig = round(mfi_sig, 3)

        forecast = min(FORECAST_CAP, max(0.0, raw * FORECAST_SCALAR))
        self.state_map.signal = round(raw, 3)
        self.state_map.forecast = round(forecast, 1)
        self._log(
            f"[SIGNAL] raw={raw:.4f} forecast={forecast:.1f} "
            f"(don_sig={don_sig:.3f} × {BREAKOUT_WEIGHT} + "
            f"mfi_sig={mfi_sig:.3f} × {1-BREAKOUT_WEIGHT})"
        )

        # ── 仓位 (Vol Targeting Carver, max_lots 硬截断) ──
        atr_arr = _atr(highs, lows, closes)
        optimal_raw = calc_optimal_lots(
            forecast, atr_arr[bar_idx], close,
            p.capital, p.max_lots, self._multiplier, ANNUAL_FACTOR,
        )
        optimal = round(optimal_raw)
        target = apply_buffer(optimal, self._own_pos)
        target = min(target, p.max_lots, MAX_LOTS)   # 双保险

        if forecast == 0 and self._own_pos > 0:
            target = 0

        self.state_map.optimal = optimal
        self.state_map.target_lots = target
        self.state_map.own_pos = self._own_pos
        bpos = self.get_position(p.instrument_id)
        self.state_map.broker_pos = bpos.net_position if bpos else 0

        self._log(
            f"[POS_DECISION] optimal={optimal} (raw={optimal_raw:.2f}) "
            f"own_pos={self._own_pos} → target={target} "
            f"broker_pos={self.state_map.broker_pos} "
            f"(capital={p.capital} atr={_nz_last(atr_arr, bar_idx, 0.0):.2f})"
        )

        # 持仓追踪
        if self._own_pos == 0:
            self.avg_price = 0.0
            self.peak_price = 0.0
        else:
            self._risk.update_peak_trough_tick(close, self._own_pos)
            self.peak_price = self._risk.peak_price
        self.state_map.avg_price = round(self.avg_price, 1)
        self.state_map.peak_price = round(self.peak_price, 1)
        self.state_map.hard_line = (
            round(self.avg_price * (1 - p.hard_stop_pct / 100), 1)
            if self._own_pos > 0 else 0.0
        )
        self.state_map.trail_line = (
            round(self.peak_price * (1 - p.trailing_pct / 100), 1)
            if self._own_pos > 0 else 0.0
        )

        # 权益
        acct = self._get_account()
        equity = pos_profit = 0.0
        if acct:
            equity = acct.balance
            pos_profit = acct.position_profit
            self._risk.update(equity)
            self.state_map.equity = round(equity, 0)
            self.state_map.drawdown = f"{self._risk.drawdown_pct:.2%}"
            self.state_map.daily_pnl = f"{self._risk.daily_pnl_pct:+.2%}"

        # 权益/熔断 止损 (硬/移动由 tick 处理, 这里只看 equity)
        if self._own_pos > 0:
            action, reason = self._risk.check(
                close=close, avg_price=self.avg_price, peak_price=self.peak_price,
                pos_profit=pos_profit, net_pos=self._own_pos,
                hard_stop_pct=999.0, trailing_pct=999.0,
                equity_stop_pct=p.equity_stop_pct,
            )
            if action and action not in ("WARNING", "REDUCE"):
                self._pending = action
                self._pending_reason = reason
                self._log(f"[{action}] {reason}")
            elif action == "REDUCE":
                target = max(0, self._own_pos // 2)
                self._pending_reason = reason
                self._log(f"[REDUCE] {reason}")
            elif action == "WARNING":
                self._log(f"[预警] {reason}")
                feishu("warning", p.instrument_id, f"**回撤预警**: {reason}")

        # Chandelier Exit
        if self._pending is None and self._own_pos > 0:
            ch_atr = _atr(highs, lows, closes, CHANDELIER_PERIOD)
            if chandelier_long(highs, closes, ch_atr, bar_idx):
                self._pending = "CLOSE"
                self._pending_reason = "Chandelier Exit (Long)"
                self._log(f"[CHANDELIER] {self._pending_reason}")

        # ── 信号 → pending ──
        if self._pending is None and target != self._own_pos:
            if self._own_pos == 0 and target > 0:
                self._pending = "OPEN"
            elif target == 0 and self._own_pos > 0:
                self._pending = "CLOSE"
            elif target > self._own_pos:
                self._pending = "ADD"
            elif target < self._own_pos:
                self._pending = "REDUCE"
            self._pending_target = target
            self._pending_reason = (
                f"signal={raw:.2f} forecast={forecast:.1f} "
                f"optimal={optimal} target={target}"
            )
            self._log(
                f"[PENDING 设置] action={self._pending} target={target} "
                f"own_pos={self._own_pos} reason={self._pending_reason}"
            )
        elif self._pending is None and target == self._own_pos:
            self._log(
                f"[NO_ACTION] target={target} == own_pos={self._own_pos} "
                f"(forecast={forecast:.1f})"
            )

        # 同 bar 立即执行
        if self._pending is not None:
            signal_price = self._execute(kline, self._pending)
            self._pending = None
            self._pending_target = None
            self._pending_reason = ""

        self.state_map.pending = self._pending or "---"
        self.state_map.my_oids_n = len(self._my_oids)
        self.state_map.slippage = self._slip.format_report()
        self.state_map.perf = self._perf.format_short()
        self._push_widget(kline, signal_price)
        self.update_status_bar()

    # ══════════════════════════════════════════════════════════════════════
    #  执行
    # ══════════════════════════════════════════════════════════════════════

    def _execute(self, kline: KLineData, action: str) -> float:
        p = self.params_map
        price = kline.close
        self._log(
            f"[EXECUTE] action={action} price={price} own_pos={self._own_pos} "
            f"target={self._pending_target} reason={self._pending_reason}"
        )
        if self._guard is not None and not self._guard.should_trade():
            self._log(f"[执行跳过] 非交易时段 {action}")
            return 0.0

        if action == "OPEN":
            return self._exec_open(price)
        if action == "ADD":
            return self._exec_add(price)
        if action == "REDUCE":
            return self._exec_reduce(price)
        if action in ("CLOSE", "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
                      "CIRCUIT", "DAILY_STOP", "FLATTEN"):
            return self._exec_close(kline, action)
        self._log(f"[EXECUTE] 未知 action={action}")
        return 0.0

    def _exec_open(self, price: float) -> float:
        p = self.params_map
        target = min(self._pending_target or 1, MAX_LOTS)
        vol = max(1, target)
        if self._own_pos > 0:
            return self._exec_add(price)

        acct = self._get_account()
        if acct:
            need = price * self._multiplier * vol * 0.15
            limit = acct.available * 0.6
            self._log(
                f"[EXEC_OPEN 保证金] need={need:,.0f} "
                f"available={acct.available:,.0f} limit(60%)={limit:,.0f}"
            )
            if need > limit:
                self._log(f"[保证金不足] OPEN {vol}手")
                feishu("error", p.instrument_id, f"**保证金不足** OPEN {vol}手")
                return 0.0

        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy", urgency="passive")
        self._log(f"[EXEC_OPEN] send_order buy {vol}手 @ {buy_price} (passive)")
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=buy_price, order_direction="buy",
        )
        self._log(f"[EXEC_OPEN] send_order 返回 oid={oid}")
        if oid is None:
            self._log(f"[OPEN] 发单失败 (oid=None)")
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, buy_price,
                         urgency="passive", direction="buy", kind="open")
        self.state_map.last_action = f"建仓{vol}手"
        feishu("open", p.instrument_id,
               f"**建仓** {vol}手 @ {buy_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: 0 → {vol}手")
        return price

    def _exec_add(self, price: float) -> float:
        p = self.params_map
        target = min(self._pending_target or (self._own_pos + 1), MAX_LOTS)
        vol = max(1, target - self._own_pos)
        if vol <= 0:
            return 0.0
        acct = self._get_account()
        if acct and price * self._multiplier * vol * 0.15 > acct.available * 0.6:
            self._log("[加仓保证金不足]")
            return 0.0
        self._slip.set_signal_price(price)
        buy_price = self._aggressive_price(price, "buy", urgency="passive")
        self._log(f"[EXEC_ADD] send_order buy {vol}手 @ {buy_price} (passive)")
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=buy_price, order_direction="buy",
        )
        self._log(f"[EXEC_ADD] send_order 返回 oid={oid}")
        if oid is None:
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, buy_price,
                         urgency="passive", direction="buy", kind="open")
        self.state_map.last_action = f"加仓{vol}手"
        feishu("add", p.instrument_id,
               f"**加仓** {vol}手 @ {buy_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: {self._own_pos} → {self._own_pos + vol}手")
        return price

    def _exec_reduce(self, price: float) -> float:
        p = self.params_map
        target = self._pending_target if self._pending_target is not None else self._own_pos // 2
        target = max(0, target)
        vol = self._own_pos - target
        if vol <= 0 or self._own_pos <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        sell_price = self._aggressive_price(price, "sell", urgency="normal")
        self._log(f"[EXEC_REDUCE] auto_close sell {vol}手 @ {sell_price} (normal)")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        self._log(f"[EXEC_REDUCE] 返回 oid={oid}")
        if oid is None:
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency="normal", direction="sell", kind="reduce")
        self.state_map.last_action = f"减仓{vol}手"
        feishu("reduce", p.instrument_id,
               f"**减仓** {vol}手 @ {sell_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"自管: {self._own_pos} → {target}手")
        return -price

    def _exec_close(self, kline: KLineData, action: str) -> float:
        labels = {
            "CLOSE": "信号平仓", "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断", "DAILY_STOP": "单日止损",
            "FLATTEN": "即将收盘清仓",
        }
        label = labels.get(action, action)
        p = self.params_map
        price = kline.close
        vol = self._own_pos
        if vol <= 0:
            return 0.0
        self._slip.set_signal_price(price)
        urgency = "normal" if action == "CLOSE" else (
            "critical" if action in ("EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN") else "urgent"
        )
        sell_price = self._aggressive_price(price, "sell", urgency=urgency)
        self._log(f"[EXEC_CLOSE] {label} auto_close sell {vol}手 @ {sell_price} ({urgency})")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        self._log(f"[EXEC_CLOSE] 返回 oid={oid}")
        if oid is None:
            feishu("error", p.instrument_id, f"**{label}发单失败** {vol}手")
            return 0.0
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency=urgency, direction="sell", kind="close")
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, vol)
        self.state_map.last_action = f"{label} {pnl_pct:+.2f}%"
        self._rec(label, vol, "卖", price, self._own_pos, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** {vol}手 @ {sell_price:,.1f}\n"
               f"逻辑: {self._pending_reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"自管: {self._own_pos} → 0手")
        return -price

    def _exec_stop_at_tick(self, price: float, action: str, reason: str) -> None:
        """Tick 触发的止损立即执行 (只平 own_pos)."""
        p = self.params_map
        if self._guard is not None and not self._guard.should_trade():
            return
        vol = self._own_pos
        if vol <= 0:
            return

        for oid in list(self.order_id):
            self.cancel_order(oid)

        self._pending_reason = reason
        self._slip.set_signal_price(price)
        urgency = "critical" if action in (
            "EQUITY_STOP", "CIRCUIT", "DAILY_STOP", "FLATTEN"
        ) else "urgent"
        sell_price = self._aggressive_price(price, "sell", urgency=urgency)
        self._log(f"[EXEC_STOP] {action} auto_close sell {vol}手 @ {sell_price} ({urgency})")
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=vol, price=sell_price, order_direction="sell",
        )
        if oid is None:
            self._log(f"[TICK_STOP] auto_close None, 保留 _pending={action}")
            self._pending = action
            feishu("error", p.instrument_id,
                   f"**止损发单失败** action={action}\n逻辑: {reason}")
            return
        self.order_id.add(oid)
        self._my_oids.add(oid)
        self._om.on_send(oid, vol, sell_price,
                         urgency=urgency, direction="sell", kind="close")

        labels = {
            "HARD_STOP": "硬止损", "TRAIL_STOP": "移动止损",
            "EQUITY_STOP": "权益止损", "CIRCUIT": "熔断",
            "DAILY_STOP": "单日止损", "FLATTEN": "即将收盘清仓",
            "CLOSE": "信号平仓",
        }
        label = labels.get(action, action)
        pnl_pct = (price - self.avg_price) / self.avg_price * 100 if self.avg_price > 0 else 0
        abs_pnl = self._perf.on_close(self.avg_price, price, vol)
        self.state_map.last_action = f"{label}[TICK] {pnl_pct:+.2f}%"
        self._rec(label, vol, "卖", price, self._own_pos, 0)
        feishu(action.lower(), p.instrument_id,
               f"**{label}** (tick触发) {vol}手 @ {price:,.1f}\n"
               f"逻辑: {reason}\n"
               f"盈亏: {pnl_pct:+.2f}% ({abs_pnl:+,.0f})\n"
               f"自管: {self._own_pos} → 0手")
        self._pending = action
        self._risk.peak_price = 0.0
        self._risk.trough_price = 0.0
        self._risk._last_trail_minute = None
        self._save()

    # ══════════════════════════════════════════════════════════════════════
    #  辅助 / 存档 / UI
    # ══════════════════════════════════════════════════════════════════════

    def _rec(self, action, lots, side, price, before, after):
        self._today_trades.append({
            "time": time.strftime("%H:%M:%S"), "action": action,
            "lots": lots, "side": side, "price": round(price, 1),
            "before": before, "after": after,
        })

    def _save(self):
        state = {
            "own_pos": self._own_pos,
            "my_oids": list(self._my_oids)[-500:],
            "avg_price": self.avg_price,
            "peak_price": self.peak_price,
            "signal": self.state_map.signal,
            "trading_day": self._current_td,
            "today_trades": self._today_trades[-50:],
        }
        if self._risk is not None:
            state.update(self._risk.get_state())
        save_state(state, name=STRATEGY_NAME)

    def _send_review(self):
        p = self.params_map
        acct = self._get_account()
        eq = acct.balance if acct else 0
        available = acct.available if acct else 0
        pos_profit = acct.position_profit if acct else 0
        start_eq = self._risk.daily_start_eq
        daily_abs = eq - start_eq
        daily_pct = self._risk.daily_pnl_pct * 100
        dd_pct = self._risk.drawdown_pct * 100
        peak_eq = self._risk.peak_equity

        account_info = (
            f"**📊 账户概览**\n"
            f"日初权益: {start_eq:,.0f}\n"
            f"当前权益: {eq:,.0f}\n"
            f"可用资金: {available:,.0f}\n"
            f"日盈亏: {daily_abs:+,.0f} ({daily_pct:+.2f}%)\n"
            f"峰值权益: {peak_eq:,.0f} | 回撤: {dd_pct:.2f}%"
        )

        bpos = self.get_position(p.instrument_id)
        broker_pos = bpos.net_position if bpos else 0
        if self._own_pos > 0:
            position_info = (
                f"\n\n**📋 自管持仓**\n"
                f"合约: {p.instrument_id} | 方向: 多 | 手数: {self._own_pos}\n"
                f"均价: {self.avg_price:.1f} | 峰值: {self.peak_price:.1f}\n"
                f"账户总持仓: {broker_pos}手 (差额 {broker_pos - self._own_pos} 不管)\n"
                f"浮盈: {pos_profit:+,.0f}"
            )
        else:
            position_info = (
                f"\n\n**📋 持仓明细**\n"
                f"自管持仓: 0 | 账户总持仓: {broker_pos}手 (非本策略)"
            )

        if self._today_trades:
            trade_info = f"\n\n**📝 今日交易 ({len(self._today_trades)}笔)**\n"
            trade_info += "| 时间 | 操作 | 手数 | 价格 | own_pos变化 |\n|--|--|--|--|--|\n"
            for t in self._today_trades[-20:]:
                trade_info += (f"| {t['time']} | {t['action']} | "
                               f"{t['lots']}({t['side']}) | {t['price']} | "
                               f"{t['before']}->{t['after']} |\n")
        else:
            trade_info = "\n\n**📝 今日交易**\n无交易"

        perf_info = (
            f"\n\n**📈 绩效统计**\n"
            f"{self._perf.format_report(p.instrument_id)}\n"
            f"{self._slip.format_report()}"
        )

        feishu("daily_review", p.instrument_id,
               f"**{STRATEGY_NAME} 每日总结**\n"
               f"交易日: {self._current_td} | max_lots={p.max_lots}\n\n"
               f"{account_info}{position_info}{trade_info}{perf_info}")

    def _push_widget(self, kline, sp=0.0):
        if self.widget is None:
            self._widget_err_count += 1
            if self._widget_err_count <= 3 or self._widget_err_count % 500 == 0:
                self._log(
                    f"[WIDGET] self.widget=None (累计 {self._widget_err_count} 次)"
                )
            return

        main = self.main_indicator_data
        sub = self.sub_indicator_data
        payload = {"kline": kline, "signal_price": float(sp)}
        for name, val in {**main, **sub}.items():
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = 0.0
            try:
                payload[name] = float(val)
            except (TypeError, ValueError):
                payload[name] = 0.0

        try:
            self.widget.recv_kline(payload)
            self._widget_ok_count += 1
            self.state_map.ui_push_count = self._widget_ok_count

            if self._widget_ok_count == 1:
                self._log(
                    f"[WIDGET] 首次推送成功! payload keys={list(payload.keys())}"
                )
            elif self._widget_ok_count <= 5:
                self._log(
                    f"[WIDGET #{self._widget_ok_count}] "
                    f"signal_price={sp} "
                    f"DC_U={payload.get('DC_U', 0):.1f} "
                    f"MFI={payload.get('MFI', 0):.1f}"
                )
            elif self._widget_ok_count % 500 == 0:
                self._log(f"[WIDGET #{self._widget_ok_count}] 累计推送 OK")
        except Exception as e:
            self._widget_err_count += 1
            if self._widget_err_count <= 3 or self._widget_err_count % 500 == 0:
                self._log(
                    f"[WIDGET] recv_kline 异常: {type(e).__name__}: {e}"
                )

    # ══════════════════════════════════════════════════════════════════════
    #  回调 — 成交 / 订单 / 错误
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData, log=True):
        """核心: 只处理 self._my_oids 里的成交, broker 其他成交完全忽略."""
        super().on_trade(trade, log=True)
        oid = trade.order_id
        self.order_id.discard(oid)
        self._om.on_fill(oid)

        self._log(
            f"[ON_TRADE] oid={oid} direction={trade.direction!r} "
            f"offset={trade.offset!r} price={trade.price} vol={trade.volume}"
        )

        if oid not in self._my_oids:
            self._log(f"[ON_TRADE] oid={oid} 非本策略, 跳过 (vol={trade.volume})")
            return

        raw = str(trade.direction).lower()
        direction = "buy" if raw in ("buy", "0", "买") else "sell"
        self.state_map.last_direction = f"{trade.direction!r}→{direction}"

        slip = self._slip.on_fill(trade.price, trade.volume, direction)
        if slip != 0:
            self._log(f"[滑点] {slip:.1f}ticks")

        if direction == "buy":
            old = self._own_pos
            new = old + trade.volume
            if new > MAX_LOTS:
                self._log(f"[WARN] own_pos={new} > MAX_LOTS, 截断")
                new = MAX_LOTS
            if old > 0 and self.avg_price > 0:
                self.avg_price = (self.avg_price * old + trade.price * trade.volume) / new
            else:
                self.avg_price = trade.price
            if trade.price > self.peak_price or self.peak_price == 0:
                self.peak_price = trade.price
            self._own_pos = new
            self._rec("建仓/加仓", trade.volume, "买", trade.price, old, new)
            self._log(
                f"[FILL BUY] {trade.volume}手 @ {trade.price:.1f} "
                f"own_pos {old}→{new} avg={self.avg_price:.1f}"
            )
        else:
            old = self._own_pos
            new = max(0, old - trade.volume)
            self._own_pos = new
            if new == 0:
                self.avg_price = 0.0
                self.peak_price = 0.0
            self._rec("平仓/减仓", trade.volume, "卖", trade.price, old, new)
            self._log(
                f"[FILL SELL] {trade.volume}手 @ {trade.price:.1f} "
                f"own_pos {old}→{new}"
            )

        self.state_map.own_pos = self._own_pos
        self.state_map.my_oids_n = len(self._my_oids)
        self._save()
        self.update_status_bar()

    def on_order(self, order: OrderData):
        mine = "(本策略)" if order.order_id in self._my_oids else "(非本策略)"
        self._log(
            f"[ON_ORDER] oid={order.order_id} status={order.status} "
            f"direction={order.direction} offset={order.offset} "
            f"price={order.price} total={order.total_volume} "
            f"traded={order.traded_volume} cancel={order.cancel_volume} {mine}"
        )
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        mine = "(本策略)" if order.order_id in self._my_oids else "(非本策略)"
        self._log(
            f"[ON_ORDER_CANCEL] oid={order.order_id} "
            f"cancel_volume={order.cancel_volume} {mine}"
        )
        super().on_order_cancel(order)
        self.order_id.discard(order.order_id)
        self._om.on_cancel(order.order_id)

    def on_error(self, error):
        self._log(f"[ON_ERROR] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
        throttle_on_error(self, error)
