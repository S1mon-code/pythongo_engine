# QExp Robust Strategies — PythonGO 移植

来源: QExp `audit-2026-04-26 / 2026-04-27` 通过**严格 ROBUST**(ex-best-year Δ < 0.20)
测试的 4 个生产策略,移植到 PythonGO 实盘部署。

## 4 个策略

| 策略 | 品种 | 周期 | 方向 | 8y Sharpe | ex-best Δ | 文件 |
|------|------|------|------|-----------|-----------|------|
| Momentum Continuation | AG | 5min | long | **+0.908** | -0.09 | `AG_Long_5M_MomentumContinuation.py` |
| Vol Squeeze Breakout v2 | AG | 5min | long | +0.470 | -0.14 | `AG_Long_5M_VolSqueezeBreakout_v2.py` |
| Pullback Strong Trend | I | 15min | long | +0.374 | -0.15 | `I_Long_15M_PullbackStrongTrend.py` |
| High Vol Breakdown | HC | 30min | short | +0.544 | -0.13 | `HC_Short_30M_HighVolBreakdown.py` |

## 信号家族

不同于现行 V8/V13(forecast + Carver vol target),QExp 系列是 **binary signal + ATR profit target**:

- 信号触发 → 直接开 `capacity` 手(默认 3)
- 出场:`hard_stop = entry × (1 ∓ 2%)` 或 `profit_target = entry ± 2 × entry_ATR`
- 无 trailing / 无 forecast / 无 Carver / 无 Chandelier exit
- 一旦开仓,持有到任一出场触发,期间不加仓不减仓

### 1. Momentum Continuation (AG 5min)

```
fires iff:
    close > open                              (上涨 bar)
    AND body > 1.5 × ATR(20)
    AND body / range >= 0.6                   (close 在 range top 80%)
    AND cooldown >= 3 bars
```

**思路**: 单根强阳线是趋势资金即将入场的方向信号,我们 front-run 1 根 bar。

### 2. Vol Squeeze Breakout v2 (AG 5min)

```
fires iff:
    z_60(log_returns) > +0.5                  (uptrend filter)
    AND cur_ATR / mean(60 hist ATR) <= 0.8    (squeeze)
    AND close > 20-bar high (excl current)    (breakout)
    AND body / range >= 0.5                   (强收盘)
    AND cooldown >= 5 bars
```

**思路**: V1 在 chop 市场频繁 false breakout,V2 加 z-trend filter 只在确认上涨时触发。

### 3. Pullback Strong Trend (I 15min)

```
fires iff:
    z_60(log_returns) > +1.0                  (强 uptrend)
    AND 20-bar rolling max 在最近 8 bar 内   (最近创新高)
    AND (rolling_max - cur_close) / atr >= 1.5 (深度回撤 1.5 ATR)
    AND cooldown >= 5 bars
```

**思路**: 强趋势中的健康回调买入,趋势确认越严越好(z>+1.0 过滤 mild trend)。

### 4. High Vol Breakdown SHORT (HC 30min)

```
fires iff (SHORT 入场):
    cur_ATR / mean(60 hist ATR) >= 1.3        (vol expansion = regime change)
    AND close < 20-bar low (excl current)     (breakdown)
    AND close < open AND body/range >= 0.5    (强阴线)
    AND cooldown >= 5 bars
```

**思路**: 不靠日线 SMA "已确认下跌",用 vol 扩张直接捕捉 regime 切换瞬间。

## 部署 (Windows 无限易)

每个策略文件可**独立部署**,自包含所有 lifecycle。共享依赖:

```
pyStrategy/
├── pythongo/                          (无限易自带)
├── modules/                           (从 src/modules/ 整个 copy)
│   ├── qexp_signals.py                ★ 必需 — 4 个信号 class
│   ├── session_guard.py
│   ├── pricing.py
│   ├── persistence.py
│   ├── risk.py (可选, 这批策略未直接调用)
│   ├── feishu.py
│   ├── slippage.py
│   ├── heartbeat.py
│   ├── order_monitor.py
│   ├── performance.py
│   ├── error_handler.py
│   ├── rollover.py
│   ├── trading_day.py
│   └── contract_info.py
└── self_strategy/
    └── {选择部署的 .py}                # AG_Long_5M_MomentumContinuation.py 等
```

### Params 面板必填字段

| 字段 | 默认 | 说明 |
|------|------|------|
| `instrument_id` | 主力合约 | 如 `ag2606` `i2609` `hc2510` |
| `max_lots` | 3 | 硬上限 (= QExp capacity) |
| `hard_stop_pct` | 2.0 | 硬止损百分比 (vs 入场均价) |
| `profit_target_atr_mult` | 2.0 | 止盈 ATR 倍数 |
| `takeover_lots` | 0 | 启动接管手数 (0=新进程, >0=接管已有持仓) |

## Session Skip Windows (开盘 15min 不发新信号)

| 策略 | 09:00-09:15 | 13:30-13:45 | 21:00-21:15 | 02:00-02:30 |
|------|:----------:|:----------:|:----------:|:----------:|
| AG (×2) | ✓ | ✓ | ✓ | ✓ (AG 夜盘 21:00-02:30) |
| I | ✓ | ✓ | ✓ | — |
| HC | ✓ | ✓ | ✓ | — |

## 与现行 V8/V13 的差异

| 特性 | V8/V13 | QExp robust |
|------|--------|-------------|
| 信号输出 | forecast (0-10) | binary fires |
| Sizing | Carver vol target + buffer + apply_buffer | 直接 max_lots |
| 出场 | Chandelier + trail_stop + 信号反转 + hard_stop | profit_target (ATR) + hard_stop |
| 加减仓 | 信号变化时加/减 | 一次性开 max_lots, 不动 |
| Forecast scaling | 是 (Vol Targeting) | 否 |
| 复杂度 | ~1500 行 | ~715 行 |

## Takeover 模式

7 个生产策略的 `takeover_lots` 模式同样适用 (4 处 patch 已完整):

```
启动时 Params 填 takeover_lots = N → 策略接管 N 手, 底仓不动
```

详见 `docs/TAKEOVER.md`。

## 验证清单 (实盘启动)

1. ✅ 无限易 Params 面板填 `instrument_id`, 留 `takeover_lots = 0`(全新进程)或 `> 0`(接管)
2. ✅ 启动后 StraLog 出现:
   - `[ON_START] 模块初始化 multiplier=X`
   - `=== 启动完成 === | XXX MN | max_lots=3`
3. ✅ 飞书收到 `**策略启动** ... (QExp robust)` 通知
4. ✅ 等历史 K 线 push 完成,首个实盘 K 线 close 后:
   - `[ON_BAR 实盘] bar#1 close=X own_pos=0 signal=idle/fires`
5. ✅ 信号触发后:
   - `[EXEC_OPEN] send_order buy/sell 3手 @ X (passive) signal_price=X atr=X`
   - `[ON_TRADE] oid=X direction='X' offset='0' price=X vol=3`
   - `[OPEN]` 或 `[OPEN SHORT]` own_pos 0→3
6. ✅ 出场触发后:
   - `[HARD_STOP][TICK]` 或 `[PROFIT_TARGET][TICK]`
   - `[EXEC_STOP] HARD_STOP/PROFIT_TARGET auto_close ...`
   - `[CLOSE]` own_pos→0

## 已知限制

- **K 线 buffer 在内存里累计**(`self._opens/highs/lows/closes` list)— 长时间运行会增长。
  M5 一年 ~12000 bar × 4 数组,内存可控但建议定期重启 (与 18:00 清算 / 21:00 重启同步即可)。
- **首 tick takeover 模式无 ATR**: 接管时进程不知道历史 ATR,首 tick 兜底 `entry_atr = 0`,
  profit_target 暂时禁用直到下一根 bar close。hard_stop 仍正常工作(基于 avg_price)。
- **HC short 跨夜 SHFE 平今 / 平昨**: `auto_close_position` 由 broker 决定 offset='1'/'3',
  策略只关心净持仓减少 = `_own_pos` 减少量 ↔ broker 总仓位减少量。

## 移植说明

信号代码来自 `~/Desktop/QExp/overlay/signals/`,**保持数学等价**:

| QExp 原文件 | PythonGO 实现 |
|------------|---------------|
| `momentum_continuation.py` | `modules/qexp_signals.py::MomentumContinuationSignal` |
| `vol_squeeze_breakout_long_v2.py` | `modules/qexp_signals.py::VolSqueezeBreakoutLongV2Signal` |
| `pullback_strong_trend.py` | `modules/qexp_signals.py::PullbackStrongTrendSignal` |
| `high_vol_breakdown_short.py` | `modules/qexp_signals.py::HighVolBreakdownShortSignal` |

QExp 用 `bars._high_raw[:end]` 数组切片 + `Triggers(buy_levels, sell_levels)`;
PythonGO 用 `np.asarray(self._highs, dtype=float)` + `SignalResult(fires, entry_price, atr, metadata)`。
信号触发条件、cooldown、ATR 计算公式完全一致。

执行差异:
- QExp: `buy_levels = (close,)` → AlphaForge 在下一 bar open 成交
- PythonGO: 当前 bar close 时直接 `send_order`(passive 限价穿盘口)+ AggressivePricer

实际成交价会比 AlphaForge 回测的"下一 bar open"略有偏离,但策略本质是 momentum/breakout,
1 根 bar 内偏差对 8y Sharpe 影响有限。具体偏差需实盘统计 N 周后对比。
