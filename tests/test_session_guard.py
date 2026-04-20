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


class TestOpenGrace:
    """2026-04-20: open_grace_sec — 开盘后 N 秒内 should_trade 返 False,
    避开 broker 开盘瞬间 rush."""

    def test_no_grace_default(self):
        """默认 open_grace_sec=0,09:00 立即可交易."""
        guard = SessionGuard("al2607", sim_24h=False)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 0, 0)):
            assert guard.should_trade() is True

    def test_grace_30s_blocks_first_29s(self):
        guard = SessionGuard("al2607", sim_24h=False, open_grace_sec=30)
        # 09:00:00 - 09:00:29 期间拒绝
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 0, 0)):
            assert guard.should_trade() is False
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 0, 29)):
            assert guard.should_trade() is False
        # 09:00:30 之后允许
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 0, 30)):
            assert guard.should_trade() is True
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 5, 0)):
            assert guard.should_trade() is True

    def test_grace_applies_to_tea_break_end(self):
        """10:30 茶歇后也需要 grace."""
        guard = SessionGuard("al2607", sim_24h=False, open_grace_sec=30)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 10, 30, 15)):
            assert guard.should_trade() is False  # 茶歇后 15s,仍在 grace
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 10, 30, 31)):
            assert guard.should_trade() is True

    def test_grace_applies_to_afternoon_open(self):
        """13:30 午盘开盘 grace."""
        guard = SessionGuard("al2607", sim_24h=False, open_grace_sec=30)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 13, 30, 10)):
            assert guard.should_trade() is False
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 13, 30, 45)):
            assert guard.should_trade() is True

    def test_grace_applies_to_night_open(self):
        """21:00 夜盘开盘 grace (跨午夜 session 起点)."""
        guard = SessionGuard("al2607", sim_24h=False, open_grace_sec=30)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 21, 0, 5)):
            assert guard.should_trade() is False
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 21, 0, 31)):
            assert guard.should_trade() is True

    def test_grace_does_not_affect_midnight_crossover(self):
        """跨午夜 session 中段 (00:30) grace 不应再次触发."""
        guard = SessionGuard("al2607", sim_24h=False, open_grace_sec=30)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 0, 30, 0)):
            # 00:30 距夜盘 21:00 已 3.5 小时,远超 grace
            assert guard.should_trade() is True

    def test_seconds_since_session_start_normal(self):
        guard = SessionGuard("al2607", sim_24h=False)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 9, 5, 30)):
            # 距 09:00 开盘 5min30s = 330s
            assert guard.seconds_since_session_start() == 330

    def test_seconds_since_session_start_out_of_session(self):
        guard = SessionGuard("al2607", sim_24h=False)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 10, 20, 0)):
            # 茶歇中,不在任何 session
            assert guard.seconds_since_session_start() == -1

    def test_seconds_since_night_session_across_midnight(self):
        """21:00-01:00 夜盘, 00:30 时已 3.5 小时."""
        guard = SessionGuard("al2607", sim_24h=False)
        with patch("modules.session_guard.datetime",
                   now=lambda: datetime(2026, 4, 21, 0, 30, 0)):
            # 距 21:00 开盘 3h30min = 12600s
            assert guard.seconds_since_session_start() == 12600


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
