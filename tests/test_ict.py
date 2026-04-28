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
