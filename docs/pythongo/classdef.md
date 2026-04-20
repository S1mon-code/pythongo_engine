# pythongo.classdef — 数据类

所有在回调里收到的 / 查询方法返回的对象。

## AccountData — 账户资金数据

`get_account_fund_data(investor)` 返回。

| 属性 | 类型 | 说明 |
|------|------|------|
| `investor` | `str` | 投资者账号 |
| `account` | `str` | 资金账号 |
| `balance` | `float` | **结算准备金(权益)** |
| `pre_balance` | `float` | 上次结算准备金 |
| `available` | `float` | **可用资金** |
| `pre_available` | `float` | 上日可用资金 |
| `close_profit` | `float` | **平仓盈亏(已实现)** |
| `position_profit` | `float` | **持仓盈亏(未实现)** |
| `dynamic_rights` | `float` | 动态权益 |
| `commission` | `float` | 手续费 |
| `margin` | `float` | **占用保证金** |
| `frozen_margin` | `float` | 冻结保证金 |
| `risk` | `float` | 风险度 |
| `deposit` | `float` | 入金金额 |
| `withdraw` | `float` | 出金金额 |

## Position — 合约双向持仓

`get_position(instrument_id)` 返回。

| 属性 | 类型 | 说明 |
|------|------|------|
| `long` | `Position_p` | 多头持仓数据 |
| `short` | `Position_p` | 空头持仓数据 |
| `net_position` | `int` | **合约净持仓**(CLAUDE.md 实盘验证:做空返回负数) |
| `position` | `int` | 合约总持仓(`long.position + short.position`) |

方法:
- `get_single_position(direction: Literal["long", "short"]) -> Position_p`

## Position_p — 单向持仓

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `position` | `int` | **总持仓量** |
| `position_close` | `int` | 总持仓可平仓数量(含平仓冻结) |
| `frozen_position` | `int` | 总开仓冻结 |
| `frozen_closing` | `int` | 总平仓冻结 |
| `td_frozen_closing` | `int` | 今持仓平仓冻结 |
| `td_close_available` | `int` | **今持仓可平** |
| `td_position_close` | `int` | 今持仓可平(含冻结) |
| `yd_frozen_closing` | `int` | 昨持仓平仓冻结 |
| `yd_close_available` | `int` | **昨持仓可平** |
| `yd_position_close` | `int` | 昨持仓可平(含冻结) |
| `open_volume` | `int` | 今日开仓数量(不含冻结) |
| `close_volume` | `int` | 今日平仓数量(含昨持仓平仓,不含冻结) |
| `strike_frozen_position` | `int` | 执行冻结持仓(期权) |
| `abandon_frozen_position` | `int` | 放弃执行冻结 |
| `position_cost` | `float \| int` | 总持仓成本 |
| `yd_position_cost` | `float \| int` | 初始昨日持仓成本(当日不变) |
| `close_profit` | `float \| int` | 平仓盈亏 |
| `position_profit` | `float \| int` | **持仓盈亏** |
| `open_avg_price` | `float \| int` | 开仓均价 |
| `position_avg_price` | `float \| int` | 持仓均价 |
| `used_margin` | `float \| int` | 占用保证金 |
| `close_available` | `int` | 当前总可平持仓 |

⚠️ **`get_position(simple=True)` 时以下字段为空**:
`position_cost / yd_position_cost / close_profit / position_profit / open_avg_price / position_avg_price / used_margin`

## TickData — 行情切片

`on_tick` 回调参数。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `last_price` | `float` | **最新价** |
| `open_price` | `float` | 今开盘价 |
| `high_price` | `float` | 最高价 |
| `low_price` | `float` | 最低价 |
| `volume` | `int` | **总成交量(累计)** |
| `last_volume` | `int` | 最新成交量(delta) |
| `pre_close_price` | `float` | 昨收盘价 |
| `pre_settlement_price` | `float` | 昨结算价 |
| `open_interest` | `int` | **总持仓量** |
| `upper_limit_price` | `float` | **涨停板价** |
| `lower_limit_price` | `float` | **跌停板价** |
| `turnover` | `float` | 总成交金额 |
| `bid_price1` ~ `bid_price5` | `float` | 申买价 1-5 档(**无下划线**) |
| `ask_price1` ~ `ask_price5` | `float` | 申卖价 1-5 档 |
| `bid_volume1` ~ `bid_volume5` | `int` | 申买量 1-5 档 |
| `ask_volume1` ~ `ask_volume5` | `int` | 申卖量 1-5 档 |
| `trading_day` | `str` | 交易日 |
| `update_time` | `str` | 更新时间 |
| `datetime` | `TypeDateTime` | 时间戳 |

方法:
- `copy() -> TickData` — 浅拷贝
- `update(**kwargs)` — 更新属性(一般用不到)

## TradeData — 成交数据

`on_trade` 回调参数。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `trade_id` | `str` | 成交编号 |
| `order_id` | `int` | **报单编号** |
| `order_sys_id` | `str` | 交易所报单编号 |
| `trade_time` | `str` | 成交时间 |
| `direction` | `str` | **买卖方向**(DirectionType,详见 [types.md](./types.md)) |
| `offset` | `TypeOffsetFlag` | 开平标志 |
| `hedgeflag` | `TypeHedgeFlag` | 投机套保标志 |
| `price` | `float` | **成交价格** |
| `volume` | `int` | **成交数量** |
| `memo` | `str` | 报单备注(`send_order(memo=...)` 传入的) |

## OrderData — 报单数据

`on_order` / `on_order_trade` 回调参数。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `order_id` | `int` | **报单编号** |
| `order_sys_id` | `str` | 交易所报单编号 |
| `price` | `float` | 报单价格 |
| `order_price_type` | `str` | 报单类型(OrderPriceType) |
| `total_volume` | `int` | **报单数量** |
| `traded_volume` | `int` | **已成交数量** |
| `cancel_volume` | `int` | **撤单数量** |
| `direction` | `str` | 买卖方向 |
| `offset` | `TypeOffsetFlag` | 开平标志 |
| `hedgeflag` | `TypeHedgeFlag` | 投机套保标志 |
| `status` | `OrderStatusType` | 报单状态 |
| `memo` | `str` | 报单备注 |
| `front_id` | `int` | 前置编号 |
| `session_id` | `int` | 会话编号 |
| `cancel_time` | `str` | 撤单时间 |
| `order_time` | `str` | 报单时间 |

⚠️ **OrderData 没有 `.volume` 字段**——如果代码里用 `order.volume` 会 AttributeError(详见 [README.md](./README.md) Bug A)。

## CancelOrderData — 撤单数据

`on_cancel` 回调参数(v2025.0801+ 新回调)。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `order_id` | `int` | 报单编号 |
| `order_sys_id` | `str` | 交易所报单编号 |
| `price` | `float` | 报单价格 |
| `order_price_type` | `str` | 报单类型 |
| `cancel_volume` | `int` | **撤单数量** |
| `direction` | `str` | 买卖方向 |
| `offset` | `TypeOffsetFlag` | 开平标志 |
| `hedgeflag` | `TypeHedgeFlag` | 投机套保标志 |
| `memo` | `str` | 报单备注 |
| `front_id` / `session_id` / `cancel_time` / `order_time` | | |

## KLineData — K 线数据

`on_bar` callback / `KLineGenerator.callback` 推送。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码(v2025.0424+) |
| `instrument_id` | `str` | 合约代码(v2025.0424+) |
| `open` | `float` | 开盘价 |
| `close` | `float` | **收盘价** |
| `low` | `float` | 最低价 |
| `high` | `float` | 最高价 |
| `volume` | `int` | **成交量** |
| `open_interest` | `int` | **持仓量** |
| `datetime` | `TypeDateTime` | 时间 |

方法:
- `to_json() -> dict`
- `update(**kwargs)`

## InstrumentData — 合约信息

`get_instrument_data(exchange, instrument_id)` 返回。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `instrument_name` | `str` | 合约中文名 |
| `product_id` | `str` | 品种代码 |
| `product_type` | `str` | 品种类型(ProductClassType) |
| `price_tick` | `float` | **最小变动价位** |
| `size` | `int` | **合约乘数** |
| `strike_price` | `float` | 期权行权价 |
| `underlying_symbol` | `str` | 标的物代码(期权) |
| `options_type` | `str` | 期权类型(`""` / `"CALL"` / `"PUT"`) |
| `expire_date` | `str` | 合约到期日 |
| `min_limit_order_size` | `int` | 最小下单量 |
| `max_limit_order_size` | `int` | 最大下单量 |
| `lower_limit_price` | `float` | 跌停板价位 |
| `upper_limit_price` | `float` | 涨停板价位 |

## InstrumentStatus — 合约状态

`on_contract_status` 回调参数。

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所代码 |
| `instrument_id` | `str` | 合约代码 |
| `status` | `str` | 中文状态 |

## InvestorData — 投资者

`get_investor_data(index)` 返回。

| 属性 | 类型 | 说明 |
|------|------|------|
| `broker_id` | `str` | 经纪公司编号 |
| `investor_id` | `str` | **投资者账号**(用于 `get_account_fund_data`) |
| `user_id` | `str` | 登录账号(多数同 `investor_id`) |
