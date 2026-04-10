"""心跳监控模块.

用法:
    from modules.heartbeat import HeartbeatMonitor
    hb = HeartbeatMonitor("i2609", interval_min=30)
    alerts = hb.check("i2609")  # 在on_tick中调用
    for atype, msg in alerts:
        if atype == "no_tick": feishu("no_tick", symbol, msg)
"""
import time
from datetime import datetime

from modules.contract_info import get_sessions

NO_TICK_TIMEOUT = 60  # 秒


class HeartbeatMonitor:
    def __init__(self, instrument_id: str, interval_min=30):
        self._sessions = get_sessions(instrument_id)
        self._interval = interval_min * 60
        self._last_hb = 0.0
        self._last_tick = 0.0

    def check(self, symbol):
        """每tick调用. 返回 [(alert_type, message), ...]"""
        now = time.time()
        alerts = []

        if not self._in_session():
            self._last_tick = now
            return alerts

        # tick中断检测
        if self._last_tick > 0 and (now - self._last_tick) >= NO_TICK_TIMEOUT:
            gap = now - self._last_tick
            alerts.append(("no_tick",
                           f"**行情中断** {symbol}\n无tick: {gap:.0f}秒"))

        self._last_tick = now

        # 心跳
        if self._last_hb == 0 or (now - self._last_hb) >= self._interval:
            self._last_hb = now
            alerts.append(("heartbeat", f"**心跳** {symbol} 正常运行"))

        return alerts

    def _in_session(self):
        m = datetime.now().hour * 60 + datetime.now().minute
        return any(sh * 60 + sm <= m < eh * 60 + em for (sh, sm), (eh, em) in self._sessions)
