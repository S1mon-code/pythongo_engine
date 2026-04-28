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

from .csv_parser import CsvTrade
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
    instrument_id: str | None  # al2607 等; log 来源时 None, CSV 来源时填入
    side: str                  # "buy" | "sell"
    offset: str                # '0'=open, '1'=close_today, '3'=close_yesterday
    lots: int
    fill_price: float
    send_price: float | None   # 同品种最近一次 EXEC_OPEN/EXEC_STOP price (报告显示用)
    oid: int
    is_open: bool              # offset=='0'
    # 滑点 — 来自 log [滑点] tag 或 CSV (send vs fill 差).
    # 单位 ticks/手, 正 = 有利 (与金额方向一致).
    slip_ticks: float | None = None
    # 关联的决策上下文 (开仓时填; 平仓时记录触发原因)
    decision: dict | None = None        # POS_DECISION fields
    execute: dict | None = None         # EXECUTE fields (含 reason)
    ind: dict | None = None             # IND values
    signal: dict | None = None          # SIGNAL fields
    trail_stop: dict | None = None      # TRAIL_STOP close vs line
    # ── CSV 流程专属字段 (broker 真值, log 流程下 None) ──
    fee: float = 0.0                    # 手续费 (元)
    broker_pnl: float | None = None     # broker 盯市盈亏 (含隔夜价差, 仅平仓有)
    per_trade_pnl: float | None = None  # 逐笔盈亏 (按本笔开仓价, 仅平仓有)
    full_name: str | None = None        # 棕榈油2609
    is_takeover: bool = False           # 隔夜接管的孤儿平仓标记


@dataclass
class RoundTrip:
    """一个完整 (或部分) 配对.

    特殊形态:
      - exit=None: 持有中 (open round trip)
      - is_takeover=True: 隔夜接管的孤儿平仓 (entry 是占位; 真正盈亏看 exit.broker_pnl)
    """
    symbol_root: str
    direction: str             # "long" | "short"
    lots: int
    entry: TradeLeg
    exit: TradeLeg | None      # None → 持有中
    multiplier: float
    is_takeover: bool = False  # entry 不是当日开仓, 是 state.json 接管的占位

    @property
    def is_open(self) -> bool:
        return self.exit is None

    @property
    def gross_pnl(self) -> float:
        # 隔夜接管孤儿平仓 — 没法算 gross (没有真开仓价), 用 broker_pnl 代替
        if self.is_takeover and self.exit is not None:
            return self.exit.broker_pnl or 0.0
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
        # 拆分时按 take/orig_lots 比例分摊手续费 (broker 报回的是整笔的)
        head_ratio = take / head.lots if head.lots else 1.0
        close_ratio = take / close_leg.lots if close_leg.lots else 1.0
        entry_part = TradeLeg(
            ts=head.ts, symbol_root=head.symbol_root, instrument_id=head.instrument_id,
            side=head.side, offset=head.offset, lots=take,
            fill_price=head.fill_price, send_price=head.send_price, oid=head.oid,
            is_open=True, slip_ticks=head.slip_ticks,
            decision=head.decision, execute=head.execute,
            ind=head.ind, signal=head.signal, trail_stop=head.trail_stop,
            fee=head.fee * head_ratio,
            broker_pnl=None,                 # 开仓没有 broker_pnl
            per_trade_pnl=None,
            full_name=head.full_name,
            is_takeover=head.is_takeover,
        )
        exit_part = TradeLeg(
            ts=close_leg.ts, symbol_root=close_leg.symbol_root, instrument_id=close_leg.instrument_id,
            side=close_leg.side, offset=close_leg.offset, lots=take,
            fill_price=close_leg.fill_price, send_price=close_leg.send_price, oid=close_leg.oid,
            is_open=False, slip_ticks=close_leg.slip_ticks,
            decision=close_leg.decision, execute=close_leg.execute,
            ind=close_leg.ind, signal=close_leg.signal, trail_stop=close_leg.trail_stop,
            fee=close_leg.fee * close_ratio,
            broker_pnl=(close_leg.broker_pnl * close_ratio
                        if close_leg.broker_pnl is not None else None),
            per_trade_pnl=(close_leg.per_trade_pnl * close_ratio
                           if close_leg.per_trade_pnl is not None else None),
            full_name=close_leg.full_name,
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


# ---------------------------------------------------------------------------
# CSV-driven pairing — broker 真值版 (2026-04-28+)
# ---------------------------------------------------------------------------


def _csv_to_leg(t: CsvTrade) -> TradeLeg:
    """CSV 一行 → TradeLeg (含 broker 真值字段)."""
    spec = get_spec(t.symbol_root)
    # 滑点 (单位: ticks/手, 正=有利, 与金额方向一致)
    slip_ticks: float | None = None
    if t.send_price is not None and t.fill_price is not None and spec.tick_size > 0:
        diff = t.fill_price - t.send_price
        # 买单: fill 比 send 低 = 有利; 卖单: fill 比 send 高 = 有利
        sign = -1 if t.side == "buy" else 1
        slip_ticks = sign * diff / spec.tick_size

    return TradeLeg(
        ts=t.fill_ts or t.send_ts,
        symbol_root=t.symbol_root,
        instrument_id=t.instrument_id,
        side=t.side,
        offset=t.offset,
        lots=t.fill_lots,
        fill_price=t.fill_price or 0.0,
        send_price=t.send_price,
        oid=t.seq,                       # CSV 序号当 oid 用 (broker 报单编号太长易混)
        is_open=(t.offset == "0"),
        slip_ticks=slip_ticks,
        fee=t.fee,
        broker_pnl=t.broker_pnl,
        per_trade_pnl=t.per_trade_pnl,
        full_name=t.full_name,
    )


def _attach_log_context(leg: TradeLeg, log_index: dict[str, list[LogEvent]]) -> None:
    """从 log_events 里找 leg 同 symbol 时间最近的 POS_DECISION / EXECUTE / IND /
    SIGNAL / TRAIL_STOP 注入到 leg.

    log_events 按 symbol 预分组 (大写 root). 用线性扫描 (每品种事件不多).
    """
    sym_events = log_index.get(leg.symbol_root) or log_index.get(
        leg.symbol_root.lower()
    )
    if not sym_events:
        return
    last_dec = last_exec = last_ind = last_sig = last_trail = None
    for ev in sym_events:
        if ev.event_ts > leg.ts:
            break  # 之后的 event 不影响这条 leg
        if ev.tag == "POS_DECISION":
            d = parse_pos_decision(ev.body)
            if d:
                last_dec = d
        elif ev.tag == "EXECUTE":
            d = parse_execute(ev.body)
            if d:
                last_exec = d
        elif ev.tag == "IND":
            d = parse_ind(ev.body)
            if d:
                last_ind = d
        elif ev.tag == "SIGNAL":
            d = parse_signal(ev.body)
            if d:
                last_sig = d
        elif ev.tag == "TRAIL_STOP":
            d = parse_trail_stop(ev.body)
            if d:
                last_trail = d
        elif ev.tag == "EXEC_STOP":
            # exec_stop 也用 EXECUTE 接口暴露 reason
            d = parse_exec_stop(ev.body)
            if d:
                last_exec = {
                    "action": d["reason"],  # HARD_STOP / TRAIL_STOP / 等
                    "price": d["price"],
                    "own_pos": 0,
                    "target": 0,
                    "reason": f"{d['reason']} {d['side']} {d['lots']}手",
                }
    leg.decision = last_dec
    leg.execute = last_exec
    leg.ind = last_ind
    leg.signal = last_sig
    if not leg.is_open:
        leg.trail_stop = last_trail  # 平仓才显示移损线


def _build_log_index(events: Iterable[LogEvent]) -> dict[str, list[LogEvent]]:
    """按 symbol (大写 root) 分组 LogEvent. 同 symbol 内保持时序."""
    out: dict[str, list[LogEvent]] = defaultdict(list)
    for ev in events:
        out[ev.symbol.upper()].append(ev)
    return out


def pair_from_csv(
    csv_trades: list[CsvTrade],
    log_events: Iterable[LogEvent] | None = None,
) -> list[RoundTrip]:
    """从 broker CSV 配对 round-trips.

    与 pair_trades (log 版) 的区别:
      - 使用 broker 报回的真实成交 (含手续费 / broker_pnl / 逐笔 pnl)
      - 处理隔夜接管的孤儿平仓 (today close, no today open) → is_takeover RoundTrip
      - 撤单不进 round_trips (调用方单独处理)
      - log_events 仅用于注入决策上下文 (POS_DECISION / EXECUTE / IND / SIGNAL / TRAIL_STOP)
    """
    log_index = _build_log_index(log_events) if log_events else {}

    # 按 instrument_id 分队列 (避免 hc2510 / hc2511 跨合约混)
    long_q: dict[str, deque[TradeLeg]] = defaultdict(deque)
    short_q: dict[str, deque[TradeLeg]] = defaultdict(deque)
    round_trips: list[RoundTrip] = []

    # 只配对成交 (跳过完全撤单)
    fills = [t for t in csv_trades if t.is_filled and t.fill_lots > 0]
    fills.sort(key=lambda t: t.fill_ts or t.send_ts)

    for t in fills:
        leg = _csv_to_leg(t)
        _attach_log_context(leg, log_index)
        spec = get_spec(t.symbol_root)
        inst = t.instrument_id

        if leg.is_open:
            (long_q if leg.side == "buy" else short_q)[inst].append(leg)
            continue

        # 平仓 — FIFO 取对应方向开仓
        queue = long_q[inst] if leg.side == "sell" else short_q[inst]
        direction = "long" if leg.side == "sell" else "short"

        if not queue:
            # 隔夜接管的孤儿平仓 — 没今日开仓配对 (e.g. JM 21:16 trail_stop)
            placeholder = TradeLeg(
                ts=leg.ts, symbol_root=leg.symbol_root,
                instrument_id=leg.instrument_id,
                side="buy" if direction == "long" else "sell",
                offset="0", lots=leg.lots,
                fill_price=0.0,                # 真开仓价在昨天, 这里没有
                send_price=None, oid=-1, is_open=True,
                is_takeover=True,
                full_name=leg.full_name,
            )
            round_trips.append(RoundTrip(
                symbol_root=leg.symbol_root, direction=direction, lots=leg.lots,
                entry=placeholder, exit=leg, multiplier=spec.multiplier,
                is_takeover=True,
            ))
            continue

        _close_fifo(queue, leg, direction, spec, round_trips)

    # 收尾: 持有中
    for inst, q in long_q.items():
        spec = get_spec(_root_from_inst(inst))
        for leg in q:
            round_trips.append(RoundTrip(
                symbol_root=leg.symbol_root, direction="long", lots=leg.lots,
                entry=leg, exit=None, multiplier=spec.multiplier,
            ))
    for inst, q in short_q.items():
        spec = get_spec(_root_from_inst(inst))
        for leg in q:
            round_trips.append(RoundTrip(
                symbol_root=leg.symbol_root, direction="short", lots=leg.lots,
                entry=leg, exit=None, multiplier=spec.multiplier,
            ))

    return round_trips


def _root_from_inst(instrument_id: str) -> str:
    import re as _re
    m = _re.match(r"^([A-Za-z]+)", instrument_id.strip())
    return m.group(1).upper() if m else instrument_id.upper()
