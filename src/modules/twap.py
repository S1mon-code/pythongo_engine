"""简化版TWAP执行器 — 在bar开始后第2-11分钟分批下单.

信号在bar N callback产生 → bar N+1开始 → TWAP在第2-11分钟分批执行。
止损信号不走TWAP，立即执行。

用法:
    from modules.twap import TWAPExecutor, IMMEDIATE_ACTIONS

    # on_start:
    self._twap = TWAPExecutor()

    # callback中, pending执行时:
    if action in IMMEDIATE_ACTIONS:
        self._execute(kline, action)           # 止损立即
    else:
        self._twap.submit(action, vol, direction, reason)  # 正常信号→TWAP

    # on_tick中:
    batch = self._twap.check()
    if batch is not None:
        lots = batch
        oid = self.send_order(...)
        self._twap.on_send(oid, lots)

    # on_trade中:
    self._twap.on_fill(trade.volume, trade.price)
"""
import math
import time

from modules.contract_info import is_in_session


# 止损信号立即执行, 不走TWAP
IMMEDIATE_ACTIONS = frozenset({
    "HARD_STOP", "TRAIL_STOP", "EQUITY_STOP",
    "CIRCUIT", "DAILY_STOP", "FLATTEN",
})

EXEC_START_MIN = 2   # 第2分钟开始
EXEC_END_MIN = 11    # 第11分钟结束(含)


class TWAPExecutor:
    """简化版TWAP: 在bar开始后第2-11分钟均匀分批执行."""

    def __init__(self):
        self._instrument_id = ""
        self._active = False
        self._action = ""
        self._direction = ""        # "buy" / "sell"
        self._reason = ""
        self._total_lots = 0
        self._filled_lots = 0
        self._sent_lots = 0         # 已发送(含未成交)
        self._start_ts = 0.0        # submit时间戳
        self._done_minutes = set()  # 已执行的分钟
        self._fills = []            # [(price, lots), ...]
        self._pending_oid = None    # 当前未成交订单

    # ──────────────────────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────────────────────

    def submit(self, action: str, total_lots: int, direction: str,
               reason: str = "", instrument_id: str = ""):
        """提交TWAP订单. 在callback中调用."""
        if total_lots <= 0:
            return
        self._instrument_id = instrument_id or self._instrument_id
        self._active = True
        self._action = action
        self._direction = direction
        self._reason = reason
        self._total_lots = total_lots
        self._filled_lots = 0
        self._sent_lots = 0
        self._start_ts = time.time()
        self._done_minutes = set()
        self._fills = []
        self._pending_oid = None

    def check(self):
        """每tick调用. 返回本次应下单手数, 或None.

        Returns:
            int: 应下单的手数
            None: 本tick不需要下单
        """
        if not self._active:
            return None

        # 用sent_lots防止重复下单(sent含未成交, filled仅已成交)
        remaining = self._total_lots - max(self._sent_lots, self._filled_lots)
        if remaining <= 0:
            # 检查是否全部成交完毕
            if self._filled_lots >= self._total_lots:
                self._active = False
            return None

        elapsed_sec = time.time() - self._start_ts
        elapsed_min = int(elapsed_sec // 60)

        # 非交易时段 → 不执行
        if self._instrument_id and not is_in_session(self._instrument_id):
            return None

        # 还没到第2分钟
        if elapsed_min < EXEC_START_MIN:
            return None

        # 超过第11分钟 → 强制执行全部剩余
        if elapsed_min > EXEC_END_MIN:
            self._active = False
            return remaining

        # 这一分钟已经执行过
        if elapsed_min in self._done_minutes:
            return None

        self._done_minutes.add(elapsed_min)

        # 动态分配: 剩余手数 / 剩余分钟数
        minutes_left = EXEC_END_MIN - elapsed_min + 1
        batch = max(1, math.ceil(remaining / minutes_left))
        return min(batch, remaining)

    def on_send(self, oid, lots: int):
        """下单后调用, 记录发送状态."""
        self._pending_oid = oid
        self._sent_lots += lots

    def on_fill(self, lots: int, price: float):
        """成交回报. 在on_trade中调用."""
        self._filled_lots += lots
        self._fills.append((price, lots))
        if self._filled_lots >= self._total_lots:
            self._active = False
            self._pending_oid = None

    def on_cancel(self, oid, lots: int = 0):
        """撤单回报. lots为被撤的手数, 用于回退sent_lots."""
        if oid == self._pending_oid:
            self._pending_oid = None
            if lots > 0:
                self._sent_lots = max(0, self._sent_lots - lots)

    def cancel(self):
        """取消整个TWAP."""
        self._active = False

    # ──────────────────────────────────────────────────────────────
    #  Properties
    # ──────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def action(self) -> str:
        return self._action

    @property
    def direction(self) -> str:
        return self._direction

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def vwap(self) -> float:
        """成交均价."""
        if not self._fills:
            return 0.0
        total_value = sum(p * l for p, l in self._fills)
        total_lots = sum(l for _, l in self._fills)
        return total_value / total_lots if total_lots > 0 else 0.0

    @property
    def progress(self) -> str:
        return f"{self._filled_lots}/{self._total_lots}"

    @property
    def total_lots(self) -> int:
        return self._total_lots

    @property
    def filled_lots(self) -> int:
        return self._filled_lots

    def get_status(self) -> str:
        """返回状态字符串, 用于界面显示."""
        if not self._active:
            if self._fills:
                return f"TWAP完成 {self.progress}@{self.vwap:.1f}"
            return ""
        elapsed = int((time.time() - self._start_ts) // 60)
        return f"TWAP {self.progress} min{elapsed}/{EXEC_END_MIN}"
