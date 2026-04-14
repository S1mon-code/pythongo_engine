"""
================================================================================
  DailyReporter — 全账户每日汇总飞书推送
================================================================================

  不交易，只监控。永远挂着，负责：
  1. 每日15:15推送全账户汇总
  2. 每日21:05推送新交易日开盘状态
  3. 遍历所有关注合约的持仓明细
  部署: 和其他策略一样放在 self_strategy/

================================================================================
"""
import time
from datetime import datetime

import requests
from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier
from modules.feishu import feishu
from modules.trading_day import get_trading_day


# 监控的所有合约列表 (根据你实际在跑的策略调整)
WATCH_INSTRUMENTS = [
    # (合约代码, 品种名, 方向, 乘数)
    ("i2609", "铁矿石", "多/空", 100),
    ("cu2605", "铜", "空", 5),
    ("al2605", "电解铝", "多", 5),
    ("IH2606", "上证50", "多", 300),
    ("lc2609", "碳酸锂", "空", 1),
]

# 推送时间
REVIEW_HOUR = 15
REVIEW_MINUTE = 5
MORNING_HOUR = 21
MORNING_MINUTE = 5


class Params(BaseParams):
    exchange: str = Field(default="DCE", title="交易所代码")
    instrument_id: str = Field(default="i2609", title="挂载合约(仅用于tick)")
    kline_style: str = Field(default="M1", title="K线周期")
    sim_24h: bool = Field(default=True, title="24H模拟盘模式")


class State(BaseState):
    equity: float = Field(default=0.0, title="权益")
    available: float = Field(default=0.0, title="可用")
    pos_profit: float = Field(default=0.0, title="浮盈")
    total_pos: str = Field(default="---", title="总持仓")
    trading_day: str = Field(default="", title="交易日")
    last_report: str = Field(default="---", title="上次推送")


class DailyReporter(BaseStrategy):
    """全账户监控 — 不交易, 只推送飞书日报"""

    def __init__(self):
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

        self._investor_id = ""
        self._current_td = ""
        self._review_sent = False
        self._morning_sent = False
        self._daily_start_eq = 0.0

    def _get_account(self):
        if not self._investor_id:
            return None
        return self.get_account_fund_data(self._investor_id)

    def on_start(self):
        p = self.params_map

        self.kline_generator = KLineGenerator(
            callback=self.callback,
            real_time_callback=self.real_time_callback,
            exchange=p.exchange,
            instrument_id=p.instrument_id,
            style=p.kline_style,
        )
        self.kline_generator.push_history_data()

        inv = self.get_investor_data(1)
        if inv:
            self._investor_id = inv.investor_id

        acct = self._get_account()
        if acct:
            self._daily_start_eq = acct.balance

        self._current_td = get_trading_day()
        self.state_map.trading_day = self._current_td

        super().on_start()
        self.output(f"DailyReporter 启动 | 监控 {len(WATCH_INSTRUMENTS)} 个合约")
        feishu("start", "MONITOR",
               f"**全账户监控启动**\n"
               f"监控合约: {', '.join(c[0] for c in WATCH_INSTRUMENTS)}\n"
               f"权益: {acct.balance:,.0f}" if acct else "**监控启动** 未获取账户")

    def on_stop(self):
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        try:
            # 交易日切换
            td = get_trading_day()
            if td != self._current_td and self._current_td:
                acct = self._get_account()
                if acct:
                    self._daily_start_eq = acct.balance
                self._current_td = td
                self.state_map.trading_day = td
                self._review_sent = False
                self._morning_sent = False
            if not self._current_td:
                self._current_td = td
                self.state_map.trading_day = td

            # 更新State面板
            acct = self._get_account()
            if acct:
                self.state_map.equity = round(acct.balance, 0)
                self.state_map.available = round(acct.available, 0)
                self.state_map.pos_profit = round(acct.position_profit, 0)

            # 更新持仓汇总
            positions = self._scan_positions()
            total = sum(abs(p['lots']) for p in positions if p['lots'] != 0)
            self.state_map.total_pos = f"{total}手/{len([p for p in positions if p['lots'] != 0])}品种"

            now = datetime.now()

            # 15:15 日终汇总
            if (not self._review_sent
                    and now.hour == REVIEW_HOUR
                    and REVIEW_MINUTE <= now.minute < REVIEW_MINUTE + 5):
                self._send_daily_summary()
                self._review_sent = True

            # 21:05 开盘状态
            if (not self._morning_sent
                    and now.hour == MORNING_HOUR
                    and MORNING_MINUTE <= now.minute < MORNING_MINUTE + 5):
                self._send_morning_report()
                self._morning_sent = True
        except Exception as e:
            self.output(f"[on_tick异常] {type(e).__name__}: {e}")

    def callback(self, kline: KLineData):
        pass  # 不处理K线

    def real_time_callback(self, kline: KLineData):
        pass  # 不推图表

    def _scan_positions(self):
        """扫描所有关注合约的持仓."""
        positions = []
        for inst_id, name, direction, mult in WATCH_INSTRUMENTS:
            pos = self.get_position(inst_id)
            lots = pos.net_position if pos else 0
            avg = pos.open_avg_price if pos and hasattr(pos, 'open_avg_price') else 0
            last = pos.last_price if pos and hasattr(pos, 'last_price') and pos.last_price > 0 else 0
            # 持仓盈亏: 多=(现价-均价)*手数*乘数, 空=(均价-现价)*手数*乘数
            if lots != 0 and avg > 0 and last > 0:
                if lots > 0:
                    pnl = (last - avg) * lots * mult
                else:
                    pnl = (avg - last) * abs(lots) * mult
            else:
                pnl = 0
            positions.append({
                "instrument": inst_id,
                "name": name,
                "direction": direction,
                "multiplier": mult,
                "lots": lots,
                "avg_price": avg,
                "last_price": last,
                "pnl": pnl,
            })
        return positions

    def _send_daily_summary(self):
        """15:05 全账户日终汇总 (column_set表格)."""
        acct = self._get_account()
        if not acct:
            return

        eq = acct.balance
        available = acct.available
        pos_profit = acct.position_profit
        daily_abs = eq - self._daily_start_eq
        daily_pct = daily_abs / self._daily_start_eq * 100 if self._daily_start_eq > 0 else 0

        positions = self._scan_positions()
        active = [p for p in positions if p['lots'] != 0]
        idle = [p for p in positions if p['lots'] == 0]
        total_pnl = sum(p['pnl'] for p in active) if active else 0

        # 构建卡片elements
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": (
                f"**全账户每日总结**\n交易日: {self._current_td}\n\n"
                f"**📊 账户概览**\n"
                f"日初权益: {self._daily_start_eq:,.0f} → 当前权益: {eq:,.0f}\n"
                f"可用资金: {available:,.0f} | 持仓浮盈: {pos_profit:+,.0f}\n"
                f"**日盈亏: {daily_abs:+,.0f} ({daily_pct:+.2f}%)**"
            )}},
            {"tag": "hr"},
        ]

        if active:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**📋 持仓明细 ({len(active)}个品种, 合计盈亏: {total_pnl:+,.0f})**"}})
            # 表头
            elements.append(self._table_row(
                ["**合约**", "**方向**", "**手数**", "**均价**", "**现价**", "**盈亏**"],
                bg="grey"))
            # 数据行
            for p in active:
                lots = p['lots']
                side = "空" if lots < 0 else "多"
                pnl_str = f"**{p['pnl']:+,.0f}**"
                elements.append(self._table_row([
                    f"{p['instrument']} {p['name']}", side, str(abs(lots)),
                    f"{p['avg_price']:,.1f}", f"{p['last_price']:,.1f}", pnl_str,
                ]))
        else:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "**📋 持仓明细**\n无持仓"}})

        elements.append({"tag": "hr"})
        idle_text = ", ".join(f"{p['name']}({p['instrument']})" for p in idle) if idle else "无"
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"⚪ 空仓: {idle_text}\n\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*"}})

        # 直接发送卡片
        try:
            from modules.feishu import WEBHOOK
            requests.post(WEBHOOK, json={
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": "每日回顾 | 全账户"}, "template": "purple"},
                    "elements": elements,
                },
            }, timeout=5)
        except Exception as e:
            self.output(f"[日报] 推送失败: {e}")

        self.output(f"[日报] 已推送全账户汇总")
        self.state_map.last_report = time.strftime("%H:%M")

    @staticmethod
    def _table_row(cols, bg=None):
        """构建飞书column_set表格行."""
        weights = [2, 1, 1, 2, 2, 2]
        columns = []
        for i, val in enumerate(cols):
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": weights[i],
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": val}}],
            })
        row = {"tag": "column_set", "flex_mode": "none", "columns": columns}
        if bg:
            row["background_style"] = bg
        return row

    def _send_morning_report(self):
        """21:05 新交易日开盘状态."""
        acct = self._get_account()
        if not acct:
            return

        positions = self._scan_positions()
        active = [p for p in positions if p['lots'] != 0]

        msg = (
            f"**新交易日开盘**\n"
            f"交易日: {self._current_td}\n\n"
            f"**📊 账户状态**\n"
            f"权益: {acct.balance:,.0f}\n"
            f"可用资金: {acct.available:,.0f}\n"
        )

        if active:
            msg += f"\n**📋 隔夜持仓 ({len(active)}个品种)**\n"
            msg += "| 合约 | 品种 | 方向 | 手数 |\n|--|--|--|--|\n"
            for p in active:
                lots = p['lots']
                side = "空" if lots < 0 else "多"
                msg += f"| {p['instrument']} | {p['name']} | {side} | {abs(lots)} |\n"
        else:
            msg += "\n**📋 隔夜持仓**\n全部空仓\n"

        msg += f"\n**监控合约**: {', '.join(c[0] for c in WATCH_INSTRUMENTS)}"

        self.output(f"[开盘] 已推送开盘状态")
        self.state_map.last_report = time.strftime("%H:%M")
        feishu("info", "全账户", msg)

    def on_trade(self, trade: TradeData, log=True):
        super().on_trade(trade, log=True)

    def on_order(self, order: OrderData):
        super().on_order(order)

    def on_order_cancel(self, order: OrderData):
        super().on_order_cancel(order)

    def on_error(self, error):
        self.output(f"[错误] {error}")
