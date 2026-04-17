"""滚动时间窗口 VWAP (2026-04-17).

为 ScaledEntryExecutor 提供"便宜价"参考锚。与现有 `_on_tick_vwap` 的
`_vwap_cum_pv / _vwap_cum_vol` 共用 delta_vol 累积模式, 但:
  - 跨 bar 平滑 (30 分钟滚动窗口, 不随 bar 重置)
  - 独立对象, 易测试
  - 空样本返回 0.0, 策略层判 `has_enough_samples` 决定是否信任

用法:
    from modules.rolling_vwap import RollingVWAP

    self._vwap = RollingVWAP(window_seconds=1800)

    # 每 tick:
    self._vwap.update(tick.last_price, tick.volume, datetime.now())
    cheap = tick.last_price < self._vwap.value
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Tuple


class RollingVWAP:
    """滚动时间窗口 VWAP.

    参数:
        window_seconds: 滑窗长度, 默认 1800 秒 (30 分钟)
        min_samples: 认为数据足够的最小样本数, 默认 20

    关键假设:
        tick.volume 是**累计值** (从开盘起), 因此需要 delta_vol 才能算 VWAP。
        该类内部记住上一次的 cum_volume, 计算 delta = cur - prev。
        如果 delta < 0 (比如跨交易日归零), 视为新会话, 重置 prev。
    """

    def __init__(self, window_seconds: int = 1800, min_samples: int = 20):
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        self._window = timedelta(seconds=window_seconds)
        self._min_samples = min_samples

        # (timestamp, price, delta_vol)
        self._samples: Deque[Tuple[datetime, float, int]] = deque()
        self._cum_pv = 0.0
        self._cum_vol = 0
        self._prev_cum_volume = 0

    # ------------------------------------------------------------------ #
    # Tick 喂入
    # ------------------------------------------------------------------ #

    def update(self, price: float, cum_volume: int, now: datetime) -> None:
        """每 tick 调用.

        price: tick.last_price
        cum_volume: tick.volume (累计成交量, 自开盘)
        now: tick 对应时间 (策略层传 datetime.now())
        """
        # 累计值可能因会话切换归零 — 同步 prev, 不添样本
        if cum_volume < self._prev_cum_volume:
            self._prev_cum_volume = cum_volume
            return

        delta = cum_volume - self._prev_cum_volume
        self._prev_cum_volume = cum_volume

        if delta > 0 and price > 0:
            self._samples.append((now, float(price), int(delta)))
            self._cum_pv += price * delta
            self._cum_vol += delta

        self._evict_old(now)

    def _evict_old(self, now: datetime) -> None:
        """删除超过 window 的老样本."""
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            _, p, v = self._samples.popleft()
            self._cum_pv -= p * v
            self._cum_vol -= v
            if self._cum_vol < 0:
                # 浮点误差, 夹住
                self._cum_vol = 0
                self._cum_pv = 0.0

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def value(self) -> float:
        """当前窗口 VWAP. 样本不足返回 0.0."""
        if self._cum_vol <= 0:
            return 0.0
        return self._cum_pv / self._cum_vol

    @property
    def has_enough_samples(self) -> bool:
        return len(self._samples) >= self._min_samples

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def is_cheap(self, price: float, threshold_pct: float = 0.0) -> bool:
        """price 是否低于 VWAP (threshold_pct 为额外阈值, 比如 0.5 表示需低于 0.5%).

        样本不足时返回 False (不冒险判便宜)。
        """
        if not self.has_enough_samples:
            return False
        vwap = self.value
        if vwap <= 0:
            return False
        return price < vwap * (1 - threshold_pct / 100)

    def debug_snapshot(self) -> str:
        return (
            f"vwap={self.value:.2f} samples={len(self._samples)} "
            f"cum_vol={self._cum_vol}"
        )
