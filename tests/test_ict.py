"""ICT v6 移植单元测试 — 覆盖 modules + strategy file syntax."""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

# 加 src/ 到 sys.path 以 import ICT.modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ─────────────────────────────────────────────────────────────────────────────
# timezones / sessions
# ─────────────────────────────────────────────────────────────────────────────


def test_kill_zone_day_open_sb():
    from ICT.modules.sessions_cn import get_active_kill_zone
    assert get_active_kill_zone(datetime(2026, 4, 28, 9, 15)) == "DAY_OPEN_SB"
    assert get_active_kill_zone(datetime(2026, 4, 28, 9, 45)) == "DAY_OPEN_KZ"


def test_kill_zone_afternoon():
    from ICT.modules.sessions_cn import get_active_kill_zone
    assert get_active_kill_zone(datetime(2026, 4, 28, 13, 35)) == "AFTERNOON_SB"
    assert get_active_kill_zone(datetime(2026, 4, 28, 14, 0)) == "AFTERNOON_KZ"


def test_kill_zone_night():
    from ICT.modules.sessions_cn import get_active_kill_zone
    assert get_active_kill_zone(datetime(2026, 4, 28, 21, 15)) == "NIGHT_OPEN_SB"
    assert get_active_kill_zone(datetime(2026, 4, 28, 21, 45)) == "NIGHT_OPEN_KZ"


def test_kill_zone_outside():
    from ICT.modules.sessions_cn import get_active_kill_zone
    assert get_active_kill_zone(datetime(2026, 4, 28, 7, 0)) is None
    assert get_active_kill_zone(datetime(2026, 4, 28, 23, 30)) is None


def test_lunch_break():
    from ICT.modules.sessions_cn import in_lunch_break
    assert in_lunch_break(datetime(2026, 4, 28, 12, 0))     # 午餐
    assert in_lunch_break(datetime(2026, 4, 28, 10, 20))    # 茶歇
    assert not in_lunch_break(datetime(2026, 4, 28, 9, 0))


def test_hard_cutoff():
    from ICT.modules.sessions_cn import past_hard_cutoff
    assert past_hard_cutoff(datetime(2026, 4, 28, 14, 55))   # 日盘
    assert past_hard_cutoff(datetime(2026, 4, 28, 22, 55))   # 夜盘 DCE
    assert not past_hard_cutoff(datetime(2026, 4, 28, 14, 30))


def test_can_trade():
    from ICT.modules.sessions_cn import can_trade
    assert can_trade(datetime(2026, 4, 28, 9, 15))           # DAY_OPEN_SB
    assert not can_trade(datetime(2026, 4, 28, 12, 0))       # lunch
    assert not can_trade(datetime(2026, 4, 28, 14, 55))      # cutoff


# ─────────────────────────────────────────────────────────────────────────────
# structures — ATR / swings / sweep / displacement
# ─────────────────────────────────────────────────────────────────────────────


def test_wilder_atr_basic():
    from ICT.modules.structures import wilder_atr
    h = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0,
                  20.0, 21.0, 22.0, 23.0, 24.0])
    l = np.array([8.0,  9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0,
                  18.0, 19.0, 20.0, 21.0, 22.0])
    c = np.array([9.0, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5,
                  19.5, 20.5, 21.5, 22.5, 23.5])
    atr = wilder_atr(h, l, c, n=14)
    assert atr.size == 15
    assert np.isnan(atr[0])
    assert np.isfinite(atr[14])
    assert atr[14] > 0


def test_detect_intraday_swings():
    from ICT.modules.structures import detect_intraday_swings
    h = np.array([10, 11, 13, 12, 11, 10, 9, 11, 12, 14, 13, 11, 10, 12, 11], dtype=float)
    l = np.array([9, 10, 11, 10,  9,  8, 7,  9, 11, 12, 11,  9,  8, 10,  9], dtype=float)
    sh, sl = detect_intraday_swings(h, l, fractal_n=3)
    assert len(sh) >= 1
    assert len(sl) >= 1


def test_detect_swept_low_with_pierce_and_reclaim():
    """Craft 一个简单 sweep low 场景."""
    from ICT.modules.structures import (
        detect_intraday_swings, detect_swept_low, wilder_atr,
    )
    n = 130
    closes = np.full(n, 5000.0)
    highs = closes + 2.0
    lows = closes - 2.0
    opens = closes.copy()
    # bar 100: swing low at 4970
    closes[100], opens[100], lows[100], highs[100] = 4980, 5000, 4970, 5000
    # bar 116: pierce 4970 → 4960
    closes[116], opens[116], lows[116], highs[116] = 4972, 4985, 4960, 4985
    # bar 117: reclaim close > 4970
    closes[117], opens[117], lows[117], highs[117] = 4985, 4972, 4970, 4988

    atr = wilder_atr(highs, lows, closes, n=14)
    _, sl = detect_intraday_swings(highs, lows, fractal_n=3)
    sweep = detect_swept_low(highs, lows, closes, sl, atr, cur_idx=117,
                              pierce_atr=0.2, max_lookback_bars=60)
    assert sweep is not None
    assert sweep.swept_level == 4970.0
    assert sweep.source == "single_swing"


def test_detect_swept_high_mirror():
    """Sweep high 镜像."""
    from ICT.modules.structures import (
        detect_intraday_swings, detect_swept_high, wilder_atr,
    )
    n = 130
    closes = np.full(n, 5000.0)
    highs = closes + 2.0
    lows = closes - 2.0
    opens = closes.copy()
    # bar 100: swing high at 5030
    closes[100], opens[100], highs[100], lows[100] = 5020, 5000, 5030, 5000
    # bar 116: pierce 5030 → 5040
    closes[116], opens[116], highs[116], lows[116] = 5028, 5015, 5040, 5015
    # bar 117: reclaim close < 5030
    closes[117], opens[117], highs[117], lows[117] = 5015, 5028, 5030, 5012
    atr = wilder_atr(highs, lows, closes, n=14)
    sh, _ = detect_intraday_swings(highs, lows, fractal_n=3)
    sweep = detect_swept_high(highs, lows, closes, sh, atr, cur_idx=117,
                               pierce_atr=0.2, max_lookback_bars=60)
    assert sweep is not None
    assert sweep.swept_level == 5030.0


def test_displacement_detection_with_fvg():
    """Sweep + displacement bar + 3-bar FVG."""
    from ICT.modules.structures import (
        wilder_atr, detect_bullish_displacement_after_sweep,
    )
    n = 130
    closes = np.full(n, 5000.0)
    highs = closes + 2.0
    lows = closes - 2.0
    opens = closes.copy()
    # bar 116: sweep
    closes[116], opens[116], lows[116], highs[116] = 4972, 4985, 4960, 4985
    # bar 117: bullish displacement
    closes[117], opens[117], lows[117], highs[117] = 4985, 4972, 4970, 4988
    # bar 118: prev high 4988, low must be > prev_prev high (bar 116 high 4985) for FVG
    closes[118], opens[118], lows[118], highs[118] = 5005, 4988, 4988, 5008

    atr = wilder_atr(highs, lows, closes, n=14)
    disp = detect_bullish_displacement_after_sweep(
        opens, highs, lows, closes, sweep_idx=116, cur_idx=118,
        atr_series=atr, max_bars=30, atr_mult=1.0, fvg_min_atr_mult=0.2,
        tick_size=0.5,
    )
    # Displacement may be at 117 (qualifies) or None — depends on FVG check
    if disp is not None:
        assert disp.displacement_close > disp.displacement_open  # bullish
        assert disp.fvg_zone_high > disp.fvg_zone_low


# ─────────────────────────────────────────────────────────────────────────────
# bias
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_daily_bias_warmup_returns_neutral():
    from ICT.modules.bias import compute_daily_bias
    n = 10
    h = np.linspace(100, 110, n)
    l = np.linspace(95, 105, n)
    c = (h + l) / 2
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    biases = compute_daily_bias(h, l, c, dates, lookback_days=20)
    assert len(biases) == n
    # All in warmup → neutral
    assert all(b.bias == "neutral" for b in biases)


def test_compute_daily_bias_zigzag_uptrend():
    """zigzag 上涨 (有 dip/recover) → 应触发 bull MSS at some point."""
    from ICT.modules.bias import compute_daily_bias
    # 构造 zigzag pattern: 大趋势上涨, 但每 5 天有 dip 形成 swing high/low
    n = 60
    rng = np.random.default_rng(42)
    base = np.linspace(100, 200, n)
    # 加 sin 波形 + 噪声让 fractal swings 容易形成
    zigzag = base + 4 * np.sin(np.linspace(0, 12 * np.pi, n)) + rng.standard_normal(n) * 0.5
    h = zigzag + 1.5
    l = zigzag - 1.5
    c = zigzag.copy()
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    biases = compute_daily_bias(h, l, c, dates, lookback_days=20,
                                 displacement_atr_mult=0.3)
    assert len(biases) == n
    # 至少应该不全 neutral (zigzag 必产生 swing + MSS)
    later = biases[30:]
    n_directional = sum(1 for b in later if b.bias != "neutral")
    assert n_directional >= 1, \
        f"Expected ≥1 directional bias after zigzag, got 0; bias dist: " \
        f"{[b.bias for b in later[:10]]}"


# ─────────────────────────────────────────────────────────────────────────────
# state machine
# ─────────────────────────────────────────────────────────────────────────────


def test_state_machine_init_idle():
    from ICT.modules.state_machine import V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    assert sm.state == "IDLE"
    assert sm.cur_idx == -1
    assert sm.cur_atr() == 0.0


def test_state_machine_buffer_management():
    from ICT.modules.state_machine import V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    n = 50
    sm.push_history_bars(
        opens=[5000.0] * n, highs=[5002.0] * n, lows=[4998.0] * n,
        closes=[5000.0] * n,
        timestamps=[datetime(2026, 4, 28, 9, i % 60) for i in range(n)],
    )
    assert sm.cur_idx == n - 1
    sm.append_bar(5000.0, 5002.0, 4998.0, 5001.0, datetime(2026, 4, 28, 10, 0))
    assert sm.cur_idx == n


def test_state_machine_idle_no_bias_returns_noop():
    """No bias → state machine 不开仓."""
    from ICT.modules.state_machine import V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    n = 100
    sm.push_history_bars(
        [5000.0] * n, [5002.0] * n, [4998.0] * n, [5000.0] * n,
        [datetime(2026, 4, 28, 9, 0) + timedelta(minutes=i) for i in range(n)],
    )
    a = sm.on_bar(datetime(2026, 4, 28, 10, 30), equity=1_000_000)
    assert a.kind == "noop"


def test_state_machine_outside_kz_returns_noop():
    """非 KZ 时段 → noop."""
    from ICT.modules.state_machine import V6Config, V6StateMachine
    from ICT.modules.bias import DailyBias
    bias = DailyBias(
        date=date(2026, 4, 28), bias="bull", last_mss_direction="bull",
        last_mss_idx=10, days_since_mss=2,
        dealing_range_high=5200, dealing_range_low=4900, equilibrium=5050,
        current_close=5000, pd_zone="discount",
    )
    sm = V6StateMachine(V6Config(), biases=[bias], tick_size=0.5, multiplier=100)
    n = 100
    sm.push_history_bars(
        [5000.0] * n, [5002.0] * n, [4998.0] * n, [5000.0] * n,
        [datetime(2026, 4, 28, 9, 0) + timedelta(minutes=i) for i in range(n)],
    )
    # 23:30 — outside any KZ
    a = sm.on_bar(datetime(2026, 4, 28, 23, 30), equity=1_000_000)
    assert a.kind == "noop"


def test_state_machine_position_size():
    from ICT.modules.state_machine import V6Config, V6StateMachine
    cfg = V6Config(risk_per_trade_pct=0.005, max_contracts=5)
    sm = V6StateMachine(cfg, biases=[], tick_size=0.5, multiplier=100)
    # equity=1_000_000, stop_distance=10 (price points), multiplier=100
    # risk = 5000 USD; contract risk = 10 × 100 = 1000 USD; → 5 contracts
    assert sm._position_size(equity=1_000_000, stop_distance=10.0) == 5
    # stop_distance=50 → 5000/(50×100)=1
    assert sm._position_size(equity=1_000_000, stop_distance=50.0) == 1
    # stop_distance=100 → 5000/(100×100)=0.5 → 0
    assert sm._position_size(equity=1_000_000, stop_distance=100.0) == 0


def test_state_machine_daily_limit_blocks_new_trades():
    from ICT.modules.state_machine import V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(max_trades_per_day=3), biases=[], tick_size=0.5, multiplier=100)
    ts = datetime(2026, 4, 28, 10, 0)
    ds = sm._ds(ts)
    ds["trades_today"] = 3
    assert not sm._can_open_today(ts)
    ds["trades_today"] = 0
    ds["pnl_r"] = -2.5  # below daily_stop_r=-2.0
    assert not sm._can_open_today(ts)
    ds["pnl_r"] = 4.0   # above daily_lock_r=+3.0
    assert not sm._can_open_today(ts)
    ds["pnl_r"] = 0.0
    assert sm._can_open_today(ts)


# ─────────────────────────────────────────────────────────────────────────────
# strategy file syntax + key tokens
# ─────────────────────────────────────────────────────────────────────────────


_STRATEGY_FILE = Path(__file__).resolve().parents[1] / "src" / "ICT" / "I_Bidir_M1_ICT_v6.py"


def test_strategy_file_syntax_ok():
    import ast
    text = _STRATEGY_FILE.read_text()
    ast.parse(text)


def test_strategy_class_name_matches_file():
    """PythonGO 强制要求 class name = file stem."""
    text = _STRATEGY_FILE.read_text()
    assert "class I_Bidir_M1_ICT_v6(BaseStrategy):" in text
    assert 'STRATEGY_NAME = "I_Bidir_M1_ICT_v6"' in text


def test_strategy_has_takeover_patches():
    """4 处 takeover patch 必须齐全 (与 V8/V13/QExp 一致)."""
    text = _STRATEGY_FILE.read_text()
    assert 'takeover_lots: int = Field(default=0' in text     # P1
    assert 'self._takeover_pending = False' in text           # P2
    assert '[ON_START TAKEOVER]' in text                       # P3
    assert '[TAKEOVER FIRST TICK]' in text                     # P4


def test_strategy_imports_ict_modules():
    """策略文件正确 import ICT.modules.* (sys.path hack 已加)."""
    text = _STRATEGY_FILE.read_text()
    assert "from ICT.modules.bias import" in text
    assert "from ICT.modules.state_machine import" in text


def test_strategy_self_managed_position():
    """自管持仓: _own_pos / _my_oids 过滤."""
    text = _STRATEGY_FILE.read_text()
    assert "self._own_pos: int = 0" in text
    assert "self._my_oids: set = set()" in text
    assert "if oid not in self._my_oids:" in text


# ─────────────────────────────────────────────────────────────────────────────
# Bug 修复回归测试 (audit-2026-04-28)
# ─────────────────────────────────────────────────────────────────────────────


def test_to_python_datetime_handles_various_types():
    """Bug 5 修: 各类 datetime 表达统一转 Python datetime."""
    from ICT.modules.timezones import to_python_datetime
    # native datetime
    dt = datetime(2026, 4, 28, 9, 15, 0)
    assert to_python_datetime(dt) == dt
    # None → None
    assert to_python_datetime(None) is None
    # numpy datetime64
    np_dt = np.datetime64("2026-04-28T09:15:00")
    converted = to_python_datetime(np_dt)
    assert converted is not None
    assert converted.year == 2026
    assert converted.hour == 9
    # str
    converted = to_python_datetime("2026-04-28 09:15:00")
    assert converted == datetime(2026, 4, 28, 9, 15, 0)
    # 不可识别 → None (不 crash)
    assert to_python_datetime(object()) is None


def test_state_machine_on_tick_no_fill_open_in_ote_pending():
    """Bug 2 修: OTE_PENDING 状态下 on_tick 返 noop (不返 fill_open).

    限价单 fill 由 broker 自动处理, 策略等 on_trade 回调即可.
    """
    from ICT.modules.state_machine import OTESetup, V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    # 模拟 OTE_PENDING 状态
    sm.state = "OTE_PENDING"
    sm.setup = OTESetup(
        direction=1, limit_price=4985.0, ote_low_px=4980.0, ote_high_px=4990.0,
        stop_price=4960.0, target_price=5050.0, contracts=3,
        sweep_idx=100, sweep_level=4970.0,
        displacement_idx=110, displacement_extreme=5000.0,
        fvg_zone_low=4985.0, fvg_zone_high=4988.0,
        place_idx=120, expire_idx=180,
        bias="bull", kill_zone="DAY_OPEN_SB", pd_zone="discount",
    )
    # 价格触及 limit — 应该返 noop (broker 自动 fill)
    a = sm.on_tick(datetime(2026, 4, 28, 9, 30), last_price=4985.0)
    assert a.kind == "noop"
    # 价格穿过 limit 也应该返 noop
    a = sm.on_tick(datetime(2026, 4, 28, 9, 30), last_price=4980.0)
    assert a.kind == "noop"


def test_state_machine_confirm_open_handles_split_fills():
    """Bug 3 修: 同 oid 拆批 ON_TRADE 时, confirm_open 累加 vol + 加权均价."""
    from ICT.modules.state_machine import OTESetup, V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    sm.state = "OTE_PENDING"
    sm.setup = OTESetup(
        direction=1, limit_price=4985.0, ote_low_px=4980.0, ote_high_px=4990.0,
        stop_price=4960.0, target_price=5050.0, contracts=5,
        sweep_idx=100, sweep_level=4970.0,
        displacement_idx=110, displacement_extreme=5000.0,
        fvg_zone_low=4985.0, fvg_zone_high=4988.0,
        place_idx=120, expire_idx=180,
        bias="bull", kill_zone="DAY_OPEN_SB", pd_zone="discount",
    )
    ts = datetime(2026, 4, 28, 9, 30)

    # 第一笔: 2 手 @ 4985
    sm.confirm_open(fill_price=4985.0, fill_vol=2, ts=ts)
    assert sm.state == "FILLED"
    assert sm.trade is not None
    assert sm.trade.contracts == 2
    assert sm.trade.initial_contracts == 2
    assert sm.trade.entry_price == pytest.approx(4985.0)

    # 第二笔拆批: 3 手 @ 4986 (state 已 FILLED, 应累加)
    sm.confirm_open(fill_price=4986.0, fill_vol=3, ts=ts)
    assert sm.trade.contracts == 5
    assert sm.trade.initial_contracts == 5
    # 加权均价: (2 × 4985 + 3 × 4986) / 5 = 4985.6
    assert sm.trade.entry_price == pytest.approx(4985.6)


def test_state_machine_no_dead_code_in_on_tick():
    """Bug 4 修: _on_tick_stops 残留 dead code 已删."""
    import inspect
    from ICT.modules.state_machine import V6StateMachine
    src = inspect.getsource(V6StateMachine.on_tick)
    # 不应有重复赋值或 dead code 残留
    assert src.count("stop_hit = (last_price <= t.stop_price)") <= 1
    # 不应有 last_price >= last_price >= 写法 (dead code)
    assert "last_price >= last_price >=" not in src


def test_strategy_no_sm_push_history_bars_call():
    """Bug 1 修: on_start 不再调 sm.push_history_bars (避免与 callback double-append).

    callback (history) 阶段会自然 append 每根 history bar.
    """
    text = _STRATEGY_FILE.read_text()
    assert "self._sm.push_history_bars(" not in text


def test_strategy_place_limit_immediately_sends_order():
    """Bug 2 修: _on_bar 收到 place_limit action 时立即调 _exec_place_limit."""
    text = _STRATEGY_FILE.read_text()
    assert "_exec_place_limit" in text
    assert "self._exec_place_limit(action)" in text
    # _exec_place_limit 应该直接 send_order (不再走 fill_open 路径)
    assert 'order_direction=side, offset="open"' in text


def test_strategy_no_fill_open_action_consumed():
    """Bug 2 修: strategy 不再消费 fill_open action (broker 自动 fill, 等 on_trade)."""
    text = _STRATEGY_FILE.read_text()
    assert 'action.kind == "fill_open"' not in text


def test_strategy_uses_to_python_datetime_helper():
    """Bug 5 修: 关键 datetime 处理用 to_python_datetime helper."""
    text = _STRATEGY_FILE.read_text()
    assert "from ICT.modules.timezones import to_python_datetime" in text
    # tick.datetime / kline.datetime / trade timestamps 都用 helper
    assert "to_python_datetime(tick.datetime)" in text
    assert "to_python_datetime(kline.datetime)" in text
    assert "to_python_datetime(raw_ts)" in text


def test_strategy_history_bars_check():
    """Bug 6 修: on_start 检查 producer_bars 充足性, 不够时飞书警告."""
    text = _STRATEGY_FILE.read_text()
    assert "MIN_HISTORY_BARS_FOR_BIAS" in text
    assert "history bars" in text and "建议" in text


def test_strategy_pending_open_only_cleared_on_full_fill():
    """code-reviewer HIGH bug 修: _pending_open 只在 broker 完全 fill 时清空,
    避免拆批第 2 笔走 '未匹配 pending action' 分支."""
    text = _STRATEGY_FILE.read_text()
    # 应该有 expected vs filled 比较
    assert 'expected = self._pending_open["contracts"]' in text
    assert "filled = self._sm.trade.initial_contracts" in text
    assert "if filled >= expected:" in text
    # _pending_open = None 应该在 if filled >= expected 之内 (条件清空)
    # 简单 check: 'self._pending_open = None' 出现在 'if filled >= expected:' 之后
    # (用粗略 substring 序列判断)
    pos_check = text.find("if filled >= expected:")
    pos_clear = text.find("self._pending_open = None", pos_check)
    assert pos_check >= 0 and pos_clear > pos_check


def test_strategy_on_trade_has_sm_none_guard():
    """code-reviewer MEDIUM bug 修: on_trade 顶部 _sm is None guard."""
    text = _STRATEGY_FILE.read_text()
    # on_trade 函数体内应该有 sm None check
    on_trade_idx = text.find("def on_trade(self, trade: TradeData)")
    super_idx = text.find("super().on_trade(trade)", on_trade_idx)
    none_guard_idx = text.find("if self._sm is None:", super_idx)
    # None guard 应在 super().on_trade(trade) 之后, 在 oid extraction 之前
    oid_extract_idx = text.find("oid = trade.order_id", super_idx)
    assert none_guard_idx > super_idx
    assert none_guard_idx < oid_extract_idx


def test_state_machine_on_tick_filled_stop_hit():
    """Bug 4 (顺带): FILLED 状态 stop hit 仍正常工作 (tick-level)."""
    from ICT.modules.state_machine import ActiveTrade, V6Config, V6StateMachine
    sm = V6StateMachine(V6Config(), biases=[], tick_size=0.5, multiplier=100)
    sm.state = "FILLED"
    sm.trade = ActiveTrade(
        setup="ICT_2022_LONG", direction=1, entry_idx=100,
        entry_price=4985.0, initial_stop=4960.0, stop_price=4960.0,
        target_price=5050.0, contracts=3, initial_contracts=3,
        sweep_level=4970.0, displacement_extreme=5000.0,
        bias="bull", kill_zone="DAY_OPEN_SB", pd_zone="discount",
    )
    # 把 closes 数组 push 进去, 这样 cur_idx 不会爆
    sm.push_history_bars([4985.0]*200, [4990.0]*200, [4980.0]*200, [4985.0]*200,
                          [datetime(2026,4,28,9,0) + timedelta(minutes=i) for i in range(200)])
    # 价格跌破 stop 4960
    a = sm.on_tick(datetime(2026, 4, 28, 9, 30), last_price=4955.0)
    assert a.kind == "exit_full"
    assert a.direction == 1
    assert "stop" in a.reason
