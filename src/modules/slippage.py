"""滑点记录模块.

用法:
    from modules.slippage import SlippageTracker
    slip = SlippageTracker("i2509")
    slip.set_signal_price(800.0)          # 信号产生时
    ticks = slip.on_fill(800.5, 3, "buy") # 成交时 → 1.0 ticks
    stats = slip.get_stats()              # {"avg": 1.0, "max": 1.0, "n": 1}
"""
import time

from modules.contract_info import get_tick_size


class SlippageTracker:
    def __init__(self, instrument_id: str):
        self._tick_size = get_tick_size(instrument_id)
        self._signal_px = 0.0
        self._records = []

    def set_signal_price(self, px):
        """在pending信号产生时调用, 记录信号价格."""
        self._signal_px = float(px)

    def on_fill(self, fill_px, lots, direction="buy"):
        """在on_trade中调用. 返回滑点(ticks), 正数=不利."""
        if self._signal_px <= 0:
            return 0.0
        if direction == "buy":
            raw = fill_px - self._signal_px
        else:
            raw = self._signal_px - fill_px
        ticks = raw / self._tick_size
        self._records.append({
            "signal": self._signal_px, "fill": float(fill_px),
            "ticks": ticks, "lots": int(lots), "dir": direction,
            "time": time.strftime("%H:%M:%S"),
        })
        self._signal_px = 0.0
        return ticks

    def get_stats(self):
        if not self._records:
            return {"avg": 0.0, "max": 0.0, "n": 0}
        t = [r["ticks"] for r in self._records]
        return {"avg": sum(t) / len(t), "max": max(t), "n": len(t)}

    def format_report(self):
        s = self.get_stats()
        if s["n"] == 0:
            return "滑点: 无记录"
        return f"滑点({s['n']}笔): 平均{s['avg']:.1f}tick 最大{s['max']:.1f}tick"
