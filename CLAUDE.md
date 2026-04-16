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
│   ├── risk.py           # 止损体系 — 移动止损/硬止损/回撤熔断/单日止损
│   ├── session_guard.py  # 交易时段守卫 — 判断是否在交易时段内
│   ├── contract_info.py  # 合约信息 — 乘数、最小变动价、交易时段
│   ├── feishu.py         # 飞书通知 — 非阻塞Webhook推送
│   ├── persistence.py    # 状态持久化 — save/load策略状态
│   ├── trading_day.py    # 交易日检测 — 21:00为日切换点
│   ├── slippage.py       # 滑点记录
│   ├── heartbeat.py      # 心跳监控
│   ├── order_monitor.py  # 订单超时监控
│   ├── performance.py    # 绩效追踪
│   ├── rollover.py       # 换月提醒
│   └── position_sizing.py # 仓位计算 — Vol Targeting + Carver buffer
├── AL/long/              # 电解铝做多策略
├── CU/short/             # 铜做空策略
├── I/long/  I/short/     # 铁矿石做多+做空策略
├── IH/long/              # 上证50做多策略
├── LC/short/             # 生猪做空策略
├── DailyReporter.py      # 全账户每日汇总飞书推送（不交易，只监控）
└── test/                 # 集成测试策略
    ├── TestFullModule.py # 全模块协同测试（MA双均线 + 12模块）
    └── *_TEST.py         # 各品种/模块独立测试
docs/
├── ARCHITECTURE.md       # 架构与开发规范（核心文档）
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

- **Same-bar执行 (2026-04-14)**: 信号在当前bar产生后立即提交TWAP，不等下一根bar。止损立即`_execute()`，正常信号立即`_submit_twap()`，TWAP在后续tick流中分批成交。和QBase回测时序一致（Bar N收盘出信号→Bar N+1期间执行）
- **每bar开头撤挂单**: `for oid in list(self.order_id): self.cancel_order(oid)`
- **21:00 Day Start**: 所有模块统一以21:00作为交易日切换点（夜盘开始）
- **双时间框架**: Portfolio策略同时运行两个KLineGenerator（如H1+H4），各自独立出信号，合并成net_target

### 止损优先级

止损 > 止盈 > 出场信号 > 加仓 > 建仓

止损信号（HARD_STOP/TRAIL_STOP/CIRCUIT等）立即执行，不走TWAP。

### PythonGO API 踩坑

- `get_account_fund_data("")` 会崩溃，必须先 `get_investor_data(1)` 拿investor_id
- `self.output()` 替代 `print()`
- `market=True` 用市价单，price参数仅用于显示
- KLineProducer没有open_interest，需在callback中从KLineData手动收集
- 只提供tick数据，K线全靠KLineGenerator从tick合成

### 模块导入模板

每个策略文件的模块导入保持一致（与TestFullModule对齐）:

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
from modules.twap import TWAPExecutor, IMMEDIATE_ACTIONS
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer
```

## 关键文件

| 文件 | 用途 |
|------|------|
| `docs/ARCHITECTURE.md` | 最重要的参考文档 — 模板规范、API踩坑、双频率架构、止损体系、仓位管理 |
| `docs/MODULE_REFERENCE.md` | 模块API参考 + 部署说明 |
| `src/modules/risk.py` | 四层止损体系（移动/硬止/回撤熔断/单日） |
| `src/modules/eal.py` | EAL执行算法层 — 大单拆批多因子评分 |
| `src/modules/twap.py` | TWAP分批执行 — 止损立即，正常信号分批 |
| `src/test/TestFullModule.py` | 全模块集成测试模板 — 新策略参考此文件结构 |
| `src/DailyReporter.py` | 全账户监控 — 15:15收盘汇总 + 21:05开盘状态 |
