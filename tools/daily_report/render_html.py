"""HTML 渲染 — 白色主题中文日报.

按 Simon 要求:
  - 白色背景
  - 全部中文
  - 去掉手续费(净利 = 毛利 + 滑点损益)
  - 在每品种 pivot box 显示移动止损线生成公式
"""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .csv_parser import CsvTrade
from .pivot_extractor import PivotSpec
from .specs import ContractSpec, get_spec
from .trade_pairing import RoundTrip, TradeLeg


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    target_date: date
    window_start: datetime
    window_end: datetime
    total_round_trips: int
    closed_count: int
    open_count: int
    takeover_count: int          # 隔夜接管的孤儿平仓数
    cancelled_count: int          # 撤单数
    symbols: list[str]
    gross_pnl: float              # 当日策略层毛盈亏 (剔除 takeover)
    slippage_pnl: float
    net_pnl: float                # broker_pnl - fee (有 broker 时) / 否则 gross + slip
    avg_slip_ticks: float
    avg_leg_ticks: float
    # broker 真值字段 (CSV 流程才有)
    broker_pnl_total: float       # broker 平仓盈亏合计 (含隔夜价差)
    per_trade_pnl_total: float    # 逐笔盈亏合计 (按本笔开仓价)
    fee_total: float              # 手续费合计
    has_broker_data: bool         # 标记是否有 broker 真值数据


def _aggregate(trips: list[RoundTrip], target_date: date,
               win_start: datetime, win_end: datetime,
               cancelled_count: int = 0) -> Summary:
    gross = slip = 0.0
    broker_total = per_total = fee_total = 0.0
    symbols: set[str] = set()
    closed = open_n = takeover_n = 0
    has_broker = False
    total_ticks_sum = 0.0
    total_legs = 0
    for rt in trips:
        symbols.add(rt.symbol_root)
        spec = get_spec(rt.symbol_root)
        leg_pnl = rt.slippage_pnl(spec)
        total_ticks_sum += abs(rt.total_slip_ticks())
        total_legs += 1 if rt.is_open else 2

        # 累计 broker 真值字段 (CSV 流程)
        if rt.entry.fee:
            fee_total += rt.entry.fee
        if rt.exit and rt.exit.fee:
            fee_total += rt.exit.fee
        if rt.exit and rt.exit.broker_pnl is not None:
            broker_total += rt.exit.broker_pnl
            has_broker = True
        if rt.exit and rt.exit.per_trade_pnl is not None:
            per_total += rt.exit.per_trade_pnl

        if rt.is_takeover:
            takeover_n += 1
            closed += 1   # takeover 也算已平仓
            slip += leg_pnl
        elif rt.is_open:
            open_n += 1
            slip += leg_pnl
        else:
            closed += 1
            gross += rt.gross_pnl
            slip += leg_pnl
    n = len(trips)
    avg_per_trip = total_ticks_sum / n if n else 0.0
    avg_per_leg = total_ticks_sum / total_legs if total_legs else 0.0
    # 净盈亏: 有 broker 数据时用 broker 真值 - 手续费; 否则用 gross + slip
    net = (broker_total - fee_total) if has_broker else (gross + slip)
    return Summary(
        target_date=target_date,
        window_start=win_start, window_end=win_end,
        total_round_trips=len(trips),
        closed_count=closed, open_count=open_n,
        takeover_count=takeover_n,
        cancelled_count=cancelled_count,
        symbols=sorted(symbols),
        gross_pnl=gross, slippage_pnl=slip,
        net_pnl=net,
        avg_slip_ticks=avg_per_trip,
        avg_leg_ticks=avg_per_leg,
        broker_pnl_total=broker_total,
        per_trade_pnl_total=per_total,
        fee_total=fee_total,
        has_broker_data=has_broker,
    )


# ---------------------------------------------------------------------------
# 白色主题样式
# ---------------------------------------------------------------------------


_CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 40px; background: #ffffff; color: #1a1a1a;
       font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC',
                    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
       line-height: 1.55; font-size: 13px; }
.wrap { max-width: 1280px; margin: 0 auto; }
h1 { color: #1a1a1a; font-size: 22px; margin: 0 0 4px 0; font-weight: 600;
     letter-spacing: -0.2px; }
h2 { color: #1a1a1a; font-size: 15px; margin: 28px 0 10px 0;
     padding-bottom: 6px; border-bottom: 1px solid #1a1a1a;
     font-weight: 600; letter-spacing: 0.2px; }
h3 { color: #555; font-size: 12px; margin: 18px 0 6px 0; font-weight: 600;
     text-transform: uppercase; letter-spacing: 0.5px; }
.subtitle { color: #777; font-size: 12px; margin-bottom: 20px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
         gap: 0; margin: 12px 0 8px 0; border: 1px solid #d0d0d0; }
.card { padding: 14px 18px; border-right: 1px solid #d0d0d0; }
.card:last-child { border-right: none; }
.card .label { font-size: 11px; color: #666; margin-bottom: 6px;
                text-transform: uppercase; letter-spacing: 0.3px; }
.card .value { font-size: 20px; font-weight: 600; color: #1a1a1a;
                font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px;
        margin: 0 0 8px 0; }
th { text-align: left; padding: 8px 10px; background: transparent;
     border-bottom: 1px solid #1a1a1a; color: #1a1a1a; font-weight: 600;
     font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px; }
td { padding: 7px 10px; border-bottom: 1px solid #eeeeee; color: #1a1a1a; }
tr:last-child td { border-bottom: 1px solid #d0d0d0; }
.num { text-align: right; font-variant-numeric: tabular-nums;
       font-family: 'SF Mono', Menlo, Consolas, monospace; }
.neg { color: #b10000; }
.dim { color: #999; }
.section { padding: 0; margin-bottom: 24px; }
.chart-box { margin: 8px 0 14px 0; border: 1px solid #e0e0e0; padding: 4px;
              background: #fafafa; }
.chart-box img { display: block; max-width: 100%; height: auto; }
.pivot-box { background: #fafafa; border: 1px solid #e0e0e0;
             padding: 12px 16px; margin: 8px 0 12px 0; font-size: 12.5px; }
.pivot-box .formula { font-family: 'SF Mono', Menlo, Consolas, monospace;
                       font-size: 12px; color: #1a1a1a; padding: 4px 0;
                       white-space: pre-wrap; }
.pivot-box .conds { color: #1a1a1a; font-size: 12.5px; padding-left: 20px;
                     margin: 2px 0 6px 0; }
.pivot-box .conds li { margin: 2px 0; }
.pivot-box .label { font-size: 11px; color: #666; margin-top: 10px;
                     font-weight: 600; text-transform: uppercase;
                     letter-spacing: 0.3px; }
.pivot-box hr { border: 0; border-top: 1px solid #e0e0e0; margin: 10px 0; }
.muted { color: #999; font-size: 11px; }
.footer { color: #999; font-size: 11px; text-align: left; margin-top: 32px;
          padding-top: 12px; border-top: 1px solid #e0e0e0; }
.strategy-meta { color: #555; font-size: 11.5px; margin-top: 2px; }
.takeover-tag { display: inline-block; padding: 1px 6px; border-radius: 3px;
                font-size: 10.5px; background: #fff4e6; color: #b35900;
                border: 1px solid #ffd99e; }
@media print {
  body { padding: 16px; }
  .section { page-break-inside: avoid; }
}
"""


def _money(x: float) -> str:
    sign = "−" if x < 0 else ""
    return f"{sign}¥{abs(x):,.2f}"


def _money_signed(x: float) -> str:
    if abs(x) < 0.005:
        return "¥0.00"
    return _money(x)


def _money_class(x: float) -> str:
    """只标记负数(红);正数与零保持黑色,避免颜色情绪化."""
    if x < -0.005: return "neg"
    return ""


def _slip_with_ticks(rt: "RoundTrip", spec: ContractSpec) -> str:
    """滑点损益 + (合计跳数 · 平均每腿跳数).

    跳数来源: log [滑点] N.N ticks (策略 SlippageTracker 算的真滑点, 每手单位).
    我们内部已 flip 符号 → 正=有利, 负=不利, 与金额方向一致.

    合计 = entry.ticks + exit.ticks (signed)
    平均 = 合计 / 腿数 (已平仓 2 腿, 持有中 1 腿)
    """
    pnl = rt.slippage_pnl(spec)
    total_ticks = rt.total_slip_ticks()
    legs = 1 if rt.is_open else 2
    if abs(total_ticks) < 0.05:
        return f"{_money_signed(pnl)} (0跳)"
    if legs == 1:
        return f"{_money_signed(pnl)} ({total_ticks:+.1f}跳)"
    avg = total_ticks / legs
    return f"{_money_signed(pnl)} (合{total_ticks:+.1f}跳·均{avg:+.1f}跳)"


def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.1f}"
    return f"{p:.2f}"


def _h(s: str) -> str:
    return html.escape(s, quote=False)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _render_summary(s: Summary) -> str:
    if s.has_broker_data:
        # broker 真值版 (CSV 流程)
        net_label = "净盈亏 (broker − 手续费)"
        net_val = s.net_pnl
        col2_label = "broker 平仓盈亏"
        col2_val = s.broker_pnl_total
        col3_label = "逐笔盈亏 (本笔成本)"
        col3_val = s.per_trade_pnl_total
        col4_label = "手续费"
        col4_val_str = f"¥{s.fee_total:,.2f}"
        col4_class = ""
    else:
        # 旧 log 流程
        net_label = "净盈亏"
        net_val = s.net_pnl
        col2_label = "毛盈亏"
        col2_val = s.gross_pnl
        col3_label = "滑点损益"
        col3_val = s.slippage_pnl
        col4_label = "平均滑点"
        col4_val_str = f"{s.avg_slip_ticks:.1f} 跳/笔"
        col4_class = ""

    closed_open = (
        f"{s.closed_count} / {s.open_count}"
        + (f"<span class='muted'> (含接管 {s.takeover_count})</span>"
           if s.takeover_count else "")
        + (f"<span class='muted'> · 撤单 {s.cancelled_count}</span>"
           if s.cancelled_count else "")
    )
    return f"""
<div class="cards">
  <div class="card">
    <div class="label">{net_label}</div>
    <div class="value {_money_class(net_val)}">{_money_signed(net_val)}</div>
  </div>
  <div class="card">
    <div class="label">{col2_label}</div>
    <div class="value {_money_class(col2_val)}">{_money_signed(col2_val)}</div>
  </div>
  <div class="card">
    <div class="label">{col3_label}</div>
    <div class="value {_money_class(col3_val)}">{_money_signed(col3_val)}</div>
  </div>
  <div class="card">
    <div class="label">{col4_label}</div>
    <div class="value {col4_class}">{col4_val_str}</div>
  </div>
  <div class="card">
    <div class="label">已平仓 / 持有中</div>
    <div class="value">{closed_open}</div>
  </div>
  <div class="card">
    <div class="label">交易品种</div>
    <div class="value">{', '.join(s.symbols) if s.symbols else '—'}</div>
  </div>
</div>
"""


def _trade_status(rt: RoundTrip) -> str:
    return "持有中" if rt.is_open else "已平仓"


def _render_master_table(trips: list[RoundTrip], has_broker: bool) -> str:
    if not trips:
        return '<p class="muted">本日无交易。</p>'
    rows = []
    trips_sorted = sorted(trips, key=lambda r: r.entry.ts)
    for rt in trips_sorted:
        spec = get_spec(rt.symbol_root)
        e = rt.entry
        x = rt.exit
        gross = rt.gross_pnl if not rt.is_open else 0.0
        slip = rt.slippage_pnl(spec)
        side_tag = "多" if rt.direction == "long" else "空"

        # 状态标记
        if rt.is_takeover:
            status_html = '<span class="takeover-tag">隔夜接管</span>'
        elif rt.is_open:
            status_html = '持有中'
        else:
            status_html = '已平仓'

        # 入场价: takeover 显示 "—" (没有真开仓价)
        entry_px = '<span class="dim">—</span>' if rt.is_takeover else _fmt_price(e.fill_price)
        exit_px = _fmt_price(x.fill_price) if x else '<span class="dim">—</span>'

        # broker pnl + 逐笔 + fee
        if has_broker:
            broker_pnl = (x.broker_pnl if x and x.broker_pnl is not None else None)
            per_pnl = (x.per_trade_pnl if x and x.per_trade_pnl is not None else None)
            fee = (e.fee or 0.0) + ((x.fee if x else 0.0) or 0.0)
            broker_html = (f'<td class="num {_money_class(broker_pnl)}">{_money_signed(broker_pnl)}</td>'
                           if broker_pnl is not None else '<td class="num dim">—</td>')
            per_html = (f'<td class="num {_money_class(per_pnl)}">{_money_signed(per_pnl)}</td>'
                        if per_pnl is not None else '<td class="num dim">—</td>')
            fee_html = (f'<td class="num">¥{fee:.2f}</td>' if fee else '<td class="num dim">—</td>')
            net = (broker_pnl or 0.0) - fee if broker_pnl is not None else (gross + slip)
            net_html = f'<td class="num {_money_class(net)}"><b>{_money_signed(net)}</b></td>'
        else:
            net = gross + slip
            gross_disp = _money_signed(gross) if not rt.is_open else '<span class="dim">—</span>'
            broker_html = f'<td class="num {_money_class(gross)}">{gross_disp}</td>'
            per_html = f'<td class="num {_money_class(slip)}">{_h(_slip_with_ticks(rt, spec))}</td>'
            fee_html = ''
            net_html = f'<td class="num {_money_class(net)}"><b>{_money_signed(net)}</b></td>'

        rows.append(f"""
<tr>
  <td>{_h(e.ts.strftime('%m-%d %H:%M:%S'))}</td>
  <td><b>{_h(rt.symbol_root)}</b></td>
  <td>{side_tag}</td>
  <td class="num">{rt.lots}</td>
  <td class="num">{entry_px}</td>
  <td class="num">{exit_px}</td>
  {broker_html}
  {per_html}
  {fee_html}
  {net_html}
  <td>{status_html}</td>
</tr>
""")
    if has_broker:
        header = """
<tr>
  <th>时间</th><th>品种</th><th>方向</th><th class="num">手数</th>
  <th class="num">入场价</th><th class="num">出场价</th>
  <th class="num">broker盈亏</th><th class="num">逐笔盈亏</th>
  <th class="num">手续费</th><th class="num">净盈亏</th><th>状态</th>
</tr>"""
    else:
        header = """
<tr>
  <th>开仓时间</th><th>品种</th><th>方向</th><th class="num">手数</th>
  <th class="num">入场价</th><th class="num">出场价</th>
  <th class="num">毛盈亏</th><th class="num">滑点损益</th>
  <th class="num">净盈亏</th><th>状态</th>
</tr>"""
    return f"""
<table>
  <thead>{header}</thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _render_cancelled_table(cancelled: list[CsvTrade]) -> str:
    if not cancelled:
        return ""
    rows = []
    for c in sorted(cancelled, key=lambda t: t.send_ts):
        rows.append(f"""
<tr>
  <td>{_h(c.send_ts.strftime('%m-%d %H:%M:%S'))}</td>
  <td><b>{_h(c.symbol_root)}</b></td>
  <td>{_h('买' if c.side == 'buy' else '卖')}</td>
  <td>{_h(c.offset_cn)}</td>
  <td class="num">{_fmt_price(c.send_price) if c.send_price else '—'}</td>
  <td class="num">{c.send_lots}</td>
  <td class="muted">{_h(c.status)}</td>
</tr>
""")
    return f"""
<h2>当日撤单 ({len(cancelled)} 笔)</h2>
<p class="muted">通常由 OrderMonitor escalator 触发: 挂单超时未成 → 撤单 → 重发新价位.</p>
<table>
  <thead><tr>
    <th>报单时间</th><th>品种</th><th>方向</th><th>开平</th>
    <th class="num">报价</th><th class="num">手数</th><th>状态</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _render_pivot_box(spec: PivotSpec) -> str:
    conds = "".join(f"<li>{_h(c)}</li>" for c in spec.entry_conditions)
    exits = "".join(f"<li>{_h(p)}</li>" for p in spec.exit_pivots)
    consts_str = ", ".join(f"{k}={v}" for k, v in spec.constants.items())

    # V8/V13 显示 trail_stop_formula; QExp 显示 profit_target_formula
    if spec.trail_stop_formula:
        exit_label = "移动止损线生成方式"
        exit_body = spec.trail_stop_formula
        meta_extra = f"硬止损 {spec.hard_stop_pct}%　|　移动止损 {spec.trailing_pct}%"
    else:
        exit_label = f"ATR 止盈线生成方式 (止盈 ATR×{spec.profit_target_atr_mult:g})"
        exit_body = spec.profit_target_formula
        meta_extra = (f"硬止损 {spec.hard_stop_pct}%　|　止盈 {spec.profit_target_atr_mult:g}×ATR"
                      f"　|　方向 {spec.bias}")

    bias_label = "做多" if spec.bias == "long" else "做空"
    return f"""
<div class="pivot-box">
  <div><b>{_h(spec.strategy_name)}</b> <span class="muted">({spec.family} {bias_label}信号家族)</span></div>
  <div class="strategy-meta">参数:{_h(consts_str)}　|　{_h(meta_extra)}</div>

  <div class="label">入场信号公式</div>
  <div class="formula">{_h(spec.entry_formula)}</div>

  <div class="label">入场前置条件(全部满足才开仓)</div>
  <ul class="conds">{conds}</ul>

  <div class="label">出场触发(任一即平仓)</div>
  <ul class="conds">{exits}</ul>

  <div class="label">{_h(exit_label)}</div>
  <div class="formula">{_h(exit_body)}</div>
</div>
"""


def _render_entry_table(trips: list[RoundTrip]) -> str:
    rows = []
    for rt in sorted(trips, key=lambda r: r.entry.ts):
        e = rt.entry
        d = e.decision or {}
        ex = e.execute or {}
        ind = e.ind or {}
        ind_str = _format_ind(ind)
        reason = ex.get("reason", "") if ex else ""
        rows.append(f"""
<tr>
  <td>{_h(e.ts.strftime('%H:%M:%S'))}</td>
  <td class="num">{e.lots}</td>
  <td class="num">{_fmt_price(e.fill_price)}</td>
  <td class="num">{_fmt_price(e.send_price) if e.send_price else '<span class="dim">—</span>'}</td>
  <td class="num">{f"{d.get('raw'):.2f}" if d.get('raw') is not None else '—'}</td>
  <td class="num">{d.get('optimal', '—') if d else '—'}</td>
  <td class="num">{d.get('target', '—') if d else '—'}</td>
  <td class="num">{f"{d.get('atr'):.2f}" if d.get('atr') is not None else '—'}</td>
  <td>{_h(ind_str) if ind_str else '<span class="dim">—</span>'}</td>
  <td>{_h(reason) if reason else '<span class="dim">—</span>'}</td>
</tr>
""")
    if not rows:
        return '<p class="muted">本日无开仓。</p>'
    return f"""
<table>
  <thead><tr>
    <th>开仓时间</th><th class="num">手数</th><th class="num">成交价</th>
    <th class="num">送单价</th>
    <th class="num">原始信号 raw</th><th class="num">理论手数</th>
    <th class="num">目标手数</th><th class="num">ATR</th>
    <th>当时指标值</th><th>EXECUTE 原因</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _render_exit_table(trips: list[RoundTrip]) -> str:
    rows = []
    for rt in sorted([r for r in trips if not r.is_open], key=lambda r: r.exit.ts):
        x = rt.exit
        ts_obj = x.trail_stop or {}
        ex = x.execute or {}
        d = x.decision or {}
        action = (ex.get("action") or "").upper()
        if ts_obj:
            trigger = "移动止损"
            detail = f"当前价 {ts_obj.get('close')} ≤ 移损线 {ts_obj.get('line')}"
        elif action == "TRAIL_STOP":
            trigger = "移动止损"
            detail = ex.get("reason", "")
        elif action == "HARD_STOP":
            trigger = "硬止损"
            detail = ex.get("reason", "")
        elif action == "PROFIT_TARGET":
            trigger = "ATR 止盈"
            detail = ex.get("reason", "")
        elif d.get("target") == 0 and (d.get("optimal") or 0) <= 0:
            trigger = "信号反转"
            raw = d.get('raw')
            detail = (f"raw={raw:.2f} → optimal={d.get('optimal')} → target=0"
                      if raw is not None else f"target=0")
        else:
            trigger = "—"
            detail = ex.get("reason", "") if ex else ""
        rows.append(f"""
<tr>
  <td>{_h(x.ts.strftime('%H:%M:%S'))}</td>
  <td class="num">{x.lots}</td>
  <td class="num">{_fmt_price(x.fill_price)}</td>
  <td class="num">{_fmt_price(x.send_price) if x.send_price else '<span class="dim">—</span>'}</td>
  <td><b>{_h(trigger)}</b></td>
  <td>{_h(detail)}</td>
</tr>
""")
    if not rows:
        return '<p class="muted">本日无平仓。</p>'
    return f"""
<table>
  <thead><tr>
    <th>平仓时间</th><th class="num">手数</th><th class="num">成交价</th>
    <th class="num">送单价</th><th>触发条件</th><th>详情</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _format_ind(ind: dict) -> str:
    if not ind:
        return ""
    if ind.get("kind") == "v8":
        return (f"DC_U={ind['dc_u']:.1f} DC_M={ind['dc_m']:.1f} "
                f"close={ind['close']:.1f} ADX={ind['adx']:.1f} "
                f"PDI={ind['pdi']:.1f} MDI={ind['mdi']:.1f}")
    if ind.get("kind") == "v13":
        return (f"DC_U={ind['dc_u']:.1f} DC_M={ind['dc_m']:.1f} DC_L={ind['dc_l']:.1f} "
                f"close={ind['close']:.1f} MFI={ind['mfi']:.1f}")
    return ""


def _render_symbol_pnl_table(trips: list[RoundTrip], spec: ContractSpec) -> str:
    rows = []
    for rt in sorted(trips, key=lambda r: r.entry.ts):
        e = rt.entry
        x = rt.exit
        side_tag = "多" if rt.direction == "long" else "空"
        gross = rt.gross_pnl if not rt.is_open else 0.0
        slip = rt.slippage_pnl(spec)
        net = gross + slip
        rows.append(f"""
<tr>
  <td>{_h(e.ts.strftime('%H:%M:%S'))}</td>
  <td>{side_tag}</td>
  <td class="num">{rt.lots}</td>
  <td class="num">{_fmt_price(e.fill_price)}</td>
  <td class="num">{_fmt_price(x.fill_price) if x else '<span class="dim">持有中</span>'}</td>
  <td class="num {_money_class(gross)}">{_money_signed(gross) if not rt.is_open else '—'}</td>
  <td class="num {_money_class(slip)}">{_h(_slip_with_ticks(rt, spec))}</td>
  <td class="num {_money_class(net)}"><b>{_money_signed(net)}</b></td>
</tr>
""")
    return f"""
<table>
  <thead><tr>
    <th>开仓时间</th><th>方向</th><th class="num">手数</th>
    <th class="num">入场成本</th><th class="num">出场成本</th>
    <th class="num">毛盈亏</th><th class="num">滑点损益</th>
    <th class="num">净盈亏</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def _render_symbol_section(
    symbol: str,
    trips: list[RoundTrip],
    pivot: PivotSpec | None,
    image_filename: str | None,
) -> str:
    spec = get_spec(symbol)
    pivot_html = (
        _render_pivot_box(pivot) if pivot else
        '<p class="muted">未找到该品种的策略源文件。</p>'
    )
    image_html = (
        f'<div class="chart-box"><img src="{_h(image_filename)}" alt="{_h(symbol)} 盘面" /></div>'
        if image_filename else ""
    )

    if not trips:
        # 有图但无交易 — 简洁版面
        return f"""
<div class="section">
  <h2>{_h(symbol)} <span class="muted" style="font-size:14px;font-weight:400;">合约乘数 = {spec.multiplier:g}　|　最小变动价位 = {spec.tick_size:g}</span></h2>
  {image_html}
  {pivot_html}
  <p class="muted">本日窗口内无开仓/平仓事件。</p>
</div>
"""

    return f"""
<div class="section">
  <h2>{_h(symbol)} <span class="muted" style="font-size:14px;font-weight:400;">合约乘数 = {spec.multiplier:g}　|　最小变动价位 = {spec.tick_size:g}</span></h2>
  {image_html}
  {pivot_html}
  <h3>开仓原因</h3>
  {_render_entry_table(trips)}
  <h3>平仓原因</h3>
  {_render_exit_table(trips)}
  <h3>本品种交易明细</h3>
  {_render_symbol_pnl_table(trips, spec)}
</div>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_report(
    *,
    target_date: date,
    win_start: datetime,
    win_end: datetime,
    trips: list[RoundTrip],
    pivots: dict[str, PivotSpec | None],
    log_path: str,
    symbols: list[str] | None = None,
    sym_images: dict[str, str] | None = None,
    cancelled: list[CsvTrade] | None = None,
    csv_path: str | None = None,
) -> str:
    cancelled = cancelled or []
    summary = _aggregate(trips, target_date, win_start, win_end,
                         cancelled_count=len(cancelled))

    by_symbol: dict[str, list[RoundTrip]] = defaultdict(list)
    for rt in trips:
        by_symbol[rt.symbol_root].append(rt)

    if symbols is None:
        symbols = sorted(by_symbol.keys())
    images = sym_images or {}

    sym_sections = "".join(
        _render_symbol_section(sym, by_symbol.get(sym, []), pivots.get(sym), images.get(sym))
        for sym in sorted(symbols)
    )

    # 数据源标记
    if csv_path:
        source_html = (
            f"日志文件:{_h(Path(log_path).name)}  | "
            f"<b>broker CSV:{_h(Path(csv_path).name)}</b>"
        )
    else:
        source_html = f"日志文件:{_h(Path(log_path).name)}"

    cancelled_html = _render_cancelled_table(cancelled) if cancelled else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>实盘交易日报 {target_date.isoformat()}</title>
<style>{_CSS}</style>
</head><body>
<div class="wrap">
  <h1>PythonGO 实盘交易日报</h1>
  <div class="subtitle">
    交易日:<b>{target_date.isoformat()}</b>　|
    时间窗口:{win_start.strftime('%Y-%m-%d %H:%M')} → {win_end.strftime('%Y-%m-%d %H:%M')}
    　|　{source_html}
  </div>

  <h2>当日汇总</h2>
  {_render_summary(summary)}

  <h2>所有交易明细(按时间排序)</h2>
  {_render_master_table(trips, has_broker=summary.has_broker_data)}

  {cancelled_html}

  {sym_sections}

  <div class="footer">
    生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    　|　工具:tools/daily_report
    {(' · 数据源: broker CSV (主) + StraLog (上下文)' if csv_path else ' · 数据源: StraLog')}
  </div>
</div>
</body></html>
"""
