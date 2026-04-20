# Type 别名 + 数值映射

所有 `pythongo` 里用到的 Literal 类型和数值映射。

## TypeOrderDIR — 报单方向 ⚠️

```python
TypeOrderDIR = Literal["buy", "sell"]
```

| 键 | 值 |
|----|------|
| `"buy"` | 买 |
| `"sell"` | 卖 |

⚠️ **与 mapping 文档冲突**:`faq/mapping` 页面 DirectionType 给的是 `"0"` / `"1"`。

两个可能性:
- 高层 BaseStrategy:用 `"buy"` / `"sell"`(英文)
- 底层 INFINIGO:用 `"0"` / `"1"`(数字 string)

**我们代码里 `trade.direction` 识别逻辑有 bug**(`"买" in str(...)` 永远不命中,不管是英文还是数字)。详见 [README.md](./README.md) Bug B。

## TypeOrderFlag — 报单指令(高层)

```python
TypeOrderFlag = Literal["GFD", "FAK", "FOK"]
```

- **GFD** = Good For Day(当日有效)
- **FAK** = Fill And Kill(立即成交,剩余撤单)
- **FOK** = Fill Or Kill(全部成交否则撤销)

默认 `"GFD"`。

## TypeRAWOrderFlag — 报单指令(底层 INFINIGO)

```python
TypeRAWOrderFlag = Literal["0", "1", "2"]
```

| 键 | 对应 |
|----|------|
| `"0"` | GFD |
| `"1"` | FAK |
| `"2"` | FOK |

## TypeHedgeFlag — 投机套保标志

```python
TypeHedgeFlag = Literal["1", "2", "3", "4", "5"]
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
TypeOffsetFlag = Literal["0", "1", "3"]
```

| 键 | 值 |
|----|------|
| `"0"` | 开仓 |
| `"1"` | 平仓 |
| `"3"` | 平今 |

⚠️ **没有 `"4"` 平昨**——SHFE / 能源中心平昨由 `auto_close_position(shfe_close_first=True)` 自动处理。

(INFINIGO 底层文档提到 `"2"` ForceClose / `"4"` CloseYesterday,但高层 API 不使用这两个值。)

## TypeProduct — 品种类型

```python
TypeProduct = Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "h"]
```

具体对应关系见 **ProductClassType mapping**(文档没贴完整,大致:期货 / 期权 / ETF / 股票 / 现货等)。

## TypeInstResult — 合约查询结果

```python
TypeInstResult = dict[TypeProduct, list[InstrumentData | str]]
```

`get_instruments_by_product(raw_data=True)` 返回 `InstrumentData` 列表,`raw_data=False` 返回合约代码 `str` 列表。

## TypeDateTime — 时间类型

```python
TypeDateTime = datetime.datetime
```

等同 Python 标准库 `datetime.datetime`。

## KLineStyle — K 线周期枚举

```python
from pythongo.utils import KLineStyle

KLineStyle.M1    # 1 分钟
KLineStyle.M2    # 2 分钟
KLineStyle.M3    # 3 分钟
KLineStyle.M4    # 4 分钟
KLineStyle.M5    # 5 分钟
KLineStyle.M10   # 10 分钟
KLineStyle.M15   # 15 分钟
KLineStyle.M30   # 30 分钟
KLineStyle.M45   # 45 分钟
KLineStyle.H1    # 1 小时
KLineStyle.H2    # 2 小时
KLineStyle.H3    # 3 小时
KLineStyle.H4    # 4 小时
KLineStyle.D1    # 日线(1440 分钟)
```

`KLineStyleType` 是 type 注释别名。

**注意**:我们策略 `Params` 里用 `kline_style: str = Field(default="H1")` 传字符串,`_freq_to_sec` 做三种形式兼容(str / enum.value / `"KLineStyle.H1"` 格式)。

## ObjDataType — 数据类入参类型

```python
ObjDataType = dict[str, str | int | float]
```

INFINIGO 层数据类实例化用。

---

## 🚨 mapping 文档冲突汇总

### DirectionType — 两个版本

| 来源 | 值 | 说明 |
|------|-----|------|
| `types.py` / `TypeOrderDIR` | `"buy"` / `"sell"` | **高层 BaseStrategy** |
| `faq/mapping` DirectionType | `"0"` / `"1"` | CTP raw / INFINIGO |
| **我们代码假设的** | `"买"` / `"卖"`(中文) | **不对** |

**实盘用 `self.output(f"{trade.direction!r}")` 打印验证**。

### 其他已确认一致的 mapping

- `TypeHedgeFlag`:types 和 mapping 都是 `"1"-"5"` ✓
- `TypeOffsetFlag`:types 说 `"0"/"1"/"3"`,mapping 说 `"0"=开/1=平/2=强平/3=平今/4=平昨`——高层只暴露前 3 个 ✓
- `TypeOrderFlag`:types 说 `"GFD"/"FAK"/"FOK"`,RAW 版本是 `"0"/"1"/"2"` ✓

## OrderStatusType — 报单状态(文档未完整列出)

`OrderData.status` 字段类型。文档指向 `faq/mapping#orderstatus`,但该页面未详细列出所有值。

可能的值(根据 CTP 常见定义推测):
- 未成交
- 部分成交
- 全部成交
- 已撤单
- 等等

**实盘用 `self.output(f"{order.status!r}")` 确认实际值**。
