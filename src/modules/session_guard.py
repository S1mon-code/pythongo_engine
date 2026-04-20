"""
交易时段守卫模块

判断当前是否在交易时段内，并提供"即将收盘"状态提示。
盘前清仓功能已全局禁用，should_trade()不再受flatten zone影响。

使用示例::

    from modules.session_guard import SessionGuard
    guard = SessionGuard("i2609", flatten_minutes=5)

    # In callback:
    if guard.should_trade():
        # normal signal logic (交易时段内，包括收盘前)
    else:
        # outside trading hours, skip
"""

from datetime import datetime

from modules.contract_info import get_sessions


class SessionGuard:
    """交易时段守卫，判断当前是否在交易时段内并提供收盘提示。"""

    def __init__(
        self,
        instrument_id: str,
        flatten_minutes: int = 5,
        sim_24h: bool = False,
        open_grace_sec: int = 0,
    ):
        """
        Args:
            instrument_id: 合约代码，如 "i2609"，用于查询交易时段。
            flatten_minutes: 距每节收盘多少分钟开始触发清仓（默认 5 分钟）。
            sim_24h: True 时跳过时段检查，适用于24小时模拟盘。
            open_grace_sec: 开盘后 N 秒内 should_trade 仍返 False (避开 broker
                开盘瞬间 rush 导致的拒单). 默认 0 (不启用). 生产策略推荐 30.
                影响所有 session 起点 (早盘 09:00, 茶歇后 10:30, 午盘 13:30,
                夜盘 21:00). (2026-04-20 新增)
        """
        self._instrument_id = instrument_id
        self._flatten_minutes = flatten_minutes
        self._sim_24h = sim_24h
        self._open_grace_sec = max(0, int(open_grace_sec))
        self._sessions = get_sessions(instrument_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_trade(self) -> bool:
        """当前时刻是否允许交易.

        False 当: 周末 / 非交易时段 / session 开始后 open_grace_sec 秒内.
        盘前清仓已全局禁用。
        """
        if self._sim_24h:
            return True
        if self._is_weekend():
            return False
        if not self._in_session():
            return False
        # 开盘后 grace period 保护: 避开 broker 开盘瞬间 rush
        if self._open_grace_sec > 0:
            elapsed = self.seconds_since_session_start()
            if 0 <= elapsed < self._open_grace_sec:
                return False
        return True

    def seconds_since_session_start(self) -> int:
        """返回距离当前 session 开始的秒数 (不在 session 返 -1).

        用途: 策略可根据开盘后秒数决定是否延迟执行某些操作.
        """
        now = datetime.now()
        cur_sec = now.hour * 3600 + now.minute * 60 + now.second

        for (start_h, start_m), (end_h, end_m) in self._sessions:
            start_sec = start_h * 3600 + start_m * 60
            end_sec = end_h * 3600 + end_m * 60

            if start_sec <= end_sec:
                # 普通时段
                if start_sec <= cur_sec < end_sec:
                    return cur_sec - start_sec
            else:
                # 跨午夜时段: 21:00-01:00
                if cur_sec >= start_sec:
                    return cur_sec - start_sec
                if cur_sec < end_sec:
                    return cur_sec + 86400 - start_sec

        return -1

    def should_flatten(self) -> bool:
        """当前时刻是否处于清仓区间（在某节收盘前 flatten_minutes 分钟内且仍在时段内）。"""
        if self._sim_24h:
            return False
        if self._is_weekend():
            return False
        return self._in_session() and self._in_flatten_zone()

    def get_status(self) -> str:
        """返回当前状态字符串，用于界面显示。"""
        if self._sim_24h:
            return "24H模拟"
        if self._is_weekend() or not self._in_session():
            return "非交易时段"
        if self._in_flatten_zone():
            return "即将收盘"
        return "交易中"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_weekend() -> bool:
        """判断今天是否是周末（周六=5, 周日=6）。"""
        return datetime.now().weekday() >= 5

    @staticmethod
    def _to_minutes(hour: int, minute: int) -> int:
        """将时:分转为当天的分钟数（0-1439）。"""
        return hour * 60 + minute

    def _in_session(self) -> bool:
        """判断当前时刻是否落在任一交易时段内.

        区间约定: [start, end) — 包含 start 不含 end.
        这样相邻 sessions(如早盘上段结束 10:15 = 茶歇开始)边界正确,
        不会重复包含。与 `contract_info._is_time_in_session` 对齐 (2026-04-20)。
        """
        now = datetime.now()
        cur = self._to_minutes(now.hour, now.minute)

        for (start_h, start_m), (end_h, end_m) in self._sessions:
            start = self._to_minutes(start_h, start_m)
            end = self._to_minutes(end_h, end_m)

            if start <= end:
                # 普通时段，如 09:00-10:15 (茶歇开始 10:15 被正确排除)
                if start <= cur < end:
                    return True
            else:
                # 跨午夜时段，如 21:00-02:30
                if cur >= start or cur < end:
                    return True

        return False

    def _in_flatten_zone(self) -> bool:
        """判断当前时刻是否在任一时段的清仓区间内。"""
        now = datetime.now()
        cur = self._to_minutes(now.hour, now.minute)

        for (_start_h, _start_m), (end_h, end_m) in self._sessions:
            start = self._to_minutes(_start_h, _start_m)
            end = self._to_minutes(end_h, end_m)
            flatten_start = end - self._flatten_minutes

            if start <= end:
                # 普通时段：清仓区间 [flatten_start, end]
                if flatten_start <= cur <= end:
                    return True
            else:
                # 跨午夜时段
                # flatten_start 可能 < 0（如 end=150 即 02:30，减 5 = 145 即 02:25）
                # 也可能需要绕到前一天（不太常见，flatten_minutes 一般较小）
                if flatten_start >= 0:
                    # 清仓区间在午夜后的一小段，如 02:25-02:30
                    if flatten_start <= cur <= end:
                        return True
                else:
                    # flatten_start < 0 → 绕到前一天 23:xx
                    adjusted = 1440 + flatten_start  # e.g. -5 → 1435 即 23:55
                    if cur >= adjusted or cur <= end:
                        return True

        return False
