"""Trade 配对 — FIFO 把 OPEN 和 CLOSE 配对算盈亏.

对每个品种 (symbol root) 按时序遍历 ON_TRADE:
  direction='0' offset='0'        → 买开 (push long_queue)
  direction='1' offset∈('1','3')  → 卖平多 (FIFO 取 long_queue)
  direction='1' offset='0'        → 卖开 (push short_queue) [long-only 策略不出现]
  direction='0' offset∈('1','3')  → 买平空 (FIFO 取 short_queue)

每对 OPEN+CLOSE = 一个 round-trip. 未配对 OPEN = 持有中.

滑点: 直接用 log 里的 [滑点] N.N ticks (策略 SlippageTracker 算的真滑点 —
  signal_price (decision price / trail trigger close) vs fill_price 之差).
  - log 约定: 正 ticks = 不利, 负 ticks = 有利
  - 我们内部 leg.slip_ticks_advantage = -log_ticks (正 = 有利, 与损益方向一致)
  - 损益 pnl = slip_ticks_advantage × tick_size × lots × multiplier
  - 同 oid 的 split fills 共享同 ticks (broker 拆批同价成交)

入场/出场原因: 关联同品种最近一次 POS_DECISION + EXECUTE + IND.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .log_parser import (
    LogEvent,
    parse_exec_open,
    parse_exec_stop,
    parse_execute,
    parse_ind,
    parse_on_trade,
    parse_pos_decision,
    parse_signal,
    parse_slip,
    parse_trail_stop,
)
from .specs import ContractSpec, get_spec


@dataclass
class TradeLeg:
    """一笔成交腿 (一个 ON_TRADE)."""
    ts: datetime
    symbol_root: str
    instrument_id: str | None  # al2607 等; log 里没明确写, 留 None
    side: str                  # "buy" | "sell"
    offset: str                # '0'=open, '1'=close_today, '3'=close_yesterday
    lots: int
    fill_price: float
    send_price: float | None   # 同品种最近一次 EXEC_OPEN/EXEC_STOP price (报告显示用)
    oid: int
    is_open: bool              # offset=='0'
    # 滑点 — 来自 log [滑点] tag, 同 oid 的 split fills 共享.
    # 单位 ticks/手, 正 = 有利 (与金额方向一致, 已 flip log 原始符号).
    slip_ticks: float | None = None
    # 关联的决策上下文 (开仓时填; 平仓时记录触发原因)
    decision: dict | None = None        # POS_DECISION fields
    execute: dict | None = None         # EXECUTE fields (含 reason)
    ind: dict | None = None             # IND values
    signal: dict | None = None          # SIGNAL fields
    trail_stop: dict | None = None      # TRAIL_STOP close vs line


@dataclass
class RoundTrip:
    """一个完整 (或部分) 配对."""
    symbol_root: str
    direction: str             # "long" | "short"
    lots: int
    entry: TradeLeg
    exit: TradeLeg | None      # None → 持有中
    multiplier: float

    @property
    def is_open(self) -> bool:
        return self.exit is None

    @property
    def gross_pnl(self) -> float:
        if self.exit is None:
            return 0.0
        if self.direction == "long":
            return (self.exit.fill_price - self.entry.fill_price) * self.lots * self.multiplier
        return (self.entry.fill_price - self.exit.fill_price) * self.lots * self.multiplier

    def slippage_pnl(self, spec: ContractSpec) -> float:
        """两端滑点损益合计 (signed, 正=有利, 负=不利)."""
        return _leg_slippage(self.entry, spec) + (
            _leg_slippage(self.exit, spec) if self.exit else 0.0
        )

    def total_slip_ticks(self) -> float:
        """两腿合计跳数 (signed, 正=有利, 负=不利, 单位 ticks/手)."""
        e = self.entry.slip_ticks or 0.0
        x = (self.exit.slip_ticks or 0.0) if self.exit else 0.0
        return e + x

    def commission(self, spec: ContractSpec) -> float:
        c = spec.calc_commission(self.entry.fill_price, self.entry.lots, offset="0")
        if self.exit is not None:
            c += spec.calc_commission(
                self.exit.fill_price, self.exit.lots, offset=self.exit.offset
            )
        return c

    def net_pnl(self, spec: ContractSpec) -> float:
        return self.gross_pnl + self.slippage_pnl(spec) - self.commission(spec)


def _leg_slippage(leg: TradeLeg, spec: ContractSpec) -> float:
    """单腿滑点损益 (signed, 正=有利, 负=不利).

    公式: pnl = slip_ticks × tick_size × lots × multiplier
    其中 leg.slip_ticks 已是 advantage 视角 (正=有利).
    """
    if leg.slip_ticks is None:
        return 0.0
    return leg.slip_ticks * spec.tick_size * leg.lots * spec.multiplier


# ---------------------------------------------------------------------------
# Pairing engine
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    """逐品种解析状态."""
    last_decision: dict | None = None       # POS_DECISION fields
    last_execute: dict | None = None        # EXECUTE fields
    last_ind: dict | None = None            # IND fields
    last_signal: dict | None = None         # SIGNAL fields
    last_trail_stop: dict | None = None     # TRAIL_STOP fields
    last_send_price: float | None = None    # 最近 EXEC_OPEN/EXEC_STOP 的挂单价
    pending_oid_send: dict[int, float] = field(default_factory=dict)  # oid → send price
    long_queue: deque[TradeLeg] = field(default_factory=deque)
    short_queue: deque[TradeLeg] = field(default_factory=deque)
    last_pending_send_price: float | None = None  # 上一笔 EXEC_OPEN/STOP send price (escalator 兜底)
    # 滑点关联: oid → ticks (advantage 视角, 正=有利)
    oid_slip_ticks: dict[int, float] = field(default_factory=dict)
    last_traded_oid: int | None = None      # 用于关联紧跟的 [滑点] log
    legs_by_oid: dict[int, list[TradeLeg]] = field(default_factory=dict)  # oid → 该 oid 的所有 leg


def _new_leg(ev: LogEvent, parsed: dict, st: _SymbolState) -> TradeLeg:
    side = "buy" if parsed["direction"] == "0" else "sell"
    is_open = parsed["offset"] == "0"
    send_price = st.pending_oid_send.get(parsed["oid"])
    if send_price is None:
        send_price = st.last_pending_send_price
    oid = parsed["oid"]
    # 同 oid 已有滑点 → 直接继承 (split fills 共享)
    inherited_slip = st.oid_slip_ticks.get(oid)
    leg = TradeLeg(
        ts=ev.event_ts,
        symbol_root=ev.symbol.upper(),
        instrument_id=None,
        side=side,
        offset=parsed["offset"],
        lots=parsed["vol"],
        fill_price=parsed["price"],
        send_price=send_price,
        oid=oid,
        is_open=is_open,
        slip_ticks=inherited_slip,
        decision=st.last_decision,
        execute=st.last_execute,
        ind=st.last_ind,
        signal=st.last_signal,
        trail_stop=st.last_trail_stop if not is_open else None,
    )
    st.legs_by_oid.setdefault(oid, []).append(leg)
    st.last_traded_oid = oid
    return leg


def pair_trades(events: Iterable[LogEvent]) -> list[RoundTrip]:
    states: dict[str, _SymbolState] = defaultdict(_SymbolState)
    round_trips: list[RoundTrip] = []

    for ev in events:
        sym = ev.symbol.upper()
        st = states[sym]
        tag = ev.tag

        if tag == "POS_DECISION":
            d = parse_pos_decision(ev.body)
            if d:
                st.last_decision = d
        elif tag == "EXECUTE":
            d = parse_execute(ev.body)
            if d:
                st.last_execute = d
        elif tag == "IND":
            d = parse_ind(ev.body)
            if d:
                st.last_ind = d
        elif tag == "SIGNAL":
            d = parse_signal(ev.body)
            if d:
                st.last_signal = d
        elif tag == "TRAIL_STOP":
            d = parse_trail_stop(ev.body)
            if d:
                st.last_trail_stop = d
        elif tag == "EXEC_OPEN":
            d = parse_exec_open(ev.body)
            if d is None:
                pass
            elif d.get("kind") == "send":
                st.last_pending_send_price = d["price"]
                # 如果下一行是 oid, 暂存到一个 "current send" 让 oid 行 pickup
                st._pending_send = d["price"]  # type: ignore[attr-defined]
            elif d.get("kind") == "oid":
                p = getattr(st, "_pending_send", None)
                if p is not None:
                    st.pending_oid_send[d["oid"]] = p
                    st._pending_send = None  # type: ignore[attr-defined]
        elif tag == "EXEC_STOP":
            d = parse_exec_stop(ev.body)
            if d:
                st.last_pending_send_price = d["price"]
                # EXEC_STOP 通常会接 ON_TRADE 而不会先打 oid log; 用 last_pending_send_price 作 fallback
        elif tag == "ON_TRADE":
            d = parse_on_trade(ev.body)
            if d is None:
                continue
            leg = _new_leg(ev, d, st)
            spec = get_spec(sym)

            if leg.is_open:
                if leg.side == "buy":
                    st.long_queue.append(leg)
                else:
                    st.short_queue.append(leg)
            else:
                # 平仓 — FIFO 取出对应方向开仓
                if leg.side == "sell":  # 卖平多
                    _close_fifo(st.long_queue, leg, "long", spec, round_trips)
                else:  # 买平空
                    _close_fifo(st.short_queue, leg, "short", spec, round_trips)
        elif tag == "滑点":
            ticks_log = parse_slip(ev.body)
            if ticks_log is None or st.last_traded_oid is None:
                continue
            # log 约定: 正=不利, 负=有利. 我们 flip → 正=有利 (与金额方向一致)
            advantage_ticks = -ticks_log
            oid = st.last_traded_oid
            st.oid_slip_ticks[oid] = advantage_ticks
            # 1) 写到该 oid 已创建的所有原始 leg (在 queue 里)
            for leg in st.legs_by_oid.get(oid, []):
                leg.slip_ticks = advantage_ticks
            # 2) 写到已配对 round_trip 里的 leg copies (entry_part/exit_part)
            for rt in round_trips:
                if rt.entry.symbol_root == sym and rt.entry.oid == oid:
                    rt.entry.slip_ticks = advantage_ticks
                if rt.exit and rt.exit.symbol_root == sym and rt.exit.oid == oid:
                    rt.exit.slip_ticks = advantage_ticks
            st.last_traded_oid = None  # consume

    # 收尾: 队列里剩下的都是持有中 — 转为 open RoundTrip
    for sym, st in states.items():
        spec = get_spec(sym)
        for leg in list(st.long_queue):
            round_trips.append(RoundTrip(
                symbol_root=sym, direction="long", lots=leg.lots,
                entry=leg, exit=None, multiplier=spec.multiplier,
            ))
        for leg in list(st.short_queue):
            round_trips.append(RoundTrip(
                symbol_root=sym, direction="short", lots=leg.lots,
                entry=leg, exit=None, multiplier=spec.multiplier,
            ))

    return round_trips


def _close_fifo(
    queue: deque[TradeLeg],
    close_leg: TradeLeg,
    direction: str,
    spec: ContractSpec,
    out: list[RoundTrip],
) -> None:
    remaining = close_leg.lots
    while remaining > 0 and queue:
        head = queue[0]
        take = min(head.lots, remaining)
        # 拆分 entry 和 exit, 保留剩余手数
        entry_part = TradeLeg(
            ts=head.ts, symbol_root=head.symbol_root, instrument_id=head.instrument_id,
            side=head.side, offset=head.offset, lots=take,
            fill_price=head.fill_price, send_price=head.send_price, oid=head.oid,
            is_open=True, slip_ticks=head.slip_ticks,
            decision=head.decision, execute=head.execute,
            ind=head.ind, signal=head.signal, trail_stop=head.trail_stop,
        )
        exit_part = TradeLeg(
            ts=close_leg.ts, symbol_root=close_leg.symbol_root, instrument_id=close_leg.instrument_id,
            side=close_leg.side, offset=close_leg.offset, lots=take,
            fill_price=close_leg.fill_price, send_price=close_leg.send_price, oid=close_leg.oid,
            is_open=False, slip_ticks=close_leg.slip_ticks,
            decision=close_leg.decision, execute=close_leg.execute,
            ind=close_leg.ind, signal=close_leg.signal, trail_stop=close_leg.trail_stop,
        )
        out.append(RoundTrip(
            symbol_root=head.symbol_root, direction=direction, lots=take,
            entry=entry_part, exit=exit_part, multiplier=spec.multiplier,
        ))
        if take == head.lots:
            queue.popleft()
        else:
            head.lots -= take  # type: ignore[misc]  # mutating dataclass; ok for queue head
        remaining -= take
    # 若 remaining > 0 (平仓量超出开仓队列) — 忽略, 可能是策略外的预存仓
