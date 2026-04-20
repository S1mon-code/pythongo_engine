# pythongo.option — 期权定价 + 期权链

**源码**:`pythongo/option.py`(793 行)

两大核心类:
- **`Option`** — 单个期权定价 + 希腊值(BSM / BAW / CRR 三种算法)
- **`OptionChain`** — 期权链封装(月份合约 / 到期日 / 行权价 / CALL / PUT / 平值期权查询)

---

## Option 类 — 单个期权定价 + 希腊值

### 构造

```python
from pythongo.option import Option

opt = Option(
    option_type="CALL",         # 或 "PUT"(只能大写)
    underlying_price=3500.0,    # 标的当前价
    strike_price=3500.0,        # 行权价
    time_to_expire=32/365,      # 剩余时间(年), 32 天就 32/365
    risk_free=0.025,            # 无风险利率(小数,不是%)
    market_price=85.0,          # 期权当前市场价(会被内部贴现一次!)
    dividend_rate=0.0,          # 股息率
    init_sigma=0.0,             # 0 → 自动用 bs_iv() 算 IV
)
```

### ⚠️ `market_price` 会被内部贴现

```python
# option.py L65
self.market_price: float = market_price / self.disc
```

`self.disc = exp(-r*T)`。传入的是市场原价,内部保存的是贴现后的值(用于 IV 反推)。**不要在外部再贴现一次**。

### 关键属性

| 属性 | 说明 | 默认 |
|------|------|------|
| `option_type_sign` | CALL=1.0, PUT=-1.0(方便带符号公式) | |
| `sigma` | 波动率,若传 0 会用 `bs_iv()` 自动算 IV | 0(→ 自动 IV) |
| `sigma_default` | IV 无解时的 fallback | 0.8 |
| `crr_n` | 二叉树节点数量 | 1000 |
| `disc` | `exp(-r*T)` 贴现因子 | 计算 |
| `disc_q` | `exp(-q*T)` 股息贴现 | 计算 |
| `s_t` | `S * exp(-q*T)` 含股息调整的标的价 | 计算 |
| `d_1 / d_2` | BS 标准变量 | 计算 |

### 三种定价模型

| 方法前缀 | 模型 | 适用 |
|---------|------|------|
| `bs_*` | Black-Scholes | 欧式期权 |
| `baw_*` | Barone-Adesi-Whaley 美式近似 | 美式期权(解析解,快) |
| `crr_*` | Cox-Ross-Rubinstein 二叉树 | 美式期权(数值解,准但慢) |

### 定价方法

| 方法 | 用途 |
|------|------|
| `bs_price()` | BSM 欧式期权理论价 |
| `bs_iv()` | 二分法算 BSM 隐含波动率 |
| `baw_price()` | BAW 美式期权定价 |
| `baw_iv()` | 二分法算 BAW 美式隐含波动率 |
| `crr_price()` | CRR 二叉树定价(n=1000 节点) |

### 希腊值(每种模型都有完整一套)

| 希腊值 | bs_* | baw_* | crr_* | 含义 |
|--------|------|-------|-------|------|
| delta | ✓ | ✓ | ✓ | 标的变化 1 元的期权价变化 |
| gamma | ✓ | ✓ | ✓ | delta 的变化率 |
| vega | ✓ | ✓ | ✓ | 波动率变化 1% 的期权价变化(/100 归一化) |
| theta | ✓ | ✓ | ✓ | **每天**时间损耗(/365 日度) |
| rho | ✓ | ✓ | ✓ | 无风险利率变化 1% 的期权价变化(/100) |
| **rho_q** | ✓ | ✗ | ✗ | 股息率的 rho(仅 BSM) |
| **vanna** | ✓ | ✗ | ✗ | delta 对波动率的敏感度(仅 BSM) |

### `@calculate_once` 装饰器

```python
# option.py L12-22
def calculate_once(method):
    def wrapper(self, *args, **kwargs):
        if not getattr(self, '_crr_calculated', False) and method.__name__.startswith('crr_'):
            self.crr_price()
            setattr(self, '_crr_calculated', True)
        elif not getattr(self, '_baw_calculated', False) and method.__name__.startswith('baw_'):
            self._baw_simulate(self.sigma)
            setattr(self, '_baw_calculated', True)
        return method(self, *args, **kwargs)
    return wrapper
```

CRR / BAW 希腊值**首次调用时才跑基础模拟**(crr_price / _baw_simulate),后续直接用缓存。

**性能影响**:
- **第一次算 CRR 希腊值**慢(n=1000 节点二叉树展开)
- **第二次之后**快(用缓存)

### 数值方法

#### `bs_iv() / baw_iv()` — 二分法算隐含波动率

```python
binary_search(max_guess=2, min_guess=1e-5, min_precision=1e-7, ...)
```

- 200 次迭代上限
- 无解或深度实值时 **fallback 到 `sigma_default=0.8`**
- 参考价前先过滤:`option_type_sign * (s_t - k_t) >= market_price` 直接返回默认(已是深度实值)

#### `find_minimum()` — Nelder-Mead 一元优化

BAW 内部用来求解 S_star(关键价)。

### 代码示例

```python
from pythongo.option import Option

# 期权当前市场价 85,标的 3500,行权价 3500,30 天剩余
opt = Option(
    option_type="CALL",
    underlying_price=3500.0,
    strike_price=3500.0,
    time_to_expire=30/365,
    risk_free=0.025,
    market_price=85.0,
    dividend_rate=0.0,
)

# 看看 IV (bs_iv() 自动在构造时算,sigma 已经是 IV)
print(f"IV: {opt.sigma:.4f}")

# BSM 希腊值
print(f"Delta: {opt.bs_delta():.4f}")
print(f"Gamma: {opt.bs_gamma():.6f}")
print(f"Vega:  {opt.bs_vega():.4f}")
print(f"Theta: {opt.bs_theta():.4f}")
print(f"Rho:   {opt.bs_rho():.4f}")

# 美式期权用 BAW(快)
print(f"BAW Price: {opt.baw_price():.4f}")
print(f"BAW Delta: {opt.baw_delta():.4f}")

# 最精确但最慢:CRR 二叉树
print(f"CRR Price: {opt.crr_price():.4f}")  # 第一次慢
print(f"CRR Delta: {opt.crr_delta():.4f}")  # 用缓存,快
```

---

## OptionChain 类 — 期权链封装

### 构造

```python
from pythongo.option import OptionChain

# 查询 CFFEX IO 品种(沪深 300 指数期权)的期权链
chain = OptionChain(exchange="CFFEX", product_id="IO")
```

**初始化时调 `infini.get_instruments_by_product`** 一次——**速率限制敏感**,只在 `on_start` 建 chain,不要反复。

### 数据结构

```python
# 源码 L626-657 组织
option_chain: dict[str, dict[str, OptionChainData]] = {
    "IF2509": {                                    # 标的月份合约(sorted)
        "20250919": {                              # 到期日(sorted)
            "strike_prices": [3000, 3050, 3100, ...],   # 行权价升序
            "call_options": [InstrumentData, ...],      # 按行权价升序
            "put_options": [InstrumentData, ...]
        },
        "20251017": { ... },
    },
    "IF2510": { ... },
}
```

`OptionChainData` 是 TypedDict:
```python
class OptionChainData(TypedDict):
    strike_prices: list[float]
    call_options: list[InstrumentData]
    put_options: list[InstrumentData]
```

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` | `str` | 交易所 |
| `product_id` | `str` | 品种代码 |
| `all_options` | `list[InstrumentData]` | 原始期权合约列表(过滤自 `get_instruments_by_product`) |
| `option_chain` | `dict[str, dict[str, OptionChainData]]` | 预处理后的链结构 |
| `option_type` | `Literal["期权", "股票期权", "现货期权"]` | 从第一个合约读 `product_type` |

### 查询方法

#### `get_month_contracts() -> list[str]`

返回所有标的月份合约(sorted)。

```python
# 期货期权
chain.get_month_contracts()
# ['IF2509', 'IF2510', 'IF2511', 'IF2512', 'IF2603', 'IF2606']

# ETF 期权
chain.get_month_contracts()
# ['510050', '510300', '510500', '588000', '588080']  # 注意:ETF 合约代码本身就是 "月份"
```

#### `get_expire_dates(underlying_symbol=None) -> list[str]`

- `underlying_symbol=None` → 返回**所有**月份合约的到期日(合并去重)
- 传 `underlying_symbol` → 返回该合约的到期日列表

```python
# IO 品种所有到期日
chain.get_expire_dates()
# ['20250919', '20251017', '20251121', '20251219', '20260320', '20260619']

# 某个月份合约的到期日
chain.get_expire_dates("IF2509")
# ['20250919']

# ETF 期权多到期
chain.get_expire_dates("510050")
# ['20250924', '20251022', '20251224', '20260325']
```

#### `get_strike_prices(underlying_symbol, expire_date=None) -> list[float]`

行权价升序。`expire_date=None` → 默认取**最近到期日**(`dates[0]`)。

```python
chain.get_strike_prices("IF2509")
# [3000.0, 3050.0, 3100.0, 3150.0, ..., 4000.0]
```

#### `get_call_options(underlying_symbol, expire_date=None) -> list[str]`
#### `get_put_options(underlying_symbol, expire_date=None) -> list[str]`

**返回合约代码 str 列表**(不是 InstrumentData 对象!),按行权价升序。

```python
chain.get_call_options("IF2509")
# ['IO2509-C-3000', 'IO2509-C-3050', 'IO2509-C-3100', ...]
```

要拿完整信息用 `get_instrument_data(exchange, instrument_id)` 再查一次。

#### `get_atm_option(underlying_symbol, underlying_price, expire_date=None) -> int | None`

**返回平值期权在行权价列表中的索引**(不是合约代码!)。

```python
chain.get_strike_prices("IF2509")
# [3000.0, 3050.0, 3100.0, 3150.0, 3200.0]

chain.get_atm_option("IF2509", underlying_price=3120.0)
# 2  (3100 比 3050 更接近 3120)

# 取对应 ATM 合约:
atm_idx = chain.get_atm_option("IF2509", 3120.0)
atm_call = chain.get_call_options("IF2509")[atm_idx]  # "IO2509-C-3100"
atm_put = chain.get_put_options("IF2509")[atm_idx]    # "IO2509-P-3100"
```

### ⚠️ 陷阱总结

1. **`get_call_options / get_put_options` 返回合约代码 str,不是 InstrumentData**
2. **`get_atm_option` 返回索引,不是合约代码**
3. **`expire_date=None` 默认取最近到期**(`dates[0]`,因为按时间排序)
4. **ETF 期权必须显式传 `expire_date`**(多到期日),期货期权默认可
5. **源码 L697 小瑕疵**:`is not "股票期权"` 用 `is` 比较 str 不严谨,但 CPython string interning 让它能跑
6. **过滤条件**:`instrument.product_type in ["期权", "股票期权", "现货期权"]`(这是 `PRODUCT_MAP` 转换后的中文值)

---

## 对期权策略开发的建议

### 1. 建立期权链只用一次

```python
def on_start(self):
    super().on_start()
    self.chain = OptionChain(exchange=self.params_map.option_exchange,
                              product_id=self.params_map.option_product)
    # 缓存好,后续不再查
```

### 2. 期权定价缓存希腊值

如果策略 tick 级算 delta 做 hedge,**每 tick new Option 对象会重新算 IV**(二分法 200 次)——慢。应:
- 把 Option 对象 cache,只在标的价 / 市场价变化显著时重算
- 或手动传 `init_sigma`(用上一次算的 IV 做初值)

### 3. 美式期权用 BAW 还是 CRR

- BAW:**解析近似**,毫秒级,精度足够做希腊值
- CRR:n=1000 节点,**首次几十 ms 到数百 ms**,精度最高

**策略实时决策用 BAW,最终估值 / 风险报告用 CRR**。

### 4. 回测期权策略不要用 pythongo 内置回测引擎

`backtesting/engine.py` 的 `margin_rate=0.13` 对期权不对,手续费公式也不对——**用 QBase 回测系统**。

### 5. 合约检测

```python
# 用 get_instrument_data 检测期权 vs 期货
inst = self.get_instrument_data(exchange, instrument_id)
if inst.product_type in ["期权", "股票期权", "现货期权"]:
    # 期权逻辑
    if inst.options_type == "CALL":  # 或 "PUT" 或 "" (期货是空字符串)
        ...
```

### 6. 期权保证金计算

PythonGO 默认 `margin_rate=0.13` 不对期权适用。实盘由 broker 计算,但策略**预检保证金**时要自己算:

**期权买方**:只付权利金 = `premium * multiple * volume`
**期权卖方(裸卖)**:保证金公式(中金所 CFFEX 标准):
```
max(virtual_intrinsic_value + 0.1 * underlying_price * multiple,
    virtual_intrinsic_value + 0.05 * strike_price * multiple) + premium * multiple
```

具体公式要查交易所文档,不能简单套 `0.13 * price * multiple`。
