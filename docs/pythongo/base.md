# pythongo.base — 策略基础模块

**源码**:`pythongo/base.py`(720 行)

## BaseParams — 参数映射模型

```python
class BaseParams(BaseModel, validate_assignment=True):
    exchange: str = Field(default="", title="交易所代码")
    instrument_id: str = Field(default="", title="合约代码")
```

基于 Pydantic `BaseModel`,**`validate_assignment=True`**——赋值时自动验证类型。

- 默认有 `exchange` 和 `instrument_id`(固定),继承时可覆盖
- 映射关系展示在「PythonGO 窗口 - 参数」,可界面编辑
- `title` 参数作为界面显示的中文名

## BaseState — 状态映射模型

```python
class BaseState(BaseModel, validate_assignment=True):
    ...
```

- 展示在「PythonGO 窗口 - 状态」栏
- **仅供显示,无法编辑**
- `update_status_bar()` 把 `state_map` 的字段(用 `title` 作 key)推到界面

## BaseStrategy — 策略模板

所有策略的父类。

### 属性

| 属性 | 类型 | 默认 | 说明 |
|------|------|-----|------|
| `strategy_id` | `int` | 0 | 策略实例 ID,无限易自动赋值 |
| `strategy_name` | `str` | `""` | 实例名称,由创建时填入 |
| `params_map` | `BaseParams` | `BaseParams()` | 参数映射 |
| `state_map` | `BaseState` | `BaseState()` | 状态映射 |
| `limit_time` | `int` | **2** | 错单限制时间(秒) |
| `trading` | `bool` | `False` | **是否允许交易**,False 时 `make_order_req` 静默返回 None |
| `class_name` | `str` | `@property` | `self.__class__.__name__` |
| `exchange_list` | `list[str]` | `@property` | `params_map.exchange.split(";")` |
| `instrument_list` | `list[str]` | `@property` | `params_map.instrument_id.split(";")` |
| `instance_file` | `str` | `@property` | `{pythongo parent}/instance_files/{strategy_name}.json` |

### 系统方法(无限易自调,一般不用)

- `get_params() -> list[dict[str, str]]` — 把 params_map 转成界面格式
- `set_params(data: dict[str, str])` — 界面改参数时调
- `save_instance_file()` — 暂停时自动保存 params 到 JSON
- `load_instance_file()` — `on_init` 自动加载

### 生命周期回调

| 方法 | 说明 |
|------|------|
| `on_init()` | 创建实例/加载实例。**调 `load_instance_file` + `update_status_bar` + `output("策略初始化完毕")`** |
| `on_start()` | 点击运行。**`self.trading=True` + `sub_market_data()` + `update_status_bar`** |
| `on_stop()` | 点击暂停。**`self.trading=False` + `Scheduler("PythonGO").stop()` + `save_instance_file()` + `unsub_market_data()`** |

⚠️ **`on_stop` 会停全局 `Scheduler("PythonGO")` 单例**(清空所有 jobs)。我们用自己的 Scheduler 必须用**不同的 `scheduler_name`**,否则会被清。

### 回调方法(策略层 override)

#### `on_tick(tick: TickData)` — 行情推送

默认空实现,策略 override。

#### `on_contract_status(status: InstrumentStatus)` — 合约状态

默认空实现,若需要精细化时段管理可 override。

#### `on_cancel(order: CancelOrderData)` — **v2025.0801+ 新回调**

默认空实现,替代旧的 `on_order_cancel`。

#### `on_order(order: OrderData)` — 报单变化

**默认实现根据 `order.status` 分发**(源码 L245-249):

```python
def on_order(self, order: OrderData) -> None:
    if order.status == "已撤销":
        self.on_order_cancel(order)
    elif order.status in ["全部成交", "部成部撤"]:
        self.on_order_trade(order)
```

⚠️ **策略 override `on_order` 时必须调 `super().on_order(order)`**,否则 `on_order_cancel` / `on_order_trade` 不会触发。

#### `on_order_cancel(order: OrderData)` — 撤单(**旧回调**)

**不是 PythonGO 底层直接触发**,是 `on_order` 根据中文 status 手动分发。CLAUDE.md/feishu 等文档说"未来弃用"——建议迁移到 `on_cancel(CancelOrderData)`。

#### `on_order_trade(order: OrderData)` — 成交(**内部用**)

官方推荐:**用 `on_trade` 而非 `on_order_trade`**(文档原文:"最好使用 on_trade")。

#### `on_trade(trade: TradeData, log: bool = False)` — 成交推送

默认实现(源码 L259-276):

```python
def on_trade(self, trade: TradeData, log: bool = False) -> None:
    if log:
        self.output(
            f"[成交回调] 合约: {trade.instrument_id}, "
            f"方向: {trade.direction} {trade.offset}, "  # ⭐ 直接打印 "0"/"1"
            f"价格: {trade.price}, 手数: {trade.volume}, "
            f"时间: {trade.trade_time}"
        )
```

**log 默认 False**,只有显式传 `log=True` 才输出。

#### `on_error(error: dict[str, str])` — 错误推送(**有自动流控!**)

**默认实现(源码 L278-297)**:

```python
def on_error(self, error: dict[str, str]) -> None:
    self.trading = False  # ⚠️ 任何错误都停止交易!

    def limit_contorl():
        self.trading = True
        self.output("错单流控已关闭")

    if error["errCode"] == "0004":  # 撤单错误
        self.output(f"错单流控开启，{self.limit_time} 秒后关闭，错单原因：{error['errMsg']}")
        Timer(self.limit_time, limit_contorl).start()
    else:
        self.output(error)  # ⚠️ 非 0004 → trading 永远 False,需手动恢复!
```

**`error` 字典可能有**:
- `errCode: str` — 错误代码
- `errMsg: str` — 错误信息
- `orderID: str` — 有此 key 说明是报单错误,无则是函数运行错误

**`errCode == "0004"`** = 撤单错误,自动流控(限定时间后恢复)。

**我们策略 override `on_error` 没调 super**,导致:
- ✅ 非 0004 错误不会永久 freeze 策略(避免意外停机)
- ❌ 0004 连续错误无流控,可能刷屏

### 方法(业务相关)

#### `output(*msg: Any)` — 输出日志

```python
# 源码 L303-313
def output(self, *msg: Any) -> None:
    log_time = datetime.datetime.now().replace(microsecond=0)
    infini.write_log(f"[{log_time}] [{self.strategy_name}] {' '.join(map(str, msg))}")
```

格式:`[YYYY-MM-DD HH:MM:SS] [strategy_name] msg1 msg2 ...`

#### `update_status_bar()` — 更新状态栏

把 `state_map` 每个字段(以中文 `title` 作 key,值 str 化)推到 INFINIGO。

#### `pause_strategy()` — 暂停策略

等同点击暂停。**⚠️ 不要在 `on_start` / `on_stop` 里调,死循环!**

#### `sub_market_data(exchange=None, instrument_id=None)` / `unsub_market_data(...)`

- 都传 → 订阅/取消该合约
- 都不传 → 默认用 `exchange_list` + `instrument_list`(自动分 `;`)zip 订阅所有

```python
# 源码 L315-326
if exchange and instrument_id:
    infini.sub_market_data(...)
    return
for exchange, instrument_id in zip(self.exchange_list, self.instrument_list):
    infini.sub_market_data(...)
```

#### `sync_position(simple=False)` — 同步持仓(**`get_position` 自动调**)

```python
# 源码 L371-404
def sync_position(self, simple=False):
    self._position = {}  # 每次重建
    for investor in infini.get_investor_list():
        investor_id = investor["InvestorID"]
        investor_position = infini.get_investor_position(investor_id, simple)
        # 重构为 {investor: {instrument_id: {hedgeflag: Position}}}
```

**遍历所有投资者 + 每个都调 `infini.get_investor_position`**——这是潜在的性能热点。

#### `get_position(instrument_id, hedgeflag="1", investor=None, simple=False) -> Position`

```python
# 源码 L406-428
def get_position(self, ...):
    self.sync_position(simple)  # ⭐ 每次调用都全量 sync!
    if not investor:
        investor = self.get_investor_data().investor_id
    return self._position.get(investor, {}).get(instrument_id, {}).get(hedgeflag, Position())
```

**⚠️ 性能警告**:**每次 `get_position` 都调 `sync_position`**,遍历所有投资者 + 所有持仓。

**我们策略 `_on_tick_stops` 每 tick 调 `get_position(p.instrument_id)`**——瓶颈点。

**未找到时返回空 `Position()`**(`pos.long.position=0, pos.short.position=0, pos.net_position=0`),不是 None。

#### `get_account_fund_data(investor) -> AccountData`

```python
# 源码 L654-662
def get_account_fund_data(self, investor: str) -> AccountData:
    return AccountData(infini.get_investor_account(investor))
```

⚠️ **`investor` 必须非空**!传 `""` 会崩溃(CLAUDE.md 记录)——先调 `get_investor_data(1).investor_id`。

#### `get_investor_data(index=1) -> InvestorData`

```python
# 源码 L707-719
def get_investor_data(self, index=1):
    investor_list = infini.get_investor_list()
    index = 0 if index > len(investor_list) else index - 1  # 超出范围回退到 0
    return InvestorData(investor_list[index])
```

**1-based index**,超出范围自动用第一个。

#### `get_instrument_data(exchange, instrument_id) -> InstrumentData`

```python
# 源码 L664-673
def get_instrument_data(self, exchange, instrument_id):
    return InstrumentData(infini.get_instrument(exchange, instrument_id))
```

**包含合约乘数(size)+ 最小变动价(price_tick)+ 涨跌停价**。未来可用来交叉验证 `contract_info.py` 硬编码。

#### `get_instruments_by_product(exchange, product_id, raw_data=True) -> TypeInstResult`

查询品种下所有合约。`raw_data=False` 只返合约代码 str。

## 报单 / 撤单

### `send_order(...)` — 报单(固定开仓)

```python
# 源码 L430-477
def send_order(
    self, exchange, instrument_id, volume, price,
    order_direction: TypeOrderDIR,         # "buy" / "sell"
    order_type: TypeOrderFlag = "GFD",
    investor: str = "",
    hedgeflag: TypeHedgeFlag = "1",
    market: bool = False,
    memo: Any = None,
) -> int | None:
    order_direction = order_direction.upper()  # "buy" → "BUY"
    if order_direction not in OrderDirectionEnum.__members__:
        self.output(f"[报单函数] 报单方向 {order_direction} 错误")
        return                                # ⚠️ 返回 None
    return self.make_order_req(
        ..., offset=OrderOffsetEnum.OPEN.value,  # "0" 固定开仓
        ...
    )
```

- `order_direction` 接受 `"buy"` / `"sell"`(不分大小写)
- **固定 `offset="0"`**——只能开仓,不能平仓
- **返回值**:
  - `int` = 报单编号
  - `-1` = 报单失败(INFINIGO 层返回)
  - `None` = 方向错误 或 `self.trading=False`

### `auto_close_position(...)` — 自动平仓(**默认平今优先**)

```python
# 源码 L492-601
def auto_close_position(
    self, exchange, instrument_id, volume, price,
    order_direction: TypeOrderDIR,
    order_type: TypeOrderFlag = "GFD",
    investor: str = "",
    hedgeflag: TypeHedgeFlag = "1",
    shfe_close_first: bool = False,        # SHFE / INE 平昨优先
    market: bool = False,
    memo: Any = None,
) -> int | None:
```

**逻辑**:

1. 用 `OrderDirectionEnum[order_direction].match_direction` 找对应持仓(买→short,卖→long)
2. 拿 `position.td_close_available` + `position.yd_close_available`
3. **SHFE / INE 特殊路径**(exchange in ["SHFE", "INE"]):
   - `shfe_close_first=True` + position_y > 0 → 先发一单(默认 `offset="1"`),消耗 position_y
   - 剩余 + position_t > 0 → 再发一单(`offset="3"` CLOSE_TODAY),消耗 position_t
4. **非 SHFE/INE**:默认一单 `offset="1"`,broker 自己决定
5. 检查 `close_available = position_t + position_y`:
   - `== 0` → `self.output("[自动平仓] 可平仓量为 0")`,返回 None
   - `< volume` → 用可平仓量下单,`self.output("可平仓量小于报单数...")`

⚠️ **SHFE/INE 一次调用可能触发 2 个 `send_order`**,但**只返回最后一个 `order_id`**!

**我们策略 `self.order_id.add(oid)` 会丢掉前一个 oid**——CU/AL(SHFE)平仓时追踪可能失效。

### `cancel_order(order_id: int) -> int`

```python
# 源码 L479-490
def cancel_order(self, order_id):
    if order_id is None:
        return
    return infini.cancel_order(order_id)
```

**返回值**:
- `-1` — order_id 不存在
- `0` — 撤单请求已发送(**不代表撤单成功**)

### `make_order_req(...)` — 底层发单

```python
# 源码 L603-652
def make_order_req(
    self, ..., order_direction, offset, order_type, ...
) -> int | None:
    if self.trading is False:
        return  # ⭐ 静默返回 None,不报错不报单!

    return infini.send_order(
        ...,
        direction=OrderDirectionEnum[order_direction.upper()].flag,  # "BUY"→"0"
        order_type=ORDER_TYPE_MAP.get(order_type, "GFD"),            # "GFD"→"0"
        ...,
        offset=offset,
    )
```

**关键行为**:
- `self.trading=False` **静默 return None**——策略暂停/错误流控期间发单无效
- `order_direction` 和 `order_type` 经过 enum/dict 转译成 INFINIGO 底层格式
- **必须传 `offset`**(send_order 固定 "0",auto_close_position 根据逻辑传 "1"/"3")

---

# INFINIGO 模块(附录 — 低层封装)

**源码**:`pythongo/infini.py`

## 导入 fallback

```python
try:
    import INFINIGO  # Windows 专有 C++ 模块
except ImportError:
    from pythongo.backtesting.fake_class import INFINIGO  # fallback
```

**副作用**:`from pythongo.backtesting.fake_class import ...` 会触发 `pythongo/backtesting/__init__.py` 执行,设 `PYTHONGO_MODE=BACKTESTING` 环境变量。macOS/Linux 会自动进回测模式。

## 方法列表

每个方法都是一层薄 wrapper,把 snake_case 参数转成 INFINIGO 的 PascalCase:

| infini 方法 | INFINIGO 方法 | 用途 |
|------------|--------------|------|
| `update_param(strategy_id, data)` | `updateParam` | 界面改参数时更新 |
| `update_state(strategy_id, data)` | `updateState` | 更新状态栏 |
| `pause_strategy(strategy_id)` | `pauseStrategy` | 暂停策略 |
| `write_log(msg)` | `writeLog` | 输出日志(BaseStrategy `output` 最终调这个) |
| `sub_market_data(strategy_obj, exchange, instrument_id)` | `subMarketData` | 订阅行情 |
| `unsub_market_data(...)` | `unsubMarketData` | 取消订阅 |
| `send_order(strategy_id, ..., direction="0", ..., offset="0")` | `sendOrder` | 发单(**direction 是 `"0"/"1"`**) |
| `cancel_order(order_id)` | `cancelOrder` | 撤单 |
| `get_instrument(exchange, instrument_id)` | `getInstrument` | 合约信息 |
| `get_instruments_by_product(exchange, product_id)` | `getInstListByExchAndProduct` | 品种合约列表 |
| `get_investor_list()` | `getInvestorList` | 所有投资者 |
| `get_investor_account(investor)` | `getInvestorAccount` | 账号资金 |
| `get_investor_position(investor, simple=False)` | `getInvestorPosition` | 账号持仓 |

## INFINIGO 字段风格

- **参数**:PascalCase(`ExchangeID / InstrumentID / OrderID / BrokerID / HedgeFlag / Offset`)
- **返回 dict**:PascalCase(同上,所以 classdef 的 `data.get("Exchange", "")` 这样读)
- **Position.Direction** = **中文 "多"/"空"**(唯一的中文字段)

## INFINIGO 层 direction 是 `"0"/"1"`

```python
# infini.py L92
direction: Literal["0", "1"],  # "0"=买, "1"=卖
```

所以 `TypeOrderDirection` 就是 INFINIGO 层的表达;`TypeOrderDIR` ("buy"/"sell")是 BaseStrategy 高层包装。

## backtesting/fake_class.py 的 Mock INFINIGO

macOS/Linux 测试时用。关键 mock 行为:

- `getInvestorList()` 返回固定 `[{"BrokerID":"0001","InvestorID":"0001","UserID":"0001"}]`
- `getInvestorAccount(investor)` 从 `engine.account` 构造
- `getInvestorPosition(investor, Simple)` 遍历 `engine.order_details`,**Direction 字段输出中文 "多"/"空"**(印证 Position._direction_map)
- `getInstrument` 从 `engine.market_center.get_instrument_data` 拉
- `sendOrder(**kwargs)` → `engine.make_order(**kwargs)`(回测订单簿)
- `cancelOrder(OrderID)` → 直接 `return`(mock 不处理撤单)
- `writeLog(msg)` → `print(msg)`(macOS 上日志会去 stdout)

**不是**完全等价的真实 INFINIGO 行为,**只用于本地回测**,实盘必须在 Windows PythonGO 环境。
