"""订单超时监控模块.

用法:
    from modules.order_monitor import OrderMonitor
    om = OrderMonitor(timeout=30)
    om.on_send(oid, vol=3, px=800.0)        # 下单后
    om.on_fill(oid)                          # on_trade中
    om.on_cancel(oid)                        # on_order_cancel中
    expired = om.check_timeouts(self.cancel_order)  # 每bar开头
"""
import time


class OrderMonitor:
    def __init__(self, timeout=30):
        self._orders = {}  # oid -> {"t": time, "vol": int, "px": float}
        self._timeout = timeout

    def on_send(self, oid, vol, px):
        """记录新订单."""
        if oid is not None:
            self._orders[oid] = {"t": time.time(), "vol": int(vol), "px": float(px)}

    def on_fill(self, oid):
        """成交后移除."""
        self._orders.pop(oid, None)

    def on_cancel(self, oid):
        """撤单后移除."""
        self._orders.pop(oid, None)

    def check_timeouts(self, cancel_fn):
        """检查超时订单并撤单. 返回被撤的oid列表."""
        now = time.time()
        expired = [(oid, info) for oid, info in self._orders.items()
                   if now - info["t"] >= self._timeout]
        result = []
        for oid, info in expired:
            cancel_fn(oid)
            self._orders.pop(oid, None)
            result.append(oid)
        return result

    def pending_count(self):
        return len(self._orders)
