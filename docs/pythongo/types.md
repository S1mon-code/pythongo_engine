# Type 别名 + 数值映射

所有 pythongo 里用到的 Literal 类型别名和数值映射。**源码位置:`pythongo/types.py` + `pythongo/const.py`**。

## ⚠️ 两个 direction 类型!不要混

```python
# pythongo/types.py

type TypeOrderDIR = Literal["buy", "sell"]      # L21
"""报单方向 (高层 API 输入)"""

type TypeOrderDirection = Literal["0", "1"]     # L70
"""买卖方向类型 (数据回调里收到的)"""
```

### 用途区分

| 类型 | 出现在 | 值 |
|------|-------|-----|
| `TypeOrderDIR` | `send_order(order_direction)` / `auto_close_position(order_direction)` / `make_order_req(order_direction)` | `"buy"` / `"sell"`(接受大写 `"BUY"` / `"SELL"`,内部 `.upper()`)|
| `TypeOrderDirection` | `TradeData.direction` / `OrderData.direction` / `CancelOrderData.direction` | `"0"`(买)/ `"1"`(卖)|

### 转译链路(源码 `base.py` L645)

```python
# 用户调:
self.send_order(order_direction="buy", ...)

# 内部转译:
order_direction = "buy".upper()                      # → "BUY"
OrderDirectionEnum["BUY"]                            # 枚举
.flag                                                # → "0"
# 传给 INFINIGO:
infini.send_order(direction="0", ...)
```

回调时 `trade.direction` **原路返回底层值 "0"/"1"**——没做反向转译。

### `OrderDirectionEnum`(`pythongo/const.py` L37-43)

```python
class OrderDirectionEnum(OrderDirectionDataMixin, Enum):
    BUY = "0", "short"       # flag="0", match_direction="short"
    """买"""

    SELL = "1", "long"       # flag="1", match_direction="long"
    """卖"""
```

`match_direction` 很巧妙——用于**反向匹配持仓方向**:
- 买入操作用来平**空头**仓位 → match="short"
- 卖出操作用来平**多头**仓位 → match="long"

这是 `auto_close_position` 找对应持仓的依据(`base.py` L549)。

## TypeOrderFlag — 报单指令(高层)

```python
type TypeOrderFlag = Literal["GFD", "FAK", "FOK"]
```

- **GFD** = Good For Day(当日有效)
- **FAK** = Fill And Kill(立即成交,剩余撤单)
- **FOK** = Fill Or Kill(全部成交否则撤销)

默认 `"GFD"`。

## TypeRAWOrderFlag — 报单指令(底层 INFINIGO)

```python
type TypeRAWOrderFlag = Literal["0", "1", "2"]
```

| 键 | 对应 |
|----|------|
| `"0"` | GFD |
| `"1"` | FAK |
| `"2"` | FOK |

`ORDER_TYPE_MAP`(`pythongo/const.py` L21-25)做转译:

```python
ORDER_TYPE_MAP = {"GFD": "0", "FAK": "1", "FOK": "2"}
```

## TypeHedgeFlag — 投机套保标志

```python
type TypeHedgeFlag = Literal["1", "2", "3", "4", "5"]
```

| 键 | 值 |
|----|------|
| `"1"` | 投机 |
| `"2"` | 套利 |
| `"3"` | 套保 |
| `"4"` | 做市商 |
| `"5"` | 备兑 |

默认 `"1"`(投机),策略基本不需要改。

## TypeOffsetFlag — 开平标志

```python
type TypeOffsetFlag = Literal["0", "1", "3"]
```

| 键 | 值 |
|----|------|
| `"0"` | 开仓 |
| `"1"` | 平仓 |
| `"3"` | 平今 |

`OrderOffsetEnum`(`pythongo/const.py` L46-54)定义:

```python
class OrderOffsetEnum(StrEnum):
    OPEN = "0"
    CLOSE = "1"
    CLOSE_TODAY = "3"
```

⚠️ **没有 `"4"` 平昨**——SHFE / 能源中心平昨由 `auto_close_position(shfe_close_first=True)` 自动处理:
- `shfe_close_first=True` + position_y > 0 → 先发一单 offset="1"(底层 broker 平昨)
- 剩余 + position_t > 0 → 再发一单 offset="3"(平今)

## TypeOrderPriceType — 报单价格类型

```python
type TypeOrderPriceType = Literal["1", "2", "3", "4"]
```

**`OrderData.order_price_type`** 的类型。注释来自源码 `order.py` L59-64:

| 键 | 值 |
|----|------|
| `"1"` | 任意价(市价) |
| `"2"` | 限价 |
| `"3"` | 最优价 |
| `"4"` | 五档价 |

## TypeOrderStatus — 报单状态

```python
# pythongo/types.py L51-67
type TypeOrderStatus = Literal[
    "未知",
    "未成交",
    "全部成交",
    "部分成交",
    "部成部撤",
    "已撤销",
    "已报入未应答",
    "部分撤单还在队列中",
    "部成部撤还在队列中",
    "待报入",
    "投顾报单",
    "投资经理驳回",
    "投资经理通过",
    "交易员已报入",
    "交易员驳回"
]
```

**全部是中文字符串**。`OrderData.status` 是此类型。

### `on_order` 内部根据 status 分发(`base.py` L245-249)

```python
def on_order(self, order: OrderData) -> None:
    if order.status == "已撤销":
        self.on_order_cancel(order)
    elif order.status in ["全部成交", "部成部撤"]:
        self.on_order_trade(order)
```

所以策略 override `on_order` **必须调 `super().on_order(order)`**,否则 `on_order_cancel` / `on_order_trade` 不触发。

## TypeProduct — 品种类型

```python
type TypeProduct = Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "h"]
```

**`PRODUCT_MAP`**(`pythongo/const.py` L4-17):

| 键 | 值 |
|----|------|
| `"1"` | 期货 |
| `"2"` | 期权 |
| `"3"` | 组合 |
| `"4"` | 即期 |
| `"5"` | 期转现 |
| `"6"` | 未知类型 |
| `"7"` | 证券 |
| `"8"` | 股票期权 |
| `"9"` | 金交所现货 |
| `"a"` | 金交所递延 |
| `"b"` | 金交所远期 |
| `"h"` | 现货期权 |

`InstrumentData.product_type` 返回的是**经过 `PRODUCT_MAP` 转换的中文**(`instrument.py` L70),不是原始 "1"/"2"。

## TypeInstResult — 合约查询结果

```python
type TypeInstResult = dict[TypeProduct, list["InstrumentData" | str]]
```

`get_instruments_by_product(raw_data=True)` 返回 `InstrumentData` 列表,`raw_data=False` 返回合约代码 `str` 列表,按 `TypeProduct` 分组。

## OPTION_MAP — 期权类型

`pythongo/const.py` L19:

```python
OPTION_MAP = {"1": "CALL", "2": "PUT"}
```

`InstrumentData.options_type` 返回转换后的 `""` / `"CALL"` / `"PUT"`(`instrument.py` L95)。

## TypeDateTime — 时间类型

```python
type TypeDateTime = datetime.datetime
```

等同 Python 标准库 `datetime.datetime`。

## KLineStyle — K 线周期枚举

**源码**:`pythongo/core.pyi` L6-20

```python
from pythongo.core import KLineStyle

KLineStyle.M1    # 1 分钟
KLineStyle.M2    # 2
KLineStyle.M3    # 3
KLineStyle.M4    # 4
KLineStyle.M5    # 5
KLineStyle.M10   # 10
KLineStyle.M15   # 15
KLineStyle.M30   # 30
KLineStyle.M45   # 45
KLineStyle.H1    # 60 (1 小时)
KLineStyle.H2    # 120
KLineStyle.H3    # 180
KLineStyle.H4    # 240
KLineStyle.D1    # 1440
```

### `KLineStyleType` 类型注解

```python
type KLineStyleType = Literal[
    KLineStyle.M1, KLineStyle.M2, ...,
    "M1", "M2", ..., "H1", ..., "D1"
]
```

**同时接受枚举和字符串**。`KLineGenerator.style.setter` 两种都支持:

```python
# utils.py L301-307
if value in KLineStyle.__members__:
    self._style = KLineStyle[value]       # 字符串 "H1" → KLineStyle.H1
elif isinstance(value, KLineStyle):
    self._style = value                   # 枚举直接用
```

## ObjDataType — 数据类入参类型

`pythongo/classdef/common.py`:

```python
type ObjDataType = dict[str, str | int | float]
```

所有数据类(`TickData / OrderData / TradeData / Position / AccountData` 等)的 `__init__(data: ObjDataType = {})` 接受这个类型——从 INFINIGO 的 dict 构造对象。

## StatusCode — 客户端交互状态码

`pythongo/const.py` L58-60:

```python
class StatusCode(object):
    """与无限易客户端交互状态码"""
    STOP = 20001
```

目前只有 `STOP = 20001`,具体用途需查 INFINIGO 底层。

---

## Mapping 冲突对照(重要!)

`faq/mapping` 页和 `types.py` 源码的**一处差异**:

| 概念 | mapping 文档 (DirectionType) | types.py 源码 (TypeOrderDIR) | types.py 源码 (TypeOrderDirection) | 用途 |
|------|-----|------------------------------|--------------------------------------|------|
| 报单方向 | `"0"` / `"1"` | `"buy"` / `"sell"` | `"0"` / `"1"` | 高层传 TypeOrderDIR,回调收 TypeOrderDirection |

**所以两个都是对的,用途不同。**

mapping 文档的 `DirectionType` = 源码的 `TypeOrderDirection`(回调的类型)。
