# pythongo.classdef — 数据类

所有在回调里收到的 / 查询方法返回的对象。**源码位置:`pythongo/classdef/*.py`**。

所有数据类都基于 `ObjDataType = dict[str, str | int | float]` 构造,底层字段用 **PascalCase**(CTP 原始)。对外 property 用 **snake_case**。

## AccountData — 账户资金数据

**源码**:`pythongo/classdef/account.py`

`get_account_fund_data(investor)` 返回。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `investor` | `str` | InvestorID | 投资者账号 |
| `account` | `str` | AccountID | 资金账号 |
| `balance` | `float` | Balance | **结算准备金(权益)** |
| `pre_balance` | `float` | PreBalance | 上次结算准备金 |
| `available` | `float` | Available | **可用资金** |
| `pre_available` | `float` | PreAvailable | 上日可用资金 |
| `close_profit` | `float` | CloseProfit | **平仓盈亏(已实现)** |
| `position_profit` | `float` | PositionProfit | **持仓盈亏(未实现)** |
| `dynamic_rights` | `float` | DynamicRights | 动态权益 |
| `commission` | `float` | Fee | 手续费 |
| `margin` | `float` | Margin | **占用保证金** |
| `frozen_margin` | `float` | FrozenMargin | 冻结保证金 |
| `risk` | `float` | **动态计算** `margin / dynamic_rights` | ⚠️ `dynamic_rights=0` 会 ZeroDivisionError |
| `deposit` | `float` | Deposit | 入金金额 |
| `withdraw` | `float` | Withdraw | 出金金额 |

**注意**:`risk` **不是 broker 字段**,是 `@property` 现算(源码 L104-106)。

## Position — 合约双向持仓

**源码**:`pythongo/classdef/position.py`

`get_position(instrument_id)` 返回。

| 属性 | 类型 | 说明 |
|------|------|------|
| `long` | `Position_p` | 合约多头持仓 |
| `short` | `Position_p` | 合约空头持仓 |
| `net_position` | `int` | **`long.position - short.position`**(做空返负) |
| `position` | `int` | **`long.position + short.position`**(总量) |

方法:
- `get_single_position(direction: Literal["long", "short"]) -> Position_p` — 等同于 `pos.long` / `pos.short`

### 实现说明

```python
# position.py L9
_direction_map = {"多": "long", "空": "short"}

def __init__(self, data: list[dict] = []) -> None:
    self._init_null()   # 先把 long/short 初始化为 Position_p({})
    for position in data:
        setattr(self, self._direction_map[position["Direction"]], position)
```

**INFINIGO 底层的 Position `Direction` 字段是中文 "多"/"空"**,pythongo 层转为英文属性。

**空字典初始化为 Position_p({})**,所有字段默认 0——`pos.long.position` 永远不会 AttributeError。

## Position_p — 单向持仓

**源码**:`pythongo/classdef/position.py` L74+

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `position` | `int` | Position | **总持仓量** |
| `position_close` | `int` | PositionClose | 总持仓可平(含平仓冻结) |
| `frozen_position` | `int` | FrozenPosition | 总开仓冻结 |
| `frozen_closing` | `int` | FrozenClosing | 总平仓冻结 |
| `td_frozen_closing` | `int` | **现算** `frozen_closing - yd_frozen_closing` | 今持仓平仓冻结 |
| `td_close_available` | `int` | **现算** `td_position_close - td_frozen_closing` | **今持仓可平** |
| `td_position_close` | `int` | **现算** `position_close - yd_position_close` | 今持仓可平(含冻结) |
| `yd_frozen_closing` | `int` | YdFrozenClosing | 昨持仓平仓冻结 |
| `yd_close_available` | `int` | **现算** `yd_position_close - yd_frozen_closing` | **昨持仓可平** |
| `yd_position_close` | `int` | YdPositionClose | 昨持仓可平(含冻结) |
| `open_volume` | `int` | OpenVolume | 今日开仓(不含冻结) |
| `close_volume` | `int` | CloseVolume | 今日平仓(含昨持仓平仓,不含冻结) |
| `strike_frozen_position` | `int` | StrikeFrozenPosition | 执行冻结持仓(期权) |
| `abandon_frozen_position` | `int` | AbandonFrozenPosition | 放弃执行冻结 |
| `position_cost` | `float \| int` | PositionCost | 总持仓成本 |
| `yd_position_cost` | `float \| int` | YdPositionCost | 初始昨日持仓成本(当日不变) |
| `close_profit` | `float \| int` | CloseProfit | 平仓盈亏 |
| `position_profit` | `float \| int` | PositionProfit | **持仓盈亏** |
| `open_avg_price` | `float \| int` | OpenAvgPrice | 开仓均价 |
| `position_avg_price` | `float \| int` | PositionAvgPrice | 持仓均价 |
| `used_margin` | `float \| int` | UsedMargin | 占用保证金 |
| `close_available` | `int` | CloseAvailable | 当前总可平持仓 |

⚠️ **`get_position(simple=True)` 时以下字段为空**(底层跳过查询):
`position_cost / yd_position_cost / close_profit / position_profit / open_avg_price / position_avg_price / used_margin`

## TickData — 行情切片

**源码**:`pythongo/classdef/tick.py`

`on_tick` 回调参数。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `last_price` | `float` | LastPrice | **最新价** |
| `open_price` | `float` | OpenPrice | 今开盘价 |
| `high_price` | `float` | HighPrice | 最高价 |
| `low_price` | `float` | LowPrice | 最低价 |
| `volume` | `int` | Volume | **总成交量(累计)** |
| `last_volume` | `int` | **运行时计算** | 最新成交量 delta(见下) |
| `pre_close_price` | `float` | PreClosePrice | 昨收盘价 |
| `pre_settlement_price` | `float` | PreSettlementPrice | 昨结算价 |
| `open_interest` | `int` | OpenInterest | **总持仓量** |
| `upper_limit_price` | `float` | UpperLimitPrice | **涨停板价** |
| `lower_limit_price` | `float` | LowerLimitPrice | **跌停板价** |
| `turnover` | `float` | Turnover | 总成交金额 |
| `bid_price1` ~ `bid_price5` | `float` | BidPrice1~5 | 申买价 1-5 档(**字段名无下划线**) |
| `ask_price1` ~ `ask_price5` | `float` | AskPrice1~5 | 申卖价 1-5 档 |
| `bid_volume1` ~ `bid_volume5` | `int` | BidVolume1~5 | 申买量 |
| `ask_volume1` ~ `ask_volume5` | `int` | AskVolume1~5 | 申卖量 |
| `trading_day` | `str` | TradingDay | 交易日 |
| `update_time` | `str` | UpdateTime | 更新时间 |
| `datetime` | `TypeDateTime` | Datetime | 时间戳(`datetime.datetime`) |

### `last_volume` 实现(源码 L12-60)

**不是 broker 字段,是 pythongo 运行时算的 delta**:

```python
class TickData:
    _last_total_volume: ClassVar[dict[str, int]] = {}  # 类级 state,跨 tick 共享

    def __init__(self, data):
        previous = self._last_total_volume.get(instrument_id)
        self._last_volume = 0 if previous is None else self._volume - previous
        self._last_total_volume[instrument_id] = self._volume
```

- 首 tick 的 `last_volume = 0`
- 之后 `last_volume = current - previous`
- **类级 dict 跨策略共享**(多策略订阅同合约时无锁竞争,dict 原子赋值大概率无问题)

### 方法

- `copy() -> TickData` — 浅拷贝(`copy.copy`)
- `update(**kwargs)` — 直接改属性(一般用不到)

## TradeData — 成交数据

**源码**:`pythongo/classdef/trade.py`

`on_trade(trade, log=False)` 回调参数。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `trade_id` | `str` | TradeID | **成交编号**(`.strip()` 去空格) |
| `order_id` | `int` | OrderID | **报单编号**(本地自增 int) |
| `order_sys_id` | `str` | OrderSysID | 交易所报单编号(`.replace(" ", "")` 去空格) |
| `trade_time` | `str` | TradeTime | 成交时间(格式 `yyyymmdd hh:mm:ss`) |
| `direction` | **`TypeOrderDirection`** | Direction | **`"0"`(买)/ `"1"`(卖)** — ⚠️ 不是中文,不是 "buy"/"sell" |
| `offset` | `TypeOffsetFlag` | Offset | 开平标志 |
| `hedgeflag` | `TypeHedgeFlag` | Hedgeflag | 投机套保标志 |
| `price` | `float` | Price | **成交价格** |
| `volume` | `int` | Volume | **成交数量** |
| `memo` | `str` | Memo | 报单备注(`send_order(memo=...)` 传入的) |

### 🔴 Bug B 源码证据

`direction` 属性:

```python
# trade.py L82-84
@property
def direction(self) -> TypeOrderDirection:  # = Literal["0", "1"]
    """买卖方向"""
    return self._direction
```

**所以策略代码 `"买" in str(trade.direction)` 恒为 False**(详见 [README.md](./README.md) Bug B)。

## OrderData — 报单数据

**源码**:`pythongo/classdef/order.py` L145+(继承 `BaseOrderData`)

`on_order` / `on_order_trade` 回调参数。

### 继承自 `BaseOrderData` 的字段

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `order_id` | `int` | OrderID | 报单编号(本地自增) |
| `order_sys_id` | `str` | OrderSysID | 交易所报单编号(去空格) |
| `price` | `float` | Price | 报单价格 |
| `order_price_type` | `TypeOrderPriceType` | OrderPriceType | 报单类型("1"任意价 / "2"限价 / "3"最优价 / "4"五档价) |
| `cancel_volume` | `int` | CancelVolume | **撤单数量** |
| `direction` | `TypeOrderDirection` | Direction | **`"0"`/`"1"`**(同 TradeData) |
| `offset` | `TypeOffsetFlag` | Offset | 开平标志 |
| `hedgeflag` | `TypeHedgeFlag` | Hedgeflag | 投机套保标志 |
| `front_id` | `int` | FrontID | 前置编号 |
| `session_id` | `int` | SessionID | 会话编号 |
| `cancel_time` | `str` | CancelTime | 撤单时间(注:源码注释说"应该为空值",可能不可靠) |
| `memo` | `str` | Memo | 报单备注 |
| `order_time` | `str` | OrderTime | 报单时间 |

### `OrderData` 独有字段(继承 BaseOrderData 之外)

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `total_volume` | `int` | TotalVolume | **报单数量** |
| `traded_volume` | `int` | TradedVolume | **已成交数量** |
| `status` | `TypeOrderStatus` | Status | 报单状态(**15 个中文字符串**,详见 [types.md](./types.md)) |

### 🔴 Bug A 源码证据

**`OrderData` 和 `BaseOrderData` 都没有 `.volume` 属性**(详见 [README.md](./README.md) Bug A)。

### `on_order` 根据中文 status 分发(`base.py` L245-249)

```python
def on_order(self, order: OrderData) -> None:
    if order.status == "已撤销":
        self.on_order_cancel(order)
    elif order.status in ["全部成交", "部成部撤"]:
        self.on_order_trade(order)
```

## CancelOrderData — 撤单数据

**源码**:`pythongo/classdef/order.py` L180-184(继承 `BaseOrderData`,无额外字段)

`on_cancel(order)` 回调参数(v2025.0801+ 新回调)。

**字段完全等同 `BaseOrderData`**——没有 `total_volume / traded_volume / status`。所以在 `on_cancel` 里不能知道订单是部成还是已成,只有 `cancel_volume` 表示撤单数量。

## KLineData — K 线数据

**源码**:`pythongo/classdef/kline.py`

`on_bar` callback / `KLineGenerator.callback` 推送。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | **exchange**(小写!) | 交易所代码(v2025.0424+) |
| `instrument_id` | `str` | **instrument_id**(小写!) | 合约代码(v2025.0424+) |
| `open` | `float` | open | 开盘价 |
| `close` | `float` | close | **收盘价** |
| `low` | `float` | low | 最低价 |
| `high` | `float` | high | 最高价 |
| `volume` | `int` | volume | **成交量** |
| `open_interest` | `int` | open_interest | **持仓量** |
| `datetime` | `TypeDateTime` | datetime | 时间 |

### ⚠️ KLineData 用 **snake_case** key(不是 PascalCase)

这是**唯一的例外**——其他所有数据类(Tick / Order / Trade / Position / Account)都用 PascalCase key 从 INFINIGO 底层字典构造。

**原因**:`KLineData` 不是从 INFINIGO 底层直接构造的,而是在 pythongo 层由 `KLineGenerator` 合成时从内部 dict 创建。用 snake_case 保持 Python 风格。

### 方法

- `to_json() -> dict` — 转字典(用于 UI 推图 / 保存 state)
- `update(**kwargs)` — 直接改属性(`KLineGenerator` 用它累积 OHLC)

## InstrumentData — 合约信息

**源码**:`pythongo/classdef/instrument.py`

`get_instrument_data(exchange, instrument_id)` 返回。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `instrument_name` | `str` | InstrumentName | 合约中文名 |
| `product_id` | `str` | ProductID | 品种代码 |
| `product_type` | `str` | ProductClass | **经 PRODUCT_MAP 转中文**(不是 "1"/"2"/"3"...,而是 "期货"/"期权"...) |
| `price_tick` | `float` | PriceTick | **最小变动价位** |
| `size` | `int` | VolumeMultiple | **合约乘数** |
| `strike_price` | `float` | StrikePrice | 期权行权价 |
| `underlying_symbol` | `str` | UnderlyingInstrID | 标的物代码(期权) |
| `options_type` | `str` | OptionsType | **经 OPTION_MAP 转**:`""` / `"CALL"` / `"PUT"` |
| `expire_date` | `str` | ExpireDate | 合约到期日 |
| `min_limit_order_size` | `int` | MinLimitOrderVolume | 最小下单量 |
| `max_limit_order_size` | `int` | MaxLimitOrderVolume | 最大下单量 |
| `lower_limit_price` | `float` | LowerLimitPrice | 跌停板价位 |
| `upper_limit_price` | `float` | UpperLimitPrice | 涨停板价位 |

## InstrumentStatus — 合约状态

**源码**:`pythongo/classdef/instrument.py` L123-152

`on_contract_status(status)` 回调参数。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `exchange` | `str` | Exchange | 交易所代码 |
| `instrument_id` | `str` | InstrumentID | 合约代码 |
| `status` | `str` | Status | 中文状态 |

## InvestorData — 投资者

**源码**:`pythongo/classdef/investor.py`

`get_investor_data(index=1)` 返回。

| 属性 | 类型 | 底层 key | 说明 |
|------|------|---------|------|
| `broker_id` | `str` | BrokerID | 经纪公司编号 |
| `investor_id` | `str` | InvestorID | **投资者账号**(用于 `get_account_fund_data`) |
| `user_id` | `str` | UserID | 登录账号(多数同 `investor_id`) |

### 实现注意

`get_investor_data(index=1)` 是 1-based,内部转成 0-based(`base.py` L717)。**超过范围自动回退到第一个**:
```python
index = 0 if index > len(investor_list) else index - 1
```

---

## 特殊说明:所有数据类都有 `__repr__`

所有类都实现了 `__repr__` 返回属性字典的 str,**方便 debug 时 `print(trade)` 看完整字段**。

```python
# 例如 TradeData.__repr__ 返回:
{
    "exchange": "SHFE", "instrument_id": "al2605",
    "trade_id": "12345", "order_id": 678,
    "direction": "0", "offset": "0",
    "hedgeflag": "1", "price": 18500.0,
    "volume": 5, ...
}
```

**实盘 DIAG 时直接 `self.output(f"{trade!r}")` 就能看所有字段**。
