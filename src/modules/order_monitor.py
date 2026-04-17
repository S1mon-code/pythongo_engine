"""订单超时监控模块 + Urgency 升级调度 (2026-04-17 extended).

用法 (基本):
    from modules.order_monitor import OrderMonitor
    om = OrderMonitor(timeout=30)
    om.on_send(oid, vol=3, px=800.0, urgency="normal")  # 下单后
    om.on_fill(oid)                                      # on_trade中
    om.on_cancel(oid)                                    # on_cancel中
    expired = om.check_timeouts(self.cancel_order)       # 每bar开头

Escalator 用法:
    to_escalate = om.check_escalation()  # 每 tick 在 _on_tick_aux
    for oid, next_urgency in to_escalate:
        self.cancel_order(oid)
        # 按 next_urgency 重新挂单 (策略层决定新价格)
        new_px = self._pricer.price(direction, next_urgency)
        new_oid = self.send_order(..., price=new_px)
        om.on_send(new_oid, vol, new_px, urgency=next_urgency)
"""
from __future__ import annotations

import time


# ---------------------------------------------------------------------------
# Escalator 调度: 每个 urgency 不成交多久升到下一级
#   tuple 列表, (age_sec, next_urgency)
#   首次满足条件即升级, 之后不再对该订单用本条规则
# ---------------------------------------------------------------------------
ESCALATION_SCHEDULE: dict[str, list[tuple[float, str]]] = {
    "passive":  [(5.0, "normal"), (10.0, "cross"), (15.0, "urgent")],
    "normal":   [(3.0, "cross"),  (8.0,  "urgent"), (15.0, "critical")],
    "cross":    [(3.0, "urgent"), (8.0,  "critical")],
    "urgent":   [(2.0, "critical")],
    "critical": [],  # 已到顶
}


class OrderMonitor:
    def __init__(self, timeout: float = 30):
        self._orders: dict = {}   # oid -> {"t", "vol", "px", "urgency", "escalations"}
        self._timeout = timeout

    # ------------------------------------------------------------------ #
    # 基本登记
    # ------------------------------------------------------------------ #

    def on_send(self, oid, vol, px, urgency: str = "normal",
                direction: str | None = None, kind: str | None = None):
        """记录新订单.

        Args:
            oid: broker 返回的订单号
            vol: 手数
            px: 挂单价
            urgency: passive/normal/cross/urgent/critical (用于 escalator)
            direction: "buy" / "sell" — escalator 重挂时复用
            kind: "open" (send_order) 或 "close" (auto_close_position) — 重挂时复用
        """
        if oid is None:
            return
        if urgency not in ESCALATION_SCHEDULE:
            urgency = "normal"
        self._orders[oid] = {
            "t": time.time(),
            "vol": int(vol),
            "px": float(px),
            "urgency": urgency,
            "direction": direction,
            "kind": kind,
            "escalations": 0,
        }

    def on_fill(self, oid):
        self._orders.pop(oid, None)

    def on_cancel(self, oid):
        self._orders.pop(oid, None)

    # ------------------------------------------------------------------ #
    # 超时撤单 (legacy, 保留)
    # ------------------------------------------------------------------ #

    def check_timeouts(self, cancel_fn):
        """超过 self._timeout 秒的订单直接撤掉. 返回被撤的 oid 列表."""
        now = time.time()
        expired = [(oid, info) for oid, info in self._orders.items()
                   if now - info["t"] >= self._timeout]
        result = []
        for oid, _ in expired:
            cancel_fn(oid)
            self._orders.pop(oid, None)
            result.append(oid)
        return result

    # ------------------------------------------------------------------ #
    # Escalator — 返回需要升级的订单,策略层负责撤单并按新 urgency 重挂
    # ------------------------------------------------------------------ #

    def check_escalation(self, now_ts: float | None = None
                         ) -> list[tuple[object, str, dict]]:
        """返回需要升级的订单清单 [(oid, next_urgency, info), ...].

        调用方负责:
          1. 撤 oid
          2. 按 next_urgency 算新价
          3. 发新单
          4. 调用 on_send(new_oid, ..., urgency=next_urgency)

        同一订单不会在同一调用中被返回多次; escalations 计数自增,
        下一级升级门槛在 schedule 列表中索引 escalations 之后。
        """
        if now_ts is None:
            now_ts = time.time()
        out = []
        for oid, info in list(self._orders.items()):
            urgency = info["urgency"]
            schedule = ESCALATION_SCHEDULE.get(urgency, [])
            step = info["escalations"]
            if step >= len(schedule):
                continue
            threshold, next_urgency = schedule[step]
            age = now_ts - info["t"]
            if age >= threshold:
                info["escalations"] = step + 1
                out.append((oid, next_urgency, dict(info)))
        return out

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #

    def pending_count(self) -> int:
        return len(self._orders)

    def get_order(self, oid) -> dict | None:
        return self._orders.get(oid)
