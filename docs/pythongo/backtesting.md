# pythongo.backtesting — 内置回测引擎

**源码**:
- `pythongo/backtesting/__init__.py` (3 行)— 设环境变量
- `pythongo/backtesting/engine.py` (734 行)— 引擎 + 回测 Frame + run 主函数
- `pythongo/backtesting/models.py` (504 行)— Config / Account / Tick / Order 等模型
- `pythongo/backtesting/fake_class.py` (160 行)— Mock INFINIGO
- `pythongo/backtesting/logger.py` (23 行)— 日志 wrapper
- `pythongo/backtesting/tools.py` (28 行)— tick zip 下载

⚠️ **不建议用此引擎回测期货策略**:
- `margin_rate=0.13` 对期权不适用
- 手续费公式对期权权利金会严重低估
- **不支持撤单**——限价单立即成交或拒单
- **严格对手盘撮合**——比实盘更保守

**我们策略的真实回测系统**是 QBase(独立 repo),不走这个。

---

## 环境变量副作用

```python
# __init__.py
import os
os.environ["PYTHONGO_MODE"] = "BACKTESTING"
```

`from pythongo.backtesting.*` 任何子模块会触发这个 `__init__.py`,把环境变量设成 BACKTESTING。

**`utils.is_backtesting()` 检测此变量**,决定:
- `KLineGenerator` 是否拉 snapshot + 启 scheduler
- `KLineContainer.init` 是否调 `get_kline_data`

---

## Config — 回测配置

```python
from pydantic import BaseModel
from pythongo.backtesting.models import Config

config = Config(
    access_key="...",       # 必填,拉历史 tick 数据的 API key
    access_secret="...",    # 必填
    fee_rate=0.0023,        # 手续费率(基于 price * multiple)
    fee_extra=0,            # 额外手续费
    margin_rate=0.13,       # 保证金率(期权不适用!)
    show_progress=False,    # 显示进度条
    save_order_details=False,  # 结束时保存 orders.csv
)
```

`cache_dir` 是 `@computed_field`:

```python
@computed_field
@property
def cache_dir(self) -> str:
    return os.path.join(os.getenv("APPDATA"), "PythonGO")
```

⚠️ **macOS/Linux 没有 `APPDATA` 环境变量**——`os.getenv("APPDATA")` 返回 None,`os.path.join(None, "PythonGO")` 会 TypeError。**macOS 无法用此回测**。

---

## Account — 回测账户模型

```python
class Account(BaseModel):
    initial_capital: int | float      # 初始资金
    available: float                   # 可用资金
    dynamic_rights: float = 0.0        # 动态权益
    fee: float = 0.0                   # 总手续费
    margin: float = 0.0                # 总保证金
    closed_profit: float = 0.0         # 已平仓盈亏
    position_profit: float = 0.0       # 持仓盈亏
    daily_profit: dict[str, float] = {}  # 合约日收益
```

计算属性:
- `rate = (dynamic_rights - initial_capital) / initial_capital`(总收益率)
- `total_profit = dynamic_rights - initial_capital`(总盈亏)

---

## Tick — 回测 Tick 数据

和 `classdef.TickData` **等价但不同**(pydantic 模型,alias 映射 PascalCase → snake_case):

```python
class Tick(BaseModel):
    exchange: str = Field("", alias="Exchange")
    instrument_id: str = Field("", alias="InstrumentID")
    last_price: float = Field(0.0, alias="LastPrice")
    # ... 完整盘口字段同 TickData
    timestamp: int = Field(0, alias="Timestamp")
    
    # v1 字段做兼容(2024-05-27 之前的数据)
    v1_natural_day: str = Field(alias="NaturalDay")
    v1_update_time: str = Field("", alias="UpdateTime")
    v1_update_millisec: str = Field("", alias="UpdateMillisec")
```

### `datetime` 计算属性

```python
@computed_field
@property
def datetime(self) -> datetime:
    if self.timestamp:
        return datetime.fromtimestamp(self.timestamp / 1000)
    return self.v1_datetime  # 2024-05-27 之前用 natural_day + update_time + millisec
```

### `last_volume` 运行时算 delta

和 `classdef.TickData` 同构——`_last_total_volume: ClassVar[dict[str, int]]` 跨策略共享。

---

## InstrumentData — 回测合约(**和 classdef 不同!**)

字段用 **snake_case**(不是 PascalCase):

```python
class InstrumentData(object):
    def __init__(self, data):
        self.exchange = data.get("exchange_code", "")
        self.product_id = data.get("product_code", "")
        self.instrument_id = data.get("instrument_code", "")
        self.instrument_name = data.get("instrument_name", "")
        self.instrument_type = data.get("instrument_type", "")  # ← 不是 product_type
        self.open_date = data.get("setup_date", "")
        self.expire_date = data.get("shutdown_date", "")
        self.price_tick = data.get("tick_price", 0.0)
        self.volume_multiple: int = data.get("volume_multiple", 0)
        self.upper_limit_price = data.get("upper_limit_price", 0.0)
        self.lower_limit_price = data.get("lower_limit_price", 0.0)
        self.underlying_symbol = data.get("underlying_code", "")
        self.max_market_order_size = data.get("max_market_order_volume", 0)
        self.min_market_order_size = data.get("min_market_order_volume", 0)
        self.max_limit_order_size = data.get("max_limit_order_volume", 0)
        self.min_limit_order_size = data.get("min_limit_order_volume", 0)
        self.options_type = {"1": "CALL", "2": "PUT"}.get(data.get("options_type", ""), None)
        self.strike_price = data.get("strike_price", 0.0)
        self.deliver_year = data.get("delivery_year", "")
        self.deliver_month = data.get("delivery_month", "")
        self.start_delivery_date = data.get("start_delivery_date", "")
        self.end_delivery_date = data.get("end_delivery_date", "")
        self.settle_price = data.get("settlement_price", 0.0)
```

### 和 `classdef.InstrumentData` 对照

| 回测 (snake_case) | 正常 (PascalCase 源) | 正常 (snake_case 属性) |
|-----------|-------------|-------------|
| `exchange_code` | Exchange | `exchange` |
| `instrument_code` | InstrumentID | `instrument_id` |
| `instrument_type` | ProductClass | `product_type`(中文) |
| `setup_date` | — | — |
| `shutdown_date` | ExpireDate | `expire_date` |
| `tick_price` | PriceTick | `price_tick` |
| `volume_multiple` | VolumeMultiple | `size` |
| `underlying_code` | UnderlyingInstrID | `underlying_symbol` |

**别混**:若在回测代码里误用 classdef 的字段访问会失败。

---

## OrderDetail — 回测持仓明细

```python
class OrderDetail(BaseModel):
    investor_id: str
    exchange: str
    instrument_id: str
    direction: str        # "0"=买, "1"=卖
    offset: str           # "0"=开, "1"=平, "3"=平今
    hedgeflag: str
    price: float
    volume: int
    trade_volume: int     # 成交数量
    cancel_volume: int = 0
    close_available: int
    fee: float
    margin: float
    closed_profit: float = 0.0
    position_profit: float = 0.0
    order_time: datetime
    trade_time: datetime
    trading_day: str
    closed: bool = False   # 全部平完后 True
    memo: str = ""
    order_sys_id: str
    orders: list[Order]    # 部分成交拆分
```

### `remaining_volume` 计算属性

```python
@computed_field
def remaining_volume(self) -> int:
    return self.volume - self.trade_volume
```

### `Order` — 单条拆单

```python
class Order(BaseModel):
    price: float
    volume: int
    fee: float
    margin: float
    closed: bool = False
```

一个 `OrderDetail` 可能包含多个 `Order`(部分成交历史)。平仓 **先开先平(FIFO)**。

---

## Engine — 回测引擎

**源码**:`pythongo/backtesting/engine.py` L18-523

### 核心方法

#### `make_order(**kwargs) -> int` — **核心撮合**

见价对手盘成交:
- **买单**:`price >= ask1` → 成交价按 `ask1`
- **卖单**:`price <= bid1` → 成交价按 `bid1`
- 不满足 → `return -1`

**开仓**(`offset="0"`):
```python
fee = (fee_rate * tick_price * volume_multiple + fee_extra) * volume
margin = margin_rate * tick_price * volume_multiple * volume

if (fee + margin) > account.available:
    return -1  # 资金不足

# 找现有 direction 相同 OrderDetail,合并;无则新建
# 扣 margin + fee,推 on_order + on_trade
```

**平仓**(`offset="1" / "3"`):
1. 找 direction 相反的 OrderDetail(没找到 → -1)
2. 检查 `close_available >= volume`(不够 → -1)
3. **先开先平**(遍历 `orders`,按 FIFO 消耗)
4. 计算 profit = `(tick_price - order.price) * volume_multiple * volume`
5. **卖方向利润反向**(空仓)
6. 按比例释放保证金
7. 全平时 `order_detail.closed = True`
8. 推 on_order + on_trade

#### `subscribe(exchange, instrument_id)` — 订阅 + 加载 tick 文件

```python
def subscribe(self, exchange, instrument_id):
    self.subscribe_instruments.append({...})
    self.handle_tick_file(exchange, instrument_id)
```

`handle_tick_file` 拉历史 tick 元数据 → 检查本地缓存 → 没有就下载 zip → 读 CSV → 转 Tick 对象 → 追加 `play_ticks`。

### 属性

| 属性 | 说明 |
|------|------|
| `account` | Account |
| `config` | Config |
| `strategy` | 策略实例(BacktestingFrame 设置) |
| `subscribe_instruments` | 已订阅合约 |
| `play_ticks` | `dict[instrument_id, list[Tick]]` |
| `current_ticks` | `dict[instrument_id, TickData]` 最新 tick |
| `order_queue` | 报单队列 |
| `order_details` | 实时持仓(含已平) |
| `instruments_data` | 合约信息 cache |
| `market_center` | MarketCenter(key=access_key, sn=access_secret) |

---

## BacktestingFrame — 回测编排

### 生命周期

```python
frame.init()  # FakeQtGuiSupport + strategy.on_init()
frame.start() # FakeWidget + strategy.on_start()
# async:
await frame.tick()
    # for tick in sorted_market_queue:
    #     engine.current_ticks[tick.instrument_id] = tick
    #     strategy.on_tick(tick.copy())   # ⭐ 传 copy 避免策略修改原数据
    # finish() 自动调
frame.stop()  # strategy.on_stop()
```

### `finish()` — 计算最终盈亏 + 打印

- 已平仓的 daily_pnl[trading_day] += closed_profit - fee
- 未平仓用最后 tick 的 last_price 估 position_profit
- `dynamic_rights = initial_capital + closed_profit + position_profit - fee`
- 调 `BacktestingResult.print()` 打印:
  - 初始资金 / 结束资金 / 总盈亏 / 总收益率
  - 总手续费 / 总成交额 / 总成交笔数 / 总交易日
  - **年化收益率**
  - 盈亏交易日分布
  - **夏普比率**(默认 242 交易日/年)
  - **最大回撤**

### 进度条

```python
if self.engine.config.show_progress:
    # ASCII 进度条
    print(f"\r进度 |{bar}| {percent:.3f}% 完成", end="\r")
```

---

## `run()` — 主入口

```python
from pythongo.backtesting.engine import run
from pythongo.backtesting.models import Config
from my_strategy import MyStrategy, MyParams

account = run(
    config=Config(access_key=..., access_secret=..., show_progress=True),
    strategy_cls=MyStrategy(),    # 实例化后的策略
    strategy_params=MyParams(exchange="DCE", instrument_id="i2505"),
    start_date="2024-01-01",
    end_date="2024-06-30",
    initial_capital=10_000_000,
)

# account 就是回测后的账户对象,可以读 total_profit / rate 等
```

### ⚠️ `initial_capital` 默认值怪癖

```python
initial_capital: int | float = 100_10000  # = 1,001,000
```

看起来像 typo(应该是 `10_000_000`)。**必须显式传入**。

### 内部流程

```python
def run(...):
    account = Account(initial_capital=..., available=...)
    engine = Engine(account, start_date, end_date, config)
    INFINIGO.engine = engine  # ⭐ 把引擎绑到 fake INFINIGO
    loop = asyncio.new_event_loop()
    frame = BacktestingFrame(engine, strategy_cls, strategy_params)
    frame.init()
    frame.start()
    try:
        loop.run_until_complete(frame.tick())
        return account
    except KeyboardInterrupt:
        logger.debug("退出回测")
        os._exit(0)
```

**注意**:`run()` 没显式调 `frame.stop()`,因为 `tick()` 结束会自动调 `finish()`。

---

## BacktestingResult — 回测结果

**源码**:`pythongo/backtesting/models.py` L377-504

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `account` | `Account` | 账户 |
| `turnover` | `int` | 总成交额 |
| `total_volume` | `int` | 总成交笔数 |
| `trading_days` | `list[str]` | 交易日序列 |
| `daily_pnl` | `defaultdict[str, float]` | 每日盈亏 |
| `trading_days_per_year` | `int = 242` | 年交易日 |
| `df` | `pd.DataFrame` | `prepare_data()` 后才填 |

### 计算方法

- `sharpe_ratio(risk_free_rate=0, annualize=True)` — 年化 = 日度 * √242
- `annual_return(start_value, end_value)` — 非标准年化(直接用交易日比例),不是复利
- `max_drawdown(initial_capital)` — 百分比

---

## Mock INFINIGO(`fake_class.py`)

### 关键:`Position.Direction` 字段用**中文 "多"/"空"**

```python
# fake_class.py L138-139
"Direction": "多" if order_details.direction == "0" else "空",
```

印证:`Position._direction_map = {"多": "long", "空": "short"}` 在回测里是这样来的。真实 INFINIGO 大概率也返回中文(Position 类的假设)。

### Mock 方法

| Mock 方法 | 行为 |
|----------|------|
| `writeLog(msg)` | `print(msg)`(走 stdout,不进 PythonGO 控制台) |
| `subMarketData / unsubMarketData` | 走 `engine.subscribe / unsubscribe` |
| `sendOrder(**kwargs)` | 走 `engine.make_order(**kwargs)` |
| `cancelOrder(OrderID)` | `return None`(**不支持撤单**) |
| `getInstrument` | 从 `engine.market_center.get_instrument_data` 拉 |
| `getInvestorList` | 返回固定 `[{"BrokerID":"0001","InvestorID":"0001","UserID":"0001"}]` |
| `getInvestorAccount` | 从 `engine.account` 构造 |
| `getInvestorPosition` | 遍历 `engine.order_details`(direction 转中文) |

---

## 为什么我们不用

1. **期权不适用**:`margin_rate=0.13` 错,手续费公式错
2. **不支持撤单**:我们的 executor/止损依赖撤单重挂,回测无法精确模拟
3. **严格对手盘撮合**:`price >= ask1` 才成交,比实盘保守
4. **macOS 不跑**:`cache_dir` 依赖 `APPDATA` 环境变量(Windows 专有)
5. **速率限制**:`market_center` 拉历史 tick 频繁调用会封 IP

**真正的回测系统**:QBase(独立 repo)—— 自己控制撮合逻辑、手续费、保证金,不依赖 pythongo 内置引擎。
