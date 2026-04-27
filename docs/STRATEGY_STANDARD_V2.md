# 策略模版标准 V2 (2026-04-24 确立)

> 所有新策略 **必须** 按本标准编写。老策略需要逐步迁移。
> 参考实现:`src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py`

---

## 1. 核心设计原则

### 1.1 自管理持仓 (Self-Managed Position)

策略**只管自己开的仓**,账户上别的策略/手动交易的仓位一律不碰。

**三道关口实现:**
1. 所有发出的 `oid` 加入 `self._my_oids` (持久化到 state.json,重启保留)
2. `on_trade` 入口:`if oid not in self._my_oids: return` — 非自己的成交完全忽略
3. 所有决策 (开/加/减/平/止损/权益) 都基于 `self._own_pos`,不用 `get_position()` 的 broker 总持仓

**为什么:**
- 防止启动时策略接管历史仓位 (V8 旧版 `startup_eval` 的坑)
- 多策略同账户时互不干扰
- 简化 crash recovery (只需恢复自己的状态)

### 1.2 Max 5 手 (硬上限)

5 道保险,缺一不可:

| # | 位置 | 代码 |
|---|------|------|
| 1 | 模块常量 | `MAX_LOTS = 5` |
| 2 | Params 默认值 | `max_lots: int = Field(default=MAX_LOTS)` |
| 3 | on_start 强制拉回 | `if p.max_lots > MAX_LOTS: p.max_lots = MAX_LOTS` |
| 4 | _on_bar target 截断 | `target = min(target, p.max_lots, MAX_LOTS)` |
| 5 | on_trade 买入后截断 | `if new > MAX_LOTS: new = MAX_LOTS` |

### 1.3 UI 全量展示

**主图 (价格尺度) 4 条线 (或其他同尺度指标):**
- 例: Donchian 上沿/中轴/下沿 + Chandelier 止损线
- 预热期 fallback 到当前 close,避免 Y 轴被 0 拉爆

**副图 (0-100 振子尺度):**
- 例: ADX/PDI/MDI 或 MFI/MFI_Floor

**买卖箭头:**
- 策略实际发单时通过 `signal_price ≠ 0` 让图上自动画紫色/黄色箭头

### 1.4 状态栏全量字段 (40+ 个)

#### 必须字段 (所有策略都有)
```python
# 信号类
signal: float              # 原始信号 (0-1)
forecast: float            # 预测 (0-FORECAST_CAP)
optimal: int               # Vol Targeting 算出的理想手数
target_lots: int           # 最终目标手数 (截断后)

# 持仓类
own_pos: int               # 自管持仓
broker_pos: int            # 账户总持仓 (仅展示, 不决策)
my_oids_n: int             # 已发单累计

# UI 实时字段 (每 tick 更新)
last_price: float          # 最新价
last_bid1: float           # 买一价
last_ask1: float           # 卖一价
spread_tick: int           # 盘口价差 (tick 数)
last_tick_time: str        # 最后 tick 时间
heartbeat: int             # 心跳 (每 tick +1, mod 1000)
tick_count: int
bar_count: int
ui_push_count: int         # widget.recv_kline 成功次数
status_bar_updates: int    # update_status_bar 调用次数

# 持仓追踪
avg_price: float           # 均价
peak_price: float          # 峰价
hard_line: float           # 硬止损线
trail_line: float          # 移动止损线

# 账户
equity: float
drawdown: str
daily_pnl: str

# 状态
trading_day: str
session: str
pending: str               # 待执行 action
last_action: str
last_direction: str        # on_trade 收到的 direction 原始值 (DIAG)

# 辅助
slippage: str
perf: str
```

#### 信号指标字段 (按策略各自加)
每个策略把所使用的 **所有指标当前值** 暴露到 state_map。例:
- V8 家族: `dc_upper / dc_mid / dc_lower / chandelier / adx / pdi / mdi / atr`
- V13 家族: `dc_upper / dc_mid / dc_lower / chandelier / mfi_value / don_sig / mfi_sig / atr`

### 1.5 全量日志 (16 个核心 tag)

必须的日志 tag (便于排查):

| Tag | 位置 | 作用 |
|-----|------|------|
| `[ON_START]` | on_start 各阶段 | 启动流程分步确认 |
| `[ON_STOP]` | on_stop | 停止快照 |
| `[ON_BAR 实盘/历史/非交易时段/预热]` | _on_bar 入口 | 每 bar 进入状态 |
| `[BAR #N]` | callback 前3根+每20根 | bar 合成计数 |
| `[RT_CB #N]` | real_time_callback 第1+每200 | 图表心跳 |
| `[SESSION_CHANGE]` | _on_tick_aux | 交易时段切换 |
| `[IND]` | _on_bar | 当前所有指标值 |
| `[SIGNAL]` | _on_bar | raw + forecast |
| `[POS_DECISION]` | _on_bar | optimal / target / own_pos |
| `[PENDING]` | _on_bar | action 决策 |
| `[NO_ACTION]` | _on_bar | target 与 own_pos 相同 |
| `[EXECUTE]` | _execute 入口 | 路由到具体分支前 |
| `[EXEC_OPEN/ADD/REDUCE/CLOSE/STOP]` | 各 _exec_* | 发单详情 |
| `[ON_ORDER]` / `[ON_ORDER_CANCEL]` | on_order / on_order_cancel | 订单状态回推 (含 本策略/非本策略 标记) |
| `[ON_TRADE]` + `[FILL BUY/SELL]` | on_trade | 成交更新 own_pos |
| `[WIDGET]` | _push_widget 第1/每500次/异常 | UI 推送状态 |
| `[ESCALATE]` | _resubmit_escalated | urgency 升级 |

**不打的:** `[TICK #N]` (太频繁,只在测试策略用)

### 1.6 双写日志实时落盘

```python
def _log(self, msg: str) -> None:
    """output() → StraLog.txt + print() → stdout."""
    self.output(msg)
    try:
        print(f"[{STRATEGY_NAME}] {msg}", flush=True)
        sys.stdout.flush()
    except Exception:
        pass
```

替代所有关键路径的 `self.output(...)` (非关键的也可以保留 output)。

### 1.7 周期状态栏刷新

**每 10 tick** 主动调 `update_status_bar()`,保证 state_map 字段实时传播到 UI。

```python
if self._tick_count % 10 == 0:
    self.update_status_bar()
    self.state_map.status_bar_updates += 1
```

### 1.8 Type-Safe Widget Push

`_push_widget` 必须做:
- `self.widget is None` 检查 + 失败计数日志
- 所有 payload 值 NaN → 0.0 兜底 (防图表 Y 轴污染)
- 首次成功推送打 `[WIDGET] 首次推送成功`
- 前 5 次推送都日志 (验证 UI 链通)

---

## 2. 文件布局

### 2.1 命名 (PythonGO 硬要求: 类名 = 文件名)

```
{品种}_{方向}_{频率}_{版本}_{指标简称}.py

例:
  AL_Long_1H_V8_Donchian_ADX_Filter.py   类名同
  AG_Long_1H_V13_Donchian_MFI.py         类名同
```

### 2.2 目录结构

```
src/
  ├── {品种大写}/{方向}/*.py       # 生产策略
  │      例: AL/long/ CU/short/ AG/long/
  ├── test/*.py                   # 测试/smoke 策略
  └── modules/*.py                # 共享模块
```

### 2.3 STRATEGY_NAME 唯一

`STRATEGY_NAME = "{品种}_{方向}_{频率}_{版本}"` (简短版,不含指标名)

独立 state 文件:`pyStrategy/state/{STRATEGY_NAME}.json`

---

## 3. 参数常量 (按策略调)

### 3.1 信号参数
从 QBase_v3 research 拿默认值。记录来源:`# from QBase_v3 research/...`。

### 3.2 Chandelier Exit
```python
CHANDELIER_PERIOD = 22
CHANDELIER_MULT = 2.58 或 3.0   # 按策略调
```

### 3.3 Vol Targeting
```python
FORECAST_SCALAR = 10.0
FORECAST_CAP = 20.0
ANNUAL_FACTOR = 252 * N         # N 按交易所夜盘时长
```

**ANNUAL_FACTOR 对照表:**

| 交易所分类 | 日bar数(H1) | ANNUAL_FACTOR |
|-----------|------------|---------------|
| SHFE 有色/黑色 (AL/CU/HC/RB/ZN/PB/NI/SN/...): 夜盘到 01:00 | 8 | 252 × 8 |
| SHFE 贵金属 (AU/AG): 夜盘到 02:30 | 10 | 252 × 10 |
| SHFE 黑色/化工 (RU/BU/SP/FU/OP): 夜盘到 23:00 | 6 | 252 × 6 |
| INE SC: 夜盘到 02:30 | 10 | 252 × 10 |
| DCE 大部分 (A/B/M/Y/P/J/JM/I/JD/...): 夜盘到 23:00 | 6 | 252 × 6 |
| CZCE 大部分: 夜盘到 23:00 | 6 | 252 × 6 |
| CFFEX (IF/IH/IC/IM/TS/TF/T/TL): 09:30-11:30 连续 + 13:00-15:15 无夜盘 | 5-6 | 252 × 5 |
| GFEX (SI/LC/PS/PT/PD): 无夜盘 | 4 | 252 × 4 |

### 3.4 止损参数 (Params 默认)
```python
hard_stop_pct: float = 0.5
trailing_pct: float = 0.3
equity_stop_pct: float = 2.0
```

测试版可放宽 (5% / 3% / 20%) 避免干扰。

---

## 4. 模块导入 (标准 14 个)

```python
from modules.contract_info import get_multiplier, get_tick_size
from modules.error_handler import throttle_on_error
from modules.feishu import feishu
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.persistence import load_state, save_state
from modules.position_sizing import apply_buffer, calc_optimal_lots
from modules.pricing import AggressivePricer
from modules.risk import RiskManager
from modules.rollover import check_rollover
from modules.session_guard import SessionGuard
from modules.slippage import SlippageTracker
from modules.trading_day import get_trading_day
```

---

## 5. 三层止损架构

```
Tick 级 (每 tick)
  ├── update_peak_trough_tick(price, own_pos)
  ├── check_hard_stop_tick(...)    # 硬止损, 立即触发
  └── check_trail_minutely(...)    # 移动止损, 分钟门控
         ↓
         _exec_stop_at_tick(price, action, reason)
            - 撤所有挂单
            - auto_close_position(urgency=urgent/critical)
            - 设 _pending = action 防止 tick 重复触发

Bar 级 (每 bar 闭合)
  └── self._risk.check(..., equity_stop_pct=...)    # 权益/熔断
         ↓
         _pending = EQUITY_STOP/CIRCUIT/DAILY_STOP/FLATTEN
         _execute → _exec_close
```

**urgency 对照:**
- `passive` — OPEN/ADD (建仓, 不急)
- `normal` — REDUCE/CLOSE (减平, 一般)
- `urgent` — HARD_STOP/TRAIL_STOP (止损, 穿盘口)
- `critical` — EQUITY_STOP/CIRCUIT/DAILY_STOP/FLATTEN (熔断, 极限穿盘口)

---

## 6. 订单流

### 6.1 发单
- **开仓/加仓**: `send_order(order_direction="buy")` — 不传 `market=True`
- **平仓/减仓/止损**: `auto_close_position(order_direction="sell")` — 自动处理 SHFE 平今

### 6.2 定价
`AggressivePricer` 穿盘口定价 (spread-aware):
```python
buy_price = self._aggressive_price(price, "buy", urgency="passive")
sell_price = self._aggressive_price(price, "sell", urgency="normal")
```

### 6.3 Escalator
`OrderMonitor` 监控未成交订单,超时按 urgency 梯度升级重挂:
```
passive → normal → cross → urgent → critical
```
升级时 `_resubmit_escalated` 自动撤旧单重发,新 oid 加入 `self._my_oids`。

### 6.4 bar 开头撤单
每个 bar 开头先撤本策略所有挂单:
```python
for oid in list(self.order_id):
    self.cancel_order(oid)
```

---

## 7. 生命周期

```
on_init (无限易加载时)
  └── 父类处理: load_instance_file + init_widget

on_start (点 Run)
  ├── _log("[ON_START] 开始初始化 ...")
  ├── 初始化所有模块 (pricer, guard, slip, hb, om, perf)
  ├── KLineGenerator + push_history_data()
  ├── get_investor_data → self._investor_id
  ├── RiskManager + load_state (恢复 own_pos + my_oids + avg + peak)
  ├── acct 获取 + on_day_change 剔除过夜浮盈
  ├── get_position → broker_pos (仅展示)
  ├── super().on_start() — trading=True + sub_market_data + load_data_signal
  └── _log("=== 启动完成 ===")

on_tick (每 tick)
  ├── 更新 tick_count / heartbeat / last_price / spread_tick 等 UI 字段
  ├── 每 10 tick: update_status_bar()
  ├── kline_generator.tick_to_kline(tick)
  ├── _pricer.update(tick)
  ├── _on_tick_stops(tick)   — 止损检查
  └── _on_tick_aux(tick)     — session 切换/escalate/换月/心跳

callback (bar 闭合)
  └── _on_bar(kline)
        ├── _refresh_indicators()
        ├── 撤本策略挂单
        ├── 处理残留 pending
        ├── 预热检查 (producer_bars >= WARMUP+2)
        ├── 信号 → forecast → target (capped at 5)
        ├── 持仓追踪 + 权益 + 止损检查
        ├── 信号 → pending
        └── _execute(action) → _push_widget + update_status_bar

real_time_callback (每 tick 更新最后一根 K)
  └── _push_widget(kline)   — 让图上最后一根蜡烛跳动

on_trade (成交回推)
  ├── 过滤: if oid not in self._my_oids → return
  ├── 健壮识别 direction ("0"/"1"/"买"/"buy")
  └── 更新 own_pos + avg_price + peak_price

on_order / on_order_cancel
  └── 日志 (本策略) / (非本策略)

on_error
  └── throttle_on_error (0004 流控)

on_stop (点 Pause)
  └── _save() + super().on_stop()
```

---

## 8. 7 个参考实现 (2026-04-24 已部署)

### V8 家族 (Donchian + ADX)
| 文件 | 品种 | 合约默认 | 交易所 |
|------|------|---------|--------|
| AL_Long_1H_V8_Donchian_ADX_Filter.py | 电解铝 | al2607 | SHFE |
| CU_Long_1H_V8_Donchian_ADX_Filter.py | 铜 | cu2606 | SHFE |
| HC_Long_1H_V8_Donchian_ADX_Filter.py | 热卷 | hc2510 | SHFE |

### V13 家族 (Donchian + MFI)
| 文件 | 品种 | 合约默认 | 交易所 |
|------|------|---------|--------|
| AG_Long_1H_V13_Donchian_MFI.py | 白银 | ag2606 | SHFE |
| P_Long_1H_V13_Donchian_MFI.py | 棕榈油 | p2609 | DCE |
| PP_Long_1H_V13_Donchian_MFI.py | 聚丙烯 | pp2606 | DCE |
| JM_Long_1H_V13_Donchian_MFI.py | 焦煤 | jm2605 | DCE |

### 测试模版
| 文件 | 用途 |
|------|------|
| src/test/AL_Long_M1_Test_Oscillator.py | M1 振子 (12-bar 周期), 最高频覆盖所有代码路径 |
| src/test/TestAllFixes.py | 2026-04-20 fleet-wide 修复 smoke test |

---

## 9. 新策略 Checklist (抄这个)

迁移 / 新写策略时必须完成:

- [ ] 类名 = 文件名
- [ ] `STRATEGY_NAME` 唯一
- [ ] `Params` 含 max_lots (默认 5) + hard_stop/trailing/equity_stop_pct + sim_24h + flatten_minutes
- [ ] `State` 包含全 40+ 字段 (参考 §1.4)
- [ ] `MAX_LOTS = 5` + 5 道保险全有
- [ ] 指标缓存 (`self._ind_*`) + `main_indicator_data` + `sub_indicator_data`
- [ ] `_log()` 双写 + 所有 16 个 tag
- [ ] `self._own_pos` + `self._my_oids` + on_trade 过滤
- [ ] 14 个标准模块导入
- [ ] 信号函数签名正确 (带 volumes 参数如果用量价指标)
- [ ] `ANNUAL_FACTOR` 按交易所夜盘对照 §3.3
- [ ] tick 级 UI 字段更新 + 每 10 tick status_bar 刷新
- [ ] _push_widget type-safe (NaN→0, 首次推送日志)
- [ ] on_trade 健壮识别 direction ("0"/"1"/"买")
- [ ] on_error 调 `throttle_on_error`
- [ ] 无 `market=True` (全限价)
- [ ] 语法 `python -c "import ast; ast.parse(open('file.py').read())"` 通过

---

## 10. 已知踩坑 (不要再犯)

| # | 坑 | 正确 |
|---|---|---|
| 1 | 合约代码大写 | 必须小写: `al2607` 不是 `AL2607` |
| 2 | `trade.direction` 当中文 | 是 `"0"/"1"`, 用 `raw in ("buy", "0", "买")` 健壮识别 |
| 3 | `order.volume` | 不存在! 用 `cancel_volume / total_volume / traded_volume` |
| 4 | SHFE 拒市价 | 全限价 + AggressivePricer 穿盘口 |
| 5 | 茶歇 10:15-10:30 | contract_info sessions 已拆,不要自己合回去 |
| 6 | open_grace 未开 | `SessionGuard(open_grace_sec=30)` 避开开盘 rush |
| 7 | on_order override 丢 super | 必须调 `super().on_order(order)` 保留分发 |
| 8 | 0004 流控忘关 | on_error 必须调 `throttle_on_error(self, error)` |
| 9 | `get_position()` 当 None | 永不返 None,空持仓返 `Position()` `net_position=0` |
| 10 | `_save` 漏 null-guard | `_risk` 可能是 None,加 `if self._risk is not None:` |
| 11 | 指标 NaN 传图表 | `_nz_last(arr, idx, fallback=closes[-1])` 兜底 |
| 12 | widget=None 吞异常 | 加计数日志,不要 `except: pass` |
| 13 | `Field(title=...)` 含特殊字符 | title 不能含逗号/等号/大于号/括号, 否则 `__package_params` 截断 → `KeyError`. 详细说明只放注释, title 简洁中文 (2026-04-27 takeover 实盘发现, 7 文件修复) |

---

## 11. 上线 checklist

1. 合约代码确认 (主力换月检查)
2. 清 `pyStrategy/state/{STRATEGY_NAME}.json` (新旧 state 结构不兼容)
3. `src/modules/` → `pyStrategy/modules/` (整个拷)
4. 策略 `.py` → `pyStrategy/self_strategy/`
5. 观察启动日志顺序:
   - `=== 启动完成 ===`
   - `[WIDGET] 首次推送成功`
   - `[TICK #1]`
   - `[BAR #1]`
   - `[ON_BAR 实盘]` + `[IND]` + `[SIGNAL]` + `[POS_DECISION]`
6. 实盘首小时持续观察 `[ON_TRADE] oid=X 非本策略, 跳过` — 证明自管过滤在起作用

---

**本文档最后更新: 2026-04-24 @ 新标准确立日**
