"""ICT v6 state machine — IDLE → OTE_PENDING → FILLED → IDLE.

封装核心策略 logic, 让 PythonGO strategy file 只负责:
    - on_start: load D1 bars + build biases, init state machine
    - on_bar (1m close): 调 sm.on_bar() → 收 ActionResult → 转译为 send_order
    - on_tick: 调 sm.on_tick() → 检查 stop/target/cutoff → 转译为 auto_close
    - on_trade: 调 sm.on_trade(direction, price, vol) 更新状态

源: ~/Desktop/ICT/ict_v3/model.py simulate_v3 主循环, 抽象成 stateful class.

简化版 (MVP):
    ✓ Bidirectional long+short
    ✓ Sweep + displacement + FVG (single swing 不带 EQL cluster priority)
    ✓ Reactive entry off (limit fill at OTE 70.5%, 不需 engulfing 确认)
    ✓ R-ladder 0.5R / 1.5R / 3R (33%/33%/runner)
    ✓ Chandelier trail (HH−1×ATR for long, LL+1×ATR for short)
    ✓ Per-day limits (max 3 trades, daily_stop_r −2.0, daily_lock_r +3.0)
    ✓ Hard cutoff (CN 14:50 + 22:50)
    ✓ Max hold (240 1m bars = 4h)
    ✗ EQL/EQH cluster sweep priority (Phase 2)
    ✗ Multi-tier OTE (Phase 2)
    ✗ Strict MSS (Phase 2)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import numpy as np

from .bias import DailyBias, bias_for_date
from .sessions_cn import (
    DEFAULT_CN_ALLOWED_KZS,
    can_trade,
    get_active_kill_zone,
    in_lunch_break,
    past_hard_cutoff,
)
from .structures import (
    DisplacementInfo,
    SweepInfo,
    detect_bearish_displacement_after_sweep,
    detect_bullish_displacement_after_sweep,
    detect_intraday_swings,
    detect_swept_high,
    detect_swept_low,
    wilder_atr,
)


State = Literal["IDLE", "OTE_PENDING", "FILLED"]


# ════════════════════════════════════════════════════════════════════════════
#  Config
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class V6Config:
    """ICT v6 主策略参数 (源 V3Config, 只保留 v6 必需 + MVP 简化)."""
    # ── 风险 ──
    starting_capital: float = 1_000_000.0
    risk_per_trade_pct: float = 0.005      # 0.5% per trade
    min_rr: float = 3.0
    max_contracts: int = 5
    max_trades_per_day: int = 3
    daily_stop_r: float = -2.0
    daily_lock_r: float = 3.0
    # ── 双向 ──
    enable_short_setups: bool = True
    # ── PD-zone gate ──
    require_discount_for_long: bool = True
    pd_threshold: float = 0.70
    eq_band: float = 0.10
    # ── OTE ──
    ote_low_pct: float = 0.62
    ote_mid_pct: float = 0.705              # 70.5% sweet spot
    ote_high_pct: float = 0.79
    ote_fill_max_bars: int = 60
    # ── R-ladder ──
    enable_r_ladder: bool = True
    r_target_1: float = 0.5
    r_target_2: float = 1.5
    r_target_3: float = 3.0
    r_share_1: float = 0.33
    r_share_2: float = 0.33
    # ── Chandelier trail ──
    enable_chandelier_trail: bool = True
    chandelier_atr_mult: float = 1.0
    chandelier_lookback_bars: int = 60
    # ── Sweep / displacement ──
    sweep_lookback_bars: int = 60
    sweep_pierce_atr: float = 0.2
    sweep_to_displacement_max_bars: int = 30
    displacement_atr_mult: float = 1.0
    fvg_min_atr_mult: float = 0.2
    intraday_fractal_n: int = 3
    # ── Stop ──
    stop_atr_buffer: float = 1.0
    stop_max_atr: float = 2.0
    max_hold_bars: int = 240
    # ── Time ──
    enable_kill_zone_gate: bool = True
    enable_hard_cutoff: bool = True
    allowed_kill_zones: tuple[str, ...] = DEFAULT_CN_ALLOWED_KZS
    # ── ATR ──
    atr_lookback_1m: int = 14


# ════════════════════════════════════════════════════════════════════════════
#  Setup / Trade state
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class OTESetup:
    """限价单 setup 状态 (state OTE_PENDING)."""
    direction: int                     # +1 long, −1 short
    limit_price: float                 # OTE 70.5% 价
    ote_low_px: float
    ote_high_px: float
    stop_price: float
    target_price: float
    contracts: int
    sweep_idx: int
    sweep_level: float
    displacement_idx: int
    displacement_extreme: float        # long: high, short: low
    fvg_zone_low: float
    fvg_zone_high: float
    place_idx: int                     # OTE 挂单时的 bar index
    expire_idx: int                    # 超过此 idx 取消
    bias: str
    kill_zone: str
    pd_zone: str
    target_label: str = ""


@dataclass
class ActiveTrade:
    """已开仓 active trade (state FILLED)."""
    setup: str                         # "ICT_2022_LONG" | "ICT_2022_SHORT"
    direction: int
    entry_idx: int                     # entry bar index
    entry_price: float
    initial_stop: float
    stop_price: float                  # 动态调整 (R-ladder 后 BE, chandelier trail)
    target_price: float
    contracts: int
    initial_contracts: int
    sweep_level: float
    displacement_extreme: float
    bias: str
    kill_zone: str
    pd_zone: str
    bars_in_trade: int = 0
    partial_exits: list[dict] = field(default_factory=list)
    partial_pnl: float = 0.0           # in points (累计 R-ladder 已实现)
    notes: list[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════
#  Action result (state machine output)
# ════════════════════════════════════════════════════════════════════════════


@dataclass
class Action:
    """state_machine.on_bar / on_tick 返回的操作建议."""
    kind: Literal[
        "noop",                 # 无操作
        "place_limit",          # 挂 OTE 限价单
        "cancel_limit",         # 取消现有限价单 (超时)
        "fill_open",            # OTE 限价单成交 (开仓)
        "partial_exit",         # R-ladder partial 平仓
        "trail_update",         # 移动 stop (chandelier)
        "exit_full",            # 全平 (stop hit / max hold / hard cutoff)
    ]
    direction: int = 0                  # +1 long, −1 short
    price: float = 0.0
    contracts: int = 0
    reason: str = ""
    new_stop: float = 0.0
    setup: OTESetup | None = None
    metadata: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
#  State machine
# ════════════════════════════════════════════════════════════════════════════


class V6StateMachine:
    """ICT v6 主策略状态机.

    on_bar() 按 1m K线 close 调用 — 检测 sweep/displacement/FVG → 挂 OTE limit;
    on_tick() 按 tick 调用 — 检查 stop/target/trail/max-hold/hard-cutoff;
    on_trade() 当 broker 成交回调时调用 — 更新 internal state.

    数据 buffer:
        opens / highs / lows / closes — list of float, 历史 + 当前 1m bars
        bar_idx — 当前 cur_idx (= len(closes) - 1)
        atr_1m — 预计算的 ATR 数组
        intraday_swings — 已检测的 swing high/low list

    State:
        IDLE — 等下一个 sweep+displacement
        OTE_PENDING — 限价单已挂, 等成交 (or expire)
        FILLED — 开仓中, 等出场触发
    """

    def __init__(self, config: V6Config, biases: list[DailyBias],
                 tick_size: float, multiplier: float, slippage_ticks: int = 2):
        self.config = config
        self.biases = biases
        self.tick_size = tick_size
        self.multiplier = multiplier
        self.slippage_ticks = slippage_ticks

        self.state: State = "IDLE"
        self.setup: OTESetup | None = None
        self.trade: ActiveTrade | None = None
        self.daily_state: dict[date, dict] = {}   # {date → {trades_today, pnl_r}}

        # 数据 buffers
        self.opens: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.closes: list[float] = []
        self.bar_timestamps: list[datetime] = []
        self.atr_1m: np.ndarray = np.empty(0, dtype=float)

    # ──────────────────────────────────────────────────────────────────────
    #  Buffer management
    # ──────────────────────────────────────────────────────────────────────

    def push_history_bars(self, opens: list[float], highs: list[float],
                          lows: list[float], closes: list[float],
                          timestamps: list[datetime]) -> None:
        """初始化时灌入历史 1m bars."""
        self.opens = list(map(float, opens))
        self.highs = list(map(float, highs))
        self.lows = list(map(float, lows))
        self.closes = list(map(float, closes))
        self.bar_timestamps = list(timestamps)
        self._recompute_atr()

    def append_bar(self, o: float, h: float, l: float, c: float, ts: datetime) -> None:
        self.opens.append(float(o))
        self.highs.append(float(h))
        self.lows.append(float(l))
        self.closes.append(float(c))
        self.bar_timestamps.append(ts)
        # 增量更新 ATR (简化: recompute 整个数组, 适合 short-running sessions)
        self._recompute_atr()

    def _recompute_atr(self) -> None:
        if not self.closes:
            self.atr_1m = np.empty(0, dtype=float)
            return
        self.atr_1m = wilder_atr(
            np.asarray(self.highs), np.asarray(self.lows),
            np.asarray(self.closes), n=self.config.atr_lookback_1m,
        )

    @property
    def cur_idx(self) -> int:
        return len(self.closes) - 1

    def cur_atr(self) -> float:
        if self.atr_1m.size == 0:
            return 0.0
        v = self.atr_1m[-1]
        return float(v) if np.isfinite(v) else 0.0

    # ──────────────────────────────────────────────────────────────────────
    #  Daily state
    # ──────────────────────────────────────────────────────────────────────

    def _daily_key(self, ts: datetime) -> date:
        # 21:00 之后归属"下一交易日"
        if ts.hour >= 21:
            from datetime import timedelta
            return (ts + timedelta(days=1)).date()
        return ts.date()

    def _ds(self, ts: datetime) -> dict:
        k = self._daily_key(ts)
        if k not in self.daily_state:
            self.daily_state[k] = {"trades_today": 0, "pnl_r": 0.0}
        return self.daily_state[k]

    def _can_open_today(self, ts: datetime) -> bool:
        ds = self._ds(ts)
        if ds["trades_today"] >= self.config.max_trades_per_day:
            return False
        if ds["pnl_r"] <= self.config.daily_stop_r:
            return False
        if ds["pnl_r"] >= self.config.daily_lock_r:
            return False
        return True

    # ──────────────────────────────────────────────────────────────────────
    #  Position sizing
    # ──────────────────────────────────────────────────────────────────────

    def _position_size(self, equity: float, stop_distance: float) -> int:
        """0.5% 风险 / stop_distance ≤ max_contracts."""
        if stop_distance <= 0 or equity <= 0:
            return 0
        risk_dollars = equity * self.config.risk_per_trade_pct
        contract_risk = stop_distance * self.multiplier
        if contract_risk <= 0:
            return 0
        n = int(risk_dollars / contract_risk)
        return max(0, min(n, self.config.max_contracts))

    # ──────────────────────────────────────────────────────────────────────
    #  on_bar — 1m K线 close 调用
    # ──────────────────────────────────────────────────────────────────────

    def on_bar(self, ts: datetime, equity: float) -> Action:
        """每根 1m bar close 调用. 返回 Action."""
        # State FILLED — 出场逻辑在 on_tick 里处理 (更精确, tick 级 stop)
        if self.state == "FILLED":
            return Action(kind="noop")

        # State OTE_PENDING — 等限价单成交 / expire
        if self.state == "OTE_PENDING" and self.setup is not None:
            if self.cur_idx >= self.setup.expire_idx:
                self.setup = None
                self.state = "IDLE"
                return Action(kind="cancel_limit", reason="expire")
            # 限价单 fill 在 on_tick 里 (更精确)
            return Action(kind="noop")

        # State IDLE — 扫描 setup
        if self.state != "IDLE":
            return Action(kind="noop")

        cfg = self.config

        # 时间门控
        if cfg.enable_kill_zone_gate and not can_trade(ts, cfg.allowed_kill_zones):
            return Action(kind="noop")
        kz = get_active_kill_zone(ts) or "unknown"

        # 当日限制
        if not self._can_open_today(ts):
            return Action(kind="noop")

        # ATR 暖机
        if self.cur_idx < cfg.atr_lookback_1m + 1:
            return Action(kind="noop")
        atr = self.cur_atr()
        if atr <= 0:
            return Action(kind="noop")

        # 取 D1 bias
        bias = bias_for_date(self.biases, ts.date())
        if bias is None or bias.bias == "neutral":
            return Action(kind="noop")
        if bias.bias == "bull":
            direction = 1
        elif bias.bias == "bear" and cfg.enable_short_setups:
            direction = -1
        else:
            return Action(kind="noop")

        # PD-zone gate
        if cfg.require_discount_for_long:
            cur_close = self.closes[-1]
            dr_high = bias.dealing_range_high
            dr_low = bias.dealing_range_low
            if dr_high > dr_low:
                pos = (cur_close - dr_low) / (dr_high - dr_low)
                if direction == 1 and pos > cfg.pd_threshold:
                    return Action(kind="noop")
                if direction == -1 and pos < (1.0 - cfg.pd_threshold):
                    return Action(kind="noop")

        # ── Sweep + displacement + FVG ──
        opens_arr = np.asarray(self.opens)
        highs_arr = np.asarray(self.highs)
        lows_arr = np.asarray(self.lows)
        closes_arr = np.asarray(self.closes)

        sh, sl = detect_intraday_swings(highs_arr, lows_arr, fractal_n=cfg.intraday_fractal_n)

        if direction == 1:
            sweep = detect_swept_low(
                highs_arr, lows_arr, closes_arr, sl,
                self.atr_1m, self.cur_idx,
                pierce_atr=cfg.sweep_pierce_atr,
                max_lookback_bars=cfg.sweep_lookback_bars,
            )
            if sweep is None:
                return Action(kind="noop")
            disp = detect_bullish_displacement_after_sweep(
                opens_arr, highs_arr, lows_arr, closes_arr,
                sweep_idx=sweep.sweep_idx, cur_idx=self.cur_idx,
                atr_series=self.atr_1m,
                max_bars=cfg.sweep_to_displacement_max_bars,
                atr_mult=cfg.displacement_atr_mult,
                fvg_min_atr_mult=cfg.fvg_min_atr_mult,
                tick_size=self.tick_size,
            )
        else:
            sweep = detect_swept_high(
                highs_arr, lows_arr, closes_arr, sh,
                self.atr_1m, self.cur_idx,
                pierce_atr=cfg.sweep_pierce_atr,
                max_lookback_bars=cfg.sweep_lookback_bars,
            )
            if sweep is None:
                return Action(kind="noop")
            disp = detect_bearish_displacement_after_sweep(
                opens_arr, highs_arr, lows_arr, closes_arr,
                sweep_idx=sweep.sweep_idx, cur_idx=self.cur_idx,
                atr_series=self.atr_1m,
                max_bars=cfg.sweep_to_displacement_max_bars,
                atr_mult=cfg.displacement_atr_mult,
                fvg_min_atr_mult=cfg.fvg_min_atr_mult,
                tick_size=self.tick_size,
            )

        if disp is None:
            return Action(kind="noop")

        # ── 计算 OTE / stop / target ──
        atr_buffer = min(cfg.stop_atr_buffer * atr, cfg.stop_max_atr * atr)

        if direction == 1:
            disp_extreme = disp.displacement_high
            leg = disp_extreme - sweep.swept_level
            if leg <= 0:
                return Action(kind="noop")
            limit_px = disp_extreme - cfg.ote_mid_pct * leg
            ote_low_px = disp_extreme - cfg.ote_high_pct * leg
            ote_high_px = disp_extreme - cfg.ote_low_pct * leg
            stop_price = sweep.swept_level - atr_buffer
            stop_distance = limit_px - stop_price
            if stop_distance <= 0:
                return Action(kind="noop")
            # Target: 选 disp_extreme 上方 ≥ min_rr × R 的水平 (简化 MVP: 用 disp_extreme 作 target)
            target_price = limit_px + cfg.min_rr * stop_distance
            target_label = "min_rr_3R"
        else:
            disp_extreme = disp.displacement_low
            leg = sweep.swept_level - disp_extreme
            if leg <= 0:
                return Action(kind="noop")
            limit_px = disp_extreme + cfg.ote_mid_pct * leg
            ote_low_px = disp_extreme + cfg.ote_low_pct * leg
            ote_high_px = disp_extreme + cfg.ote_high_pct * leg
            stop_price = sweep.swept_level + atr_buffer
            stop_distance = stop_price - limit_px
            if stop_distance <= 0:
                return Action(kind="noop")
            target_price = limit_px - cfg.min_rr * stop_distance
            target_label = "min_rr_3R"

        contracts = self._position_size(equity, stop_distance)
        if contracts < 1:
            return Action(kind="noop")

        # ── 挂 OTE 限价单 ──
        self.setup = OTESetup(
            direction=direction,
            limit_price=limit_px,
            ote_low_px=ote_low_px, ote_high_px=ote_high_px,
            stop_price=stop_price, target_price=target_price,
            contracts=contracts,
            sweep_idx=sweep.sweep_idx, sweep_level=sweep.swept_level,
            displacement_idx=disp.displacement_idx,
            displacement_extreme=disp_extreme,
            fvg_zone_low=disp.fvg_zone_low, fvg_zone_high=disp.fvg_zone_high,
            place_idx=self.cur_idx,
            expire_idx=self.cur_idx + cfg.ote_fill_max_bars,
            bias=bias.bias, kill_zone=kz, pd_zone=bias.pd_zone,
            target_label=target_label,
        )
        self.state = "OTE_PENDING"

        return Action(
            kind="place_limit",
            direction=direction, price=limit_px, contracts=contracts,
            new_stop=stop_price,
            setup=self.setup,
            reason=f"sweep@{sweep.swept_level:.1f} disp@{disp_extreme:.1f} leg={leg:.1f}",
            metadata={
                "stop": stop_price, "target": target_price,
                "ote_low": ote_low_px, "ote_high": ote_high_px,
                "bias": bias.bias, "kz": kz, "pd_zone": bias.pd_zone,
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    #  on_tick — tick 级 stop / target / trail / cutoff 检查
    # ──────────────────────────────────────────────────────────────────────

    def on_tick(self, ts: datetime, last_price: float) -> Action:
        """每个 tick 调用. 状态 FILLED / OTE_PENDING 时检查触发."""
        cfg = self.config

        # OTE_PENDING — 检查限价单是否成交
        if self.state == "OTE_PENDING" and self.setup is not None:
            d = self.setup.direction
            limit_px = self.setup.limit_price
            # Long: 价格 ≤ limit 即 fill (tick 触及 limit)
            # Short: 价格 ≥ limit 即 fill
            hit = (last_price <= limit_px) if d == 1 else (last_price >= limit_px)
            if hit:
                # 限价单 fill (策略层会 send_order)
                return Action(
                    kind="fill_open",
                    direction=d, price=limit_px, contracts=self.setup.contracts,
                    new_stop=self.setup.stop_price,
                    setup=self.setup,
                    reason="limit_fill",
                )
            return Action(kind="noop")

        # FILLED — 出场检查
        if self.state == "FILLED" and self.trade is not None:
            t = self.trade
            d = t.direction

            # Hard cutoff
            if cfg.enable_hard_cutoff and past_hard_cutoff(ts):
                return Action(
                    kind="exit_full",
                    direction=d, contracts=t.contracts,
                    price=last_price, reason="hard_cutoff",
                )

            # Stop hit (tick-level)
            stop_hit = (last_price <= t.stop_price) if d == 1 else (last_price >= last_price >= t.stop_price)
            # 修正写法
            stop_hit = (last_price <= t.stop_price) if d == 1 else (last_price >= t.stop_price)
            if stop_hit:
                if d * (t.stop_price - t.entry_price) < 0:
                    reason = "stop_loss"
                elif abs(t.stop_price - t.entry_price) < self.tick_size:
                    reason = "break_even"
                else:
                    reason = "trail_stop"
                return Action(
                    kind="exit_full",
                    direction=d, contracts=t.contracts,
                    price=t.stop_price, reason=reason,
                )

            # R-ladder partial check
            if cfg.enable_r_ladder and t.contracts > 0:
                R = abs(t.entry_price - t.initial_stop)
                n_partials = len(t.partial_exits)
                if n_partials < 3:
                    targets_R = [cfg.r_target_1, cfg.r_target_2, cfg.r_target_3]
                    shares = [cfg.r_share_1, cfg.r_share_2, 1.0]
                    target_R = targets_R[n_partials]
                    target_px = t.entry_price + d * target_R * R
                    target_hit = (last_price >= target_px) if d == 1 else (last_price <= target_px)
                    if target_hit:
                        share = shares[n_partials]
                        tier_qty = max(1, int(round(t.initial_contracts * share)))
                        tier_qty = min(tier_qty, t.contracts)
                        return Action(
                            kind="partial_exit",
                            direction=d, contracts=tier_qty, price=target_px,
                            new_stop=(t.entry_price if n_partials == 0 else t.stop_price),
                            reason=f"r_target_{target_R}R",
                            metadata={"R": R, "target_R": target_R, "tier_idx": n_partials},
                        )

            # Max hold
            if t.bars_in_trade >= cfg.max_hold_bars:
                return Action(
                    kind="exit_full",
                    direction=d, contracts=t.contracts,
                    price=last_price, reason="max_hold",
                )

        return Action(kind="noop")

    # ──────────────────────────────────────────────────────────────────────
    #  Bar-level state updates (chandelier trail) — call after on_tick
    # ──────────────────────────────────────────────────────────────────────

    def on_bar_close_filled(self) -> Action:
        """FILLED 状态时, 每根 bar close 后做 chandelier trail 检查."""
        if self.state != "FILLED" or self.trade is None:
            return Action(kind="noop")
        cfg = self.config
        t = self.trade
        t.bars_in_trade += 1

        # Chandelier trail (要求 R-ladder 第一档已 hit)
        if (cfg.enable_chandelier_trail and t.partial_exits
                and t.contracts > 0 and self.atr_1m.size > 0):
            atr_now = self.cur_atr()
            if atr_now > 0:
                lookback = cfg.chandelier_lookback_bars
                start = max(0, self.cur_idx - lookback)
                if t.direction == 1:
                    hh = max(self.highs[start:self.cur_idx + 1])
                    new_stop = hh - cfg.chandelier_atr_mult * atr_now
                    if new_stop > t.stop_price:
                        old_stop = t.stop_price
                        t.stop_price = new_stop
                        return Action(
                            kind="trail_update", direction=1, new_stop=new_stop,
                            reason=f"chandelier_long {old_stop:.2f}→{new_stop:.2f}",
                        )
                else:
                    ll = min(self.lows[start:self.cur_idx + 1])
                    new_stop = ll + cfg.chandelier_atr_mult * atr_now
                    if new_stop < t.stop_price:
                        old_stop = t.stop_price
                        t.stop_price = new_stop
                        return Action(
                            kind="trail_update", direction=-1, new_stop=new_stop,
                            reason=f"chandelier_short {old_stop:.2f}→{new_stop:.2f}",
                        )
        return Action(kind="noop")

    # ──────────────────────────────────────────────────────────────────────
    #  Trade lifecycle — strategy file 在收到 broker 成交回调时调用
    # ──────────────────────────────────────────────────────────────────────

    def confirm_open(self, fill_price: float, fill_vol: int, ts: datetime) -> None:
        """Strategy file 收到 ON_TRADE open 回调时调用. 把 setup → trade 转换."""
        if self.state != "OTE_PENDING" or self.setup is None:
            return
        s = self.setup
        d = s.direction
        self.trade = ActiveTrade(
            setup="ICT_2022_LONG" if d == 1 else "ICT_2022_SHORT",
            direction=d,
            entry_idx=self.cur_idx,
            entry_price=fill_price,
            initial_stop=s.stop_price, stop_price=s.stop_price,
            target_price=s.target_price,
            contracts=fill_vol, initial_contracts=fill_vol,
            sweep_level=s.sweep_level,
            displacement_extreme=s.displacement_extreme,
            bias=s.bias, kill_zone=s.kill_zone, pd_zone=s.pd_zone,
            notes=[s.target_label],
        )
        self.setup = None
        self.state = "FILLED"
        self._ds(ts)["trades_today"] += 1

    def confirm_partial(self, fill_price: float, fill_vol: int, target_R: float) -> None:
        if self.state != "FILLED" or self.trade is None:
            return
        t = self.trade
        d = t.direction
        pts = d * (fill_price - t.entry_price) * fill_vol
        t.partial_exits.append({
            "price": fill_price, "qty": fill_vol, "target_R": target_R,
            "pts": pts,
        })
        t.partial_pnl += pts
        t.contracts = max(0, t.contracts - fill_vol)
        # 第一档 hit 后, stop 移到 BE
        if len(t.partial_exits) == 1:
            t.stop_price = t.entry_price

    def confirm_close(self, fill_price: float, fill_vol: int, reason: str, ts: datetime) -> dict:
        """全平 trade. 返回 trade summary dict."""
        if self.state != "FILLED" or self.trade is None:
            return {}
        t = self.trade
        d = t.direction
        final_pts = d * (fill_price - t.entry_price) * fill_vol
        total_pts = t.partial_pnl + final_pts
        R = abs(t.entry_price - t.initial_stop)
        total_R = (total_pts / max(t.initial_contracts, 1)) / R if R > 0 else 0.0

        summary = {
            "setup": t.setup, "direction": d,
            "entry_price": t.entry_price, "exit_price": fill_price,
            "contracts": t.initial_contracts,
            "total_pts": total_pts, "total_R": total_R,
            "reason": reason,
            "partial_exits": list(t.partial_exits),
            "bias": t.bias, "kill_zone": t.kill_zone, "pd_zone": t.pd_zone,
        }
        self._ds(ts)["pnl_r"] += total_R
        self.trade = None
        self.state = "IDLE"
        return summary
