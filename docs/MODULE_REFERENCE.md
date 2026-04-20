# PythonGO Engine — 模块参考手册

> **2026-04-20 更新**:
> - 新增 `modules/error_handler.py`(0004 流控)
> - 更新至 PythonGO 2025.0925.1420 源码对齐
> - `contract_info.py` 70 个非 CFFEX 商品品种早盘拆分 10:15-10:30 茶歇
> - `SessionGuard` 新增 `open_grace_sec=30` 开盘保护 + `seconds_since_session_start()` helper
> - `risk.py` 的 `on_day_change(balance, position_profit=0.0)` 剔除过夜浮盈
> - `execution.py` (ScaledEntryExecutor) 新增 `get_state/load_state/force_lock` 崩溃恢复
>
> 详见 `docs/SESSION_2026_04_20.md` + `docs/pythongo/` 8 md 完整 API 对照。

所有模块已在无限易模拟盘测试通过 (2026-04-02,2026-04-20 全队修复后 pytest **200/200 绿**,2 轮实盘 smoke test 确认)。

## 部署位置

modules/ 放在 `pyStrategy/modules/` (与pythongo/同级), **不是** self_strategy/ 下。

```
pyStrategy/
  pythongo/           ← 框架
  modules/            ← ★ 我们的模块
  self_strategy/
    TestFullModule.py ← 策略文件
```

## 模块导入

```python
# 策略文件中:
from modules.feishu import feishu
from modules.persistence import save_state, load_state
from modules.trading_day import get_trading_day
from modules.risk import check_stops
from modules.slippage import SlippageTracker
from modules.heartbeat import HeartbeatMonitor
from modules.order_monitor import OrderMonitor
from modules.performance import PerformanceTracker
from modules.rollover import check_rollover
from modules.position_sizing import calc_optimal_lots, apply_buffer
# 2026-04-17 Phase 3/4:
from modules.pricing import AggressivePricer
from modules.rolling_vwap import RollingVWAP
from modules.execution import EntryParams, ExecAction, ScaledEntryExecutor
# 2026-04-20:
from modules.error_handler import throttle_on_error
```
```

## 模块清单

| 模块 | 文件 | 类型 | 说明 |
|------|------|------|------|
| 飞书通知 | `modules/feishu.py` | 函数 | `feishu(action, symbol, msg)` 非阻塞 |
| 状态持久化 | `modules/persistence.py` | 函数 | `save_state(data)` / `load_state()` |
| 交易日检测 | `modules/trading_day.py` | 函数 | `get_trading_day()` → "20260402" |
| 止损体系 | `modules/risk.py` | 类 | `RiskManager` — tick/M1 级止损 (2026-04-17 重构) |
| 滑点记录 | `modules/slippage.py` | 类 | `SlippageTracker` |
| 心跳监控 | `modules/heartbeat.py` | 类 | `HeartbeatMonitor` |
| 订单超时 | `modules/order_monitor.py` | 类 | `OrderMonitor` + escalator (Phase 3) |
| 绩效追踪 | `modules/performance.py` | 类 | `PerformanceTracker` |
| 换月提醒 | `modules/rollover.py` | 函数 | `check_rollover(id)` → (level, days) |
| 仓位计算 | `modules/position_sizing.py` | 函数 | `calc_optimal_lots()` / `apply_buffer()` |
| **Aggressive Pricer** | `modules/pricing.py` | 类 | `AggressivePricer` — 盘口追 bid/ask + urgency (Phase 3) |
| **Rolling VWAP** | `modules/rolling_vwap.py` | 类 | `RollingVWAP` — 滚动 30min VWAP (Phase 4) |
| **Scaled Entry** | `modules/execution.py` | 类 | `ScaledEntryExecutor` — 分仓进场状态机 (Phase 4) |

### Phase 3: Aggressive Pricing (2026-04-17)

```python
from modules.pricing import AggressivePricer

pricer = AggressivePricer(tick_size=5.0)
# 每 tick:
pricer.update(tick)   # 自动读 bid1/ask1/last_price, 滚动 spread

# Urgency 分级版本:
px = pricer.price("buy", urgency="passive")   # 挂 bid1
px = pricer.price("buy", urgency="urgent")    # ask1 + 5 tick

# 连续 urgency 分数版本 (ScaledEntryExecutor 使用):
px = pricer.price_with_urgency_score("buy", urgency=0.75, max_ticks=10)  # 穿 ~8 tick
```

### Phase 4: Scaled Entry (2026-04-17)

```python
from modules.execution import EntryParams, ScaledEntryExecutor
from modules.rolling_vwap import RollingVWAP

# on_start:
self._rvwap = RollingVWAP(window_seconds=1800)
self._entry = ScaledEntryExecutor(EntryParams(
    bottom_lots=2,             # 或 bottom_ratio=0.33 二选一
    force_start_sec=1800,      # T=30min 进入 FORCE
    force_slot_sec=300,        # 每 5 min 一个 force slot
    force_peg_sec=120,         # slot 前 2 min peg
    force_cross_min_urgency=0.75,   # cross 段最低 urgency
    opp_min_submit_interval_sec=10,
    max_concurrent_pending=3,
    over_target_enabled=True,
    over_target_vwap_pct=0.5,
    over_target_forecast=5.0,
    over_target_ratio=0.20,
    # 未来扩展 (None=不启用):
    max_pov_ratio=None,        # POV 参与率限制
    visible_lots=None,         # Iceberg 冰山单
))

# on_signal (bar close 时调):
actions = self._entry.on_signal(
    target=6, direction="buy",
    now=datetime.now(),
    current_position=net_pos,
    forecast=forecast,
    bar_total_sec=3600,    # 1h bar
)
for a in actions:
    self._apply_entry_action(a)    # 策略层执行 submit/cancel/feishu

# on_tick (每 tick 驱动):
actions = self._entry.on_tick(
    now=datetime.now(),
    last_price=tick.last_price,
    bid1=self._pricer.bid1, ask1=self._pricer.ask1,
    tick_size=self._pricer.tick_size,
    vwap_value=self._rvwap.value,
    forecast=forecast, current_position=net_pos,
)

# on_trade:
claimed = self._entry.on_trade(oid, price, vol, now)  # 返回 bool

# on_stop_triggered (止损触发时):
stop_actions = self._entry.on_stop_triggered(datetime.now())
```

---

## 1. PythonGO API 正确用法

### 账户资金查询 (踩坑修复)
```python
# ❌ 崩溃写法
account = self.get_account_fund_data("")

# ✅ 正确写法: 先拿investor_id
investor = self.get_investor_data(1)
self._investor_id = investor.investor_id
account = self.get_account_fund_data(self._investor_id)

# AccountData属性:
# balance, available, position_profit, close_profit,
# margin, commission, risk, pre_balance, pre_available,
# dynamic_rights, frozen_margin, deposit, withdraw
```

### 持仓查询
```python
pos = self.get_position(instrument_id)
net_pos = pos.net_position  # 净持仓 (不区分策略/手动)
```

### 下单 (市价)
```python
# 开仓/加仓
oid = self.send_order(
    exchange=exchange, instrument_id=instrument_id,
    volume=vol, price=price,         # price仅显示用
    order_direction="buy", market=True,
)
if oid is not None:
    self.order_id.add(oid)

# 平仓/减仓
oid = self.auto_close_position(
    exchange=exchange, instrument_id=instrument_id,
    volume=vol, price=price,
    order_direction="sell", market=True,
)
```

### KLineGenerator
```python
# 必须在 super().on_start() 之前
self.kline_generator = KLineGenerator(
    callback=self.callback,
    real_time_callback=self.real_time_callback,
    exchange=..., instrument_id=..., style=...,
)
self.kline_generator.push_history_data()
super().on_start()
```

### import
```python
from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy          # ui版, 不是base版
from pythongo.utils import KLineGenerator
```

---

## 2. Next-bar规则

```python
def callback(self, kline):
    # 1. 撤挂单
    for oid in list(self.order_id):
        self.cancel_order(oid)

    # 2. 执行pending (执行后必须return!)
    if self._pending:
        self._execute(kline, self._pending)
        self._pending = None
        return  # ← 关键: 防止同bar重复信号

    # 3. 生成信号 → 存入self._pending
    # 4. 下一根bar开头执行
```

**为什么要return**: 执行pending后`get_position`可能还没更新，继续走信号逻辑会看到旧持仓，导致重复开仓。

---

## 3. 止损体系

### 优先级
```
权益止损 > 硬止损(价格) > 移动止损 > Portfolio Stops > 单日止损 > 正常信号
```

### 3.1 权益止损 (2%)
```python
if (net_pos > 0 and account and pos_profit < 0
        and abs(pos_profit) > equity * (equity_stop_pct / 100)):
    self._pending = "EQUITY_STOP"
```

### 3.2 硬止损 (价格)
```python
if (net_pos > 0 and avg_price > 0
        and close <= avg_price * (1 - hard_stop_pct / 100)):
    self._pending = "HARD_STOP"
```

### 3.3 移动止损
```python
# 持仓期间追踪peak_price
if close > self.peak_price:
    self.peak_price = close

if (net_pos > 0 and peak_price > 0
        and close <= peak_price * (1 - trailing_pct / 100)):
    self._pending = "TRAIL_STOP"
```

### 3.4 Portfolio Stops
```python
dd = (equity - peak_equity) / peak_equity

if dd <= -0.20:    # 熔断: 全平
    self._pending = "CIRCUIT"
elif dd <= -0.15:  # 减仓: 平半仓
    self._pending = "REDUCE"
elif dd <= -0.10:  # 预警: 只通知
    feishu("warning", ...)
```

### 3.5 单日止损
```python
daily_pnl = (equity - daily_start_eq) / daily_start_eq
if daily_pnl <= -0.05:
    self._pending = "DAILY_STOP"
```

---

## 4. 飞书非阻塞通知

```python
import threading, requests, time

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a6aeb603-..."

def _feishu_post(action, symbol, msg):
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": f"{label} | {symbol}"},
                       "template": color},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": msg}}],
        },
    }
    try:
        requests.post(FEISHU_WEBHOOK, json=payload, timeout=3)
    except Exception:
        pass

def feishu(action, symbol, msg):
    """非阻塞: daemon线程发送, 不影响交易."""
    threading.Thread(target=_feishu_post, args=(action, symbol, msg), daemon=True).start()
```

### 颜色映射
| action | 颜色 | 中文 |
|--------|------|------|
| open | green | 开仓 |
| add | blue | 加仓 |
| reduce | orange | 减仓 |
| close | red | 平仓 |
| hard_stop | carmine | 硬止损 |
| trail_stop | red | 移动止损 |
| equity_stop | carmine | 权益止损 |
| circuit | carmine | 熔断 |
| daily_stop | carmine | 单日止损 |
| start | turquoise | 策略启动 |
| shutdown | grey | 策略停止 |
| daily_review | purple | 每日回顾 |
| warning | yellow | 预警 |
| error | carmine | 异常 |

---

## 5. 状态持久化

```python
import os, json

STATE_DIR = "./state"

def save_state(data, name="StrategyName"):
    """原子写: temp → fsync → rename."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, f"{name}_state.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try: os.replace(path, path + ".bak")
            except OSError: pass
        os.replace(tmp, path)
    except Exception:
        pass  # 持久化失败不影响交易

def load_state(name="StrategyName"):
    """读主文件, 失败读备份."""
    for suffix in ("", ".bak"):
        path = os.path.join(STATE_DIR, f"{name}_state.json{suffix}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except: continue
    return None
```

### 保存内容
```python
save_state({
    "peak_equity": ...,
    "daily_start_eq": ...,
    "peak_price": ...,
    "avg_price": ...,
    "trading_day": ...,
    "net_pos": ...,
    "today_trades": [...],  # 最多50条
})
```

### 保存时机
- 每笔成交后
- 策略停止时 (on_stop)
- 交易日切换时

### 恢复 (on_start)
```python
saved = load_state()
if saved:
    self._peak_equity = saved.get("peak_equity", 0.0)
    self._daily_start_eq = saved.get("daily_start_eq", 0.0)
    # ...

# 持仓永远信任broker
actual = self.get_position(instrument_id).net_position
```

---

## 6. 交易日检测

```python
from datetime import datetime, timedelta

def get_trading_day():
    """当前时间+4小时 → 夜盘自动归下一交易日."""
    shifted = datetime.now() + timedelta(hours=4)
    wd = shifted.weekday()
    if wd == 5: shifted += timedelta(days=2)   # 周六→周一
    elif wd == 6: shifted += timedelta(days=1)  # 周日→周一
    return shifted.strftime("%Y%m%d")
```

### on_tick中检测
```python
td = get_trading_day()
if td != self._current_trading_day and self._current_trading_day:
    # 新交易日: 重置daily P&L
    self._daily_start_eq = account.balance
    self._today_trades = []
    self._daily_review_sent = False
```

---

## 7. Carver 10% Buffer

```python
BUFFER_FRACTION = 0.10
MIN_TRADE_SIZE = 1

def apply_buffer(optimal, current):
    """目标在buffer内不交易, 减少约50%交易次数."""
    buffer = max(abs(optimal) * BUFFER_FRACTION, 0.5)
    if (current - buffer) <= optimal <= (current + buffer):
        return current  # 不交易
    if optimal > current + buffer:
        return max(0, math.floor(optimal - buffer))
    else:
        return max(0, math.ceil(optimal + buffer))
```

---

## 8. Vol Targeting

```python
def calc_optimal_lots(forecast, atr_val, price, capital, max_lots):
    """
    optimal = (forecast/10) × (target_vol/realized_vol) × (capital/notional)
    """
    realized_vol = (atr_val * math.sqrt(ANNUAL_FACTOR)) / price
    vol_scalar = TARGET_VOL / realized_vol
    notional = price * MULTIPLIER
    raw = (forecast / 10.0) * vol_scalar * (capital / notional)
    return max(0.0, min(raw, float(max_lots)))
```

### ANNUAL_FACTOR
| 频率 | 值 |
|------|-----|
| M1 | 252 × 240 = 60480 |
| H1 | 252 × 4 = 1008 |
| D1 | 252 |

---

## 9. 每日回顾 (15:15收盘后推送)

```python
DAILY_REVIEW_HOUR = 15
DAILY_REVIEW_MINUTE = 15

# on_tick中检测
now = datetime.now()
if (not self._daily_review_sent
        and now.hour == DAILY_REVIEW_HOUR
        and DAILY_REVIEW_MINUTE <= now.minute < DAILY_REVIEW_MINUTE + 5):
    self._send_daily_review()
    self._daily_review_sent = True
```

### 内容
- 账户权益 + 峰值
- 昨日盈亏 (金额+百分比)
- 回撤百分比
- 当前持仓 + 均价
- 昨日操作表格 (时间/操作/手数/价格/持仓变化)

---

## 10. 保证金检查

```python
# 下单前检查
account = self._get_account()
if account:
    needed = price * MULTIPLIER * vol * 0.15  # 保守估算15%
    if needed > account.available * 0.6:      # 只用60%可用资金
        return  # 放弃下单
```

---

## 11. 重启恢复流程

```
on_start:
  1. KLineGenerator初始化 + push_history_data
  2. get_investor_data(1) → 缓存investor_id
  3. load_state() → 恢复peak_equity, daily_start_eq, avg_price, peak_price
  4. get_account_fund_data(investor_id) → 补充缺失的权益数据
  5. get_position(instrument_id) → 信任broker实际持仓
     - 有仓: 保留avg/peak (从JSON恢复)
     - 无仓: 清零avg/peak
  6. super().on_start()
  7. 飞书推送策略启动通知
```

---

## 12. 完整文件结构模板

```python
"""策略描述..."""
import math, os, json, time, threading
from datetime import datetime, timedelta
import numpy as np
import requests
from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy
from pythongo.utils import KLineGenerator

# CONFIG
# INDICATORS (纯numpy, 从QBase移植)
# SIGNAL (策略信号逻辑)
# POSITION SIZING (Vol Targeting + Carver Buffer)
# STATE PERSISTENCE (save_state / load_state)
# TRADING DAY (get_trading_day)
# FEISHU (非阻塞通知)
# Params / State (BaseParams / BaseState)
# Strategy (BaseStrategy)
#   on_start → on_tick → callback → _execute
#   止损检查在callback中, 执行在_execute中
#   所有执行走next-bar规则
```
