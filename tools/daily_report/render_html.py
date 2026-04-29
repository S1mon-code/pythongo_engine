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
from .log_parser import LogEvent, parse_ind
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
.action-block { background: #fafafa; border: 1px solid #e0e0e0;
                padding: 12px 16px; margin: 10px 0; border-radius: 2px; }
.action-block p { margin: 6px 0; line-height: 1.7; font-size: 12.8px; }
.action-title { font-weight: 600; color: #1a1a1a; font-size: 13.5px;
                margin-bottom: 8px; padding-bottom: 6px;
                border-bottom: 1px solid #e0e0e0; }
.summary-list { padding-left: 20px; margin: 4px 0; font-size: 12.8px;
                line-height: 1.8; }
.summary-list li { margin: 3px 0; }
.swing-tag { display: inline-block; padding: 1px 7px; border-radius: 3px;
             font-size: 10.5px; background: #e7f1fb; color: #0b5cad;
             border: 1px solid #c4dcf3; margin-left: 6px; font-weight: 500; }
.trend-tag { display: inline-block; padding: 1px 7px; border-radius: 3px;
             font-size: 10.5px; background: #f3e8fb; color: #6a1b9a;
             border: 1px solid #ddc1f0; margin-left: 6px; font-weight: 500; }
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


# 策略类型映射 (2026-04-29 起): swing=波段单, trend=趋势单
# 当前所有 7 个 V8/V13 + 4 个 QExp 都是波段单, ICT v6 (未来上线) 将是趋势单.
# 修改: 在此 dict 里加映射即可.
_STRATEGY_TYPES: dict[str, str] = {
    # V8 (Donchian + ADX)
    "AL": "swing", "CU": "swing", "HC": "swing",
    # V13 (Donchian + MFI)
    "AG": "swing", "JM": "swing", "P": "swing", "PP": "swing",
    # QExp robust (M5/M15/M30 binary fires)
    "AG_MOM": "swing", "AG_VSV2": "swing",
    "I_PULL": "swing", "HC_S": "swing",
    # ICT v6 (future: trend) — 当前 swing 占位
    "I": "swing",
}


def _strategy_type(symbol: str) -> str:
    """返回策略类型: 'swing' (波段单) | 'trend' (趋势单). 未知品种默认 swing."""
    return _STRATEGY_TYPES.get(symbol.upper(), "swing")


def _strategy_type_tag(symbol: str) -> str:
    """渲染策略类型小标签 HTML."""
    t = _strategy_type(symbol)
    if t == "trend":
        return '<span class="trend-tag">趋势单</span>'
    return '<span class="swing-tag">波段单</span>'


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


def _render_master_table(
    trips: list[RoundTrip], has_broker: bool,
    startup_states: dict[str, dict] | None = None,
) -> str:
    if not trips:
        return '<p class="muted">本日无交易。</p>'
    startup_states = startup_states or {}
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

        # 入场价显示规则:
        # 1. CSV 解析有真实价 (当日开仓 / carryover 接管) → 直接显示
        # 2. 孤儿 takeover + strategy 启动时 own_pos > 0 → 用 ON_START avg_price (state.json 恢复)
        # 3. 孤儿 takeover + strategy 启动时 own_pos = 0 → "—" (broker 异常事件)
        startup = startup_states.get(rt.symbol_root, {})
        if e.fill_price > 0:
            entry_px = _fmt_price(e.fill_price)
            if rt.is_takeover:
                entry_px += '<span class="muted" style="font-size:10px;"> (前日)</span>'
        elif rt.is_takeover and startup.get("own_pos", 0) > 0:
            entry_px = (
                f'{_fmt_price(startup["avg_price"])}'
                '<span class="muted" style="font-size:10px;"> (state恢复)</span>'
            )
        else:
            entry_px = '<span class="dim">—</span>'
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
    sym_events: list[LogEvent] | None = None,
    startup_state: dict | None = None,
) -> str:
    """新版 (2026-04-29+): 品种点评 — 配图 + 行情速览 + 本策略动作 + 今日总结.

    去掉旧版的 pivot box / 入场前置条件 / 移动止损公式 / 开仓原因表 / 平仓原因表 /
    本品种交易明细表. 改成中文叙述风格.
    """
    spec = get_spec(symbol)
    full_name = _full_name_from_trips(trips)
    instrument_id = _instrument_from_trips(trips)
    title_html = _h(symbol)
    if full_name:
        title_html += f' <span class="muted" style="font-size:14px;font-weight:400;">({_h(full_name)} {_h(instrument_id)})</span>'
    title_html += _strategy_type_tag(symbol)

    image_html = (
        f'<div class="chart-box"><img src="{_h(image_filename)}" alt="{_h(symbol)} 盘面" /></div>'
        if image_filename else ""
    )

    if not trips:
        return f"""
<div class="section">
  <h2>{title_html}</h2>
  {image_html}
  <p class="muted">本日窗口内无交易动作。</p>
</div>
"""

    market_html = _render_market_overview(symbol, sym_events or [], trips)
    actions_html = _render_strategy_actions(trips, spec, startup_state)
    summary_html = _render_symbol_summary(trips, spec)

    return f"""
<div class="section">
  <h2>{title_html}</h2>
  {image_html}
  <h3>今日行情速览</h3>
  {market_html}
  <h3>本策略动作</h3>
  {actions_html}
  <h3>今日总结</h3>
  {summary_html}
</div>
"""


def _full_name_from_trips(trips: list[RoundTrip]) -> str:
    for rt in trips:
        if rt.entry.full_name:
            return rt.entry.full_name
        if rt.exit and rt.exit.full_name:
            return rt.exit.full_name
    return ""


def _instrument_from_trips(trips: list[RoundTrip]) -> str:
    for rt in trips:
        if rt.entry.instrument_id:
            return rt.entry.instrument_id
        if rt.exit and rt.exit.instrument_id:
            return rt.exit.instrument_id
    return ""


def _render_market_overview(
    symbol: str, sym_events: list[LogEvent], trips: list[RoundTrip]
) -> str:
    """从 IND log 序列 + trades 推今日行情走势.

    IND log 由策略每根 bar close 时打印一次, 含 close + 关键指标 (V8: ADX/PDI/MDI;
    V13: MFI). 我们用 IND 序列的 close 推开盘/最高/最低/收盘 + 涨跌幅.
    """
    inds: list[dict] = []
    for ev in sym_events:
        if ev.tag != "IND":
            continue
        d = parse_ind(ev.body)
        if d:
            d["_ts"] = ev.event_ts
            inds.append(d)

    if not inds:
        # fallback: 从 trades 估开盘 / 收盘
        trades_sorted = sorted(trips, key=lambda r: r.entry.ts)
        first = trades_sorted[0].entry
        last = trades_sorted[-1].exit or trades_sorted[-1].entry
        first_px = first.fill_price or 0
        last_px = last.fill_price or 0
        if first_px and last_px:
            pct = (last_px - first_px) / first_px * 100
            return (f"<p>(IND 数据不足, 仅基于成交价推断) 首笔成交 ¥{first_px:.1f}, "
                    f"末笔成交 ¥{last_px:.1f}, 区间变化 {pct:+.2f}%.</p>")
        return "<p class='muted'>(行情数据不足)</p>"

    closes = [d["close"] for d in inds]
    first_close = closes[0]
    last_close = closes[-1]
    hi = max(closes)
    lo = min(closes)
    pct = (last_close - first_close) / first_close * 100 if first_close else 0
    direction = "上涨" if pct > 0.05 else ("下跌" if pct < -0.05 else "横盘")

    last = inds[-1]
    parts = [
        f'<p>当日 <b>{_h(symbol)}</b> 主力合约整体<b>{direction} {pct:+.2f}%</b> — ',
        f'窗口起点 ¥{first_close:.1f}, ',
        f'最高 ¥{hi:.1f}, 最低 ¥{lo:.1f}, ',
        f'收盘 ¥{last_close:.1f} (波动幅度 {(hi - lo)/first_close*100:.2f}%).</p>'
    ]

    # 关键指标 (收盘视角)
    if last.get("kind") == "v8":
        adx_judge = ("强趋势" if last["adx"] >= 22 else "趋势弱")
        pdi_mdi_judge = ("多头主导" if last["pdi"] > last["mdi"] else "空头主导")
        parts.append(
            f'<p>收盘指标: ADX={last["adx"]:.1f} ({adx_judge}, 阈值 22), '
            f'PDI={last["pdi"]:.1f} {"&gt;" if last["pdi"] > last["mdi"] else "&lt;"} '
            f'MDI={last["mdi"]:.1f} ({pdi_mdi_judge}); '
            f'Donchian 通道 {last["dc_l"]:.1f} ~ {last["dc_u"]:.1f}, '
            f'中轨 {last["dc_m"]:.1f}.</p>'
        )
    elif last.get("kind") == "v13":
        mfi_judge = ("资金流入强" if last["mfi"] >= 50 else "资金流出")
        parts.append(
            f'<p>收盘指标: MFI={last["mfi"]:.1f} ({mfi_judge}, 阈值 50); '
            f'Donchian 通道 {last["dc_l"]:.1f} ~ {last["dc_u"]:.1f}, '
            f'中轨 {last["dc_m"]:.1f}.</p>'
        )
    return "".join(parts)


def _render_strategy_actions(
    trips: list[RoundTrip], spec: ContractSpec,
    startup_state: dict | None = None,
) -> str:
    """逐笔叙述 — 按时间排序, 每个 round-trip 一段中文."""
    parts: list[str] = []
    trips_sorted = sorted(trips, key=lambda r: r.entry.ts)
    for idx, rt in enumerate(trips_sorted, 1):
        parts.append(f'<div class="action-block">')
        parts.append(f'<div class="action-title">动作 #{idx}: {_action_title(rt)}</div>')

        # 开仓段
        if rt.is_takeover:
            if rt.entry.fill_price > 0:
                # carryover: 有前日真实入场价
                parts.append(_describe_carryover_open(rt.entry, rt.lots, rt.direction))
            elif startup_state and startup_state.get("own_pos", 0) > 0:
                # strategy 启动时有持仓但 CSV 推不出 → 用 ON_START 恢复的 avg_price
                parts.append(_describe_startup_recovered_open(
                    startup_state, rt.lots, rt.direction
                ))
            else:
                # 孤儿 takeover: strategy 启动时 own_pos=0, 此笔与 strategy 无关
                parts.append(
                    '<p><b>开仓</b>: <span class="muted">无对应 strategy 入场</span> — '
                    'strategy 启动时 own_pos=0, broker 端报回此平仓但 strategy log 无记录. '
                    '可能原因: broker 自动平仓 / 手动操作 / 底仓延迟成交 / broker 端时钟错位 '
                    '与本日 strategy 平仓配对错乱.</p>'
                )
        else:
            parts.append(_describe_open_leg(rt.entry, rt.lots, rt.direction))

        # 平仓段 / 持仓段
        if rt.exit is not None:
            parts.append(_describe_close_leg(rt.exit, rt.entry, rt, spec))
        else:
            parts.append(f'<p><b>当前状态</b>: 持仓中, 未触发平仓信号, 持有 '
                         f'{rt.lots} 手 @ ¥{rt.entry.fill_price:.1f} 过夜.</p>')
        parts.append('</div>')
    return "".join(parts)


def _action_title(rt: RoundTrip) -> str:
    side = "做多" if rt.direction == "long" else "做空"
    if rt.is_takeover:
        return f"{side} {rt.lots} 手 (隔夜接管 → 平仓)"
    if rt.exit is None:
        return f"{side} {rt.lots} 手 (新开, 持仓中)"
    return f"{side} {rt.lots} 手 (开 → 平 完整 round-trip)"


def _describe_startup_recovered_open(
    startup_state: dict, lots: int, direction: str,
) -> str:
    """从 strategy [ON_START 恢复] log 注入的入场叙述 (CSV 推不出但 state.json 恢复了).

    使用场景: 前日 CSV 缺失, 但当日 strategy 启动时 [ON_START 恢复] own_pos > 0.
    """
    side_text = "买入" if direction == "long" else "卖出"
    avg = startup_state.get("avg_price", 0.0)
    own = startup_state.get("own_pos", 0)
    peak = startup_state.get("peak_price", 0.0)
    ts = startup_state.get("ts")
    ts_str = ts.strftime("%m-%d %H:%M:%S") if ts else "前日"
    return (
        f'<p><b>开仓 (前日接管 / state.json 恢复)</b>: '
        f'当日 {ts_str} strategy 启动时 own_pos={own}, '
        f'avg_price=¥{avg:.1f}, peak=¥{peak:.1f}. '
        f'本笔 {lots} 手按平均成本 ¥{avg:.1f} 计入 (前日 CSV 缺失, '
        f'入场详情来自 strategy state.json).</p>'
    )


def _describe_carryover_open(leg: TradeLeg, lots: int, direction: str) -> str:
    """前日接管开仓叙述 — 含真实入场价 + 决策上下文 (从前日 StraLog 注入)."""
    d = leg.decision or {}
    ind = leg.ind or {}
    side_text = "买入" if direction == "long" else "卖出"
    prev_ts_str = leg.ts.strftime("%m-%d %H:%M:%S")

    lines = [
        f'<p><b>开仓 (前日接管)</b>: {prev_ts_str} {side_text} '
        f'{lots} 手 @ ¥{leg.fill_price:.1f} '
        f'<span class="muted">(state.json 自昨日恢复 own_pos)</span></p>'
    ]

    # 决策理由 (前日 StraLog 注入的 POS_DECISION)
    if d.get("raw") is not None:
        lines.append(
            f'<p><b>当时仓位决策</b>: 原始信号 raw={d["raw"]:.2f} '
            f'→ Carver vol target 算出 {d["optimal"]} 手 '
            f'(ATR={d.get("atr", 0):.1f}) → 目标 {d["target"]} 手, '
            f'当时已持 {d["own_pos"]} 手.</p>'
        )

    # 信号详情
    if ind.get("kind") == "v8":
        adx = ind.get("adx", 0)
        pdi = ind.get("pdi", 0)
        mdi = ind.get("mdi", 0)
        signal_strong = "✓" if adx >= 22 and pdi > mdi else "△"
        lines.append(
            f'<p><b>当时信号 (V8 Donchian+ADX)</b>: '
            f'close={ind["close"]:.1f}, Donchian 上轨={ind["dc_u"]:.1f}, '
            f'中轨={ind["dc_m"]:.1f}; '
            f'ADX={adx:.1f}, PDI={pdi:.1f} '
            f'{"&gt;" if pdi > mdi else "&lt;"} MDI={mdi:.1f} '
            f'— 趋势确认 {signal_strong}</p>'
        )
    elif ind.get("kind") == "v13":
        mfi = ind.get("mfi", 0)
        signal_strong = "✓" if mfi >= 50 else "△"
        lines.append(
            f'<p><b>当时信号 (V13 Donchian+MFI)</b>: '
            f'close={ind["close"]:.1f}, Donchian 上轨={ind["dc_u"]:.1f}, '
            f'中轨={ind["dc_m"]:.1f}; '
            f'MFI={mfi:.1f} — 资金确认 {signal_strong}</p>'
        )

    return "".join(lines)


def _describe_open_leg(leg: TradeLeg, lots: int, direction: str) -> str:
    """开仓中文叙述."""
    d = leg.decision or {}
    ind = leg.ind or {}
    ex = leg.execute or {}

    side_text = "买入" if direction == "long" else "卖出"
    lines = [
        f'<p><b>开仓时间</b>: {leg.ts.strftime("%H:%M:%S")} · '
        f'{side_text} {lots} 手 @ ¥{leg.fill_price:.1f}'
    ]
    if leg.send_price and abs(leg.send_price - leg.fill_price) >= 0.01:
        diff_ticks = (leg.fill_price - leg.send_price) / get_spec(leg.symbol_root).tick_size
        diff_sign = "+" if diff_ticks > 0 else ""
        lines.append(f' (报价 ¥{leg.send_price:.1f}, 滑 {diff_sign}{diff_ticks:.1f} 跳)')
    lines.append("</p>")

    # 仓位决策逻辑
    if d.get("raw") is not None:
        lines.append(
            f'<p><b>仓位决策</b>: 原始信号强度 raw={d["raw"]:.2f} '
            f'→ Carver vol target 算出 {d["optimal"]} 手 '
            f'(ATR={d.get("atr", 0):.1f}, 资金 ¥{d.get("capital", 0):,.0f}) '
            f'→ apply_buffer 后目标 {d["target"]} 手, 当时已持 {d["own_pos"]} 手.</p>'
        )

    # 信号详情 (V8 / V13)
    if ind.get("kind") == "v8":
        adx = ind.get("adx", 0)
        pdi = ind.get("pdi", 0)
        mdi = ind.get("mdi", 0)
        signal_strong = "✓" if adx >= 22 and pdi > mdi else "△"
        lines.append(
            f'<p><b>信号详情 (V8 Donchian+ADX)</b>: '
            f'close={ind["close"]:.1f}, Donchian 上轨={ind["dc_u"]:.1f}, '
            f'中轨={ind["dc_m"]:.1f}; '
            f'ADX={adx:.1f} (阈值 22), '
            f'PDI={pdi:.1f} {"&gt;" if pdi > mdi else "&lt;"} MDI={mdi:.1f} '
            f'— 趋势确认 {signal_strong}</p>'
        )
    elif ind.get("kind") == "v13":
        mfi = ind.get("mfi", 0)
        signal_strong = "✓" if mfi >= 50 else "△"
        lines.append(
            f'<p><b>信号详情 (V13 Donchian+MFI)</b>: '
            f'close={ind["close"]:.1f}, Donchian 上轨={ind["dc_u"]:.1f}, '
            f'中轨={ind["dc_m"]:.1f}; '
            f'MFI={mfi:.1f} (阈值 50) — 资金确认 {signal_strong}</p>'
        )

    # 不显示 "开仓动作" (内容已在仓位决策里, 避免重复)
    return "".join(lines)


def _describe_close_leg(
    exit_leg: TradeLeg, entry_leg: TradeLeg, rt: RoundTrip, spec: ContractSpec
) -> str:
    """平仓中文叙述."""
    ts = exit_leg.trail_stop or {}
    ex = exit_leg.execute or {}
    d = exit_leg.decision or {}
    action = (ex.get("action") or "").upper()

    side_text = "卖出" if rt.direction == "long" else "买入"

    lines = [
        f'<p><b>平仓时间</b>: {exit_leg.ts.strftime("%H:%M:%S")} · '
        f'{side_text} {exit_leg.lots} 手 @ ¥{exit_leg.fill_price:.1f}'
    ]
    if exit_leg.send_price and abs(exit_leg.send_price - exit_leg.fill_price) >= 0.01:
        diff_ticks = (exit_leg.fill_price - exit_leg.send_price) / spec.tick_size
        # 卖单有利 = fill > send, 买单有利 = fill < send
        adv = -diff_ticks if rt.direction == "long" else diff_ticks
        adv_sign = "+" if adv > 0 else ""
        lines.append(f' (报价 ¥{exit_leg.send_price:.1f}, 滑点 {adv_sign}{adv:.1f} 跳)')
    lines.append("</p>")

    # 触发原因
    trigger_html = ""
    if rt.is_takeover:
        trigger_html = (
            '<p><b>触发条件</b>: 隔夜接管的持仓在新窗口内被平仓 — '
            '可能是策略启动后立即触发的移动止损 / 硬止损 / 信号反转.'
        )
        if action == "TRAIL_STOP":
            trigger_html += " 实际触发: 移动止损."
        elif action == "HARD_STOP":
            trigger_html += " 实际触发: 硬止损."
        trigger_html += "</p>"
    elif ts:
        trigger_html = (
            f'<p><b>触发条件</b>: 移动止损 — 当前价 ¥{ts.get("close", 0):.1f} '
            f'≤ 移损线 ¥{ts.get("line", 0):.1f} '
            f'(peak × (1 - trailing_pct%) 公式生成)</p>'
        )
    elif action == "TRAIL_STOP":
        trigger_html = f'<p><b>触发条件</b>: 移动止损 — {_h(ex.get("reason", ""))}</p>'
    elif action == "HARD_STOP":
        trigger_html = (
            f'<p><b>触发条件</b>: 硬止损 — '
            f'价格回撤超过开仓价 ×{0.5}% (默认), {_h(ex.get("reason", ""))}</p>'
        )
    elif action == "PROFIT_TARGET":
        trigger_html = f'<p><b>触发条件</b>: ATR 止盈 — {_h(ex.get("reason", ""))}</p>'
    elif d.get("target") == 0 and (d.get("optimal") or 0) <= 0:
        raw = d.get("raw")
        raw_str = f"{raw:.2f}" if raw is not None else "—"
        trigger_html = (
            f'<p><b>触发条件</b>: 信号反转 — 原始信号 raw={raw_str} '
            f'→ optimal={d.get("optimal")} → target=0, 强制平仓.</p>'
        )
    # 注意: 不 fallback 到 ex.reason — 因为时间最近的 EXECUTE 可能是开仓那个,
    # 真正的平仓触发在 EXEC_STOP / TRAIL_STOP log 里 (action 字段已捕获到上面 if 中)
    if not trigger_html:
        # 启发式: broker_pnl 与当日 round-trip 的 gross 偏离很大时, 可能是 takeover
        gross_estimate = 0.0
        if not rt.is_takeover and entry_leg.fill_price and exit_leg.fill_price:
            gross_estimate = (
                (exit_leg.fill_price - entry_leg.fill_price)
                * exit_leg.lots * spec.multiplier
                * (1 if rt.direction == "long" else -1)
            )
        if (exit_leg.broker_pnl is not None and gross_estimate
                and abs(exit_leg.broker_pnl - gross_estimate) > abs(gross_estimate) * 0.5
                and abs(exit_leg.broker_pnl - gross_estimate) > 100):
            trigger_html = (
                '<p><b>触发条件</b>: (未捕获到 EXEC_STOP / TRAIL_STOP 日志). '
                f'broker 平仓盈亏 ({_money_signed(exit_leg.broker_pnl)}) 与当日 '
                f'round-trip 的毛盈亏 ({_money_signed(gross_estimate)}) 差异较大, '
                '可能实际平的是隔夜接管的持仓 (state.json 自昨日恢复, CSV 不可见).</p>'
            )
        else:
            trigger_html = (
                '<p><b>触发条件</b>: (策略 log 未打印明确 EXEC_STOP / TRAIL_STOP / '
                '信号反转 tag, 可能是手动平仓 / 接管平仓)</p>'
            )
    lines.append(trigger_html)

    # 盈亏
    pnl_parts = []
    if exit_leg.broker_pnl is not None:
        pnl_parts.append(f"broker 平仓盈亏 {_money_signed(exit_leg.broker_pnl)}")
    if exit_leg.per_trade_pnl is not None:
        pnl_parts.append(f"逐笔 {_money_signed(exit_leg.per_trade_pnl)}")
    fee_total = (entry_leg.fee or 0.0) + (exit_leg.fee or 0.0)
    if fee_total:
        pnl_parts.append(f"手续费 ¥{fee_total:.2f}")
    if pnl_parts:
        lines.append(f'<p><b>盈亏</b>: {", ".join(pnl_parts)}</p>')

    return "".join(lines)


def _render_symbol_summary(trips: list[RoundTrip], spec: ContractSpec) -> str:
    """单品种的当日小结."""
    closed = [r for r in trips if not r.is_open]
    open_ = [r for r in trips if r.is_open]
    takeover = [r for r in trips if r.is_takeover]

    broker_total = sum(
        (r.exit.broker_pnl or 0.0) for r in closed if r.exit
    )
    fee_total = sum(
        (r.entry.fee or 0.0) + ((r.exit.fee if r.exit else 0.0) or 0.0) for r in trips
    )
    has_broker = any(r.exit and r.exit.broker_pnl is not None for r in closed)

    parts = ['<ul class="summary-list">']
    parts.append(f'<li>已平仓 <b>{len(closed)}</b> 笔'
                 + (f' (含 {len(takeover)} 笔隔夜接管)' if takeover else '')
                 + f', 持仓中 <b>{len(open_)}</b> 笔.</li>')
    if has_broker:
        sign_class = _money_class(broker_total)
        parts.append(f'<li>broker 平仓盈亏: <b class="{sign_class}">'
                     f'{_money_signed(broker_total)}</b>; '
                     f'手续费: ¥{fee_total:.2f}; '
                     f'净 (broker − 手续费): <b class="{_money_class(broker_total - fee_total)}">'
                     f'{_money_signed(broker_total - fee_total)}</b>.</li>')
    if open_:
        held = open_[0]
        parts.append(f'<li>持仓过夜: {held.lots} 手 @ ¥{held.entry.fill_price:.1f} '
                     f'({held.entry.ts.strftime("%H:%M")} 开仓).</li>')
    parts.append('</ul>')
    return "".join(parts)


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
    log_events: list[LogEvent] | None = None,
    startup_states: dict[str, dict] | None = None,
) -> str:
    cancelled = cancelled or []
    summary = _aggregate(trips, target_date, win_start, win_end,
                         cancelled_count=len(cancelled))

    by_symbol: dict[str, list[RoundTrip]] = defaultdict(list)
    for rt in trips:
        by_symbol[rt.symbol_root].append(rt)

    # 按 symbol 分组 log events (用于 narrative)
    events_by_symbol: dict[str, list[LogEvent]] = defaultdict(list)
    for ev in (log_events or []):
        events_by_symbol[ev.symbol.upper()].append(ev)

    if symbols is None:
        symbols = sorted(by_symbol.keys())
    images = sym_images or {}

    startup_states = startup_states or {}
    sym_sections = "".join(
        _render_symbol_section(
            sym, by_symbol.get(sym, []), pivots.get(sym), images.get(sym),
            sym_events=events_by_symbol.get(sym),
            startup_state=startup_states.get(sym),
        )
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
  {_render_master_table(trips, has_broker=summary.has_broker_data, startup_states=startup_states)}

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
