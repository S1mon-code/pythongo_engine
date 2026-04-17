# PythonGO Engine

## 概述

将QBase/QBase_v2中回测验证过的量化策略，转换为PythonGO格式的.py文件，部署到Windows无限易客户端进行期货实盘交易。每个策略/组合对应一个独立的.py文件，搭配共享的modules模块层提供风控、执行、监控等基础设施。当前部署5个品种（AL/CU/I/IH/LC）6个Portfolio组合。

## 技术栈

- **语言**: Python（PythonGO运行时，无限易内置）
- **依赖**: 仅numpy + requests + pythongo内置模块（不用talib）
- **指标**: 纯numpy手写，从QBase原样移植（避免信号偏差）
- **通知**: 飞书Webhook（非阻塞推送）
- **格式化**: ruff

## 项目结构

```
src/
├── modules/              # 共享模块层（部署到 pyStrategy/modules/）
│   ├── eal.py            # EAL执行算法层 — 大单拆批，M3子bar多因子评分执行
│   ├── twap.py           # TWAP分批执行器 — bar开始后2-11分钟分批下单
│   ├── risk.py           # 止损体系 — tick级硬止损 + M1级移动止损 (2026-04-17 重构)
│   ├── pricing.py        # ★ Spread-aware 限价定价 + urgency score (Phase 3)
│   ├── rolling_vwap.py   # ★ 滚动 30min VWAP — Scaled entry 的"便宜"锚点 (Phase 4)
│   ├── execution.py      # ★ ScaledEntryExecutor 状态机 — 分仓进场 (Phase 4)
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
├── AL/long/              # 电解铝做多策略 (V8 已集成 ScaledEntryExecutor)
├── CU/short/             # 铜做空策略
├── I/long/  I/short/     # 铁矿石做多+做空策略
├── IH/long/              # 上证50做多策略
├── LC/short/             # 生猪做空策略
├── DailyReporter.py      # 全账户每日汇总飞书推送
└── test/                 # 集成测试策略
    ├── TestFullModule.py     # 全模块协同测试（MA双均线, 已集成 executor）
    ├── TestScaledEntry.py    # ★ ScaledEntryExecutor 三模式验证 (v2)
    └── *_TEST.py             # 各品种/模块独立测试
tests/                    # pytest 单元测试 (2026-04-17)
├── test_risk.py          # 23 用例: tick/M1 止损, 持久化
├── test_pricing.py       # 31 用例: urgency + spread adaptation
├── test_rolling_vwap.py  # 12 用例: 滚动窗口 + delta_vol
├── test_order_monitor.py # 18 用例: escalation schedule
└── test_execution.py     # 50 用例: 状态机 + 边界 + 记账
docs/
├── ARCHITECTURE.md       # 架构与开发规范
├── MODULE_REFERENCE.md   # 模块参考手册
├── OPERATIONAL_ISSUES_RESEARCH.md
└── RESEARCH_REPORT.md
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
```

**关键**: modules/ 放在 `pyStrategy/modules/`（与pythongo/同级），不是 self_strategy/ 下。

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
- **21:00 Day Start**: 所有模块统一以21:00作为交易日切换点(夜盘开始)
- **双时间框架**: Portfolio策略同时运行两个KLineGenerator(如H1+H4)

### 止损架构 (2026-04-17 重构)

**Tick 级硬止损 + M1 级移动止损** (替代旧的 bar 级):
- Peak/trough 每 tick 更新 (真实极值)
- 硬止损每 tick 判断, 破即触发, 无确认延时
- 移动止损 peak/trough tick 级追踪 + 分钟门控判断 (降噪)
- 触发时策略调 `_exec_stop_at_tick()` + `self._entry.on_stop_triggered()` 双路径

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

详见 `src/modules/execution.py` 和 `tests/test_execution.py`。

### PythonGO API 踩坑

- `get_account_fund_data("")` 会崩溃，必须先 `get_investor_data(1)` 拿investor_id
- `self.output()` 替代 `print()`
- `market=True` 用市价单，price参数仅用于显示
- KLineProducer没有open_interest，需在callback中从KLineData手动收集
- 只提供tick数据，K线全靠KLineGenerator从tick合成

### 模块导入模板 (2026-04-17 更新)

每个策略文件的模块导入保持一致(与 TestFullModule / TestScaledEntry 对齐):

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
# 新增 (Phase 3 / Phase 4):
from modules.pricing import AggressivePricer
from modules.rolling_vwap import RollingVWAP
from modules.execution import EntryParams, EntryState, ExecAction, ScaledEntryExecutor
```

### Scaled Entry 集成 checklist (21 项)

新策略若要接入 ScaledEntryExecutor, 必须实现:

1. `__init__`: `self._rvwap: RollingVWAP | None = None`
2. `__init__`: `self._entry: ScaledEntryExecutor | None = None`
3. `__init__`: `self._unknown_oid_count = 0`
4. `on_start`: 创建 `RollingVWAP(window_seconds=1800)` + `ScaledEntryExecutor(EntryParams(...))`
5. `on_tick`: 喂 pricer + rvwap + 驱动 `_drive_entry`
6. `_drive_entry`: 调 `executor.on_tick()` + apply actions + 更新 `entry_progress`
7. `_apply_entry_action`: 处理 submit / cancel / cancel_all / feishu 四种 op
8. `_apply_entry_action` submit 分支: `register_pending(oid, vol, price=a.price)` 带价位
9. `_on_bar` OPEN/ADD 路径: 调 `executor.on_signal()` 并消费返回 actions
10. `_on_bar` OPEN/ADD 路径: 加保证金预检 (audit v2)
11. `_on_bar` 开头 cancel_order 后同步 `self._entry.register_cancelled(oid)`
12. `_on_tick_stops` 触发后调 `executor.on_stop_triggered()` 并消费 actions
13. `_exec_stop_at_tick` 等止损路径: cancel_order 同步 `register_cancelled`
14. `on_trade`: `claimed_by_entry = self._entry.on_trade(...)` 判 claim
15. `on_trade`: 未 claim 也不属 VWAP 的 oid 计入 `_unknown_oid_count` + warning
16. VWAP 发单时 `self._vwap_oids.add(oid)` (若保留 legacy VWAP)
17. `on_trade`: VWAP 成交累加条件: `oid in self._vwap_oids`
18. `_vwap_cancel` / `_vwap_complete`: 清 `_vwap_oids`
19. `_freq_to_sec`: 防御性处理 str / enum / "Enum.H1" 形式
20. state_map 暴露 `entry_progress` 给 UI
21. `_save`: 序列化 `peak_price` / `trough_price` + `self._risk.get_state()`

参考实现: `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` (完整) 或
`src/test/TestScaledEntry.py` (简化测试版)。

## 关键文件

| 文件 | 用途 |
|------|------|
| `docs/ARCHITECTURE.md` | 最重要的参考文档 — 模板规范、API踩坑 |
| `docs/MODULE_REFERENCE.md` | 模块API参考 + 部署说明 |
| `src/modules/risk.py` | 止损体系 — tick/M1 两层 (2026-04-17 重构) |
| `src/modules/pricing.py` | ★ Spread-aware 限价定价 + urgency (Phase 3) |
| `src/modules/rolling_vwap.py` | ★ 滚动 30min VWAP (Phase 4) |
| `src/modules/execution.py` | ★ ScaledEntryExecutor 入场状态机 (Phase 4) |
| `src/modules/eal.py` | EAL执行算法层 (legacy) |
| `src/modules/twap.py` | TWAP分批执行 (legacy) |
| `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` | ★ 主实盘策略, 已集成完整 scaled entry |
| `src/test/TestFullModule.py` | 全模块集成测试模板 |
| `src/test/TestScaledEntry.py` | ★ ScaledEntryExecutor 三模式验证 (hold_long/reversal/stop_test) |
| `src/DailyReporter.py` | 全账户监控 |
| `tests/test_*.py` | ★ 134 个 pytest 单元测试 |

## 迭代历史 (2026-04-17)

- **2026-04-17 晨**: AL V8 10:00 发现 H1 bar 级止损延迟 68 点 → tick 级止损重构 (risk.py v2)
- **2026-04-17 下午**: 全策略审计 v1-v3, 修 8 HIGH + 7 MEDIUM (commits 90def0d / 30ef673 / 57ec7d7)
- **2026-04-17 Phase 3**: AggressivePricer 引入 urgency 分级 + escalator + 跨合约 peg
- **2026-04-17 Phase 4**: ScaledEntryExecutor 引入 — 分仓进场 + VWAP 参考 + urgency 评分
- **2026-04-17 Audit v2**: 修 executor ↔ strategy pending_oids 同步, BOTTOM 过期, FORCE slot
- **2026-04-17 Audit v3**: 对向信号透明化, VWAP/entry oid 隔离, unknown oid 观察
- **2026-04-17 TestScaledEntry v2**: 三模式验证 (hold_long / reversal / stop_test)

所有测试 134/134 绿, 实盘就绪。
