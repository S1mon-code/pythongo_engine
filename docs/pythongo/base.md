# pythongo.base — 策略基础模块

## 继承简介

Python 子类继承父类后可使用父类所有属性和方法。重写方法后用 `super().method_name()` 调用父类同名方法。

## BaseParams — 参数映射模型

- 定义好的映射关系会展示在「PythonGO 窗口 - 参数」栏,可直接界面编辑
- **默认定义了 `exchange` 和 `instrument_id`**(固定),继承后可以不重新定义但**建议显式定义**

## BaseState — 状态映射模型

- 映射关系展示在「PythonGO 窗口 - 状态」栏
- **仅供显示,无法编辑**

## BaseStrategy — 策略模板(所有策略的父类)

定义了回调函数、全局变量、封装了数据获取方法。

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `strategy_id` | `int` | 策略实例 ID,无限易自动自增,不可改 |
| `strategy_name` | `str` | 实例名称,由创建时填的「实例名称」赋值,不可改 |
| `params_map` | `BaseParams` | 实例化后的参数映射 |
| `state_map` | `BaseState` | 实例化后的状态映射 |
| `limit_time` | `int` | **错单限制时间(秒)**——错单后 N 秒不再报单 |
| `trading` | `bool` | 是否允许交易,False 时 `send_order` 不对外报单 |
| `class_name` | `str` | 策略的类名 |
| `exchange_list` | `list[str]` | `params_map.exchange.split(";")` |
| `instrument_list` | `list[str]` | `params_map.instrument_id.split(";")` |
| `instance_file` | `str` | 保存的实例信息文件路径 |

### 回调方法

| 回调 | 参数 | 触发条件 |
|------|------|----------|
| `on_init()` | — | 创建实例/加载实例触发,会自动调 `load_instance_file()` |
| `on_start()` | — | 点击运行。**将 `self.trading=True`,订阅合约行情** |
| `on_stop()` | — | 点击暂停。**将 `self.trading=False`,保存实例,取消订阅** |
| `on_tick(tick)` | `TickData` | 收到行情 tick |
| `on_contract_status(status)` | `InstrumentStatus` | 合约状态变化 |
| `on_cancel(order)` | `CancelOrderData` | **v2025.0801+ 新回调**,旧 `on_order_cancel` 未来弃用 |
| `on_order_trade(order)` | `OrderData` | `on_order` 主动触发,**等同 `on_trade`,建议使用 `on_trade`** |
| `on_order(order)` | `OrderData` | 报单变化(报单成功就进入) |
| `on_trade(trade, log)` | `TradeData, bool` | 报单成交 |
| `on_error(error)` | `dict[str, str]` | 错误推送 |

### `on_error` 特殊规则

```python
error = {"errCode": "0004", "errMsg": "...", "orderID": "..."}
```

- **`errCode = "0004"`** = 撤单错误(可针对性处理)
- **有 `orderID` 键** → 报单错误
- **无 `orderID` 键** → 函数运行错误

### 方法(按用途分组)

#### 系统方法(无限易主动调,自己不用)
- `get_params()` → `list[dict[str, str]]` — 界面读参数
- `set_params(data)` — 界面写参数
- `save_instance_file()` — 暂停时自动调
- `load_instance_file()` — `on_init` 自动调

#### 日志 / UI
- `output(*msg: Any)` — 输出到 PythonGO 控制台
- `update_status_bar()` — 更新 PythonGO 状态栏

#### 行情订阅
- `sub_market_data(exchange=None, instrument_id=None)` — None 时自动取 `exchange_list` / `instrument_list`
- `unsub_market_data(exchange=None, instrument_id=None)` — 同上

#### 持仓 / 账户查询
- `get_position(instrument_id, hedgeflag="1", investor=None, simple=False) -> Position`
  - `simple=True`(v2025.0522+):实时持仓,但 `position_cost / yd_position_cost / close_profit / position_profit / open_avg_price / position_avg_price / used_margin` **为空**
- `get_all_position(simple=False) -> dict[investor][instrument_id][hedgeflag] -> Position`(v2025.1112+)
- `sync_position()` — 手动同步(`get_position` 自动调,**一般不用**)
- `get_account_fund_data(investor) -> AccountData`
- `get_investor_data(index=1) -> InvestorData` — 第 N 个账号

#### 合约信息
- `get_instrument_data(exchange, instrument_id) -> InstrumentData`
- `get_instruments_by_product(exchange, product_id, raw_data=True) -> TypeInstResult`

#### 报单 / 撤单

```python
send_order(
    exchange: str,              # 必填
    instrument_id: str,         # 必填
    volume: int,                # 必填
    price: float | int,         # 必填
    order_direction: TypeOrderDIR,  # 必填, "buy" / "sell"
    order_type: TypeOrderFlag = "GFD",  # GFD / FAK / FOK
    investor: str = "",
    hedgeflag: TypeHedgeFlag = "1",  # "1"=投机
    market: bool = False,       # True=市价, False=限价
    memo: Any = None,           # 报单备注,回调能拿回
) -> int | None                 # 返回 order_id, -1 = 失败
```

`auto_close_position()` 签名同 `send_order`,额外参数:
- `shfe_close_first: bool = False` — SHFE 平昨优先

⚠️ **注意**:SHFE/能源中心仓位,如果既平昨又平今,**只返回最后一次平仓的 order_id**。

```python
cancel_order(order_id: int) -> int  # -1=order_id 不存在, 0=撤单请求已发送(不代表撤单成功)
```

`make_order_req()` — 低层报单(必须传 `offset`),**一般不用**。

#### 其他
- `pause_strategy()` — 等同点击暂停。**⚠️ 不要在 `on_start` / `on_stop` 里调,死循环**

## INFINIGO 模块(附录 — 低层封装)

重新封装了 INFINIGO 模块的 PythonGO 类。**99% 情况下不会直接用**,策略代码走 BaseStrategy 高层封装即可。

### 差异:底层用数字 string,高层用英文别名

| 参数 | 低层 INFINIGO | 高层 BaseStrategy |
|------|---------------|------------------|
| direction | `Literal["0", "1"]`(0=买,1=卖) | `TypeOrderDIR = Literal["buy", "sell"]` |
| order_type | `TypeRAWOrderFlag = Literal["0", "1", "2"]` | `TypeOrderFlag = Literal["GFD", "FAK", "FOK"]` |
| offset | **必填** `TypeOffsetFlag = Literal["0", "1", "3"]` | 不传,`auto_close_position` 自动处理 |

### INFINIGO 方法列表(参考)

- `update_param(strategy_id, data)` / `update_state(strategy_id, data)`
- `pause_strategy(strategy_id)`
- `write_log(msg)` — BaseStrategy 的 `output()` 对应
- `sub_market_data(strategy_obj, exchange, instrument_id)` / `unsub_market_data(...)`
- `send_order(strategy_id, exchange, instrument_id, volume, price, direction, order_type, investor, hedgeflag, offset, market, memo) -> int`
- `cancel_order(order_id) -> int`
- `get_instrument(exchange, instrument_id) -> ObjDataType`
- `get_instruments_by_product(exchange, product_id) -> list[ObjDataType]`
- `get_investor_list() -> list[ObjDataType]`
- `get_investor_account(investor) -> ObjDataType`
- `get_investor_position(investor, Simple=False) -> list[ObjDataType]`

⚠️ INFINIGO 层的 Position 字段是 **PascalCase** (`PositionCost / YdPositionCost / CloseProfit / PositionProfit / OpenAvgPrice / PositionAvgPrice / UsedMargin`),高层 Position_p 做了 snake_case 转换。
