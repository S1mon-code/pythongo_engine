"""Unit tests for modules.session_guard.SessionGuard.

覆盖 2026-04-20 发现的 10:15-10:30 茶歇问题 + [start, end) 区间约定.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from modules.session_guard import SessionGuard


def _at(h: int, m: int):
    """Context manager to mock datetime.now() in session_guard."""
    return patch(
        "modules.session_guard.datetime",
        now=lambda: datetime(2026, 4, 21, h, m),
    )


class TestAlTeaBreak:
    """AL (SHFE) 早盘 10:15-10:30 茶歇测试."""

    def setup_method(self):
        self.guard = SessionGuard("al2607", flatten_minutes=5, sim_24h=False)

    @pytest.mark.parametrize("h,m,expected", [
        (9, 0, True),    # 开盘 start 边界含
        (10, 14, True),  # 茶歇前 1min
        (10, 15, False), # 茶歇开始,end 边界排除
        (10, 20, False), # 茶歇中
        (10, 29, False), # 茶歇结束前
        (10, 30, True),  # 茶歇结束,新段 start
        (11, 0, True),   # 茶歇后
        (11, 29, True),  # 午休前 1min
        (11, 30, False), # 午休 end 边界排除
    ])
    def test_morning_tea_break(self, h, m, expected):
        with _at(h, m):
            assert self.guard.should_trade() is expected


class TestAlNightSession:
    """AL 跨午夜夜盘 21:00-01:00."""

    def setup_method(self):
        self.guard = SessionGuard("al2607", sim_24h=False)

    @pytest.mark.parametrize("h,m,expected", [
        (20, 59, False),
        (21, 0, True),    # 夜盘开始
        (23, 0, True),
        (0, 0, True),     # 午夜
        (0, 59, True),
        (1, 0, False),    # end 边界排除
        (2, 0, False),
    ])
    def test_night_session(self, h, m, expected):
        with _at(h, m):
            assert self.guard.should_trade() is expected


class TestCffexNoTeaBreak:
    """CFFEX 股指 09:30-11:30 连续,无茶歇."""

    def setup_method(self):
        self.guard = SessionGuard("if2506", sim_24h=False)

    @pytest.mark.parametrize("h,m,expected", [
        (9, 29, False),
        (9, 30, True),   # 开盘
        (10, 15, True),  # 商品期货茶歇时段, CFFEX 继续交易
        (10, 30, True),
        (11, 29, True),
        (11, 30, False),
    ])
    def test_cffex_continuous(self, h, m, expected):
        with _at(h, m):
            assert self.guard.should_trade() is expected


class TestSim24H:
    """sim_24h=True 跳过所有时段检查."""

    def test_always_trades(self):
        guard = SessionGuard("al2607", sim_24h=True)
        with _at(3, 0):  # 完全非交易时段
            assert guard.should_trade() is True

    def test_status_shows_sim(self):
        guard = SessionGuard("al2607", sim_24h=True)
        assert guard.get_status() == "24H模拟"


class TestFlattenZone:
    """即将收盘区间 (flatten_minutes)."""

    def setup_method(self):
        self.guard = SessionGuard("al2607", flatten_minutes=5, sim_24h=False)

    @pytest.mark.parametrize("h,m,expected", [
        (10, 9, False),   # 茶歇前 6min, 非清仓区
        (10, 10, True),   # 茶歇前 5min, 清仓区开始
        (10, 14, True),   # 茶歇前 1min
        (10, 15, False),  # 进入茶歇, 不在 session
    ])
    def test_flatten_before_tea_break(self, h, m, expected):
        with _at(h, m):
            assert self.guard.should_flatten() is expected

    @pytest.mark.parametrize("h,m,expected", [
        (14, 54, False),  # 收盘前 6min
        (14, 55, True),   # 收盘前 5min, 清仓区
        (14, 59, True),
        (15, 0, False),   # 收盘, 不在 session
    ])
    def test_flatten_before_day_close(self, h, m, expected):
        with _at(h, m):
            assert self.guard.should_flatten() is expected


class TestStatus:
    """get_status 字符串."""

    def setup_method(self):
        self.guard = SessionGuard("al2607", flatten_minutes=5, sim_24h=False)

    @pytest.mark.parametrize("h,m,expected", [
        (9, 30, "交易中"),
        (10, 12, "即将收盘"),   # 茶歇前 3min
        (10, 15, "非交易时段"),  # 茶歇
        (14, 57, "即将收盘"),   # 收盘前 3min
        (16, 0, "非交易时段"),   # 日盘后
    ])
    def test_status(self, h, m, expected):
        with _at(h, m):
            assert self.guard.get_status() == expected
