"""绩效追踪模块.

追踪累计和每日交易绩效。每日PnL从21:00（夜盘开盘）重置。

用法:
    from modules.performance import PerformanceTracker
    perf = PerformanceTracker("i2609")
    perf.on_close(entry_px=800.0, exit_px=810.0, lots=3)
    perf.on_day_change()   # 21:00交易日切换时调用
    summary = perf.get_summary()
    daily = perf.get_daily_summary()
    report = perf.format_report("i2609")
"""
import time

from modules.contract_info import get_multiplier


class PerformanceTracker:
    def __init__(self, instrument_id: str):
        self._multiplier = get_multiplier(instrument_id)
        self._trades = []          # 全部交易
        self._total_pnl = 0.0
        # 每日追踪（21:00重置）
        self._daily_trades = []
        self._daily_pnl = 0.0

    def on_close(self, entry_px, exit_px, lots):
        """记录平仓. 返回绝对盈亏."""
        pnl = (exit_px - entry_px) * lots * self._multiplier
        pct = ((exit_px / entry_px) - 1) * 100 if entry_px > 0 else 0
        trade = {
            "time": time.strftime("%m-%d %H:%M"),
            "entry": round(entry_px, 1), "exit": round(exit_px, 1),
            "lots": lots, "pnl": pnl, "pct": round(pct, 2),
        }
        self._trades.append(trade)
        self._total_pnl += pnl
        self._daily_trades.append(trade)
        self._daily_pnl += pnl
        return pnl

    def on_day_change(self):
        """交易日切换（21:00），重置每日统计."""
        self._daily_trades = []
        self._daily_pnl = 0.0

    @property
    def daily_pnl(self) -> float:
        """当日PnL（从21:00开始累计）."""
        return self._daily_pnl

    @property
    def daily_trade_count(self) -> int:
        """当日交易笔数."""
        return len(self._daily_trades)

    def _calc_summary(self, trades, total_pnl):
        """通用统计计算."""
        n = len(trades)
        if n == 0:
            return {"n": 0, "wr": 0, "total": 0, "avg": 0, "pf": 0,
                    "max_win": 0, "max_loss": 0}
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        gp, gl = sum(wins), sum(losses)
        return {
            "n": n,
            "wr": len(wins) / n * 100,
            "total": total_pnl,
            "avg": total_pnl / n,
            "pf": gp / gl if gl > 0 else 0,
            "max_win": max(pnls),
            "max_loss": min(pnls),
        }

    def get_summary(self):
        """全部交易统计."""
        return self._calc_summary(self._trades, self._total_pnl)

    def get_daily_summary(self):
        """当日交易统计（从21:00开始）."""
        return self._calc_summary(self._daily_trades, self._daily_pnl)

    def format_report(self, symbol):
        s = self.get_summary()
        d = self.get_daily_summary()
        if s["n"] == 0:
            return f"**{symbol} 周报**: 无交易"
        lines = [
            f"**{symbol} 策略报告**",
            f"| 指标 | 累计 | 当日 |", f"|------|------|------|",
            f"| 交易数 | {s['n']} | {d['n']} |",
            f"| 胜率 | {s['wr']:.0f}% | {d['wr']:.0f}% |",
            f"| 总盈亏 | {s['total']:+,.0f} | {d['total']:+,.0f} |",
            f"| 盈亏比 | {s['pf']:.2f} | {d['pf']:.2f} |",
            f"| 最大盈 | {s['max_win']:+,.0f} | {d['max_win']:+,.0f} |",
            f"| 最大亏 | {s['max_loss']:+,.0f} | {d['max_loss']:+,.0f} |",
        ]
        recent = self._trades[-5:]
        if recent:
            lines.append(f"\n**最近{len(recent)}笔:**")
            for t in recent:
                lines.append(f"  {t['time']} | {t['entry']}→{t['exit']} | {t['lots']}手 | {t['pnl']:+,.0f}")
        return "\n".join(lines)

    def format_short(self):
        """状态栏简短显示（含当日）."""
        s = self.get_summary()
        d = self.get_daily_summary()
        if s["n"] == 0:
            return "—"
        daily_str = f" 今日{d['n']}笔{d['total']:+,.0f}" if d["n"] > 0 else ""
        return f"{s['n']}笔 WR{s['wr']:.0f}% PnL{s['total']:+,.0f}{daily_str}"
