"""绩效追踪模块.

用法:
    from modules.performance import PerformanceTracker
    perf = PerformanceTracker()
    perf.on_close(entry_px=800.0, exit_px=810.0, lots=3)  # 平仓时
    summary = perf.get_summary()  # {"n":1, "wr":100, "total":30000, ...}
    report = perf.format_report("i2509")  # markdown周报
"""
import time

MULTIPLIER = 100


class PerformanceTracker:
    def __init__(self):
        self._trades = []
        self._total_pnl = 0.0

    def on_close(self, entry_px, exit_px, lots):
        """记录平仓. 返回绝对盈亏."""
        pnl = (exit_px - entry_px) * lots * MULTIPLIER
        pct = ((exit_px / entry_px) - 1) * 100 if entry_px > 0 else 0
        self._trades.append({
            "time": time.strftime("%m-%d %H:%M"),
            "entry": round(entry_px, 1), "exit": round(exit_px, 1),
            "lots": lots, "pnl": pnl, "pct": round(pct, 2),
        })
        self._total_pnl += pnl
        return pnl

    def get_summary(self):
        n = len(self._trades)
        if n == 0:
            return {"n": 0, "wr": 0, "total": 0, "avg": 0, "pf": 0,
                    "max_win": 0, "max_loss": 0}
        pnls = [t["pnl"] for t in self._trades]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        gp, gl = sum(wins), sum(losses)
        return {
            "n": n,
            "wr": len(wins) / n * 100,
            "total": self._total_pnl,
            "avg": self._total_pnl / n,
            "pf": gp / gl if gl > 0 else 0,
            "max_win": max(pnls),
            "max_loss": min(pnls),
        }

    def format_report(self, symbol):
        s = self.get_summary()
        if s["n"] == 0:
            return f"**{symbol} 周报**: 无交易"
        lines = [
            f"**{symbol} 策略周报**",
            f"| 指标 | 数值 |", f"|------|------|",
            f"| 交易数 | {s['n']} |",
            f"| 胜率 | {s['wr']:.0f}% |",
            f"| 总盈亏 | {s['total']:+,.0f} |",
            f"| 盈亏比 | {s['pf']:.2f} |",
            f"| 最大盈 | {s['max_win']:+,.0f} |",
            f"| 最大亏 | {s['max_loss']:+,.0f} |",
        ]
        recent = self._trades[-5:]
        if recent:
            lines.append(f"\n**最近{len(recent)}笔:**")
            for t in recent:
                lines.append(f"  {t['time']} | {t['entry']}→{t['exit']} | {t['lots']}手 | {t['pnl']:+,.0f}")
        return "\n".join(lines)

    def format_short(self):
        """状态栏简短显示."""
        s = self.get_summary()
        if s["n"] == 0:
            return "—"
        return f"{s['n']}笔 WR{s['wr']:.0f}% PnL{s['total']:+,.0f}"
