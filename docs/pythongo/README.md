# PythonGO V2 API 参考

整理自 https://infinitrader.quantdo.com.cn/pythongo_v2/ (2026-04-20)

按 PythonGO 原生模块组织。本目录是**代码的事实来源**——策略/模块与 PythonGO API 的对照,以这里为准。

## 文件组织

| 文件 | 内容 |
|------|------|
| [base.md](./base.md) | `pythongo.base` — BaseStrategy / BaseParams / BaseState / 回调 / 报单撤单 API + INFINIGO 低层附录 |
| [classdef.md](./classdef.md) | `pythongo.classdef` — 所有数据类 (Account / Position / Tick / Trade / Order / KLine / Instrument / Investor) |
| [utils.md](./utils.md) | `pythongo.utils` + `pythongo.core` — KLineGenerator / Scheduler / Indicators / MarketCenter |
| [ui.md](./ui.md) | `pythongo.ui` — 带 UI 的 BaseStrategy + KLWidget |
| [types.md](./types.md) | 所有 Type 别名 + 数值映射 (Direction / Offset / Hedge / OrderFlag / OrderStatus / ProductClass) |

## 导入约定

```python
# 策略入口 (必须用 ui 的,有图表支持)
from pythongo.ui import BaseStrategy
from pythongo.base import BaseParams, BaseState, Field

# 数据类
from pythongo.classdef import (
    TickData, KLineData, TradeData, OrderData,
    CancelOrderData, Position, AccountData,
    InstrumentData, InstrumentStatus, InvestorData,
)

# 工具类
from pythongo.utils import KLineGenerator, Scheduler
```

**注意**:CLAUDE.md 历史注意事项:`from pythongo.ui import BaseStrategy`(带图表),**不是** `pythongo.base.BaseStrategy`。

## 速率限制提醒 🚨

**`pythongo.core.MarketCenter` 的所有 `get_*` 方法**(`get_kline_data` / `get_dominant_list` / `get_*_trade_time` 等)受 **AI 风控限制**。

- 无具体次数上限,AI 判定是否封 IP
- **封禁是永久的**
- 规则:**不要在 `on_tick` / `on_bar` 循环里调用**,只在 `on_start` 拉一次 cache

## ❌ 已发现的 Bug (需修)

### Bug A: `on_order_cancel` 访问不存在的 `.volume` 字段

`OrderData` **没有** `.volume` 属性,只有 `total_volume / traded_volume / cancel_volume`。

```python
# ❌ 现状 (30 个文件):
if self._vwap_active and order.volume > 0:  # AttributeError
    ...

# ✅ 应改为:
cancelled = getattr(order, "cancel_volume", 0)
if self._vwap_active and cancelled > 0:
    ...
```

**历史上可能没爆**:因 v2025.0801+ `on_order_cancel` 被 `on_cancel(CancelOrderData)` 替代,旧回调从不触发。

### Bug B: `trade.direction` 识别错误 (30 个文件)

```python
# ❌ 现状:
direction = "buy" if "买" in str(trade.direction) else "sell"
```

文档明确 `TypeOrderDIR = Literal["buy", "sell"]`(英文),mapping 页又说是 `"0"/"1"`——**不管哪个,都不是中文「买」「卖」**,所以 `"买" in str(...)` 恒为 False,**所有 buy 成交被记为 sell**。

```python
# ✅ 健壮修法(cover 所有可能):
raw = str(trade.direction).lower()
direction = "buy" if raw in ("buy", "0", "买") else "sell"
```

**先实盘 DIAG 打印确认实际值,再批量修**。

## 🟡 Tier 2 未来优化(文档发现的可用 API)

| 现状 | 文档给出的更好方案 |
|------|-----------------|
| `contract_info.py` 硬编码 78 品种乘数/tick | `get_instrument_data(exch, inst).size / price_tick` 动态拿 |
| 涨跌停不检测(Tier 2 #6) | `TickData.upper_limit_price / lower_limit_price` 每 tick 免费可用 |
| `SessionGuard` 硬编码时段 | `get_product_trade_time()` / `get_avl_close_time()` 动态拉 |
| `DailyReporter` 手搓 `on_tick` 时间判断 | `Scheduler().add_job(cron)` 定时任务 |
| `_current_bar_ts` 用 `time.time()` 对齐 | `producer.datetime[-1].timestamp()` 取真实 K 线时间 |
| 多策略共账号无法区分成交归属 | `send_order(memo=f"{strategy}:{action}")` + `trade.memo` |
| 回调用 `on_order_cancel` (旧) | v2025.0801+ 迁移到 `on_cancel(CancelOrderData)` |

## 🟢 字段 / API 全量对照(已确认对齐)

| 用法 | 我们代码 | 文档 | 状态 |
|------|---------|------|------|
| `send_order(order_direction="buy")` | AL V8 `_execute` | TypeOrderDIR = `Literal["buy", "sell"]` | ✓ |
| `cancel_order(oid)` | 多处 | 返回 0/-1 | ✓ 不检查返回值安全 |
| `get_position(inst)` 默认 `hedgeflag="1"` | AL V8 | ✓ | ✓ |
| `pos.net_position` | AL V8 | 文档无符号说明,CLAUDE.md 实盘验证做空返负 | ✓ |
| `acct.balance / available / position_profit` | AL V8 | 完整存在 | ✓(包括我新增的 `position_profit`) |
| `KLineGenerator(exchange, instrument_id, callback, style, real_time_callback)` | AL V8 | 参数对齐 | ✓ |
| `producer.close / high / low / volume / datetime` | AL V8 | ✓ | ✓ |
| `producer.open_interest` | V26 / V14 | **不存在**,需手动从 `kline.open_interest` 收集 | ✓ CLAUDE.md 已记录 |
| `widget.recv_kline({...})` | AL V8 `_push_widget` | ✓ | ✓ |
| `main_indicator_data` 用 `@property` | AL V8 | 文档说"不能直接赋值,应自行定义" | ✓ |
