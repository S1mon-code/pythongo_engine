"""动态 aggressive pricing 模块 (2026-04-17).

核心职责:
  - 追踪 tick 的 bid1/ask1/last_price
  - 滚动估算 typical spread (降低单 tick 噪音)
  - 按 urgency 分级 + spread 自适应给出限价
  - 若 TickData 不暴露 bid/ask, 自动 fallback 到 last_price + N*tick

Urgency 语义:
  passive  — 在本侧挂单等对手来吃 (快单行情易挂不到)
  normal   — 取对手 best,大概率立即成交
  cross    — 穿 2 tick 吃多档,减仓/平仓默认
  urgent   — 穿 5 tick,移动/硬止损
  critical — 穿 10 tick,熔断/权益止损,优先成交

用法:
    from modules.pricing import AggressivePricer

    self._pricer = AggressivePricer(tick_size=5.0)
    # 每 tick:
    self._pricer.update(tick)
    # 下单前:
    px = self._pricer.price(direction="buy", urgency="urgent")
"""
from __future__ import annotations

from collections import deque
from typing import Deque


_URGENCY_BASE_TICKS = {
    "passive":  -1,   # 特殊值:挂本侧 best
    "normal":    0,   # 取对手 best
    "cross":     2,   # 对手 best + 2
    "urgent":    5,
    "critical": 10,
}


# Spread 放大系数:当 typical_spread > 1 tick 时,额外 cross 更多
# cross/urgent/critical 三档启用自适应,passive/normal 不启用
_SPREAD_SCALED = {"cross", "urgent", "critical"}


class AggressivePricer:
    """Spread-aware, urgency-tiered limit price calculator.

    参数:
        tick_size: 最小变动价位(元/吨,由 contract_info 提供)
        spread_window: 滚动 spread 样本窗口,用于估算 typical spread
        dump_first_tick: 首 tick 打印所有可读字段,用于诊断 TickData schema
    """

    def __init__(self, tick_size: float, spread_window: int = 50,
                 dump_first_tick: bool = True):
        if tick_size <= 0:
            raise ValueError(f"tick_size must be > 0, got {tick_size}")
        self.tick_size = float(tick_size)
        self.last = 0.0
        self.bid1 = 0.0
        self.ask1 = 0.0
        self._spreads: Deque[int] = deque(maxlen=spread_window)
        self._dump_done = not dump_first_tick

    # ------------------------------------------------------------------ #
    # Tick 喂入
    # ------------------------------------------------------------------ #

    def update(self, tick) -> None:
        """从 tick 读取 last/bid1/ask1, 更新 spread 滚动窗口.

        TickData 字段名未在 docs 中明确, 因此用 getattr 安全读取。
        常见命名:
          - CTP 标准: bid_price_1, ask_price_1
          - 某些 wrapper: bid_price1, ask_price1 (无下划线)
          - 或: bid_price[0] / ask_price[0] (数组)
        """
        last = getattr(tick, "last_price", 0.0) or 0.0
        if last > 0:
            self.last = float(last)

        bid = self._read_field(tick, ["bid_price_1", "bid_price1", "bid_price"])
        ask = self._read_field(tick, ["ask_price_1", "ask_price1", "ask_price"])
        if bid > 0 and ask > 0 and ask > bid:
            self.bid1 = float(bid)
            self.ask1 = float(ask)
            spread_ticks = max(1, round((ask - bid) / self.tick_size))
            self._spreads.append(spread_ticks)

        if not self._dump_done:
            self._dump_done = True
            self._dump_schema(tick)

    @staticmethod
    def _read_field(tick, names) -> float:
        """安全读取 tick 的某个字段,支持标量或数组首元素."""
        for name in names:
            val = getattr(tick, name, None)
            if val is None:
                continue
            if hasattr(val, "__len__"):  # 数组型
                if len(val) > 0:
                    return float(val[0])
            else:  # 标量
                return float(val)
        return 0.0

    def _dump_schema(self, tick) -> None:
        """首 tick 诊断:打印对象上所有非私有字段,供未来排查字段名."""
        try:
            fields = {
                k: type(getattr(tick, k)).__name__
                for k in dir(tick)
                if not k.startswith("_") and not callable(getattr(tick, k, None))
            }
            print(f"[AggressivePricer][schema] {fields}")
        except Exception as e:
            print(f"[AggressivePricer][schema] dump failed: {e}")

    # ------------------------------------------------------------------ #
    # 核心:给出限价
    # ------------------------------------------------------------------ #

    @property
    def typical_spread_ticks(self) -> int:
        """滚动窗口中位 spread (ticks).没数据时返回 1."""
        if not self._spreads:
            return 1
        return sorted(self._spreads)[len(self._spreads) // 2]

    @property
    def has_book(self) -> bool:
        """bid/ask 是否可用."""
        return self.bid1 > 0 and self.ask1 > 0

    def price(self, direction: str, urgency: str = "normal") -> float:
        """返回给 send_order / auto_close_position 的限价.

        direction: "buy" 或 "sell"
        urgency: passive/normal/cross/urgent/critical

        Fallback: 若 bid/ask 未知, 用 last_price + cross_ticks * tick_size
                  (退化为传统 aggressive_price, spread 放大不生效)
        """
        if direction not in ("buy", "sell"):
            raise ValueError(f"direction must be buy/sell, got {direction}")
        if urgency not in _URGENCY_BASE_TICKS:
            raise ValueError(f"unknown urgency: {urgency}")

        base_ticks = _URGENCY_BASE_TICKS[urgency]

        # Spread 自适应: 高流动性品种 spread=1 不调; 薄品种 spread>1 放大穿档
        extra = 0
        if urgency in _SPREAD_SCALED and self.typical_spread_ticks > 1:
            extra = self.typical_spread_ticks - 1

        # Passive: 挂本侧, 不穿
        if urgency == "passive":
            if direction == "buy":
                return self.bid1 if self.has_book else self.last
            return self.ask1 if self.has_book else self.last

        total_ticks = base_ticks + extra

        if self.has_book:
            # 基准从对手 best 开始
            if direction == "buy":
                return self.ask1 + total_ticks * self.tick_size
            return self.bid1 - total_ticks * self.tick_size

        # Fallback: last_price ± ticks * tick_size
        if direction == "buy":
            return self.last + total_ticks * self.tick_size
        return self.last - total_ticks * self.tick_size

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #

    def debug_snapshot(self) -> str:
        book = f"bid={self.bid1:.2f} ask={self.ask1:.2f}" if self.has_book else "book=n/a"
        return (f"last={self.last:.2f} {book} "
                f"typical_spread={self.typical_spread_ticks}t "
                f"samples={len(self._spreads)}")
