# PythonGO V2 API 参考

整理自官方文档 (https://infinitrader.quantdo.com.cn/pythongo_v2/) + pythongo 源码逐文件审计 (2026-04-20)。

按 PythonGO 原生模块组织。本目录是**代码的事实来源**——策略/模块与 PythonGO API 的对照,以这里为准。

## 文件组织

| 文件 | 内容 |
|------|------|
| [base.md](./base.md) | `pythongo.base` — BaseStrategy / BaseParams / BaseState / 回调 / 报单撤单 API + INFINIGO 低层附录 |
| [classdef.md](./classdef.md) | `pythongo.classdef` — 所有数据类 (Account / Position / Tick / Trade / Order / KLine / Instrument / Investor) |
| [utils.md](./utils.md) | `pythongo.utils` + `pythongo.core` — KLineGenerator / Scheduler / Indicators / MarketCenter |
| [ui.md](./ui.md) | `pythongo.ui` — 带 UI 的 BaseStrategy + KLWidget + drawer + crosshair |
| [options.md](./options.md) | `pythongo.option` — Option 定价(BSM/BAW/CRR)+ 希腊值 + OptionChain 期权链 |
| [backtesting.md](./backtesting.md) | `pythongo.backtesting` — 回测引擎(**不建议用,用 QBase**)|
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

**注意**:`from pythongo.ui import BaseStrategy`(带图表),**不是** `pythongo.base.BaseStrategy`。UI 版本重写了 `on_init / on_start / on_stop` 追加了 widget 逻辑。

## 环境说明

- pythongo 依赖 `INFINIGO`(Windows 专有 C++ 模块 `core.cp312-win_amd64.pyd`)+ `talib`(macOS 需 `brew install ta-lib`)
- macOS/Linux 上 `import pythongo` 会自动 fallback 到 `backtesting/fake_class.INFINIGO` mock,并把环境变量设 `PYTHONGO_MODE=BACKTESTING`
- **我们的 pytest 测试不 import pythongo**,只测我们 `src/modules/*`——所以 macOS 能跑测试

## 速率限制提醒 🚨

**`pythongo.core.MarketCenter` 的所有 `get_*` 方法**(`get_kline_data` / `get_dominant_list` / `get_*_trade_time` 等)受 **AI 风控限制**。

- 无具体次数上限,AI 判定是否封 IP
- **封禁是永久的**
- 规则:**不要在 `on_tick` / `on_bar` 循环里调用**,只在 `on_start` 拉一次 cache

---

# ✅ 已修复 Bug(2026-04-20,全部 **实盘验证完成**)

**修复状态总览**(见 `docs/SESSION_2026_04_20.md`):
- **Bug A**:30 文件 26 处 `order.volume` → `order.cancel_volume` ✓
- **Bug B**:30 文件 36 处 `"买" in str(trade.direction)` → 健壮模式 ✓
  - **实盘证据**(11:23 on 2026-04-20):`direction='0'`(买)/ `direction='1'`(卖),源码文档完全正确
- **market=True**:9 文件 25 处全改 `market=False` ✓(实盘报单显示"是否市价=否")
- **on_error 流控**:新增 `modules/error_handler.py` + 32 文件接入 `throttle_on_error` ✓
- **AL V8 startup eval 4 分支**:开/平/减/持 覆盖隔夜重启完整场景 ✓
- **A+ overnight fix 全队 propagate**:58 处 `on_day_change(balance)` → `on_day_change(balance, position_profit)` ✓
- **`_save()` null-guard**(commit `cba9b0b`,实盘 log 发现):31 文件加 `if self._risk is not None` 保护,避免 on_start 中途失败时 `_risk=None` 的 AttributeError

**实盘双轮验证**(2026-04-20):
- **第一轮 11:23**: Bug B direction 结案 + market=False 生效 + executor 状态机正常
- **第二轮 13:43-13:49 暂停恢复**: `bar_count 20→21` 续接 + `last_exit_bar_ts` 跨重启保留 + overnight fix `[DAY_CHANGE_TEST]` 双参数生效

**验证**:`src/test/TestAllFixes.py` 8 项 smoke test + pytest 154/154 绿 ✓

**15 commits**:`3a262d0` → `2e49cf5`

---

## Bug A:`on_order_cancel` 访问不存在的 `.volume` 字段 — **已修复 ✓**

**源码证据**:`pythongo/classdef/order.py`

```python
class BaseOrderData(object):
    # L19: 只有 cancel_volume
    self._cancel_volume = data.get("CancelVolume", 0)

class OrderData(BaseOrderData):
    # L150-152: 只加了 total_volume, traded_volume, status
    self._total_volume = data.get("TotalVolume", 0)
    self._traded_volume = data.get("TradedVolume", 0)
    self._status = data.get("Status", "")
```

**`OrderData` 没有 `.volume` 属性**。我们策略代码:

```python
# AL V8 L1504 (30 个文件同构)
def on_order_cancel(self, order: OrderData):
    if self._vwap_active and order.volume > 0:  # ← AttributeError!
```

**历史上可能没爆**:`pythongo/base.py` L246-249 `on_order_cancel` 是由 `on_order` 内部根据 `order.status == "已撤销"` 手动分发的——若订单成交正常没触发撤单,这条路径就不跑。

**修法**:`order.volume` → `order.cancel_volume`(legacy);或迁移到 `on_cancel(CancelOrderData)` 新回调。

## Bug B:`trade.direction` 识别错误(30 个文件) — **已修复 ✓**

**源码证据**:`pythongo/classdef/trade.py` L82-84

```python
@property
def direction(self) -> TypeOrderDirection:  # ← types.py L70
    """买卖方向"""
    return self._direction
```

**`TypeOrderDirection = Literal["0", "1"]`**(不是中文,不是 "buy"/"sell"):
- `"0"` = 买
- `"1"` = 卖

我们代码:

```python
direction = "buy" if "买" in str(trade.direction) else "sell"
# "买" in "0" = False → direction = "sell"
# "买" in "1" = False → direction = "sell"
# 所有成交恒为 "sell"
```

**影响**:
- `SlippageTracker.on_fill(..., direction="sell")` 方向参数恒定错
- `on_trade` 里 `if direction == "buy" and actual > 0: avg_price 更新` 分支**从不运行**——幸好 `_execute` 已用 `kline.close` 设 avg_price,策略功能不垮,但 avg_price 从未被真实 fill 价 refine

**健壮修法(已应用,cover 所有可能)**:
```python
# 当前全队 pattern (commit f3cd0d4):
("buy" if str(trade.direction).lower() in ("buy", "0", "买") else "sell")
```

**AL V8 额外加 DIAG 打印** `[BUG_B_DIAG] direction={trade.direction!r}`,首次实盘成交后看真实值。

---

# 🟢 其他源码级重要发现(可能影响设计)

## 1. `get_position` 是**昂贵操作**,每次都 sync

**源码**:`pythongo/base.py` L406-428

```python
def get_position(self, instrument_id, hedgeflag="1", investor=None, simple=False) -> Position:
    self.sync_position(simple)  # ⭐ 每次调用都全量同步!
    ...

def sync_position(self, simple=False):
    for investor in infini.get_investor_list():
        investor_position = infini.get_investor_position(investor_id, simple)
        # ... 重构 self._position 字典
```

**每次 `get_position` 都遍历所有投资者 + 调 `infini.get_investor_position`**。我们策略 `_on_tick_stops` L492 **每 tick 调 `get_position`**——可能是性能瓶颈。

**建议优化**:未来把 `get_position` cache 到 bar 级,tick 级用 cached。

## 2. `make_order_req` 在 `self.trading=False` 时**静默返回 None**

**源码**:`pythongo/base.py` L636-637

```python
def make_order_req(...) -> int | None:
    if self.trading is False:
        return  # ⭐ 静默 return None,不报错不报单!
```

我们代码 `if oid is not None` 检查对了——策略暂停或错误流控期间发单会静默失败。

## 3. `on_order` 根据**中文 status** 分发 `on_order_cancel` / `on_order_trade`

**源码**:`pythongo/base.py` L235-249

```python
def on_order(self, order: OrderData) -> None:
    if order.status == "已撤销":          # ⭐ 中文匹配!
        self.on_order_cancel(order)
    elif order.status in ["全部成交", "部成部撤"]:
        self.on_order_trade(order)
```

**关键**:
- `on_order_cancel` 不是 PythonGO 底层直接触发的,是 `on_order` 里**根据中文 status 手动分发**
- 我们策略 override `on_order` **必须调 `super().on_order(order)`**(否则 `on_order_cancel` / `on_order_trade` 不会触发)——AL V8 L1497 有调,对 ✓
- `TypeOrderStatus`(types.py L51-67)有 **15 个中文字符串**

## 4. `on_error` 的**自动流控**我们没继承

**源码**:`pythongo/base.py` L278-297

```python
def on_error(self, error: dict[str, str]) -> None:
    self.trading = False  # ⭐ 任何错误都停止交易!
    if error["errCode"] == "0004":
        Timer(self.limit_time, limit_contorl).start()  # 2 秒后自动恢复
    else:
        self.output(error)  # 非 0004 → trading 永远 False,需手动恢复!
```

我们策略 override 了 `on_error` **但没调 super**:

```python
def on_error(self, error):
    self.output(f"[错误] {error}")
    feishu("error", ...)
```

**后果**:
- ✅ 非 0004 错误不会永久 freeze 策略
- ❌ 0004 连续撤单错误**没有流控**,可能刷屏 / 拒单

**需要决定**:是否加回 super 的流控逻辑,或自己实现 0004 冷却。

## 5. `Scheduler("PythonGO")` 是**全局单例**,`on_stop` 会清空所有 jobs

**源码**:`pythongo/utils.py` L38-52 (单例) + L119-127 (stop)

```python
class Scheduler:
    _cache_instance: dict[str, "Scheduler"] = {}  # 单例缓存

    def stop(self):
        if self.scheduler_name in self._cache_instance:
            for job in self.get_jobs():
                job.remove()  # ⭐ 只清 jobs,不真 shutdown
            return
```

`KLineGenerator` 内部用 `Scheduler("PythonGO")`,**每个 KLineGenerator 实例共享同一 scheduler**。`on_stop` 会清这个共享 scheduler 的所有 jobs。

**如果我们用 `Scheduler("PythonGO")` 注册自己的 job,会被 `on_stop` 一起清**。要独立必须用**不同 scheduler_name**。

## 6. `producer.close/high/low` 前 10 个是**假数据**

**源码**:`pythongo/utils.py` L760-769

```python
class KLineProducer(Indicators):
    def __init__(self, ...):
        self._open = np.zeros(10)          # 10 个 0
        self._close = np.zeros(10)
        self._datetime = np.arange(
            start="1999-11-20 00", stop="1999-11-20 10",
            dtype="datetime64[h]",          # 1999 年假时间戳
        )
```

**历史数据灌完后真实数据从索引 10 开始**。AL V8 `WARMUP=80` 能自动跳过;如果其他策略 WARMUP 小,需注意。

## 7. `real_time_callback` 每 tick 触发,`producer.close[-1]` 可能是**未完成 bar**

**源码**:`pythongo/utils.py` L629-632

```python
if callable(self.real_time_callback) and self.next_gen_time:
    self.producer.update(self._cache_kline)  # ⭐ 每 tick 把未完成 K 写入 producer
    self.real_time_callback(self._cache_kline)
```

**时序**:
- `callback`(我们的 `_on_bar`)触发时:K 线已完成,`producer.close[-1]` 是完整 close(准确)
- `real_time_callback`(我们的 `_push_widget`)触发时:`producer.close[-1]` 已被当前未完成 tick 覆盖

我们策略 `_on_bar` 里读 `producer.close[-1]` 是**对的**(bar 完成时触发)。**不要在 `real_time_callback` 里用 `producer.close[-1]` 做信号计算**。

## 8. `talib` 的 ADX / Donchian / ATR 和我们手写版本**不等价**

**源码**:`pythongo/indicator.py`

- `talib.ADX` 只返回 ADX 标量,**不返 PDI/MDI**(AL V8 用 PDI>MDI 方向确认必须手写 `_adx_with_di`)
- `talib.SMA` 版 ATR(L198),**不是 Wilder RMA**(我们手写 Wilder,和 QBase 匹配)
- `talib` 版 `donchian` 只返 `(upper, lower)`,**不返 mid**(我们用 mid 算 penetration)

**所以我们所有手写指标都是必要的**。CLAUDE.md 「从 QBase 原样移植避免信号偏差」的说法 100% 正确。

## 9. `TickData.last_volume` 是**运行时计算**的 delta,不是 broker 字段

**源码**:`pythongo/classdef/tick.py` L12-60

```python
class TickData:
    _last_total_volume: ClassVar[dict[str, int]] = {}  # 类级 state

    def __init__(self, data):
        previous = self._last_total_volume.get(instrument_id)
        self._last_volume = 0 if previous is None else self._volume - previous
        self._last_total_volume[instrument_id] = self._volume
```

首 tick 的 `last_volume=0`,之后是 `volume - last_volume`。**跨策略、跨实例共享** class-level dict(多策略同合约时无锁竞争,但 dict 原子赋值,大概率无问题)。

## 10. `AccountData.risk` 是**动态计算**,可能 ZeroDivisionError

**源码**:`pythongo/classdef/account.py` L104-106

```python
@property
def risk(self) -> float:
    """风险度"""
    return self._margin / self._dynamic_rights  # ⚠️ dynamic_rights=0 会炸
```

策略没直接用 `acct.risk`,不 urgent。但若将来用要加保护。

## 11. `Position._direction_map` 中文键

**源码**:`pythongo/classdef/position.py` L9

```python
class Position(object):
    _direction_map = {"多": "long", "空": "short"}  # INFINIGO 底层 Direction 字段是中文
```

**区分**:
- `Position` 底层 dict 的 `Direction` 字段 = **中文 "多"/"空"**(INFINIGO 层)
- `TradeData.direction` 和 `OrderData.direction` = **数字字符串 "0"/"1"**(types.py `TypeOrderDirection`)

两个不同的 convention,注意别混。

## 12. 期权定价 + 期权链(为将要上的期权策略)

**Option 类**(`option.py` L25-555):
- **3 种定价模型**:BSM 欧式 / BAW 美式近似 / CRR 二叉树美式
- **完整希腊值**:delta / gamma / vega / theta / rho(3 套实现,仅 BSM 额外有 vanna + rho_q)
- **`bs_iv()` 二分法算 IV**,fallback `sigma_default=0.8`
- **`@calculate_once` 装饰器**:CRR / BAW 希腊值首次调用才跑基础模拟,后续缓存
- ⚠️ **`market_price` 构造时被贴现一次** (`/ self.disc`)——外部别再贴现
- ⚠️ **CRR n=1000 节点**,第一次算慢(几十~几百 ms),之后快

**OptionChain 类**(`option.py` L568-793):
- 初始化调 `infini.get_instruments_by_product`(**速率限制敏感**,`on_start` 建一次)
- 数据结构:`{underlying: {expire_date: {strike_prices, call_options, put_options}}}`
- `get_atm_option` **返回行权价列表索引,不是合约代码**
- ETF 期权多到期日,期货期权每月一到期
- `get_call_options / get_put_options` 返回**合约代码 str 列表**(不是 InstrumentData)

详见 [options.md](./options.md)。

## 13. pythongo 内置回测引擎**不建议用**

- `margin_rate=0.13` 对期权错(权利金 vs 保证金逻辑不同)
- **不支持撤单**(`fake_class.INFINIGO.cancelOrder` 只 `return None`)
- **严格对手盘撮合**(`price >= ask1` 才成交),比实盘保守
- **macOS 不跑**(`cache_dir` 依赖 `APPDATA` 环境变量)
- `initial_capital` 默认 `100_10000` 看起来像 typo(1,001,000 而不是 10,000,000)

真正的回测用 QBase。详见 [backtesting.md](./backtesting.md)。

## 14. `auto_close_position` SHFE/INE **一次调用可能发两单**

**源码**:`pythongo/base.py` L492-601

- `shfe_close_first=True` 先平昨(offset="1"),再平今(offset="3")
- 非 SHFE/INE 交易所统一 offset="1",broker 自己决定
- **一次调用可能触发 2 个 `send_order`,只返回最后一个 `order_id`**——这意味着我们 `self.order_id.add(oid)` 会丢失前一个 oid,可能导致跟踪失效(目前我们的 AL 长头是 DCE,没这个问题;但 CU/AL 等 SHFE 策略要留意)

---

# 🟡 Tier 2 未来优化(利用新发现的 API)

| 现状 | 文档给出的更好方案 | 源码位置 |
|------|-----------------|---------|
| `contract_info.py` 硬编码 78 品种乘数/tick | `get_instrument_data(exch, inst).size / price_tick` | `classdef/instrument.py` |
| 涨跌停不检测 | `TickData.upper_limit_price / lower_limit_price` 每 tick 免费可用 | `classdef/tick.py` |
| `SessionGuard` 硬编码时段 | `get_product_trade_time()` / `get_avl_close_time()` | `core.pyi` L130-150 |
| `DailyReporter` 手搓 `on_tick` 时间判断 | `Scheduler("DailyReport").add_job(trigger="cron", ...)` | `utils.py` L66-87 |
| `_current_bar_ts` 用 `time.time()` | `producer.datetime[-1].timestamp()` | `utils.py` L833 |
| 多策略共账号无法区分成交归属 | `send_order(memo=f"{strategy}:{action}")` + `trade.memo` | `base.py` L441 |
| 回调用 `on_order_cancel` (旧) | v2025.0801+ 迁移到 `on_cancel(CancelOrderData)` | `base.py` L251 |
| `get_position` 每 tick 调全量 sync | 在 bar 级 cache,tick 级用 cached | `base.py` L406 |
| `on_error` 没继承 super 流控 | 调 `super().on_error(error)` 或自己实现 0004 流控 | `base.py` L278 |

---

# 🟢 字段 / API 全量对照(已源码确认对齐)

| 用法 | 我们代码 | 源码确认 |
|------|---------|---------|
| `send_order(order_direction="buy")` | AL V8 `_execute` | base.py L459 `.upper()` → `OrderDirectionEnum["BUY"].flag` = "0" ✓ |
| `cancel_order(oid)` | 多处 | base.py L479,返回 0/-1,不检查安全 ✓ |
| `get_position(inst)` 默认 `hedgeflag="1"` | AL V8 | base.py L406 ✓ |
| `pos.net_position` | AL V8 | position.py L64 `long.position - short.position`,做空返负 ✓ |
| `acct.balance / available / position_profit` | AL V8 | account.py 全部存在 ✓ |
| `KLineGenerator(exchange, instrument_id, callback, style, real_time_callback)` | AL V8 | utils.py L256 参数对齐 ✓ |
| `producer.close / high / low / volume / datetime` | AL V8 | utils.py L789-840 ✓ |
| `producer.open_interest` | V26 / V14 需手动收集 | utils.py L760-769 **不存在** ✓ CLAUDE.md 记录 |
| `widget.recv_kline({...})` | AL V8 `_push_widget` | widget.py L181 ✓ |
| `main_indicator_data` 用 `@property` | AL V8 | widget.py L33-40 ✓ |
