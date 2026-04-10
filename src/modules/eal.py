"""
EAL — Execution Algorithm Layer (PythonGO 版)

将策略级大单拆成小批次，在子bar（M3）上用多因子评分决定执行时机。
移植自 AlphaForge V9.0 EAL，适配 PythonGO 实时交易。

用法:
    # on_start:
    from modules.eal import EALManager, EALConfig
    self._eal = EALManager(EALConfig())

    # 创建M3 KLineGenerator:
    self.kline_generator_exec = KLineGenerator(
        callback=self.callback_exec,
        real_time_callback=lambda k: None,
        exchange=p.exchange, instrument_id=p.instrument_id, style="M3",
    )
    self.kline_generator_exec.push_history_data()

    # on_tick:
    self.kline_generator_exec.tick_to_kline(tick)

    # callback_exec (M3 bar完成):
    def callback_exec(self, kline):
        if self._eal.is_active():
            action = self._eal.on_bar(kline)
            if action:
                vol, direction = action['volume'], action['direction']
                if direction == "sell":
                    self.send_order(..., volume=vol, order_direction="sell", market=True)
                else:
                    self.send_order(..., volume=vol, order_direction="buy", market=True)

    # 在H1/H4信号回调中，替代直接下单:
    if target != current:
        self._eal.submit(target_lots=target, current_lots=current, direction="sell")
        # 不设pending，EAL在后续M3 bar中逐批执行
"""
import time
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class EALConfig:
    """EAL 配置参数."""
    enabled: bool = True
    max_batch_size: int = 10
    execution_score_threshold: float = 0.5
    w_vwap: float = 0.40
    w_rsi: float = 0.10
    w_momentum: float = 0.25
    w_time_decay: float = 0.25
    max_participation_rate: float = 0.10
    rsi_period: int = 14
    rsi_buy_threshold: float = 35.0
    rsi_sell_threshold: float = 65.0
    momentum_lookback: int = 5
    execution_window_bars: int = 20
    max_slippage_bps: float = 50.0
    force_final_batch: bool = True
    large_reduce_threshold: float = 0.3
    liquidation_threshold: float = 0.7


@dataclass
class BatchInfo:
    """单个批次信息."""
    lots: int
    is_immediate: bool = False
    filled: bool = False
    fill_price: float = 0.0
    fill_bar: int = 0
    fill_time: str = ""
    score_at_fill: float = 0.0
    was_timeout: bool = False


@dataclass
class EALOrder:
    """EAL订单状态."""
    target_lots: int = 0
    current_lots: int = 0
    delta: int = 0
    direction: str = ""
    urgency: str = "normal"
    batches: list = field(default_factory=list)
    start_time: str = ""
    bars_elapsed: int = 0
    completed: bool = False
    vwap_fill: float = 0.0
    total_filled: int = 0


class EALManager:
    """EAL执行管理器.

    纯逻辑模块，不直接下单。返回执行动作给策略，由策略调用send_order。
    """

    def __init__(self, config=None):
        self.config = config or EALConfig()
        self._order = None

        # 子bar数据累积
        self._closes = []
        self._volumes = []
        self._cum_vp = []
        self._cum_vol = []
        self._exec_start_idx = 0

    # ══════════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════════

    def submit(self, target_lots, current_lots, direction):
        """提交执行任务.

        Args:
            target_lots: 目标手数 (绝对值)
            current_lots: 当前手数 (绝对值)
            direction: "buy" 或 "sell"
        """
        if not self.config.enabled:
            return

        delta = target_lots - current_lots
        if delta == 0:
            return

        # urgency
        if current_lots > 0 and delta < 0:
            reduce_ratio = abs(delta) / current_lots
            if reduce_ratio > self.config.liquidation_threshold:
                urgency = "liquidation"
            elif reduce_ratio > self.config.large_reduce_threshold:
                urgency = "large"
            else:
                urgency = "normal"
        else:
            urgency = "normal"

        abs_delta = abs(delta)
        batches = self._create_batch_plan(abs_delta, urgency)

        self._order = EALOrder(
            target_lots=target_lots,
            current_lots=current_lots,
            delta=delta,
            direction=direction,
            urgency=urgency,
            batches=batches,
            start_time=time.strftime("%H:%M:%S"),
            bars_elapsed=0,
            completed=False,
        )
        self._exec_start_idx = len(self._closes)

    def on_bar(self, kline):
        """每根M3子bar调用. 返回执行动作或None.

        Returns:
            dict: {'volume', 'direction', 'batch_idx', 'score', 'timeout', 'vwap_so_far'}
            None: 本bar不执行
        """
        close = kline.close
        volume = kline.volume

        self._closes.append(close)
        self._volumes.append(volume)

        vp = close * volume
        prev_vp = self._cum_vp[-1] if self._cum_vp else 0
        prev_vol = self._cum_vol[-1] if self._cum_vol else 0
        self._cum_vp.append(prev_vp + vp)
        self._cum_vol.append(prev_vol + volume)

        if self._order is None or self._order.completed:
            return None

        self._order.bars_elapsed += 1
        order = self._order

        # 找下一个未成交批次
        batch_idx = None
        batch = None
        for i, b in enumerate(order.batches):
            if not b.filled:
                batch_idx = i
                batch = b
                break

        if batch is None:
            order.completed = True
            return None

        # 锁板检测
        if hasattr(kline, 'high') and hasattr(kline, 'low'):
            if kline.high == kline.low and volume <= 1:
                return None

        # 立即执行批次
        if batch.is_immediate:
            return self._fill_batch(batch, batch_idx, close, volume, score=1.0)

        # 多因子评分
        score = self._compute_score(close, volume, order.direction)

        # 窗口最后bar
        is_final = order.bars_elapsed >= self.config.execution_window_bars
        is_last_chance = is_final and self.config.force_final_batch

        if score >= self.config.execution_score_threshold or is_last_chance:
            # 参与率检查
            if volume > 0:
                max_vol = max(1, int(volume * self.config.max_participation_rate))
                actual_lots = min(batch.lots, max_vol)
            else:
                actual_lots = batch.lots

            # 滑点检查
            vwap = self._rolling_vwap()
            if vwap > 0 and not is_last_chance:
                slippage_bps = abs(close - vwap) / vwap * 10000
                if slippage_bps > self.config.max_slippage_bps:
                    return None

            return self._fill_batch(
                batch, batch_idx, close, volume,
                score=score,
                timeout=(is_last_chance and score < self.config.execution_score_threshold),
            )

        # 窗口结束不强制 → 超时成交
        if is_final and not self.config.force_final_batch:
            return self._fill_batch(batch, batch_idx, close, volume,
                                    score=score, timeout=True)

        return None

    def is_active(self):
        """是否有执行中的订单."""
        return self._order is not None and not self._order.completed

    def get_status(self):
        """返回当前EAL状态字符串."""
        if self._order is None:
            return "空闲"
        if self._order.completed:
            filled = self._order.total_filled
            vwap = self._order.vwap_fill
            return f"完成 {filled}手@{vwap:.1f}"
        filled = sum(b.lots for b in self._order.batches if b.filled)
        total = sum(b.lots for b in self._order.batches)
        return f"执行中 {filled}/{total}手 bar{self._order.bars_elapsed}/{self.config.execution_window_bars}"

    def get_result(self):
        """返回最近一次执行结果."""
        if self._order is None:
            return None
        order = self._order
        return {
            "delta": order.delta,
            "direction": order.direction,
            "urgency": order.urgency,
            "total_filled": order.total_filled,
            "vwap_fill": order.vwap_fill,
            "bars_used": order.bars_elapsed,
            "batches": len(order.batches),
            "timeout_count": sum(1 for b in order.batches if b.was_timeout),
            "completed": order.completed,
        }

    def cancel(self):
        """取消当前执行."""
        if self._order is not None:
            self._order.completed = True

    # ══════════════════════════════════════════════════════════════
    #  Internal
    # ══════════════════════════════════════════════════════════════

    def _create_batch_plan(self, abs_delta, urgency):
        """创建批次计划."""
        max_bs = self.config.max_batch_size
        n_batches = max(1, math.ceil(abs_delta / max_bs))

        base = abs_delta // n_batches
        remainder = abs_delta % n_batches
        batches = []
        for i in range(n_batches):
            lots = base + (1 if i < remainder else 0)
            batches.append(BatchInfo(lots=lots))

        if urgency == "liquidation":
            for b in batches:
                b.is_immediate = True
        elif urgency == "large":
            n_immediate = max(1, n_batches // 2)
            for i in range(n_immediate):
                batches[i].is_immediate = True

        return batches

    def _fill_batch(self, batch, batch_idx, price, volume,
                    score=0.0, timeout=False):
        """标记批次成交并更新统计."""
        batch.filled = True
        batch.fill_price = price
        batch.fill_bar = self._order.bars_elapsed
        batch.fill_time = time.strftime("%H:%M:%S")
        batch.score_at_fill = score
        batch.was_timeout = timeout

        order = self._order

        old_total = order.total_filled
        new_total = old_total + batch.lots
        if new_total > 0:
            order.vwap_fill = (
                (order.vwap_fill * old_total + price * batch.lots) / new_total
            )
        order.total_filled = new_total

        if all(b.filled for b in order.batches):
            order.completed = True

        return {
            "volume": batch.lots,
            "direction": order.direction,
            "batch_idx": batch_idx,
            "score": score,
            "timeout": timeout,
            "vwap_so_far": order.vwap_fill,
        }

    def _compute_score(self, close, volume, direction):
        """多因子执行评分."""
        cfg = self.config

        # 1. VWAP偏离
        vwap = self._rolling_vwap()
        if vwap > 0:
            deviation = (close - vwap) / vwap
            if direction == "buy":
                vwap_score = max(0, min(1, -deviation * 100 + 0.5))
            else:
                vwap_score = max(0, min(1, deviation * 100 + 0.5))
        else:
            vwap_score = 0.5

        # 2. RSI
        rsi = self._compute_rsi()
        if rsi is not None:
            if direction == "buy":
                rsi_score = max(0, min(1, (cfg.rsi_buy_threshold - rsi) / 30 + 0.5))
            else:
                rsi_score = max(0, min(1, (rsi - cfg.rsi_sell_threshold) / 30 + 0.5))
        else:
            rsi_score = 0.5

        # 3. 短期动量 (均值回归)
        momentum_score = self._compute_momentum_score(direction)

        # 4. 时间衰减
        if self._order:
            elapsed = self._order.bars_elapsed
            window = cfg.execution_window_bars
            time_ratio = min(1.0, elapsed / max(1, window))
            time_score = time_ratio ** 2
        else:
            time_score = 0.0

        return (cfg.w_vwap * vwap_score
                + cfg.w_rsi * rsi_score
                + cfg.w_momentum * momentum_score
                + cfg.w_time_decay * time_score)

    def _rolling_vwap(self):
        """执行窗口内的rolling VWAP (无前瞻)."""
        if not self._cum_vp or not self._cum_vol:
            return 0.0

        idx = len(self._cum_vp) - 1
        start = max(0, self._exec_start_idx - 1)

        if start > 0:
            total_vp = self._cum_vp[idx] - self._cum_vp[start]
            total_vol = self._cum_vol[idx] - self._cum_vol[start]
        else:
            total_vp = self._cum_vp[idx]
            total_vol = self._cum_vol[idx]

        if total_vol <= 0:
            return 0.0
        return total_vp / total_vol

    def _compute_rsi(self):
        """计算当前RSI."""
        period = self.config.rsi_period
        if len(self._closes) < period + 1:
            return None

        closes = self._closes[-(period + 1):]
        deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
        gains = [max(0, d) for d in deltas]
        losses = [max(0, -d) for d in deltas]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1 + rs)

    def _compute_momentum_score(self, direction):
        """短期动量评分 (均值回归)."""
        lookback = self.config.momentum_lookback
        if len(self._closes) < lookback + 1:
            return 0.5

        recent = self._closes[-lookback:]
        mean_price = sum(recent) / len(recent)
        current = self._closes[-1]

        if mean_price <= 0:
            return 0.5

        deviation = (current - mean_price) / mean_price

        if direction == "buy":
            return max(0, min(1, -deviation * 50 + 0.5))
        else:
            return max(0, min(1, deviation * 50 + 0.5))
