"""Unit tests for modules.error_handler.throttle_on_error.

0004 撤单错误自动流控: trading=False → Timer(2s) → trading=True
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from modules.error_handler import throttle_on_error


class FakeStrategy:
    """Minimal stub with trading + output."""

    def __init__(self):
        self.trading = True
        self.outputs: list[str] = []

    def output(self, msg: str) -> None:
        self.outputs.append(str(msg))


class TestThrottleOnError:
    def test_non_0004_no_change(self):
        s = FakeStrategy()
        throttle_on_error(s, {"errCode": "9999", "errMsg": "bad"})
        assert s.trading is True       # 不改 trading
        assert s.outputs == []          # 不 output

    def test_missing_errcode_no_change(self):
        s = FakeStrategy()
        throttle_on_error(s, {})
        assert s.trading is True

    def test_0004_freezes_trading(self):
        s = FakeStrategy()
        throttle_on_error(s, {"errCode": "0004"}, cooldown_sec=0.1)
        assert s.trading is False       # 立即 freeze
        assert any("流控" in m for m in s.outputs)

    def test_0004_restores_after_cooldown(self):
        s = FakeStrategy()
        throttle_on_error(s, {"errCode": "0004"}, cooldown_sec=0.1)
        assert s.trading is False
        time.sleep(0.2)                # 等 Timer 触发
        assert s.trading is True
        assert any("恢复" in m for m in s.outputs)

    def test_0004_with_errmsg(self):
        """errCode 0004 + errMsg 同时处理."""
        s = FakeStrategy()
        throttle_on_error(
            s,
            {"errCode": "0004", "errMsg": "撤单失败: 订单不存在"},
            cooldown_sec=0.05,
        )
        assert s.trading is False
        time.sleep(0.15)
        assert s.trading is True

    def test_errcode_stripped(self):
        """errCode 有前后空格时正确识别."""
        s = FakeStrategy()
        throttle_on_error(s, {"errCode": "  0004  "}, cooldown_sec=0.05)
        assert s.trading is False

    def test_errcode_int_converted_to_str(self):
        """errCode 若是 int 0004 (极端 case) 应当不崩."""
        s = FakeStrategy()
        # int 0004 = 4, 不匹配 "0004" 字符串 → 不流控
        throttle_on_error(s, {"errCode": 4}, cooldown_sec=0.05)
        assert s.trading is True  # str(4) != "0004"

    def test_sequential_0004_errors(self):
        """连续两次 0004 错误,第二次应该再次触发流控."""
        s = FakeStrategy()

        throttle_on_error(s, {"errCode": "0004"}, cooldown_sec=0.05)
        assert s.trading is False
        time.sleep(0.1)
        assert s.trading is True

        throttle_on_error(s, {"errCode": "0004"}, cooldown_sec=0.05)
        assert s.trading is False
        time.sleep(0.1)
        assert s.trading is True
