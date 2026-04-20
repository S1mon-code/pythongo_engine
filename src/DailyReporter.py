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

from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

from modules.contract_info import get_multiplier
from modules.error_handler import throttle_on_error
from modules.feishu import feishu
from modules.trading_day import get_trading_day


# 全品种监控列表 — 只报告有持仓的
WATCH_INSTRUMENTS = [
    # ── 中金所 (CFFEX) ──
    ("IF2606", "沪深300", 300),
    ("IH2606", "上证50", 300),
    ("IC2606", "中证500", 200),
    ("IM2606", "中证1000", 200),
    ("TS2609", "2年国债", 20000),
    ("TF2609", "5年国债", 10000),
    ("T2609", "10年国债", 10000),
    ("TL2609", "30年国债", 10000),
    # ── 能源中心 (INE) ──
    ("sc2606", "原油", 1000),
    ("nr2607", "20号胶", 10),
    ("lu2607", "低硫燃油", 10),
    ("bc2606", "国际铜", 5),
    ("ec2608", "集运指数", 50),
    # ── 上期所 (SHFE) ──
    ("rb2610", "螺纹钢", 10),
    ("hc2610", "热卷", 10),
    ("cu2605", "铜", 5),
    ("al2605", "电解铝", 5),
    ("zn2607", "锌", 5),
    ("pb2607", "铅", 5),
    ("ni2607", "镍", 1),
    ("sn2607", "锡", 1),
    ("ss2607", "不锈钢", 5),
    ("ao2607", "氧化铝", 20),
    ("au2608", "黄金", 1000),
    ("ag2608", "白银", 15),
    ("ru2609", "天然橡胶", 10),
    ("bu2609", "沥青", 10),
    ("sp2609", "纸浆", 10),
    ("fu2609", "燃料油", 10),
    ("br2607", "丁二烯橡胶", 5),
    # ── 大商所 (DCE) ──
    ("i2609", "铁矿石", 100),
    ("j2609", "焦炭", 100),
    ("jm2609", "焦煤", 60),
    ("a2609", "豆一", 10),
    ("m2609", "豆粕", 10),
    ("y2609", "豆油", 10),
    ("p2609", "棕榈油", 10),
    ("c2609", "玉米", 10),
    ("cs2609", "玉米淀粉", 10),
    ("l2609", "聚乙烯", 5),
    ("v2609", "PVC", 5),
    ("pp2609", "聚丙烯", 5),
    ("eg2609", "乙二醇", 10),
    ("eb2609", "苯乙烯", 5),
    ("pg2609", "LPG", 20),
    ("jd2609", "鸡蛋", 5),
    ("lh2609", "生猪", 16),
    # ── 郑商所 (CZCE) ──
    ("CF609", "棉花", 5),
    ("SR609", "白糖", 10),
    ("TA609", "PTA", 5),
    ("MA609", "甲醇", 10),
    ("FG609", "玻璃", 20),
    ("SA609", "纯碱", 20),
    ("RM609", "菜粕", 10),
    ("OI609", "菜油", 10),
    ("UR609", "尿素", 20),
    ("PF609", "短纤", 5),
    ("PK609", "花生", 5),
    ("AP610", "苹果", 10),
    ("SM609", "锰硅", 5),
    ("SF609", "硅铁", 5),
    ("PX609", "对二甲苯", 5),
    ("SH609", "烧碱", 30),
    # ── 广期所 (GFEX) ──
    ("si2609", "工业硅", 5),
    ("lc2609", "碳酸锂", 1),
    ("ps2609", "多晶硅", 3),
]

# 推送时间
REVIEW_HOUR = 15
REVIEW_MINUTE = 5
MORNING_HOUR = 21
MORNING_MINUTE = 5


class Params(BaseParams):
    exchange: str = Field(default="SHFE", title="交易所代码")
    instrument_id: str = Field(default="cu2605", title="挂载合约(仅用于tick)")
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

    def _timer_loop(self):
        """独立定时线程，不依赖tick，每30秒检查一次时间."""
        import threading
        while self._timer_running:
            try:
                now = datetime.now()

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

                # 日终汇总
                if (not self._review_sent
                        and now.hour == REVIEW_HOUR
                        and REVIEW_MINUTE <= now.minute < REVIEW_MINUTE + 5):
                    self._send_daily_summary()
                    self._review_sent = True
                    self.output(f"[定时] 日终汇总已推送")

                # 开盘状态
                if (not self._morning_sent
                        and now.hour == MORNING_HOUR
                        and MORNING_MINUTE <= now.minute < MORNING_MINUTE + 5):
                    self._send_morning_report()
                    self._morning_sent = True
                    self.output(f"[定时] 开盘状态已推送")

            except Exception as e:
                self.output(f"[定时异常] {type(e).__name__}: {e}")

            time.sleep(30)  # 每30秒检查一次

    def on_start(self):
        import threading
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

        # 启动定时线程（不依赖tick）
        self._timer_running = True
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

        super().on_start()
        self.output(f"DailyReporter 启动 | 监控 {len(WATCH_INSTRUMENTS)} 个合约 | 定时线程已启动")
        start_msg = (f"**全账户监控启动**\n"
                     f"监控品种: {len(WATCH_INSTRUMENTS)}个\n"
                     f"权益: {acct.balance:,.0f}" if acct else "**监控启动** 未获取账户")
        feishu("start", "MONITOR", start_msg)

    def on_stop(self):
        self._timer_running = False
        super().on_stop()

    def on_tick(self, tick: TickData):
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)
        # tick只用于feed K线，定时检查已移到独立线程

    def callback(self, kline: KLineData):
        pass  # 不处理K线

    def real_time_callback(self, kline: KLineData):
        pass  # 不推图表

    def _scan_positions(self):
        """扫描全品种持仓，只返回有持仓的."""
        positions = []
        for inst_id, name, mult in WATCH_INSTRUMENTS:
            pos = self.get_position(inst_id)
            lots = pos.net_position if pos else 0
            if lots == 0:
                continue  # 跳过空仓
            avg = pos.open_avg_price if pos and hasattr(pos, 'open_avg_price') else 0
            last = pos.last_price if pos and hasattr(pos, 'last_price') and pos.last_price > 0 else 0
            if avg > 0 and last > 0:
                if lots > 0:
                    pnl = (last - avg) * lots * mult
                else:
                    pnl = (avg - last) * abs(lots) * mult
            else:
                pnl = 0
            positions.append({
                "instrument": inst_id,
                "name": name,
                "multiplier": mult,
                "lots": lots,
                "avg_price": avg,
                "last_price": last,
                "pnl": pnl,
            })
        return positions

    def _send_daily_summary(self):
        """15:05 全账户日终汇总 (column_set表格)."""
        import requests
        acct = self._get_account()
        if not acct:
            return

        eq = acct.balance
        available = acct.available
        pos_profit = acct.position_profit
        daily_abs = eq - self._daily_start_eq
        daily_pct = daily_abs / self._daily_start_eq * 100 if self._daily_start_eq > 0 else 0

        positions = self._scan_positions()
        total_pnl = sum(p['pnl'] for p in positions) if positions else 0

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

        if positions:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**📋 持仓明细 ({len(positions)}个品种, 合计盈亏: {total_pnl:+,.0f})**"}})
            elements.append(self._table_row(
                ["**合约**", "**方向**", "**手数**", "**均价**", "**现价**", "**盈亏**"],
                bg="grey"))
            for p in positions:
                lots = p['lots']
                side = "空" if lots < 0 else "多"
                pnl_str = f"**{p['pnl']:+,.0f}**"
                elements.append(self._table_row([
                    f"{p['instrument']} {p['name']}", side, str(abs(lots)),
                    f"{p['avg_price']:,.1f}", f"{p['last_price']:,.1f}", pnl_str,
                ]))
        else:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "**📋 持仓明细**\n全部空仓"}})

        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"监控品种: {len(WATCH_INSTRUMENTS)}个\n\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*"}})

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

        positions = self._scan_positions()  # 只返回有持仓的

        msg = (
            f"**新交易日开盘**\n"
            f"交易日: {self._current_td}\n\n"
            f"**📊 账户状态**\n"
            f"权益: {acct.balance:,.0f}\n"
            f"可用资金: {acct.available:,.0f}\n"
        )

        if positions:
            msg += f"\n**📋 隔夜持仓 ({len(positions)}个品种)**\n"
            msg += "| 合约 | 品种 | 方向 | 手数 |\n|--|--|--|--|\n"
            for p in positions:
                lots = p['lots']
                side = "空" if lots < 0 else "多"
                msg += f"| {p['instrument']} | {p['name']} | {side} | {abs(lots)} |\n"
        else:
            msg += "\n**📋 隔夜持仓**\n全部空仓\n"

        msg += f"\n**监控品种**: {len(WATCH_INSTRUMENTS)}个"

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
        throttle_on_error(self, error)
