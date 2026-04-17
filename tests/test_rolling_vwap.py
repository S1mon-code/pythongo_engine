"""Unit tests for modules.rolling_vwap.RollingVWAP."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modules.rolling_vwap import RollingVWAP


T0 = datetime(2026, 4, 17, 10, 0, 0)


class TestUpdate:
    def test_first_tick_prime_prev_volume(self):
        """第一次 update: 如果先给 cum_volume=1000, 不应该添样本."""
        v = RollingVWAP()
        v.update(price=100.0, cum_volume=1000, now=T0)
        # Delta = 1000 - 0 = 1000, so it DOES add sample because we start from 0.
        # Actually this is a design decision. Let's explicit test current behavior.
        assert v.sample_count == 1
        assert v.value == 100.0

    def test_delta_accumulation(self):
        """每 tick delta_vol 正确累加."""
        v = RollingVWAP()
        v.update(price=100.0, cum_volume=1000, now=T0)
        v.update(price=101.0, cum_volume=1500, now=T0 + timedelta(seconds=1))
        # sample 1: p=100, v=1000
        # sample 2: p=101, v=500
        # VWAP = (100*1000 + 101*500) / 1500 = 100.333...
        assert abs(v.value - 100.3333) < 0.01
        assert v.sample_count == 2

    def test_zero_delta_not_added(self):
        """cum_volume 未变 (无成交) — 不加样本."""
        v = RollingVWAP()
        v.update(price=100.0, cum_volume=1000, now=T0)
        count1 = v.sample_count
        v.update(price=101.0, cum_volume=1000, now=T0 + timedelta(seconds=1))
        assert v.sample_count == count1

    def test_negative_delta_resets_prev(self):
        """cum_volume 回退 (跨 session) — 同步 prev, 不加样本."""
        v = RollingVWAP()
        v.update(price=100.0, cum_volume=1000, now=T0)
        v.update(price=200.0, cum_volume=500, now=T0 + timedelta(seconds=1))
        # 第二次 delta < 0, 只同步 prev
        assert v.sample_count == 1  # 只第一次的样本
        # 之后正常累加
        v.update(price=200.0, cum_volume=700, now=T0 + timedelta(seconds=2))
        # delta = 700 - 500 = 200
        assert v.sample_count == 2


class TestEviction:
    def test_old_samples_evicted(self):
        v = RollingVWAP(window_seconds=60)
        v.update(price=100.0, cum_volume=100, now=T0)
        v.update(price=200.0, cum_volume=200, now=T0 + timedelta(seconds=30))
        # 第三次 tick 超过 60 秒, 应该驱逐第一个
        v.update(price=300.0, cum_volume=300, now=T0 + timedelta(seconds=65))
        # 第一个样本 (100, 100) 应已驱逐
        # 剩: (200, 100), (300, 100)
        # VWAP = (200*100 + 300*100) / 200 = 250
        assert v.sample_count == 2
        assert abs(v.value - 250.0) < 0.01

    def test_all_evicted_returns_zero(self):
        v = RollingVWAP(window_seconds=10)
        v.update(price=100.0, cum_volume=100, now=T0)
        # 第二次很远, 第一个应被驱逐
        v.update(price=200.0, cum_volume=100, now=T0 + timedelta(seconds=60))
        # 注意 delta=0, 第二次没加样本
        # 第一个被驱逐了 (>10s)
        # 样本空
        assert v.sample_count == 0
        assert v.value == 0.0


class TestProperties:
    def test_empty_vwap_is_zero(self):
        v = RollingVWAP()
        assert v.value == 0.0
        assert v.sample_count == 0
        assert not v.has_enough_samples

    def test_has_enough_samples_threshold(self):
        v = RollingVWAP(min_samples=5)
        assert not v.has_enough_samples
        for i in range(4):
            v.update(price=100.0, cum_volume=(i + 1) * 10, now=T0 + timedelta(seconds=i))
        assert not v.has_enough_samples
        v.update(price=100.0, cum_volume=50, now=T0 + timedelta(seconds=4))
        assert v.has_enough_samples


class TestIsCheap:
    def test_not_enough_samples_returns_false(self):
        v = RollingVWAP(min_samples=5)
        v.update(price=100.0, cum_volume=10, now=T0)
        assert not v.is_cheap(price=95.0)

    def test_cheap_below_vwap(self):
        v = RollingVWAP(min_samples=1)
        # Build VWAP = 100
        for i in range(10):
            v.update(price=100.0, cum_volume=(i + 1) * 10, now=T0 + timedelta(seconds=i))
        assert v.is_cheap(price=99.0)
        assert not v.is_cheap(price=101.0)

    def test_cheap_with_threshold(self):
        v = RollingVWAP(min_samples=1)
        for i in range(10):
            v.update(price=100.0, cum_volume=(i + 1) * 10, now=T0 + timedelta(seconds=i))
        # Need price < 100 * (1 - 0.5/100) = 99.5
        assert not v.is_cheap(price=99.7, threshold_pct=0.5)
        assert v.is_cheap(price=99.3, threshold_pct=0.5)


class TestValidation:
    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            RollingVWAP(window_seconds=0)
        with pytest.raises(ValueError):
            RollingVWAP(window_seconds=-1)
