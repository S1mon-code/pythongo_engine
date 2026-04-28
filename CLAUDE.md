# PythonGO Engine

## 概述

将 QBase / QExp / ICT 等 research 项目中回测验证过的量化策略,移植成 PythonGO 格式的 .py 文件,部署到 Windows 无限易客户端进行期货实盘交易. 每个策略对应一个独立的 .py 文件,搭配共享的 modules 模块层提供风控、执行、监控等基础设施.

**当前部署 12 个生产策略** (3 大家族, 验证逐步推进):

| 家族 | 数量 | 周期 | 信号风格 | 部署状态 |
|------|------|------|---------|---------|
| V8 (Donchian + ADX) | 3 (AL/CU/HC long) | 1H | forecast + Carver vol target | ✅ 半年+ 实盘 |
| V13 (Donchian + MFI) | 4 (AG/JM/P/PP long) | 1H | 同 V8 | ✅ 半年+ 实盘 |
| QExp robust | 4 (AG_Mom / AG_VSv2 / I_Pull / HC_S) | 5/15/30min | binary fires + ATR profit target | ⏸ 2026-04-28 上线 |
| ICT v6 | 1 (I 铁矿 bidirectional) | 1m + D1 bias | state machine + R-ladder + chandelier | ⏸ 2026-04-28 上线 |

## 技术栈

- **语言**: Python（PythonGO运行时，无限易内置）
- **依赖**: 仅numpy + requests + pythongo内置模块（不用talib）
- **指标**: 纯numpy手写，从QBase原样移植（避免信号偏差）
- **通知**: 飞书Webhook（非阻塞推送）
- **格式化**: ruff
- **PythonGO 版本**: 2025.0925.1420

## 项目结构

```
src/
├── modules/                          # ★ 共享 modules 层 (部署到 pyStrategy/modules/)
│   │                                   14 个 active modules + 4 个 legacy 留作 tests 引用
│   ├── contract_info.py               88 品种规格 (乘数 / tick / sessions 含茶歇)
│   ├── session_guard.py               交易时段 + open_grace_sec=30
│   ├── pricing.py                     AggressivePricer 穿盘口 limit (替代 market=True)
│   ├── order_monitor.py               订单超时 urgency escalator
│   ├── slippage.py                    [滑点] N tick 记录
│   ├── heartbeat.py                   每 tick UI 心跳
│   ├── persistence.py                 state.json 跨日恢复
│   ├── feishu.py                      飞书 webhook
│   ├── error_handler.py               throttle_on_error (0004 流控)
│   ├── trading_day.py                 21:00 trading day 边界
│   ├── rollover.py                    换月提醒
│   ├── performance.py                 R-multiple / 每日盈亏统计
│   ├── risk.py                        tick 级硬止损 + M1 trail (V8/V13 用)
│   ├── position_sizing.py             Carver vol target + buffer (V8/V13 用)
│   ├── qexp_signals.py                ★ QExp 4 个信号 helper
│   ├── eal.py / execution.py /        legacy (V8 已砍 ScaledEntryExecutor,
│   │ rolling_vwap.py / twap.py        留作 tests/test_execution.py 等引用)
│
├── AL/long/                          # V8 生产 (Donchian + ADX, 1H, long)
├── CU/long/                          # V8 生产
├── HC/long/                          # V8 生产
├── AG/long/                          # V13 生产 (Donchian + MFI, 1H, long)
├── JM/long/                          # V13 生产
├── P/long/                           # V13 生产
├── PP/long/                          # V13 生产
│
├── qexp_robust/                      # ★ QExp 4 个 robust 策略 (2026-04-28 上线)
│   ├── AG_Long_5M_MomentumContinuation.py
│   ├── AG_Long_5M_VolSqueezeBreakout_v2.py
│   ├── I_Long_15M_PullbackStrongTrend.py
│   ├── HC_Short_30M_HighVolBreakdown.py
│   └── README.md
│
├── ICT/                              # ★ ICT v6 (2022 Model state machine, 2026-04-28 上线)
│   ├── modules/                       ICT 子包 (timezones / sessions_cn / structures /
│   │                                            bias / state_machine)
│   ├── I_Bidir_M1_ICT_v6.py           铁矿双向 1m 策略 Phase 1 reference
│   └── README.md
│
├── DailyReporter.py                  # 全账户每日汇总飞书推送
│
├── test/                             # 4 个 active 集成测试策略
│   ├── TestFullModule.py              全模块协同测试 (MA 双均线 + executor)
│   ├── TestScaledEntry.py             ScaledEntryExecutor 三模式验证 (legacy 用)
│   ├── TestAllFixes.py                2026-04-20 全部 8 项修复的 smoke test
│   └── AL_Long_M1_Test_Oscillator.py  V2 标准模板测试 (M1 振子 12-bar 周期)
│
└── archive/                          # ★ 归档 — legacy 策略 (历史参考)
    └── legacy_strategies/
        ├── I/                         旧 I 品种 V7/V20/V26/V29/Portfolio
        └── templates/                 旧 Template_Long/Short.py (V2 已替代)

tests/                                # pytest 单元测试 (302 个 active)
├── test_session_guard.py              SessionGuard 边界 / 茶歇 / open_grace
├── test_risk.py                       tick/M1 止损 + on_day_change
├── test_pricing.py                    urgency + spread adaptation
├── test_order_monitor.py              escalation schedule
├── test_error_handler.py              0004 流控 + 恢复 + 并发
├── test_execution.py                  ★ legacy executor 测试 (引用 modules/execution.py)
├── test_rolling_vwap.py               ★ legacy VWAP 测试 (引用 modules/rolling_vwap.py)
├── test_pyramid_clamp.py              加仓 max_lots clamp
├── test_lookahead_guard.py            前视检查
├── test_qexp_signals.py               24 个 QExp 信号 + 4 strategy syntax
├── test_daily_report.py               37 个 daily_report 工具测试
└── test_ict.py                        37 个 ICT 单元 + bug-fix regression

docs/
├── ARCHITECTURE.md                   # ★ 核心 — 架构与开发规范
├── MODULE_REFERENCE.md               # ★ 核心 — 模块 API 手册
├── STRATEGY_STANDARD_V2.md           # ★ 核心 — 策略模版 V2 标准 (2026-04-24)
├── TAKEOVER.md                       # ★ 核心 — 启动接管模式
├── pythongo/                         # ★ PythonGO V2 源码审计 (8 md, 3135 行)
└── archive/                          # ★ 归档 — 历史 session 快照
    ├── SESSION_2026_04_17.md
    ├── SESSION_2026_04_20.md
    ├── OPERATIONAL_ISSUES_RESEARCH.md
    └── RESEARCH_REPORT.md

tools/daily_report/                   # ★ 每日实盘日报 (HTML + PDF)
                                       #   broker CSV 主源 (盈亏/手续费/撤单 真值)
                                       #   StraLog 辅源 (信号/风控/原因 上下文)
                                       #   隔夜接管孤儿平仓自动识别 (TAKEOVER 标记)
                                       #   image_map.json 支持 hash 命名截图

reports/                              # 每日 data + HTML/PDF 报告

pythongo/                             # PythonGO V2 SDK 源码 (.pyd gitignore)
```

## 部署

### 目录映射（Windows无限易）

```
pyStrategy/
  pythongo/           ← 框架（无限易自带）
  modules/            ← src/modules/ 整个拷贝过去
  self_strategy/
    I_Short_Portfolio_V26_V29.py  ← 策略文件
    DailyReporter.py              ← 监控文件
    TestAllFixes.py               ← smoke test (推荐先跑)
```

**关键**: modules/ 放在 `pyStrategy/modules/`（与pythongo/同级），不是 self_strategy/ 下。
**上线前必做**: 清空 `pyStrategy/state/` 目录 (避免旧 crash recovery 状态污染)。

## 策略命名规范

```
{品种}_{方向}_{频率}_{版本}_{指标简称}.py      # 单策略
{品种}_{方向}_Portfolio_{版本1}_{版本2}.py      # 组合策略
```

示例: `I_Short_1H_V26_OI_Flow_MACD.py`, `I_Short_Portfolio_V26_V29.py`

**类名 = 文件名**（PythonGO硬性要求）

## 开发约定

### 核心架构模式

- **Same-bar执行 (2026-04-14)**: 信号在当前bar产生后立即提交TWAP/executor，不等下一根bar
- **非交易时段门控 (2026-04-17)**: 所有挂单/撤单必须在交易时段内。`_on_bar` 入口、`_on_tick_*`、`_submit_vwap/twap`、`_execute` 全部基于 `self._guard.should_trade()` 做门控。pre-opening(如SHFE 08:55-09:00集合竞价)发单/撤单会被拒绝(`errCode 0004`)。**`_on_bar`顶部的cancel_order块必须在session gate之后执行**
- **每bar开头撤挂单**: `for oid in list(self.order_id): self.cancel_order(oid)` — 位于session gate之后;新架构下必须同步 `self._entry.register_cancelled(oid)` (2026-04-17 audit v2)
- **21:00 Day Start**: 所有模块统一以21:00作为交易日切换点(夜盘开始)。`RiskManager.on_day_change(balance, position_profit)` 剔除过夜浮盈 (2026-04-20 修复)
- **双时间框架**: Portfolio策略同时运行两个KLineGenerator(如H1+H4)
- **限价单强制**: 所有 `send_order` / `auto_close_position` **不传 `market=True`** (2026-04-20)，依赖 `_aggressive_price(price, direction, urgency)` 穿盘口定价

### 止损架构 (2026-04-17 重构)

**Tick 级硬止损 + M1 级移动止损** (替代旧的 bar 级):
- Peak/trough 每 tick 更新 (真实极值)
- 硬止损每 tick 判断, 破即触发, 无确认延时
- 移动止损 peak/trough tick 级追踪 + 分钟门控判断 (降噪)
- 触发时策略调 `_exec_stop_at_tick()` + `self._entry.on_stop_triggered()` 双路径
- 平仓/止损时写入 `_last_exit_bar_ts` 用于 re-entry guard (2026-04-20)

优先级: 止损 > 止盈 > 出场信号 > 加仓 > 建仓

### 入场架构 (2026-04-17 Phase 4)

**ScaledEntryExecutor 状态机** (替代旧的 VWAP 门控批次):

```
IDLE → BOTTOM → OPPORTUNISTIC → FORCE → COMPLETE → IDLE
任意态 → LOCKED (止损) → IDLE (新 bar on_signal)
```

5 个阶段:
- **IDLE**: 无活动
- **BOTTOM**: bar 开始 1-2 min 挂 bid1 底仓 (bottom_lots, 默认 2 手)
- **OPPORTUNISTIC**: T=5-30min, price<VWAP 机会驱动挂 1 手, 每 10s 一个, 最多 3 并发
- **FORCE**: T>30min, 每 5min slot (前 2min peg, 后 3min 穿盘口)
- **COMPLETE**: filled>=target, 清 pending 回 IDLE
- **LOCKED**: 止损后本 bar 放弃剩余

**动态 Urgency 评分 (0-1)**:
- 时间压力 40% + 持仓缺口 30% + 信号强度 15% + 价格机会 15%
- urgency × 10 = 穿盘口 tick 数 (peg=0, cross=2, urgent=5, critical=10)

**超目标加仓**: `price < VWAP × 0.995 AND forecast > 5` → `target += target × 20%` (一次性触发)

**Peg-to-bid1**: 每个 pending 订单记录 submit_price, bid1 漂移 >= threshold 自动 cancel+重挂

**Crash Recovery (2026-04-20)**: `get_state()` / `load_state()` / `force_lock()`。进程崩溃重启后,若检测到 pending_orders 非空 → force_lock + oid 加回 self.order_id + 飞书 warning,下一 bar cancel sweep 清理遗留。

详见 `src/modules/execution.py` 和 `tests/test_execution.py` (67 用例)。

### PythonGO API 踩坑 (已源码审计对齐 2025.0925.1420)

- **合约代码必须小写**:`al2607` / `i2509` / `cu2506` — **不是** `AL2607`。大写订阅无声失败(无 tick 进来,on_tick 永不触发),本地 log 只到 `策略初始化完毕` 就卡住 (2026-04-20 实盘确认)
- **`_save()` 必须 null-guard**:`on_start` 中途失败(如合约代码错)时,`self._risk = RiskManager(...)` 可能没跑到 → `self._risk` 保持 None → `on_stop` 调 `_save()` → `NoneType.get_state()` AttributeError。所有策略的 `_save` 用 `state.update(self._risk.get_state() if self._risk is not None else {})` 保护 (2026-04-20 实盘 log L167-208 发现并修复,31 文件)
- **商品期货早盘有 10:15-10:30 茶歇**:所有 SHFE / DCE / CZCE / GFEX / INE 商品品种早盘是 `09:00-10:15 + 10:30-11:30`(**不是连续的 09:00-11:30**)。茶歇期间 broker 拒单。`contract_info.py` sessions 已按此拆分,**CFFEX 股指/国债 09:30-11:30 连续无茶歇**保持不变 (2026-04-20 Simon 指出)
- **区间约定 `[start, end)`**:`SessionGuard._in_session()` 用 `start <= cur < end`,end 边界排除。好处:相邻 session(如早盘上段 10:15 = 茶歇 10:15)边界不重叠,10:15 整分属于茶歇,正确返 False。
- **`Field(title=...)` 不能含特殊字符**(逗号/等号/大于号/括号):`pythongo/base.py::__package_params` 把 title 当 key 反查 `model_fields`,含 `,/=/>/( )` 时被截断 → `KeyError`。例:`title="启动接管手数(0=按state恢复, >0=手动接管)"` 启动崩溃,简化为 `title="启动接管手数"` 后正常,详细说明只放代码注释 (2026-04-27 takeover 推广实盘发现 + 7 文件修复)
- `get_account_fund_data("")` 会崩溃，必须先 `get_investor_data(1)` 拿investor_id
- `self.output()` 替代 `print()`
- **禁止** `market=True` 市价单 — SHFE 等市场拒单(2026-04-20 全队移除 25 处)
- KLineProducer没有open_interest，需在callback中从KLineData手动收集
- 只提供tick数据，K线全靠KLineGenerator从tick合成
- **`trade.direction` 是 `TypeOrderDirection = Literal["0", "1"]`** (源码确认,不是中文)。识别必须用健壮模式 `("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell")`
- **`order.volume` 不存在** — OrderData 只有 `total_volume / traded_volume / cancel_volume`。撤单回调用 `order.cancel_volume` (2026-04-20 全队修复 26 处)
- `get_position(instrument_id)` **永远不返 None**,返回空 `Position()` 对象(`net_position = 0`)
- `make_order_req` 在 `self.trading=False` 时静默 return None
- `on_order` 根据**中文 status** 分发 `on_order_cancel / on_order_trade` — override 时必须调 `super().on_order(order)`
- `on_error` 默认实现自带 0004 流控,override 必须调 `throttle_on_error(self, error)` 保留流控功能

### 实盘每日生命周期(无限易 18:00 清算)

Simon 的无限易客户端每日 18:00-18:30 清算,强制退出所有策略进程。21:00 前必须手动重开。
AL V8 (+ 启用了 startup eval 的策略) 全周期覆盖:

```
17:00  持仓 2 手 long, forecast=8, 正常运行
         _save() 持久化 state.json (含 executor state + last_exit_bar_ts)

18:00  无限易清算 → strategy 进程退出
         持仓保留到 broker (INFINIGO 也重启)
         state.json 最后一次写入保留

21:00  Simon 手动重开策略
         on_start 流程:
         1. load_state() 恢复 avg_price / peak_price / _last_exit_bar_ts
         2. RiskManager.on_day_change(balance, position_profit)
            → 剔除过夜浮盈, 基线正确 ✓
         3. executor.load_state() (若有 pending_orders → force_lock)
         4. get_position() 从 broker 查当前持仓
         5. super().on_start() → trading=True
         6. _needs_startup_eval = True

21:00:30  首个实盘 tick 进来
          _pricer.update(tick) → last/bid1/ask1 就绪
          _rvwap.update(...) → VWAP 累积
          _evaluate_startup() 触发:
          ├─ 读 producer 历史 + 实盘价 re-check signal
          ├─ 算 forecast / target (含 apply_buffer)
          ├─ 4 分支决策:
          │   • target > net_pos → 开/加仓 (走 executor 完整状态机)
          │   • target = 0 且 net_pos > 0 → 强制平仓 (auto_close_position urgent)
          │   • target < net_pos 且 > 0 → 减仓 (auto_close_position normal)
          │   • target = net_pos → 持仓一致, 无动作
          └─ 飞书推送 "[启动评估 开/平/减/持]"

21:00:35+ 正常实盘循环
```

**关键**:4 分支保证隔夜任何信号变化都在 1 个 tick 内(~5 秒)响应,不会等下一根 bar close。

### 模块导入模板 (2026-04-20 更新)

每个策略文件的模块导入保持一致(与 TestFullModule / TestScaledEntry / TestAllFixes 对齐):

```python
from modules.contract_info import get_multiplier, get_tick_size
from modules.session_guard import SessionGuard
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day, is_new_day, DAY_START_HOUR
from modules.risk import check_stops, RiskManager
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer
# Phase 3 / Phase 4:
from modules.pricing import AggressivePricer
from modules.rolling_vwap import RollingVWAP
from modules.execution import EntryParams, EntryState, ExecAction, ScaledEntryExecutor
# 2026-04-20:
from modules.error_handler import throttle_on_error
```

### Scaled Entry 集成 checklist (24 项, 2026-04-20 更新)

新策略若要接入 ScaledEntryExecutor, 必须实现:

1. `__init__`: `self._rvwap: RollingVWAP | None = None`
2. `__init__`: `self._entry: ScaledEntryExecutor | None = None`
3. `__init__`: `self._unknown_oid_count = 0`
4. `__init__`: `self._needs_startup_eval = True` 和 `self._last_exit_bar_ts: int = 0` (2026-04-20)
5. `on_start`: 创建 `RollingVWAP(window_seconds=1800)` + `ScaledEntryExecutor(EntryParams(...))`
6. `on_start` load_state: 恢复 `_last_exit_bar_ts` + executor state + crash recovery (force_lock + oid 回填)
7. `on_start`: 调 `self._risk.on_day_change(acct.balance, acct.position_profit)` 传 position_profit (2026-04-20)
8. `on_tick`: 喂 pricer + rvwap + 驱动 `_drive_entry`
9. `on_tick`: 挂钩 `_evaluate_startup()` (首 tick 就绪时一次性触发) (2026-04-20)
10. `_drive_entry`: 调 `executor.on_tick()` + apply actions + 更新 `entry_progress`
11. `_apply_entry_action`: 处理 submit / cancel / cancel_all / feishu 四种 op
12. `_apply_entry_action` submit 分支: `register_pending(oid, vol, price=a.price)` 带价位
13. `_on_bar` OPEN/ADD 路径: 调 `executor.on_signal()` 并消费返回 actions
14. `_on_bar` OPEN/ADD 路径: 加保证金预检 (audit v2)
15. `_on_bar` 开头 cancel_order 后同步 `self._entry.register_cancelled(oid)`
16. `_on_tick_stops` 触发后调 `executor.on_stop_triggered()` 并消费 actions
17. `_exec_stop_at_tick` 等止损路径: cancel_order 同步 `register_cancelled`,并写入 `_last_exit_bar_ts`
18. `on_trade`: `claimed_by_entry = self._entry.on_trade(...)` 判 claim
19. `on_trade`: DIAG 打印 `[BUG_B_DIAG] direction={trade.direction!r}` + 健壮识别 (2026-04-20)
20. `on_trade`: 未 claim 也不属 VWAP 的 oid 计入 `_unknown_oid_count` + warning
21. `on_order_cancel`: 用 `order.cancel_volume` (不是 `.volume`) (2026-04-20)
22. **`on_error`: 调 `throttle_on_error(self, error)` 启用 0004 流控** (2026-04-20)
23. `_freq_to_sec`: 防御性处理 str / enum / "Enum.H1" 形式
24. state_map 暴露 `entry_progress` 给 UI
25. `_save`: 序列化 `executor state` + `last_exit_bar_ts` + `peak_price` + `self._risk.get_state()`

参考实现: `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` (完整) 或
`src/test/TestScaledEntry.py` (executor 三模式) 或
`src/test/TestAllFixes.py` (所有 2026-04-20 修复的 smoke test)。

## 关键文件

| 文件 | 用途 |
|------|------|
| `docs/ARCHITECTURE.md` | 核心 — 架构规范 + API 踩坑总结 |
| `docs/MODULE_REFERENCE.md` | 核心 — 模块 API 手册 + 部署说明 |
| `docs/STRATEGY_STANDARD_V2.md` | ★ 核心 — 策略模版 V2 标准 (2026-04-24) |
| `docs/TAKEOVER.md` | ★ 核心 — 启动接管模式 (跨日生命周期) |
| `docs/pythongo/` | ★ PythonGO V2 源码审计 (8 md, 3135 行) |
| `docs/archive/` | 归档 — 历史 session / research 快照 |
| `src/modules/contract_info.py` | 88 品种规格 (含茶歇 sessions) |
| `src/modules/session_guard.py` | 时段守卫 + open_grace_sec=30 |
| `src/modules/pricing.py` | AggressivePricer 穿盘口 limit |
| `src/modules/risk.py` | tick 级硬止损 + M1 trail + on_day_change |
| `src/modules/error_handler.py` | throttle_on_error 0004 流控 |
| `src/modules/qexp_signals.py` | ★ QExp 4 个信号 helper |
| `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` | V8 reference (Donchian + ADX) |
| `src/AG/long/AG_Long_1H_V13_Donchian_MFI.py` | V13 reference (Donchian + MFI) |
| `src/qexp_robust/` | ★ QExp 4 robust 策略 + README |
| `src/ICT/` | ★ ICT v6 (含子 modules) + I_Bidir_M1_ICT_v6.py + README |
| `tools/daily_report/` | 每日实盘日报 (broker CSV 主源 + StraLog 辅源, HTML + PDF) |
| `src/test/TestFullModule.py` | 全模块集成测试模板 |
| `src/test/TestScaledEntry.py` | ★ ScaledEntryExecutor 三模式验证 |
| `src/test/TestAllFixes.py` | ★ 2026-04-20 修复 smoke test (高频信号) |
| `src/DailyReporter.py` | 全账户监控 |
| `tests/test_*.py` | ★ 154 个 pytest 单元测试 (6 文件) |
| **`docs/STRATEGY_STANDARD_V2.md`** | **★ 新策略模版标准 (2026-04-24 确立, 所有新策略必须按此写)** |

## 新策略模版标准 V2 (2026-04-24 — 所有新策略必须遵循)

**详细规范见 [`docs/STRATEGY_STANDARD_V2.md`](docs/STRATEGY_STANDARD_V2.md)**

### 核心要素 (速查)

1. **自管持仓** — `self._own_pos` + `self._my_oids` 过滤, `on_trade` 入口拦截非本策略成交
2. **Max 5 手** — 5 道硬保险 (常量 / Params 默认 / on_start 拉回 / target 截断 / on_trade 截断)
3. **UI 全量** — state_map 40+ 字段 (含 heartbeat / last_price / bid1/ask1 / spread_tick / 各指标值)
4. **主/副图** — 主图 4 条价格线 (Donchian + Chandelier), 副图 3 条振子线
5. **双写日志** — `_log(msg)` = `self.output()` + `print(flush=True)`, 实时落盘
6. **16 个日志 tag 全覆盖** (除 `[TICK #N]` 不打)
7. **周期 UI 刷新** — 每 10 tick 调 `update_status_bar()`
8. **Type-safe widget push** — NaN→0 兜底, 首次推送日志, widget=None 告警

### 7 个参考实现 (已部署, 新标准)

**V8 家族 (Donchian + ADX):** `AL / CU / HC` (SHFE有色)
**V13 家族 (Donchian + MFI):** `AG` (SHFE贵金属) + `P / PP / JM` (DCE)

所有 7 个都在 `src/{品种}/long/` 下,命名 `{品种}_Long_1H_V{N}_{指标}.py`

### 测试模版

- `src/test/AL_Long_M1_Test_Oscillator.py` — M1 振子 (12-bar 周期), 覆盖 OPEN/ADD/REDUCE/CLOSE 全路径

### 新策略 Checklist (抄文档 §9)

写完后 `python -c "import ast; ast.parse(open('file.py').read())"` 语法通过 +
5 道 max_lots 保险齐全 + 14 个模块全导入 + 16 个日志 tag 全有 + 类名 = 文件名

### ANNUAL_FACTOR 对照

| 交易所分类 | H1 bars/day | ANNUAL_FACTOR |
|---|---|---|
| SHFE 有色/黑色 (夜盘01:00) | 8 | `252 × 8` |
| SHFE 贵金属 (夜盘02:30) | 10 | `252 × 10` |
| DCE/CZCE (夜盘23:00) | 6 | `252 × 6` |
| CFFEX 无夜盘 | 5-6 | `252 × 5` |
| GFEX 无夜盘 | 4 | `252 × 4` |

## 迭代历史

### 2026-04-28 (晚) — daily_report 改造为 broker CSV 主源

**痛点**: 旧 log 流程下,JM 21:16 的 trail_stop 隔夜接管平仓被 `pair_trades` 完全忽略
(没今日开仓配对),日报里 0 字提及 JM。手续费没有,撤单也漏。

**新方案**: 无限易客户端"实时回报"导出的 GBK CSV 成为日报真值源:
- 41 列结构化, 每行一笔订单 (含撤单)
- 手续费精确到分 (broker 报回, e.g. PP 4.05 / HC 13.62)
- 平仓盈亏 = broker 逐日盯市真值 (含隔夜价差)
- 平仓盈亏 (逐笔) = 按本笔开仓价精确算
- StraLog 仍保留, 只用作决策上下文 (POS_DECISION / EXECUTE / IND / SIGNAL / TRAIL_STOP)

**改造范围**:
- 新增 `tools/daily_report/csv_parser.py` (220 行): GBK + 41 列 + HH:MM:SS+文件名推
  calendar (broker"成交时间(日)"列因不同交易所标法不一致, 不可靠) + 21:00+ 时间归前一日历日
- 扩展 `TradeLeg`: 加 `fee` / `broker_pnl` / `per_trade_pnl` / `full_name` / `is_takeover` 字段
- 扩展 `RoundTrip`: 加 `is_takeover` 标记
- 新增 `pair_from_csv()`: 按 instrument_id FIFO + 没找到对应开仓 → 标 TAKEOVER + log_events
  注入决策上下文
- `generate_report.py` 加 CSV 优先逻辑 (扫"实时回报/信息导出"*.csv) + `image_map.json`
  支持 (hash 命名截图映射)
- `render_html.py` 新增"当日撤单"段 + 主表加 broker盈亏/逐笔盈亏/手续费 列 + 隔夜接管橘色标签

**4-28 实证**: broker 平仓盈亏 +1695, 手续费 88.30, 净盈亏 +1606.70.
8 round-trips (含 2 takeover: JM/PP 21:16/21:20) + 3 撤单 (10:00 PP / 14:15 P / 22:00 P,
全是 OrderMonitor escalator 触发) + 3 张盘面图 (image_map.json 接 hash 命名).

302 pytest 全绿.

### 2026-04-28 — QExp + ICT 上线 + 项目结构清理

**12 个生产策略部署完成** (3 大家族):
- V8 (Donchian+ADX): AL/CU/HC long
- V13 (Donchian+MFI): AG/JM/P/PP long
- QExp robust (4 个 audit 通过): AG_Mom (5min) / AG_VSv2 (5min) / I_Pull (15min) / HC_S (30min)
- ICT v6 (2022 Model state machine): I 铁矿 1m + D1 bias bidirectional Phase 1

**takeover_lots 模式全推广** (V8/V13 7 个策略 + QExp/ICT 全适配): 4 处 patch
(Params 字段 + __init__ flag + on_start override + 首 tick 兜底).
解决 18:00 强制清算 → 21:00 重启时承接下午仓位的痛点.

**daily_report 工具** (tools/daily_report/): 每日实盘 HTML + PDF 报告.
strategy_aliases.json 路由识别 11 个生产策略, 同时支持 V8/V13 forecast
风格 + QExp binary fires 风格 (PivotSpec 加 profit_target_formula 字段).
滑点直接用 log [滑点] N.N ticks (策略 SlippageTracker 算的真滑点),
flip 符号让正=有利与金额方向一致.
**注**: 04-28 晚改造为 CSV 主源 (上面新条目), 这里描述的是初版 log 流程, 现作 fallback.

**ICT v6 移植** (Phase 1): 自审 + code-reviewer 审, 修了 8 个 critical
bugs (limit 单时机 / push_history double append / confirm_open 拆批 /
on_trade _own_pos 拆批不累加 / dead code / datetime 类型不确定 /
history 充足检查 / _sm None guard).

**Field title 不能含特殊字符** 踩坑: Field(title="...") 含逗号/等号/
大于号/括号会让 pythongo/base.py:84 __package_params 抛 KeyError.
约定: title 必须简洁中文, 详细说明只放代码注释.

**项目结构清理**:
- 删 9 个零引用文件 (modules/daily_reporter.py / test/TestBarebone.py /
  CU_Short_*_TEST × 5 / I_Short_*_TEST × 2)
- 归档到 src/archive/legacy_strategies/: 旧 I 品种 6 个策略 + Template_Long/Short
- 归档到 docs/archive/: SESSION_2026_04_17/04_20 + OPERATIONAL/RESEARCH
- legacy modules (eal/execution/rolling_vwap/twap) 留在 modules/ 不动
  (被 tests/test_execution.py 等引用, 删了会破 tests)
- legacy V8/V15/V20/V7/Portfolio/CU short/IH/LC short Simon 已 git rm

测试: 302 pytest 全绿.

### 2026-04-24 — 新标准模版确立 (V2) + 7 品种部署

**新标准核心 (详见 `docs/STRATEGY_STANDARD_V2.md`):**
- 自管持仓 — my_oids 过滤 + own_pos 决策, broker 其他仓位完全不碰
- Max 5 手硬上限 — 5 道保险 (常量/Params/on_start/target/on_trade)
- UI 全量展示 — 40+ state_map 字段, 实时 tick 级刷新, heartbeat 肉眼观察
- 双写日志 `_log()` — output() + print(flush=True), 实时落盘
- 16 个日志 tag 覆盖全决策路径
- Type-safe widget push (NaN→0, 首次推送日志)

**7 个生产策略部署** (2 个信号家族):
- **V8 家族 (Donchian + ADX):** AL (al2607) / CU (cu2606) / HC (hc2510) — SHFE 有色/黑色
- **V13 家族 (Donchian + MFI):** AG (ag2606, SHFE贵金属) / P (p2609) / PP (pp2606) / JM (jm2605) — DCE

**架构简化** (V8 对比旧版):
- 砍掉 ScaledEntryExecutor (executor 状态机)
- 砍掉 VWAP executor (分批执行)
- 砍掉 startup_eval (不再接管 broker 历史仓位)
- 砍掉 crash recovery 复杂逻辑
- 直接 `send_order` / `auto_close_position` + AggressivePricer 穿盘口 + OrderMonitor escalator

**测试模版** (已实盘验证 OK):
- `src/test/AL_Long_M1_Test_Oscillator.py` — M1 振子, 12-bar 周期触发全路径
- 1500+ UI push 成功, Oscillator OPEN→ADD×4→NO_ACT→REDUCE×4→CLOSE 全跑通
- 验证 `on_trade` 过滤正确 (broker_pos=own_pos+1, 预有的 1 手 broker 仓不被策略碰)

**18 项上线前检查全通过** (见 `docs/STRATEGY_STANDARD_V2.md` §9)

### 2026-04-20 — Fleet-wide 修复 + 文档完整化 + 跨日生命周期

**PythonGO V2 源码审计** (morning)
- 读完 28 个源文件 (除 core.pyd 二进制), 生成 `docs/pythongo/` 8 md 共 3135 行完整对照文档

**Fleet-wide bug 修复** (midday)
- **Bug A**: `order.volume` 不存在 — 26 处修复 in 24 文件 (sed 批量)
- **Bug B**: `trade.direction` 是 `"0"/"1"` 不是中文 — 37 处健壮识别 in 30 文件 + AL V8 DIAG
- **market=True → market=False**: 25 处 in 9 文件
- **0004 流控**: 新增 `modules/error_handler.py` + 32 策略文件接入 `throttle_on_error`

**AL V8 生产级增强** (afternoon)
- **crash recovery**: executor `get_state/load_state/force_lock` + on_start 遗留订单处理
- **startup eval 4 分支决策** (v2, evening):
  - `target > net_pos` → 开/加仓 (executor)
  - `target == 0 + net_pos > 0` → **强制平仓** (覆盖隔夜信号消失)
  - `target < net_pos + target > 0` → **减仓** (覆盖隔夜信号减弱)
  - `target == net_pos` → 持仓一致,无动作
- **re-entry guard**: `_last_exit_bar_ts` 防同 bar 重入场
- **overnight fix**: `on_day_change(balance, position_profit)` 剔除过夜浮盈

**A+ Overnight fix 全队 propagate** (evening)
- 58 处 in 17 策略 + test files: `on_day_change(balance)` → `on_day_change(balance, position_profit)`
- 所有隔夜策略 daily_stop 基线正确(其他 3 项增强仍 AL V8 独家)

**实盘验证** (11:23 on 2026-04-20)
- ✅ Bug B direction 实盘证据:`direction='0'` (买) / `direction='1'` (卖),源码文档完全正确
- ✅ market=False 全部报单"是否市价=否"
- ✅ executor BOTTOM → OPP 状态转换流畅
- ✅ SHFE offset=3 平今自动处理
- 🐛 **踩坑 1**:合约代码必须小写 (`al2607` 不是 `AL2607`),大写订阅静默失败

**第二次实盘 + 暂停恢复测试** (13:43-13:49 on 2026-04-20)
- ✅ 暂停 → 重开,`bar_count` 从 20 → 21 续接,`last_exit_bar_ts` 跨重启保留
- ✅ overnight fix `[DAY_CHANGE_TEST]` 双参数调用正常
- ✅ 暂停期间 broker 延迟成交,on_trade 正确处理
- 🐛 **踩坑 2**:`_save()` 在 `_risk=None` 时 AttributeError(on_start 中途失败场景)→ 31 文件加 null-guard

**Session 边界修复** (Simon 指出 10:15-10:30 茶歇 + 开盘 grace)
- 🐛 **踩坑 3**:`contract_info.py` 70 个非 CFFEX 品种早盘都是连续 `((9, 0), (11, 30))`,漏了 **10:15-10:30 茶歇**。茶歇期间 broker 拒单但策略以为在 session → errCode 0004 刷屏
- 修:批量拆成 `((9, 0), (10, 15)), ((10, 30), (11, 30))`(81 处替换)
- 同时修 `SessionGuard._in_session` 边界:`start <= cur <= end` → `start <= cur < end`(`[start, end)` 区间),与 `contract_info._is_time_in_session` 对齐
- **37 新 pytest** 覆盖茶歇/夜盘跨午夜/CFFEX 无茶歇/sim_24h/flatten_zone

**开盘后 grace 保护** (Simon 指出"开盘后再挂单")
- 新增 `SessionGuard(open_grace_sec=30)` 参数:session 开始后 30 秒内 `should_trade()` 返 False
- 避开 broker 开盘瞬间 rush 导致的 errCode 0004 / 系统繁忙拒单
- 新增 `seconds_since_session_start()` helper(策略层可查距 session 开始多少秒)
- 适用所有 session 起点:09:00 / 10:30(茶歇后)/ 13:30 / 21:00
- **32 个策略全队 SessionGuard 实例化加 `open_grace_sec=30`**(默认 0 向后兼容)
- **9 新 pytest** 覆盖 grace 边界 / 茶歇后 grace / 午盘 grace / 跨午夜 session 中段不触发 grace

**完整品种表**(88 个品种,2026-04-20 Simon 确认):
- CFFEX 8 个(if/ih/ic/im/ts/tf/t/tl):连续 09:30-11:30 + 13:00-15:00/15:15,无茶歇无夜盘
- INE 5 个(sc/nr/lu/bc/ec):常规早盘茶歇 + 夜盘(sc 到 02:30, bc 到 01:00, nr/lu 到 23:00, ec 无夜盘)
- SHFE 20 个:常规早盘茶歇 + 夜盘(有色 cu/al/zn/pb/ni/sn/ss/ao/ad 到 01:00, 贵金属 au/ag 到 02:30, 黑色 rb/hc/fu/bu/ru/br/sp/op 到 23:00, wr 无夜盘)
- DCE 23 个:常规早盘茶歇 + 夜盘(17 个到 23:00, rr/lg/jd/lh/bb/fb 无夜盘)
- CZCE 27 个:常规早盘茶歇 + 夜盘(17 个到 23:00, sm/sf/ap/cj/wh/pm/ri/lr/jr/rs 无夜盘)
- GFEX 5 个(si/lc/ps/pt/pd):常规早盘茶歇,全部无夜盘

**测试 & 文档**
- 新增 `TestAllFixes.py`: 817 行 smoke test,覆盖 8 项修复,每 2 bar 高频信号触发
- 新增 `docs/SESSION_2026_04_20.md` 完整 session 记录
- 保留 `logs/StraLog.txt` 作历史证据,后续新 log gitignore
- **pytest**: 146 → **200** passing (+8 error_handler + 46 session_guard)
- **commits**: 18+ 个 (`3a262d0` → 最新)

### 2026-04-17 — 止损重构 + Scaled Entry

- **2026-04-17 晨**: AL V8 10:00 发现 H1 bar 级止损延迟 68 点 → tick 级止损重构 (risk.py v2)
- **2026-04-17 下午**: 全策略审计 v1-v3, 修 8 HIGH + 7 MEDIUM (commits 90def0d / 30ef673 / 57ec7d7)
- **2026-04-17 Phase 3**: AggressivePricer 引入 urgency 分级 + escalator + 跨合约 peg
- **2026-04-17 Phase 4**: ScaledEntryExecutor 引入 — 分仓进场 + VWAP 参考 + urgency 评分
- **2026-04-17 Audit v2**: 修 executor ↔ strategy pending_oids 同步, BOTTOM 过期, FORCE slot
- **2026-04-17 Audit v3**: 对向信号透明化, VWAP/entry oid 隔离, unknown oid 观察
- **2026-04-17 TestScaledEntry v2**: 三模式验证 (hold_long / reversal / stop_test)

所有测试 154/154 绿, 实盘就绪。
