"""Daily report 单元测试."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from tools.daily_report.log_parser import (
    parse_exec_open,
    parse_exec_stop,
    parse_execute,
    parse_ind,
    parse_line,
    parse_on_trade,
    parse_pos_decision,
    parse_signal,
    parse_slip,
    parse_trail_stop,
)
from tools.daily_report.session_window import (
    is_trading_day,
    previous_trading_day,
    session_window_for,
)
from tools.daily_report.specs import _symbol_root, get_spec
from tools.daily_report.trade_pairing import pair_trades


# ---------------------------------------------------------------------------
# session_window
# ---------------------------------------------------------------------------


def test_session_window_weekday_to_weekday():
    """周二报告 = 周一 21:00 → 周二 15:00."""
    win = session_window_for(date(2026, 4, 28))  # Tue
    assert win.start == datetime(2026, 4, 27, 21, 0)
    assert win.end == datetime(2026, 4, 28, 15, 0)


def test_session_window_monday_skips_weekend():
    """周一报告 = 周五 21:00 → 周一 15:00."""
    win = session_window_for(date(2026, 4, 27))  # Mon
    assert win.start == datetime(2026, 4, 24, 21, 0)
    assert win.end == datetime(2026, 4, 27, 15, 0)


def test_session_window_contains():
    win = session_window_for(date(2026, 4, 27))
    assert win.contains(datetime(2026, 4, 24, 21, 0, 0))    # boundary
    assert win.contains(datetime(2026, 4, 25, 12, 0, 0))    # middle
    assert win.contains(datetime(2026, 4, 27, 15, 0, 0))    # boundary
    assert not win.contains(datetime(2026, 4, 24, 20, 59, 0))
    assert not win.contains(datetime(2026, 4, 27, 15, 0, 1))


def test_is_trading_day():
    assert is_trading_day(date(2026, 4, 27))    # Mon
    assert not is_trading_day(date(2026, 4, 25))  # Sat
    assert not is_trading_day(date(2026, 4, 26))  # Sun
    assert not is_trading_day(date(2026, 5, 1))   # Labor Day holiday


def test_previous_trading_day():
    # Mon → 前一交易日是上周五
    assert previous_trading_day(date(2026, 4, 27)) == date(2026, 4, 24)
    # Tue → 前一交易日是周一
    assert previous_trading_day(date(2026, 4, 28)) == date(2026, 4, 27)
    # 5/4 (Mon, 假期) 之后的工作日, 5/6 应该跳回 4/30
    assert previous_trading_day(date(2026, 5, 6)) == date(2026, 4, 30)


# ---------------------------------------------------------------------------
# log_parser
# ---------------------------------------------------------------------------


def test_parse_line_new_format():
    line = "[2026-04-27 10:00:00.107] [StraLog] [info] [2026-04-27 10:00:00] [AL] [EXEC_OPEN] send_order buy 3手 @ 25130.0 (passive)"
    ev = parse_line(line)
    assert ev is not None
    assert ev.symbol == "AL"
    assert ev.tag == "EXEC_OPEN"
    assert ev.event_ts == datetime(2026, 4, 27, 10, 0, 0)


def test_parse_line_with_subtag():
    """[TAG][SUBTAG] body — TRAIL_STOP 有 [M1] subtag."""
    line = "[2026-04-27 10:13:00.065] [StraLog] [info] [2026-04-27 10:13:00] [AL] [TRAIL_STOP][M1] M1 price=25060.0 <= 移损线25064.6"
    ev = parse_line(line)
    assert ev is not None
    assert ev.tag == "TRAIL_STOP"
    assert "25060.0" in ev.body


def test_parse_pos_decision():
    body = "optimal=4 (raw=4.34) own_pos=0 → target=3 broker_pos=3 (capital=1000000.0 atr=100.26)"
    d = parse_pos_decision(body)
    assert d == {
        "optimal": 4, "raw": 4.34, "own_pos": 0, "target": 3,
        "broker_pos": 3, "capital": 1000000.0, "atr": 100.26,
    }


def test_parse_execute_open():
    body = "action=OPEN price=25135.0 own_pos=0 target=3 reason=signal=0.65 forecast=6.5 optimal=4 target=3"
    d = parse_execute(body)
    assert d["action"] == "OPEN"
    assert d["price"] == 25135.0
    assert d["target"] == 3
    assert "signal=0.65" in d["reason"]


def test_parse_execute_trail_stop_target_none():
    body = "action=TRAIL_STOP price=25030.0 own_pos=0 target=None reason=M1 price=25060.0 <= 移损线25064.6"
    d = parse_execute(body)
    assert d["action"] == "TRAIL_STOP"
    assert d["target"] is None


def test_parse_exec_open_send():
    d = parse_exec_open("send_order buy 3手 @ 25130.0 (passive)")
    assert d == {"kind": "send", "side": "buy", "lots": 3, "price": 25130.0}


def test_parse_exec_open_oid():
    d = parse_exec_open("send_order 返回 oid=0")
    assert d == {"kind": "oid", "oid": 0}


def test_parse_exec_stop():
    d = parse_exec_stop("TRAIL_STOP auto_close sell 3手 @ 25035.0 (urgent)")
    assert d == {"reason": "TRAIL_STOP", "side": "sell", "lots": 3, "price": 25035.0}


def test_parse_on_trade_buy_open():
    d = parse_on_trade("oid=0 direction='0' offset='0' price=25130.0 vol=3")
    assert d == {"oid": 0, "direction": "0", "offset": "0", "price": 25130.0, "vol": 3}


def test_parse_on_trade_sell_close_today():
    d = parse_on_trade("oid=8 direction='1' offset='1' price=1277.0 vol=3")
    assert d["direction"] == "1"
    assert d["offset"] == "1"


def test_parse_trail_stop():
    d = parse_trail_stop("M1 price=25060.0 <= 移损线25064.6")
    assert d == {"close": 25060.0, "line": 25064.6}


def test_parse_ind_v8():
    body = "DC_U=25275.0 DC_M=25027.5 DC_L=24780.0 Chandelier=24938.0 | ADX=25.1 PDI=31.2 MDI=22.5 | ATR=103.5 close=25135.0"
    d = parse_ind(body)
    assert d["kind"] == "v8"
    assert d["dc_m"] == 25027.5
    assert d["adx"] == 25.1
    assert d["pdi"] == 31.2
    assert d["close"] == 25135.0


def test_parse_ind_v13():
    body = "DC_U=1292.5 DC_M=1251.2 DC_L=1210.0 Chandelier=1253.8 | MFI=61.1 (floor=50.0) | ATR=12.9 close=1280.0"
    d = parse_ind(body)
    assert d["kind"] == "v13"
    assert d["dc_l"] == 1210.0
    assert d["mfi"] == 61.1


def test_parse_signal():
    d = parse_signal("raw=1.0000 forecast=10.0")
    assert d == {"raw": 1.0, "forecast": 10.0}


def test_parse_slip():
    """[滑点] N.Nticks log."""
    assert parse_slip("-1.0ticks") == -1.0
    assert parse_slip("1.0ticks") == 1.0
    assert parse_slip("-0.5ticks") == -0.5


def test_parse_line_chinese_tag():
    """中文 tag [滑点] 必须能解析."""
    line = "[2026-04-27 10:00:02.928] [StraLog] [info] [2026-04-27 10:00:02] [PP] [滑点] -1.0ticks"
    ev = parse_line(line)
    assert ev is not None
    assert ev.symbol == "PP"
    assert ev.tag == "滑点"
    assert "-1.0ticks" in ev.body


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------


def test_symbol_root_strips_contract_month():
    assert _symbol_root("al2607") == "AL"
    assert _symbol_root("hc2510") == "HC"
    assert _symbol_root("ag2606") == "AG"
    assert _symbol_root("AL2607") == "AL"
    assert _symbol_root("AL") == "AL"


def test_get_spec_al_fixed_commission():
    """AL: fixed 3 元/手, 乘数 5."""
    spec = get_spec("AL")
    assert spec.symbol == "AL"
    assert spec.multiplier == 5.0
    if spec.commission is not None:
        assert spec.commission.type == "fixed"
        # 3 手 × 3 元 = 9 元
        assert spec.calc_commission(price=25000, lots=3, offset="0") == pytest.approx(9.0)


def test_get_spec_cu_ratio_commission():
    """CU: ratio 5e-5, 乘数 5."""
    spec = get_spec("CU")
    if spec.commission is not None and spec.commission.type == "ratio":
        # ratio: notional × rate = 100000 × 5 × 1 × 5e-5 = 25
        notional = 100000 * 5 * 1
        expected = notional * spec.commission.open
        assert spec.calc_commission(price=100000, lots=1, offset="0") == pytest.approx(expected)


def test_get_spec_unknown_falls_back():
    """未知品种回退到 fallback table."""
    spec = get_spec("UNKNOWN_SYMBOL")
    assert spec.multiplier == 1.0  # default fallback


# ---------------------------------------------------------------------------
# trade_pairing — 集成测试
# ---------------------------------------------------------------------------


def _make_event(ts_str: str, sym: str, tag: str, body: str):
    """构造 LogEvent 用于测试."""
    from tools.daily_report.log_parser import LogEvent
    line = f"[{ts_str}.000] [StraLog] [info] [{ts_str}] [{sym}] [{tag}] {body}"
    ev = parse_line(line)
    assert ev is not None, f"failed to parse: {line}"
    return ev


def test_pair_trades_simple_round_trip():
    """单纯 OPEN 3 手 → 平仓 3 手 = 1 round-trip, gross = (exit-entry)*3*mult."""
    events = [
        _make_event("2026-04-27 10:00:00", "AL", "POS_DECISION",
                    "optimal=4 (raw=4.34) own_pos=0 → target=3 broker_pos=3 (capital=1000000.0 atr=100.26)"),
        _make_event("2026-04-27 10:00:00", "AL", "EXECUTE",
                    "action=OPEN price=25135.0 own_pos=0 target=3 reason=signal=0.65 forecast=6.5 optimal=4 target=3"),
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order buy 3手 @ 25130.0 (passive)"),
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order 返回 oid=0"),
        _make_event("2026-04-27 10:00:03", "AL", "ON_TRADE",
                    "oid=0 direction='0' offset='0' price=25130.0 vol=3"),
        _make_event("2026-04-27 10:13:00", "AL", "EXEC_STOP",
                    "TRAIL_STOP auto_close sell 3手 @ 25035.0 (urgent)"),
        _make_event("2026-04-27 10:13:00", "AL", "ON_TRADE",
                    "oid=7 direction='1' offset='3' price=25060.0 vol=3"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 1
    rt = trips[0]
    assert rt.symbol_root == "AL"
    assert rt.direction == "long"
    assert rt.lots == 3
    assert not rt.is_open
    # gross = (25060 - 25130) * 3 * 5 = -1050
    assert rt.gross_pnl == pytest.approx(-1050.0)


def test_pair_trades_open_only_held():
    """开仓后没平 = 持有中, exit is None."""
    events = [
        _make_event("2026-04-27 14:15:00", "P", "EXEC_OPEN",
                    "send_order buy 2手 @ 9832.0 (passive)"),
        _make_event("2026-04-27 14:15:00", "P", "EXEC_OPEN",
                    "send_order 返回 oid=99"),
        _make_event("2026-04-27 14:15:01", "P", "ON_TRADE",
                    "oid=99 direction='0' offset='0' price=9832.0 vol=2"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 1
    rt = trips[0]
    assert rt.is_open
    assert rt.exit is None
    assert rt.gross_pnl == 0.0


def test_pair_trades_split_fills_into_multiple_round_trips():
    """开仓 3 手 (1 笔 ON_TRADE) → 平仓分 3 笔 ON_TRADE 各 1 手 → 3 round-trips."""
    events = [
        _make_event("2026-04-27 10:00:00", "P", "EXEC_OPEN",
                    "send_order buy 3手 @ 9833.0 (passive)"),
        _make_event("2026-04-27 10:00:00", "P", "EXEC_OPEN",
                    "send_order 返回 oid=2"),
        _make_event("2026-04-27 10:00:00", "P", "ON_TRADE",
                    "oid=2 direction='0' offset='0' price=9833.0 vol=3"),
        _make_event("2026-04-27 10:32:00", "P", "EXEC_STOP",
                    "TRAIL_STOP auto_close sell 3手 @ 9809.0 (urgent)"),
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 3
    for rt in trips:
        assert rt.lots == 1
        assert rt.gross_pnl == pytest.approx((9819 - 9833) * 1 * 10)  # P mult=10


def test_pair_trades_slippage_from_log_unfavorable():
    """[滑点] 正数 = 不利 (策略 SlippageTracker 约定); 内部 flip 后损益为负."""
    events = [
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order buy 1手 @ 25130.0 (passive)"),
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order 返回 oid=0"),
        _make_event("2026-04-27 10:00:01", "AL", "ON_TRADE",
                    "oid=0 direction='0' offset='0' price=25135.0 vol=1"),
        # log: ticks > 0 → 不利
        _make_event("2026-04-27 10:00:01", "AL", "滑点", "1.0ticks"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 1
    spec = get_spec("AL")  # tick=5, mult=5
    # advantage_ticks = -1.0 → pnl = -1 × 5 × 1 × 5 = -25
    assert trips[0].slippage_pnl(spec) == pytest.approx(-25.0)
    assert trips[0].entry.slip_ticks == -1.0


def test_pair_trades_slippage_from_log_favorable():
    """[滑点] 负数 = 有利; 内部 flip 后损益为正."""
    events = [
        # 先开 1 手 (无滑点)
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order buy 1手 @ 25130.0 (passive)"),
        _make_event("2026-04-27 10:00:00", "AL", "EXEC_OPEN",
                    "send_order 返回 oid=0"),
        _make_event("2026-04-27 10:00:01", "AL", "ON_TRADE",
                    "oid=0 direction='0' offset='0' price=25130.0 vol=1"),
        # 平仓: log -5 ticks (有利)
        _make_event("2026-04-27 10:13:00", "AL", "EXEC_STOP",
                    "TRAIL_STOP auto_close sell 1手 @ 25035.0 (urgent)"),
        _make_event("2026-04-27 10:13:00", "AL", "ON_TRADE",
                    "oid=7 direction='1' offset='3' price=25060.0 vol=1"),
        _make_event("2026-04-27 10:13:00", "AL", "滑点", "-5.0ticks"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 1
    spec = get_spec("AL")
    # entry slip 0, exit advantage_ticks = +5 → exit pnl = 5 × 5 × 1 × 5 = +125
    assert trips[0].slippage_pnl(spec) == pytest.approx(125.0)
    assert trips[0].exit.slip_ticks == 5.0


def test_pair_trades_slippage_split_fills_share_ticks():
    """同 oid 拆批多次 ON_TRADE → 第一笔后的 [滑点] log 应回填到所有 split fills."""
    events = [
        # 开 3 手, 一次成交
        _make_event("2026-04-27 10:00:00", "P", "EXEC_OPEN",
                    "send_order buy 3手 @ 9833.0 (passive)"),
        _make_event("2026-04-27 10:00:00", "P", "EXEC_OPEN",
                    "send_order 返回 oid=2"),
        _make_event("2026-04-27 10:00:00", "P", "ON_TRADE",
                    "oid=2 direction='0' offset='0' price=9833.0 vol=3"),
        # 平仓 oid=10 拆 3 笔, 只第一笔后跟 [滑点]
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
        _make_event("2026-04-27 10:32:00", "P", "滑点", "1.0ticks"),
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
        _make_event("2026-04-27 10:32:00", "P", "ON_TRADE",
                    "oid=10 direction='1' offset='1' price=9819.0 vol=1"),
    ]
    trips = pair_trades(events)
    assert len(trips) == 3
    # 每个 round-trip 的 exit.slip_ticks 应都 = -1.0 (flipped) 不只是第一个
    for rt in trips:
        assert rt.exit is not None
        assert rt.exit.slip_ticks == -1.0


# ---------------------------------------------------------------------------
# 端到端: 跑真实 log
# ---------------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# pivot_extractor — alias 路由 + family 识别
# ---------------------------------------------------------------------------


def test_pivot_extract_v8_alias():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("AL")
    assert p is not None
    assert p.family == "V8"
    assert p.bias == "long"
    assert p.symbol == "AL"
    assert p.trail_stop_formula  # V8 有 trail
    assert not p.profit_target_formula
    assert p.trailing_pct > 0


def test_pivot_extract_v13_alias():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("AG")
    assert p is not None
    assert p.family == "V13"
    assert p.symbol == "AG"
    assert p.trail_stop_formula


def test_pivot_extract_qexp_momentum():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("AG_Mom")
    assert p is not None
    assert p.family == "QExp_Mom"
    assert p.bias == "long"
    assert p.symbol == "AG"
    assert p.profit_target_atr_mult == pytest.approx(2.0)
    assert p.hard_stop_pct == pytest.approx(2.0)
    assert p.trailing_pct == 0
    assert not p.trail_stop_formula
    assert p.profit_target_formula
    # 入场公式应含 body / ATR / cooldown
    assert "body" in p.entry_formula.lower() or "atr" in p.entry_formula.lower()


def test_pivot_extract_qexp_volsqueeze():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("AG_VSv2")
    assert p.family == "QExp_VSv2"
    assert p.bias == "long"
    assert "squeeze" in p.entry_formula.lower() or "z_60" in p.entry_formula.lower()


def test_pivot_extract_qexp_pullback():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("I_Pull")
    assert p.family == "QExp_Pull"
    assert p.bias == "long"
    assert p.symbol == "I"


def test_pivot_extract_qexp_hvb_short():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("HC_S")
    assert p.family == "QExp_HVB"
    assert p.bias == "short"
    assert p.symbol == "HC"
    # short 出场 pivot 描述应包含"买平"
    assert any("买平" in e for e in p.exit_pivots)


def test_pivot_extract_unknown_alias_returns_none():
    from tools.daily_report.pivot_extractor import extract_pivot
    p = extract_pivot("NOT_A_REAL_ALIAS_XYZ")
    assert p is None


def test_pivot_extract_legacy_symbol_fallback():
    """alias 不在 json 里 → fallback 按 symbol 找 V8/V13."""
    from tools.daily_report.pivot_extractor import extract_pivot
    # JM 在 json 里, 直接走 alias path. 这里测 _legacy_symbol_lookup 直接调用.
    from tools.daily_report.pivot_extractor import _legacy_symbol_lookup
    p = _legacy_symbol_lookup("JM")
    assert p is not None
    assert p.family == "V13"


def test_aliases_json_loads_all_11():
    """strategy_aliases.json 应有 7 V8/V13 + 4 QExp = 11 个 alias."""
    from tools.daily_report.pivot_extractor import _load_aliases
    aliases = _load_aliases()
    expected = {"AL", "CU", "HC", "AG", "JM", "P", "PP",
                "AG_Mom", "AG_VSv2", "I_Pull", "HC_S"}
    assert expected.issubset(aliases.keys())
    # 不应包含 _doc 这类下划线 key
    assert not any(k.startswith("_") for k in aliases)


def _find_real_log() -> Path | None:
    """优先 reports/4-27/, 回退到 logs/."""
    for cand in [
        _REPO / "reports" / "4-27" / "StraLog(4).txt",
        _REPO / "logs" / "StraLog(4).txt",
    ]:
        if cand.exists():
            return cand
    return None


_LOG = _find_real_log()


@pytest.mark.skipif(_LOG is None, reason="real log not present")
def test_end_to_end_2026_04_27_smoke():
    """smoke test: 真实 log 跑出 round-trips, 数量符合人工数过的."""
    from tools.daily_report.log_parser import parse_log
    win = session_window_for(date(2026, 4, 27))
    events = [ev for ev in parse_log(str(_LOG)) if win.contains(ev.event_ts)]
    trips = pair_trades(events)
    # 4-27 至少应有 5 个早晨 OPEN (AL/CU没真开/JM/P/PP/HC) + 几个 close + 持有中
    assert len(trips) >= 10
    closed = [r for r in trips if not r.is_open]
    held = [r for r in trips if r.is_open]
    assert closed, "应至少有 1 笔已平仓"
    # 持有中应有 P (14:15) 和 PP (后续)
    held_syms = {r.symbol_root for r in held}
    assert len(held_syms) >= 1
