# PythonGO Engine

## 概述

将QBase/QBase_v2中回测验证过的量化策略，转换为PythonGO格式的.py文件，部署到Windows无限易客户端进行期货实盘交易。每个策略/组合对应一个独立的.py文件，搭配共享的modules模块层提供风控、执行、监控等基础设施。当前部署5个品种（AL/CU/I/IH/LC）6个Portfolio组合。

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
├── modules/              # 共享模块层（部署到 pyStrategy/modules/）
│   ├── eal.py            # EAL执行算法层 — 大单拆批，M3子bar多因子评分执行
│   ├── twap.py           # TWAP分批执行器 — bar开始后2-11分钟分批下单
│   ├── risk.py           # 止损体系 — tick级硬止损 + M1级移动止损 (2026-04-17 重构)
│   │                       + on_day_change 剔除过夜浮盈 (2026-04-20)
│   ├── pricing.py        # ★ Spread-aware 限价定价 + urgency score (Phase 3)
│   ├── rolling_vwap.py   # ★ 滚动 30min VWAP — Scaled entry 的"便宜"锚点 (Phase 4)
│   ├── execution.py      # ★ ScaledEntryExecutor 状态机 — 分仓进场 (Phase 4)
│   │                       + get_state/load_state/force_lock 崩溃恢复 (2026-04-20)
│   ├── error_handler.py  # ★ throttle_on_error — 0004 撤单错误自动流控 (2026-04-20)
│   ├── session_guard.py  # 交易时段守卫
│   ├── contract_info.py  # 合约信息 — 乘数、最小变动价
│   ├── feishu.py         # 飞书通知
│   ├── persistence.py    # 状态持久化
│   ├── trading_day.py    # 交易日检测 — 21:00切换
│   ├── slippage.py       # 滑点记录
│   ├── heartbeat.py      # 心跳监控
│   ├── order_monitor.py  # 订单超时 + urgency escalator (Phase 3)
│   ├── performance.py    # 绩效追踪
│   ├── rollover.py       # 换月提醒
│   └── position_sizing.py # 仓位计算 — Vol Targeting + Carver buffer
├── AL/long/              # 电解铝做多策略 (V8 已集成完整修复 + startup eval + crash recovery)
├── CU/short/             # 铜做空策略
├── I/long/  I/short/     # 铁矿石做多+做空策略
├── IH/long/              # 上证50做多策略
├── LC/short/             # 碳酸锂做空策略
├── DailyReporter.py      # 全账户每日汇总飞书推送
└── test/                 # 集成测试策略
    ├── TestFullModule.py     # 全模块协同测试（MA双均线, 已集成 executor）
    ├── TestScaledEntry.py    # ★ ScaledEntryExecutor 三模式验证 (v2)
    ├── TestAllFixes.py       # ★ 2026-04-20 全部 8 项修复的 smoke test (高频信号)
    └── *_TEST.py             # 各品种/模块独立测试
tests/                    # pytest 单元测试
├── test_risk.py          # 27 用例: tick/M1 止损 + on_day_change 过夜浮盈
├── test_pricing.py       # 31 用例: urgency + spread adaptation
├── test_rolling_vwap.py  # 12 用例: 滚动窗口 + delta_vol
├── test_order_monitor.py # 18 用例: escalation schedule
├── test_execution.py     # 67 用例: 状态机 + 边界 + 记账 + crash recovery
└── test_error_handler.py # 8 用例: 0004 流控 + 恢复 + 并发
docs/
├── ARCHITECTURE.md       # 架构与开发规范
├── MODULE_REFERENCE.md   # 模块参考手册
├── OPERATIONAL_ISSUES_RESEARCH.md
├── RESEARCH_REPORT.md
├── SESSION_2026_04_17.md # 2026-04-17 session 记录
├── SESSION_2026_04_20.md # ★ 2026-04-20 fleet-wide 修复记录
└── pythongo/             # ★ PythonGO V2 API 完整源码审计 (8 md, 3135 行)
    ├── README.md         # 索引 + findings + bug list (已全部标 FIXED)
    ├── base.md           # BaseStrategy + INFINIGO 附录
    ├── classdef.md       # 10 个数据类字段表
    ├── utils.md          # KLineGenerator / Scheduler / Indicators / MarketCenter
    ├── ui.md             # widget + drawer + crosshair
    ├── options.md        # Option 定价 + 希腊值 + OptionChain
    ├── backtesting.md    # 回测引擎 (不建议用, 用 QBase)
    └── types.md          # 所有 Type 别名 + 数值映射
pythongo/                 # ★ PythonGO V2 源码 (供对照, .pyd 已 gitignore)
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
| `docs/ARCHITECTURE.md` | 模板规范、API踩坑 |
| `docs/MODULE_REFERENCE.md` | 模块API参考 + 部署说明 |
| `docs/SESSION_2026_04_20.md` | ★ 本轮 session 完整记录 |
| `docs/pythongo/` | ★ PythonGO V2 源码审计 (8 md, 3135 行) |
| `src/modules/risk.py` | 止损 tick/M1 + on_day_change 过夜浮盈修正 |
| `src/modules/pricing.py` | ★ Spread-aware 限价定价 + urgency (Phase 3) |
| `src/modules/rolling_vwap.py` | ★ 滚动 30min VWAP (Phase 4) |
| `src/modules/execution.py` | ★ ScaledEntryExecutor 入场状态机 + crash recovery |
| `src/modules/error_handler.py` | ★ throttle_on_error 0004 流控 (2026-04-20) |
| `src/modules/eal.py` | EAL执行算法层 (legacy) |
| `src/modules/twap.py` | TWAP分批执行 (legacy) |
| `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` | ★ 主实盘策略, 完整集成所有修复 |
| `src/test/TestFullModule.py` | 全模块集成测试模板 |
| `src/test/TestScaledEntry.py` | ★ ScaledEntryExecutor 三模式验证 |
| `src/test/TestAllFixes.py` | ★ 2026-04-20 修复 smoke test (高频信号) |
| `src/DailyReporter.py` | 全账户监控 |
| `tests/test_*.py` | ★ 154 个 pytest 单元测试 (6 文件) |

## 迭代历史

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

**Session 边界修复** (Simon 指出 10:15-10:30 茶歇)
- 🐛 **踩坑 3**:`contract_info.py` 70 个非 CFFEX 品种早盘都是连续 `((9, 0), (11, 30))`,漏了 **10:15-10:30 茶歇**。茶歇期间 broker 拒单但策略以为在 session → errCode 0004 刷屏
- 修:批量拆成 `((9, 0), (10, 15)), ((10, 30), (11, 30))`(81 处替换)
- 同时修 `SessionGuard._in_session` 边界:`start <= cur <= end` → `start <= cur < end`(`[start, end)` 区间),与 `contract_info._is_time_in_session` 对齐
- **37 新 pytest** 覆盖茶歇/夜盘跨午夜/CFFEX 无茶歇/sim_24h/flatten_zone

**测试 & 文档**
- 新增 `TestAllFixes.py`: 817 行 smoke test,覆盖 8 项修复,每 2 bar 高频信号触发
- 新增 `docs/SESSION_2026_04_20.md` 完整 session 记录
- 保留 `logs/StraLog.txt` 作历史证据,后续新 log gitignore
- **pytest**: 146 → **191** passing (+8 error_handler + 37 session_guard)
- **commits**: 17 个 (`3a262d0` → 最新)

### 2026-04-17 — 止损重构 + Scaled Entry

- **2026-04-17 晨**: AL V8 10:00 发现 H1 bar 级止损延迟 68 点 → tick 级止损重构 (risk.py v2)
- **2026-04-17 下午**: 全策略审计 v1-v3, 修 8 HIGH + 7 MEDIUM (commits 90def0d / 30ef673 / 57ec7d7)
- **2026-04-17 Phase 3**: AggressivePricer 引入 urgency 分级 + escalator + 跨合约 peg
- **2026-04-17 Phase 4**: ScaledEntryExecutor 引入 — 分仓进场 + VWAP 参考 + urgency 评分
- **2026-04-17 Audit v2**: 修 executor ↔ strategy pending_oids 同步, BOTTOM 过期, FORCE slot
- **2026-04-17 Audit v3**: 对向信号透明化, VWAP/entry oid 隔离, unknown oid 观察
- **2026-04-17 TestScaledEntry v2**: 三模式验证 (hold_long / reversal / stop_test)

所有测试 154/154 绿, 实盘就绪。
