"""
================================================================================
  I_Bidir_M1_ICT_v6 — ICT v6 (2022 Model state machine) on 铁矿 DCE
================================================================================

  来源: ~/Desktop/ICT/ict_v3/{model.py, structures.py, bias.py, sessions_cn.py}
  原始: cd-Sharpe +4.68 (NQ baseline) / +8.31 (CN 8-product portfolio)
  Audit: ICT framework 验证 80+ 次, 在 44 CN futures 上 42/44 profitable

  框架 (7 步 state machine):
    1. D1 bias  — bull / bear / neutral (从 1m 历史 resample 到 D1)
    2. KZ 时间窗口 — 09:00-10:00 / 13:30-14:30 / 21:00-22:00 (CN 适配)
    3. Sweep — long 找被 pierce + reclaim 的 swing low (short 镜像)
    4. Displacement bar + 3-bar FVG — body ≥ 1×ATR + FVG ≥ 0.2×ATR
    5. OTE 70.5% retracement (sweep_low → displacement_high) 挂限价
    6. (MVP 跳过 reactive entry; 直接 limit fill)
    7. R-ladder + chandelier trail
       - T1 +0.5R: 平 1/3, stop 移 BE
       - T2 +1.5R: 平 1/3
       - T3 +3R:   平最后 1/3 / runner chandelier ATR trail

  风险管理:
    - 0.5% equity per trade
    - max_contracts = 5
    - max 3 trades / day
    - daily_stop_r = -2.0 / daily_lock_r = +3.0
    - hard cutoff 14:50 (日盘) / 22:50 (夜盘) → 强平
    - max_hold_bars = 240 (4 小时)
================================================================================
"""
import os
import sys
import time as _time
from datetime import date, datetime, time, timedelta

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
from modules.persistence import load_state, save_state
from modules.pricing import AggressivePricer
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day

# ICT 子包: 加 src/ 或 pyStrategy/ 到 sys.path 以 import ICT.modules
_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in [os.path.dirname(_DIR), os.path.dirname(os.path.dirname(_DIR))]:
    if os.path.isdir(os.path.join(_p, "ICT", "modules")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

from ICT.modules.bias import DailyBias, compute_daily_bias  # noqa: E402
from ICT.modules.state_machine import (  # noqa: E402
    Action, ActiveTrade, OTESetup, V6Config, V6StateMachine,
)
from ICT.modules.timezones import to_python_datetime  # noqa: E402

# D1 bias 充足暖机阈值: lookback_days=20 + warmup=14 = 34 天 1m bars
# CN 期货一天大约 240 根 1m (日盘 4h + 夜盘 4h × 60). 34 天 ≈ 8160 根 (保守 5000)
# 实盘 push_history 通常 ≥ 1 万根 OK; 不够时仅警告不 crash
MIN_HISTORY_BARS_FOR_BIAS = 5000


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "I_Bidir_M1_ICT_v6"
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
    return 60


def _resample_1m_to_d1(
    timestamps: list[datetime],
    opens: list[float], highs: list[float],
    lows: list[float], closes: list[float],
) -> tuple[list[date], np.ndarray, np.ndarray, np.ndarray]:
    """把 1m bars resample 到 D1 (CN trading day, 21:00 rollover).

    CN 商品期货 trading day 定义: 21:00 之后的 bar 归属下一日.
    返回 (d1_dates, d1_highs, d1_lows, d1_closes).
    """
    if not timestamps:
        return [], np.empty(0), np.empty(0), np.empty(0)

    bucket: dict[date, dict] = {}
    for ts, o, h, l, c in zip(timestamps, opens, highs, lows, closes):
        # 21:00+ 归属下一天
        td = (ts + timedelta(days=1)).date() if ts.hour >= 21 else ts.date()
        if td not in bucket:
            bucket[td] = {"o": o, "h": h, "l": l, "c": c, "first_ts": ts}
        else:
            b = bucket[td]
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
    sorted_dates = sorted(bucket.keys())
    out_h = np.array([bucket[d]["h"] for d in sorted_dates], dtype=float)
    out_l = np.array([bucket[d]["l"] for d in sorted_dates], dtype=float)
    out_c = np.array([bucket[d]["c"] for d in sorted_dates], dtype=float)
    return sorted_dates, out_h, out_l, out_c


# ══════════════════════════════════════════════════════════════════════════════
#  PARAMS / STATE
# ══════════════════════════════════════════════════════════════════════════════


class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2609", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")
    # 实盘默认 1 (Phase 1 验证), 升级到 5 通过 UI 修改即可 (MAX_LOTS=5 是代码硬上限)
    max_lots: int = Field(default=1, title="最大持仓")
    capital: float = Field(default=1_000_000, title="配置资金")
    risk_per_trade_pct: float = Field(default=0.005, title="每笔风险百分比")
    max_trades_per_day: int = Field(default=3, title="每日最大交易数")
    flatten_minutes: int = Field(default=5, title="即将收盘提示分钟")
    sim_24h: bool = Field(default=False, title="24H模拟盘模式")
    enable_short_setups: bool = Field(default=True, title="启用做空")
    # takeover_lots: 启动时手动指定接管手数 (0=按 state 恢复; >0=手动接管, 覆盖 state)
    takeover_lots: int = Field(default=0, title="启动接管手数")


class State(BaseState):
    # State machine
    sm_state: str = Field(default="IDLE", title="状态机")
    cur_bias: str = Field(default="neutral", title="今日bias")
    pd_zone: str = Field(default="---", title="PD区")
    active_kz: str = Field(default="---", title="当前KZ")
    # 持仓
    own_pos: int = Field(default=0, title="自管持仓")
    broker_pos: int = Field(default=0, title="账户总持仓")
    direction: str = Field(default="---", title="方向")
    avg_price: float = Field(default=0.0, title="均价")
    initial_stop: float = Field(default=0.0, title="初始止损")
    cur_stop: float = Field(default=0.0, title="当前止损")
    target_price: float = Field(default=0.0, title="目标价")
    partials_done: int = Field(default=0, title="已平次数")
    # OTE
    ote_limit: float = Field(default=0.0, title="OTE限价")
    ote_low: float = Field(default=0.0, title="OTE低端")
    ote_high: float = Field(default=0.0, title="OTE高端")
    # 当日
    trades_today: int = Field(default=0, title="今日交易")
    pnl_r_today: float = Field(default=0.0, title="今日R")
    # UI 实时
    last_price: float = Field(default=0.0, title="最新价")
    last_bid1: float = Field(default=0.0, title="买一")
    last_ask1: float = Field(default=0.0, title="卖一")
    last_atr: float = Field(default=0.0, title="ATR")
    last_tick_time: str = Field(default="---", title="最后tick")
    heartbeat: int = Field(default=0, title="心跳")
    tick_count: int = Field(default=0, title="tick计数")
    bar_count: int = Field(default=0, title="bar计数")
    # 状态
    trading_day: str = Field(default="", title="交易日")
    session: str = Field(default="---", title="时段")
    last_action: str = Field(default="---", title="上次操作")
    last_setup_reason: str = Field(default="---", title="setup原因")
    skip_reason: str = Field(default="---", title="skip原因")
    slippage: str = Field(default="---", title="滑点")


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY
# ══════════════════════════════════════════════════════════════════════════════


class I_Bidir_M1_ICT_v6(BaseStrategy):
    """ICT v6 — 铁矿双向 1m 策略."""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        # ICT state machine
        self._sm: V6StateMachine | None = None
        self._biases: list[DailyBias] = []

        # 自管持仓
        self._own_pos: int = 0
        self._my_oids: set = set()
        self.avg_price = 0.0
        self._takeover_pending = False

        # Pending order tracking — oid → action metadata
        self._pending_open: dict | None = None     # {oid, contracts, ...}
        self._pending_partials: dict = {}          # oid → {"qty", "target_R"}
        self._pending_close: dict | None = None    # {oid, contracts, reason}

        self.order_id = set()

        # 账户
        self._investor_id = ""
        self._current_td = ""
        self._rollover_checked = False

        # 模块
        self._guard: SessionGuard | None = None
        self._slip: SlippageTracker | None = None
        self._hb: HeartbeatMonitor | None = None
        self._om = OrderMonitor()
        self._pricer: AggressivePricer | None = None
        self._multiplier = 100  # I default

        # 诊断计数器
        self._tick_count = 0
        self._bar_count = 0
        self._widget_err_count = 0

    def _log(self, msg: str) -> None:
        self.output(msg)
        try:
            print(f"[{STRATEGY_NAME}] {msg}", flush=True)
            sys.stdout.flush()
        except Exception:
            pass

    @property
    def main_indicator_data(self):
        """主图: OTE 限价 / 当前止损 / 目标价 / 均价."""
        return {
            "OTE": self.state_map.ote_limit,
            "Stop": self.state_map.cur_stop,
            "Target": self.state_map.target_price,
            "Entry": self.state_map.avg_price,
        }

    @property
    def sub_indicator_data(self):
        return {"ATR": self.state_map.last_atr}

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

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
        self._log(
            f"[ON_START] modules ok multiplier={self._multiplier} "
            f"tick={get_tick_size(p.instrument_id)} sim_24h={p.sim_24h}"
        )

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self._log("[ON_START] KLineGenerator created, push_history_data...")
        self.kline_generator.push_history_data()
        producer = self.kline_generator.producer
        n_history = len(producer.close)
        self._log(f"[ON_START] push_history 完成 producer_bars={n_history}")

        # 1m → D1 resample → biases (使用 producer 已 filled 的历史数据)
        timestamps_1m: list[datetime] = []
        skipped = 0
        for dt in producer.datetime:
            py_dt = to_python_datetime(dt)
            if py_dt is None:
                skipped += 1
                py_dt = datetime.now()  # last-resort, log warning below
            timestamps_1m.append(py_dt)
        if skipped > 0:
            self._log(f"[ON_START WARN] {skipped} bar timestamps 无法解析, 已 fallback now()")

        opens_1m = list(map(float, producer.open))
        highs_1m = list(map(float, producer.high))
        lows_1m = list(map(float, producer.low))
        closes_1m = list(map(float, producer.close))

        # History 充足性检查
        if n_history < MIN_HISTORY_BARS_FOR_BIAS:
            self._log(
                f"[ON_START WARN] history bars {n_history} < {MIN_HISTORY_BARS_FOR_BIAS}, "
                f"D1 bias 可能全 neutral → 永不开仓. 建议增大 push_history 深度."
            )
            feishu("error", p.instrument_id,
                   f"**ICT 启动警告** {STRATEGY_NAME}\n"
                   f"history bars 仅 {n_history} 根 (建议 ≥ {MIN_HISTORY_BARS_FOR_BIAS}).\n"
                   f"D1 bias 可能不暖机 → 不开仓.")

        d1_dates, d1_h, d1_l, d1_c = _resample_1m_to_d1(
            timestamps_1m, opens_1m, highs_1m, lows_1m, closes_1m,
        )
        self._biases = compute_daily_bias(d1_h, d1_l, d1_c, d1_dates)
        n_directional = sum(1 for b in self._biases if b.bias != "neutral")
        self._log(
            f"[ON_START] D1 bias built: {len(d1_dates)} D1 bars from "
            f"{n_history} 1m bars, {n_directional} non-neutral"
        )

        # State machine init (不灌 push_history_bars — callback 阶段会自然 append 每根 history bar)
        cfg = V6Config(
            starting_capital=p.capital,
            risk_per_trade_pct=p.risk_per_trade_pct,
            max_contracts=p.max_lots,
            max_trades_per_day=p.max_trades_per_day,
            enable_short_setups=p.enable_short_setups,
        )
        self._sm = V6StateMachine(
            config=cfg, biases=self._biases,
            tick_size=get_tick_size(p.instrument_id),
            multiplier=self._multiplier,
        )
        self._log(
            f"[ON_START] state machine init: state={self._sm.state} "
            f"(buffer 由 callback 阶段 append, 不在 on_start 灌入)"
        )

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id
        self._log(f"[ON_START] investor_id={self._investor_id}")

        saved = load_state(STRATEGY_NAME)
        if saved:
            self._own_pos = int(saved.get("own_pos", 0))
            self._my_oids = set(saved.get("my_oids", []))
            self.avg_price = saved.get("avg_price", 0.0)
            self._current_td = saved.get("trading_day", "")
            self._log(
                f"[ON_START 恢复] own_pos={self._own_pos} avg={self.avg_price:.1f}"
            )

        acct = self._get_account()
        if acct:
            self._log(
                f"[ON_START 账户] balance={acct.balance:.0f} "
                f"available={acct.available:.0f}"
            )

        pos = self.get_position(p.instrument_id)
        broker_pos = pos.net_position if pos else 0
        self.state_map.own_pos = self._own_pos
        self.state_map.broker_pos = broker_pos
        self._log(f"[ON_START 持仓] own_pos={self._own_pos} broker_pos={broker_pos}")

        # Takeover override
        if p.takeover_lots > 0:
            self._own_pos = int(p.takeover_lots)
            self._my_oids = set()
            self._takeover_pending = True
            self.avg_price = 0.0
            self.state_map.own_pos = self._own_pos
            self._log(
                f"[ON_START TAKEOVER] 手动接管 {self._own_pos} 手 "
                f"(broker_pos={broker_pos}, 底仓={broker_pos - self._own_pos} 手不动)"
            )
            feishu("start", p.instrument_id,
                   f"**TAKEOVER 启动** {STRATEGY_NAME}\n"
                   f"接管 {self._own_pos} 手 / broker {broker_pos} 手")

        if self._own_pos == 0:
            self.avg_price = 0.0

        if not self._current_td:
            self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        self._log("[ON_START] super().on_start()")
        super().on_start()

        self._log(
            f"=== 启动完成 === | {p.instrument_id} {p.kline_style} | "
            f"max_lots={p.max_lots} | own_pos={self._own_pos} broker_pos={broker_pos} | "
            f"session={self._guard.get_status()}"
        )
        feishu("start", p.instrument_id,
               f"**策略启动** {STRATEGY_NAME} (ICT v6)\n"
               f"合约 {p.instrument_id} {p.kline_style}\n"
               f"自管持仓: {self._own_pos}手 (账户总: {broker_pos}手)\n"
               f"max_lots: {p.max_lots} | risk: {p.risk_per_trade_pct*100:.1f}% | "
               f"双向: {'是' if p.enable_short_setups else '否'}")

    def on_stop(self):
        self._save()
        self._log(
            f"[ON_STOP] tick_count={self._tick_count} bar_count={self._bar_count} "
            f"own_pos={self._own_pos}"
        )
        feishu("shutdown", self.params_map.instrument_id,
               f"**策略停止** {STRATEGY_NAME}\n自管持仓: {self._own_pos}手")
        super().on_stop()

    def _save(self) -> None:
        state = {
            "own_pos": self._own_pos,
            "my_oids": list(self._my_oids),
            "avg_price": self.avg_price,
            "trading_day": self._current_td,
        }
        try:
            save_state(STRATEGY_NAME, state)
        except Exception as e:
            self._log(f"[SAVE 异常] {type(e).__name__}: {e}")

    # ══════════════════════════════════════════════════════════════════════
    #  Tick
    # ══════════════════════════════════════════════════════════════════════

    def on_tick(self, tick: TickData):
        super().on_tick(tick)

        # Takeover 兜底
        if self._takeover_pending and tick.last_price > 0:
            self.avg_price = float(tick.last_price)
            self._takeover_pending = False
            self._log(
                f"[TAKEOVER FIRST TICK] avg_price={tick.last_price:.2f} "
                f"own_pos={self._own_pos}"
            )

        self._tick_count += 1
        self.state_map.tick_count = self._tick_count
        self.state_map.heartbeat = self._tick_count % 1000

        self.state_map.last_price = float(tick.last_price)
        self.state_map.last_bid1 = float(tick.bid_price1)
        self.state_map.last_ask1 = float(tick.ask_price1)
        try:
            self.state_map.last_tick_time = tick.datetime.strftime("%H:%M:%S")
        except Exception:
            self.state_map.last_tick_time = str(tick.datetime)

        self.state_map.session = self._guard.get_status() if self._guard else "---"

        # 每 10 tick 刷新 UI
        if self._tick_count % 10 == 0:
            try:
                self.update_status_bar()
            except Exception as e:
                self._widget_err_count += 1
                if self._widget_err_count == 1:
                    self._log(f"[WIDGET 异常] {type(e).__name__}: {e}")

        # State machine: tick 级 stop / target / fill 检查
        if self._sm is None:
            return
        try:
            self._on_tick_sm(tick)
        except Exception as e:
            self._log(f"[on_tick_sm 异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**ICT on_tick 异常**\n{type(e).__name__}: {e}")

    def _on_tick_sm(self, tick: TickData) -> None:
        if not self.trading or self._guard is None or not self._guard.should_trade():
            return
        if self._pending_close:
            return  # 等 broker 平仓成交回调

        sm = self._sm
        ts = to_python_datetime(tick.datetime) or datetime.now()
        last_price = float(tick.last_price)

        # OTE_PENDING 状态: broker 自动处理 fill, 这里不需要做事
        # FILLED 状态: 检查 stop / target / cutoff
        action = sm.on_tick(ts, last_price)

        if action.kind == "exit_full":
            self._exec_full_close(action)
        elif action.kind == "partial_exit":
            self._exec_partial_close(action)

    def _exec_place_limit(self, action: Action) -> None:
        """OTE setup 触发: 立即挂 OTE 限价单 (broker 自动等价 fill)."""
        p = self.params_map
        d = action.direction
        side = "buy" if d == 1 else "sell"
        # OTE 是 passive limit: 直接用策略给的 OTE 70.5% 价位挂单
        # 不需要 AggressivePricer 穿盘口 — 我们就要 broker 等到价格回到 OTE 才 fill
        limit_px = float(action.price)
        self._log(
            f"[EXEC_OPEN] send_order {side} {action.contracts}手 @ {limit_px} (limit, passive) "
            f"OTE_target={limit_px:.2f} stop={action.new_stop:.2f}"
        )
        self._slip.set_signal_price(limit_px)
        oid = self.send_order(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=action.contracts, price=limit_px,
            order_direction=side, offset="open",
        )
        if oid:
            self._my_oids.add(oid)
            self.order_id.add(oid)
            self._om.register(oid)
            self._pending_open = {
                "oid": oid, "contracts": action.contracts,
                "direction": d, "stop": action.new_stop,
                "target": action.metadata.get("target", 0.0),
            }
        else:
            self._log(f"[EXEC_OPEN] send_order 返回 oid=None, 跳过")

    def _exec_partial_close(self, action: Action) -> None:
        """R-ladder partial exit."""
        p = self.params_map
        d = action.direction
        close_side = "sell" if d == 1 else "buy"
        target_R = action.metadata.get("target_R", 0.0)
        urgency = "urgent" if target_R >= 3.0 else "normal"
        send_px = self._pricer.aggressive(action.price, close_side, urgency=urgency)
        self._log(
            f"[EXEC_PARTIAL] {close_side} {action.contracts}手 @ {send_px} "
            f"R={target_R:.1f} reason={action.reason}"
        )
        self._slip.set_signal_price(action.price)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=action.contracts, price=send_px, order_direction=close_side,
        )
        if oid:
            self._my_oids.add(oid)
            self.order_id.add(oid)
            self._om.register(oid)
            self._pending_partials[oid] = {
                "qty": action.contracts, "target_R": target_R,
                "new_stop": action.new_stop,
            }

    def _exec_full_close(self, action: Action) -> None:
        """全平 (stop / max_hold / hard_cutoff)."""
        p = self.params_map
        d = action.direction
        close_side = "sell" if d == 1 else "buy"
        send_px = self._pricer.aggressive(action.price, close_side, urgency="urgent")
        self._log(
            f"[EXEC_CLOSE] {close_side} {action.contracts}手 @ {send_px} reason={action.reason}"
        )
        self._slip.set_signal_price(action.price)
        # 撤所有挂单 (避免 OTE pending 干扰)
        for oid in list(self.order_id):
            self.cancel_order(oid)
        oid = self.auto_close_position(
            exchange=p.exchange, instrument_id=p.instrument_id,
            volume=action.contracts, price=send_px, order_direction=close_side,
        )
        if oid:
            self._my_oids.add(oid)
            self.order_id.add(oid)
            self._om.register(oid)
            self._pending_close = {
                "oid": oid, "contracts": action.contracts,
                "reason": action.reason,
            }

    # ══════════════════════════════════════════════════════════════════════
    #  Bar callback
    # ══════════════════════════════════════════════════════════════════════

    def callback(self, kline: KLineData) -> None:
        self._on_bar(kline, is_realtime=False)

    def real_time_callback(self, kline: KLineData) -> None:
        self._on_bar(kline, is_realtime=True)

    def _on_bar(self, kline: KLineData, is_realtime: bool) -> None:
        self._bar_count += 1
        self.state_map.bar_count = self._bar_count
        if self._sm is None:
            return

        # 把新 bar push 到 state machine buffer
        ts = to_python_datetime(kline.datetime) or datetime.now()
        self._sm.append_bar(
            float(kline.open), float(kline.high), float(kline.low), float(kline.close),
            ts,
        )
        self.state_map.last_atr = self._sm.cur_atr()

        # 实盘 bar 才扫 setup (历史 bar 仅 build buffer + biases, 不发新单)
        if not is_realtime:
            return

        # 取当前 bias 显示
        from ICT.modules.bias import bias_for_date
        cur_bias = bias_for_date(self._biases, ts.date())
        if cur_bias:
            self.state_map.cur_bias = cur_bias.bias
            self.state_map.pd_zone = cur_bias.pd_zone

        from ICT.modules.sessions_cn import get_active_kill_zone
        kz = get_active_kill_zone(ts) or "—"
        self.state_map.active_kz = kz

        # 当日状态
        ds = self._sm._ds(ts)
        self.state_map.trades_today = ds["trades_today"]
        self.state_map.pnl_r_today = ds["pnl_r"]

        self._log(
            f"[ON_BAR{'实盘' if is_realtime else '历史'}] bar#{self._bar_count} "
            f"close={kline.close} bias={self.state_map.cur_bias} kz={kz} "
            f"sm={self._sm.state}"
        )

        # State machine: 1m bar close 扫 setup / 处理 chandelier trail
        try:
            # FILLED 状态: chandelier trail 检查
            if self._sm.state == "FILLED":
                trail_action = self._sm.on_bar_close_filled()
                if trail_action.kind == "trail_update":
                    self._log(f"[CHANDELIER] {trail_action.reason}")
                    self.state_map.cur_stop = trail_action.new_stop
                return

            # IDLE 状态: 扫 setup
            equity = self._current_equity()
            action = self._sm.on_bar(ts, equity)
            self.state_map.sm_state = self._sm.state

            if action.kind == "place_limit":
                # 撤所有挂单 (不应该有 pending 但保险)
                for oid in list(self.order_id):
                    self.cancel_order(oid)
                self.state_map.ote_limit = action.price
                self.state_map.ote_low = action.metadata.get("ote_low", 0.0)
                self.state_map.ote_high = action.metadata.get("ote_high", 0.0)
                self.state_map.cur_stop = action.metadata.get("stop", 0.0)
                self.state_map.target_price = action.metadata.get("target", 0.0)
                self.state_map.last_setup_reason = action.reason
                self._log(f"[PLACE_LIMIT] {action.reason}")
                # 立即挂限价单 (broker 自动等价 fill, 我们等 on_trade 回调)
                self._exec_place_limit(action)
            elif action.kind == "cancel_limit":
                for oid in list(self.order_id):
                    self.cancel_order(oid)
                self.state_map.ote_limit = 0.0
                self._log(f"[CANCEL_LIMIT] {action.reason}")

        except Exception as e:
            self._log(f"[on_bar_sm 异常] {type(e).__name__}: {e}")
            feishu("error", self.params_map.instrument_id,
                   f"**ICT on_bar 异常**\n{type(e).__name__}: {e}")

    def _current_equity(self) -> float:
        acct = self._get_account()
        if acct:
            return float(acct.balance)
        return self.params_map.capital

    # ══════════════════════════════════════════════════════════════════════
    #  Trade callbacks
    # ══════════════════════════════════════════════════════════════════════

    def on_trade(self, trade: TradeData) -> None:
        super().on_trade(trade)
        if self._sm is None:
            self._log("[ON_TRADE] _sm=None (on_start 中途失败?), 忽略")
            return
        oid = trade.order_id
        if oid not in self._my_oids:
            self._log(f"[ON_TRADE] oid={oid} 非本策略, 跳过")
            return

        direction = "buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell"
        offset_str = str(trade.offset)
        is_open = "open" in offset_str.lower() or offset_str == "0"
        vol = int(trade.volume)
        price = float(trade.price)
        # TradeData 时间字段名不固定 (PythonGO 版本差异): 试 trade_time / datetime / time
        raw_ts = (getattr(trade, "trade_time", None) or
                  getattr(trade, "datetime", None) or
                  getattr(trade, "time", None))
        ts = to_python_datetime(raw_ts) or datetime.now()

        self._log(
            f"[ON_TRADE] oid={oid} {direction!r} offset={offset_str!r} price={price} vol={vol}"
        )

        slip = self._slip.on_fill(price, vol, direction)
        if slip != 0:
            self._log(f"[滑点] {slip:.1f}ticks")

        if is_open and self._pending_open and oid == self._pending_open["oid"]:
            # 开仓成交 — 支持 broker 拆批 (同 oid 多笔 ON_TRADE)
            old = self._own_pos
            self._own_pos += vol
            if old == 0:
                self.avg_price = price
            else:
                self.avg_price = (self.avg_price * old + price * vol) / self._own_pos
            self.state_map.own_pos = self._own_pos
            self.state_map.avg_price = self.avg_price
            self.state_map.direction = "long" if direction == "buy" else "short"
            self.state_map.last_action = f"OPEN {direction.upper()} {vol}@{price}"
            # 通知 state machine (sm 内部 handle 拆批: 第 1 笔创建 trade, 后续累加)
            self._sm.confirm_open(price, vol, ts)
            self.state_map.sm_state = self._sm.state
            self.state_map.initial_stop = self._sm.trade.initial_stop if self._sm.trade else 0.0
            self.state_map.cur_stop = self._sm.trade.stop_price if self._sm.trade else 0.0
            # 只在 broker 完全 fill 时清空 _pending_open (避免拆批第 2 笔走 "未匹配" 分支)
            expected = self._pending_open["contracts"]
            filled = self._sm.trade.initial_contracts if self._sm.trade else 0
            self._log(
                f"[OPEN] own_pos→{self._own_pos} avg={self.avg_price:.2f} "
                f"stop={self.state_map.cur_stop:.2f} "
                f"filled {filled}/{expected}"
            )
            if filled >= expected:
                self._pending_open = None
            self._save()
            return

        if not is_open and oid in self._pending_partials:
            # R-ladder 部分平仓
            meta = self._pending_partials.pop(oid)
            self._own_pos = max(0, self._own_pos - vol)
            self.state_map.own_pos = self._own_pos
            self.state_map.last_action = f"PARTIAL {direction.upper()} {vol}@{price} R={meta['target_R']}"
            self._sm.confirm_partial(price, vol, meta["target_R"])
            self.state_map.partials_done = (len(self._sm.trade.partial_exits)
                                            if self._sm.trade else 0)
            self.state_map.cur_stop = self._sm.trade.stop_price if self._sm.trade else 0.0
            self._log(
                f"[PARTIAL] own_pos→{self._own_pos} R={meta['target_R']:.1f} "
                f"new_stop={self.state_map.cur_stop:.2f}"
            )
            return

        if not is_open and self._pending_close and oid == self._pending_close["oid"]:
            # 全平
            entry = self.avg_price
            d = self._sm.trade.direction if self._sm.trade else 0
            self._own_pos = max(0, self._own_pos - vol)
            self.state_map.own_pos = self._own_pos
            reason = self._pending_close["reason"]
            summary = self._sm.confirm_close(price, vol, reason, ts)
            self.state_map.last_action = f"CLOSE {direction.upper()} {vol}@{price} ({reason})"
            self.state_map.sm_state = self._sm.state
            self.state_map.partials_done = 0
            self.state_map.pnl_r_today = self._sm._ds(ts)["pnl_r"]
            self._log(
                f"[CLOSE] own_pos→{self._own_pos} entry={entry:.2f} exit={price:.2f} "
                f"reason={reason} R={summary.get('total_R', 0.0):.2f}"
            )
            if self._own_pos == 0:
                self.avg_price = 0.0
                self.state_map.avg_price = 0.0
                self.state_map.cur_stop = 0.0
                self.state_map.target_price = 0.0
                self.state_map.ote_limit = 0.0
                self.state_map.direction = "—"
            self._pending_close = None
            self._save()
            return

        self._log(f"[ON_TRADE] oid={oid} 未匹配 pending action, 忽略")

    def on_order(self, order: OrderData) -> None:
        super().on_order(order)

    def on_order_cancel(self, order: OrderData) -> None:
        oid = order.order_id
        cancel_vol = getattr(order, "cancel_volume", 0)
        self._log(f"[ON_ORDER_CANCEL] oid={oid} cancel_vol={cancel_vol}")
        self.order_id.discard(oid)
        self._om.unregister(oid)
        if self._pending_open and oid == self._pending_open["oid"]:
            self._pending_open = None
            # state machine 应该已经在 on_bar 里 cancel 了
        if oid in self._pending_partials:
            del self._pending_partials[oid]
        if self._pending_close and oid == self._pending_close["oid"]:
            self._pending_close = None

    def on_error(self, error) -> None:
        throttle_on_error(self, error)
