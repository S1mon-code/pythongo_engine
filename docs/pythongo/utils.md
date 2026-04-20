# pythongo.utils + pythongo.core — 工具与数据

**源码**:`pythongo/utils.py`(1099 行)+ `pythongo/core.pyi`(`core.cp312-win_amd64.pyd` 的类型 stub)

合并了 `pythongo.utils`(K 线合成器 / 定时器 / CustomLogHandler)和 `pythongo.core`(MarketCenter / Indicators / KLineStyle)。

---

## KLineGenerator — 分钟级 K 线合成(最重要)

**源码**:`pythongo/utils.py` L245-634

```python
from pythongo.utils import KLineGenerator

self.kline_generator = KLineGenerator(
    exchange=p.exchange,
    instrument_id=p.instrument_id,
    callback=self.callback,                 # K 线完成时推送
    style=p.kline_style,                    # M1/M3/H1/H4 ...
    real_time_callback=self.real_time_callback,  # 每 tick 推当前未完成 K
)
self.kline_generator.push_history_data()  # 启动时灌历史 K
```

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` / `instrument_id` / `style` | | 合约与周期 |
| `callback` / `real_time_callback` | | K 线 / tick 回调 |
| `producer` | `KLineProducer` | **内置指标方法 + OHLC 数据序列** |
| `scheduler` | `Scheduler` | **`Scheduler("PythonGO")` 单例**(全局共享!) |
| `market_center` | `MarketCenter` | 内部拉快照用 |
| `close_time` | `list[str]` | 收盘时间字符串列表(首次 tick 时从 MarketCenter 拉) |
| `next_gen_time` | `datetime` | 下一根 K 线的生成时间 |

### ⚠️ `Scheduler("PythonGO")` 是**全局单例**

每个 `KLineGenerator` 实例共享同一个 `"PythonGO"` scheduler,用于注册「交易时段结束 +2 秒推送最后一根 K」的定时任务。**`BaseStrategy.on_stop` 会清空这个 scheduler 所有 jobs**(utils.py L121-127 单例模式下 `stop()` 不真 shutdown,只清 jobs)。

**结论**:
- 我们用 `Scheduler("PythonGO")` 会和 KLineGenerator **共享**,`on_stop` 时被一起清
- 要独立必须用**不同 scheduler_name**(例:`Scheduler("DailyReport")`)

### 方法

#### `push_history_data()`

```python
# L309-311
def push_history_data(self) -> None:
    self.producer.worker()
```

调 `producer.worker()` 从 `KLineContainer` 读历史 K 线,逐根推到 `callback`——**此时 `self.trading=False`,策略 `_on_bar` 会早早 return**(不触发信号计算)。

#### `stop_push_scheduler()` — 停止推送定时器

单例模式下只清 jobs,不真 shutdown。

#### `tick_to_kline(tick, push=False)` — 合成 K 线

**复杂度最高的方法**(L503-634)。核心流程:

1. **首次运行**(`_first_run=True`):
   - 过滤:tick 时间 > 当前时间 + 600s → 返回
   - 调 `market_center.get_next_gen_time` 算下一根 K 生成时间
   - 调 `_init_kline(tick)` 用 K 线快照 + 首 tick 初始化 `_cache_kline`
   - 判断是否缺失 K 线(用 `producer.datetime[-1]` 算出的 next_gen_time vs 当前算出的)
   - 为每个收盘时间注册定时任务(run_date + 2 秒触发 push,保证收盘后还能收到最后一根 K)
   - 启动 scheduler
   - 保存 `close_time` 列表

2. **非首次,正常 tick**:
   - 过滤:合约不对 / 无成交量 / volume 相同 → 返回
   - 脏数据过滤(`tick.datetime < _dirty_time`)
   - 若 tick 时间 >= next_gen_time,开始合成:
     - 若在 `close_time` 列表里 → **不驱动合成**(收盘后两个 tick 不算新 bar)
     - 否则调 `_push_kline()` 把 `_cache_kline` 推给 callback,再算新 next_gen_time
   - 新周期:初始化 `_cache_kline` 的 OHLC
   - 老周期:更新 high/low
   - 计算 volume = `tick.volume - _min_last_volume`
   - **若 `real_time_callback` 存在**:
     - 把当前未完成 K 写入 `producer`
     - 触发 `real_time_callback(self._cache_kline)`

#### `get_kline_snapshot()` — 拉 K 线快照

首次 `_init_kline` 用,通过 `market_center.get_kline_snapshot`(⚠️ 有速率限制)。

### `KLineGenerator` 的重要行为

#### 1. `producer.close[-1]` 在不同回调时机**含义不同**

- `callback`(`_on_bar`)触发时:`producer.close[-1]` 是**刚完成的完整 bar**(准确)
- `real_time_callback`(`_push_widget`)触发时:`producer.close[-1]` 是**当前未完成 bar 的实时价**

**不要在 `real_time_callback` 里用 `producer.close[-1]` 做信号计算**。

#### 2. `_lose_kline` 补缺失 K 线

若检测到策略启动时与历史数据有 gap,**会调 `market_center.get_kline_data`** 从 MarketCenter 拉补——**潜在速率限制风险**,但只在 gap 首次检测时一次,可控。

#### 3. `close_time` 的作用

```python
# L586
if tick.datetime.strftime("%X") not in self.close_time:
    """每个交易时段结束后的两个 tick 不驱动 K 线合成"""
    self._push_kline()
```

收盘时段(`close_time` 字符串列表)**不驱动 K 线合成**——避免最后两个 tick 生成半截 bar。但通过 **`scheduler` 注册的定时任务**(每个收盘时间 +2 秒)用 `push=True` 强制推最后一根 K。

## KLineGeneratorSec — 秒级 K 线生成器

**源码**:`pythongo/utils.py` L130-242

```python
KLineGeneratorSec(callback, seconds=1)
```

简化版,不依赖 MarketCenter,只凭 tick 累积。策略中不常用(我们用 M1+)。

## KLineGeneratorArb — 标准套利合约 K 线

**源码**:`pythongo/utils.py` L933-1044

两腿 tick(leg1 + leg2)合成一个虚拟 tick,再走 `KLineGenerator.tick_to_kline`。**需要支持套利合约的站点环境**。我们不用。

---

## KLineProducer — K 线生产器(含指标)

**源码**:`pythongo/utils.py` L728-931

**继承 `Indicators` 类**,所以 `producer.sma() / .adx() / .donchian()` 等方法直接可调。

### 属性(OHLC 数据序列)

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` / `instrument_id` / `style` | | |
| `kline_container` | `KLineContainer` | 自动实例化 |
| `callback` | `Callable` | 推送 K 线用 |
| `open` / `close` / `high` / `low` | `list[np.float64]` | OHLC 序列 |
| `volume` | `list[np.float64]` | 成交量序列 |
| `datetime` | `list[TypeDateTime]` | 时间序列 |

### ⚠️ 前 10 个元素是**假数据**!

```python
# L760-769
def __init__(self, ...):
    self._open = np.zeros(10)          # 10 个 0
    self._close = np.zeros(10)
    self._high = np.zeros(10)
    self._low = np.zeros(10)
    self._volume = np.zeros(10)
    self._datetime = np.arange(
        start="1999-11-20 00",
        stop="1999-11-20 10",
        dtype="datetime64[h]",          # 1999-11-20 00:00~09:00,10 个假时间
    )
```

**目的**:让 `datetime[-2] < kline.datetime < datetime[-1]` 这种判断能跑(初始有可访问的索引)。

**影响**:
- 历史数据灌完后,真实数据从索引 10 开始
- **若策略 `WARMUP` 小于 10 根**,`producer.close` 前面可能混入 0 和 1999 年假数据,污染指标计算

AL V8 `WARMUP=80`,能自动跳过。其他策略需注意。

### `datetime[-1]` 不是 bar close 时间!

**`real_time_callback` 每 tick 调 `producer.update(_cache_kline)`**,所以:
- `producer.datetime[-1]` 是**当前未完成 K 线的时间戳**(其实是 `next_gen_time`,即当前 bar 的 "predicted close 时间")

### 方法

#### `update(kline)` — 智能更新(`base.py` L842-855)

```python
def update(self, kline: KLineData):
    if self.datetime[-2] < kline.datetime < self.datetime[-1]:
        self.insert_data(kline)       # 插入倒数第二
    elif self.datetime[-1] != kline.datetime:
        self.append_data(kline)        # 追加
    else:
        self.update_last_kline(kline)  # 更新最后一根
```

#### `append_data(kline)` / `insert_data(kline, index=-1)` / `update_last_kline(kline)`

用 `np.append` / `np.insert` 操作,每次**都复制整个数组**(numpy 特性)——长时间运行内存分配可能变慢。

#### `worker()` — 推送历史 K 线

`KLineGenerator.push_history_data()` 内部调。从 `kline_container` 读所有历史 K,逐根 `self._push(KLineData(...))` —— 即 update 序列 + 调 callback。

---

## KLineContainer — K 线容器

**源码**:`pythongo/utils.py` L637-725

自动缓存 K 线,同 `(exchange, instrument_id, style)` 组合不重复获取。

```python
KLineContainer(exchange, instrument_id, style)
```

- `__init__` 自动调 `init()` 拉 K 线
- 数据**不会自动更新**,需配合 `KLineGenerator` 合成时手动 `set` 新 K 线

### 方法

- `get(exchange, instrument_id, style) -> list[dict]` — 读取
- `set(exchange, instrument_id, style, data)` — 写入
- `init(exchange, instrument_id, style)` — 从 `market_center.get_kline_data` 拉(**⚠️ 速率限制!**),失败抛 ValueError

---

## Scheduler — 简易定时器(单例模式)

**源码**:`pythongo/utils.py` L25-127

基于 `apscheduler.BackgroundScheduler`(非阻塞),含单例模式和 `CustomLogHandler`。

```python
from pythongo.utils import Scheduler

scheduler = Scheduler(scheduler_name="my_timer")  # 传名字 = 单例模式(全局共享!)
scheduler.start()

# 每 5 秒执行一次
scheduler.add_job(
    func=foo,
    trigger="interval",           # "date" / "interval" / "cron"
    id="foo_print",
    seconds=5,
    next_run_time=datetime.now(), # 立刻运行,否则 5 秒后开始
)

# cron 示例(每天 15:15)
scheduler.add_job(self._send_review, trigger="cron",
                  hour=15, minute=15, id="daily_report")

scheduler.remove_job("foo_print")
scheduler.stop()
```

### 单例机制

```python
# L40-52
_cache_instance: dict[str, "Scheduler"] = {}

def __new__(cls, *args, **kwargs):
    if scheduler_name := (args and args[0]) or kwargs.get("scheduler_name"):
        if _cls := cls._cache_instance.get(scheduler_name):
            return _cls
        cls._cache_instance[scheduler_name] = super().__new__(cls)
        return cls._cache_instance[scheduler_name]
    return super().__new__(cls)
```

**传同一 `scheduler_name` 会返回同一个实例**。

### `stop()` 的双重行为(L119-127)

```python
def stop(self):
    if self.scheduler_name in self._cache_instance:
        for job in self.get_jobs():
            job.remove()
        return
    if self.scheduler.running:
        self.scheduler.shutdown(wait=False)
```

- **单例模式下**:只清空所有 jobs,**不真 shutdown**
- **非单例**:真 shutdown(且**不可重启**,需重新实例化)

⚠️ **`BaseStrategy.on_stop` 调 `Scheduler("PythonGO").stop()`** → 清空共享 scheduler 所有 jobs。我们要独立定时器必须用**不同 scheduler_name**。

### 初始化自动配 CustomLogHandler

```python
# L62-64
custom_handler = CustomLogHandler()
logger = logging.getLogger("apscheduler")
logger.addHandler(custom_handler)
```

让 apscheduler 的日志进 PythonGO 控制台(而非 stdout)。

---

## CustomLogHandler — 自定义日志 Handler

**源码**:`pythongo/utils.py` L19-22

```python
class CustomLogHandler(logging.Handler):
    def emit(self, record):
        write_log(self.format(record))
```

给第三方库(如 apscheduler)的 Logger 加这个 Handler,让它的日志进 PythonGO 而非 stdout。`Scheduler.__init__` 自动配。

---

## Indicators — 技术指标类

**源码**:`pythongo/indicator.py`

⚠️ **依赖 talib**——macOS 需 `brew install ta-lib && pip install TA-Lib`。

`KLineProducer` 继承本类,所以 `producer.sma() / .adx() / ...` 直接可用。默认参数(周期)都设好,无参调用返回最后一根 K 的值,`array=True` 返回完整数组。

| 方法 | 指标 | 实现 |
|------|------|------|
| `sma(timeperiod=9)` | 简单均线 | `talib.SMA(close, timeperiod)` |
| `ema(timeperiod=12)` | 指数平均 | `talib.EMA(close, timeperiod)` |
| `std(timeperiod=5)` | 标准差 | `talib.STDDEV(..., nbdev=sqrt(p/(p-1)))` |
| `bbi(n1=3,n2=6,n3=12,n4=24)` | 多空指数 | 4 个 SMA 取均值(手写,**不依赖 talib**) |
| `cci(timeperiod=14)` | CCI | `talib.CCI(high, low, close)` |
| `rsi(timeperiod=14)` | RSI | `talib.RSI(close)`(**和无限易有细微差距**) |
| `adx(timeperiod=14)` | ADX | `talib.ADX(high, low, close)` — **只返 ADX,不返 PDI/MDI** ⚠️ |
| `sar(acc=0.02, max=0.2)` | SAR | `talib.SAR(high, low)`(**和无限易有细微差距**) |
| `kdj(9, 3, 3)` | KDJ(返 3 值) | `talib.MIN / MAX + EMA`(手写) |
| `kd(9, 3, 3)` | KD(返 2 值) | 调 kdj 取前两个 |
| `macd(12, 26, 9)` | MACD | `talib.MACD`,**hist ×2** |
| `macdext(fastmatype=1, ...)` | MA 类型可控 MACD | `talib.MACDEXT`(0=SMA / 1=EMA / 2=WMA / 3=DEMA / 4=TEMA / 5=TRIMA / 6=KAMA / 7=MAMA / 8=T3) |
| `atr(timeperiod=14)` | ATR | **`talib.SMA(tr)`** — ⚠️ **不是 Wilder RMA!** |
| `boll(timeperiod=20, deviation=2)` | 布林通道 | SMA ± std × deviation |
| `keltner(timeperiod=20, multiple=2)` | 肯特纳通道 | EMA ± atr × multiple |
| `donchian(timeperiod=20)` | 唐奇安通道 | `talib.MAX(high) / talib.MIN(low)` — **只返 (upper, lower),没有 mid!** ⚠️ |

### 为什么 AL V8 手写所有指标

1. **talib.ADX 不返 PDI/MDI**——AL V8 用 PDI>MDI 方向确认,必须手写 `_adx_with_di`
2. **talib SMA 版 ATR ≠ Wilder RMA**——QBase 用 Wilder,必须手写
3. **talib donchian 不返 mid**——AL V8 用 mid 算 penetration,必须手写

CLAUDE.md「从 QBase 原样移植避免信号偏差」**100% 正确**。

### numpy 静音

```python
# indicator.py L8-10
def __init__(self):
    np.seterr(divide='ignore', invalid='ignore')
    np.errstate(divide="ignore", invalid="ignore")
```

防止除零警告污染日志。

---

## MarketCenter — 数据中心 API 🚨

**源码**:`pythongo/core.pyi`(`core.cp312-win_amd64.pyd` 的类型 stub)

### 🚨 速率限制警告

**所有 `get_*` 方法都有调用限制,由 AI 自动管控,频繁调用可能导致 IP 永久封禁**。

- 无具体次数上限
- AI 判定,不可申诉
- **只在 `on_start` 拉一次 cache,不要在 `on_tick` / `on_bar` 循环调**

### API 列表

#### `get_kline_data(exchange, instrument_id, style=M1, count=-1440, origin=None, start_time=None, end_time=None, simply=True)`

获取 K 线数据。

- `count` 最大 1440,正值 = 基准时间戳后,负值 = 之前
- `start_time / end_time` 按时间区间(会**忽略 count 和 origin**)
- `simply=True` 只返 OHLC + 时间

返回:`list[dict]`

#### `get_kline_data_by_day(exchange, instrument_id, day_count, origin=None, style=M1, simply=True)`

按交易日数获取。`style` 仅支持 M1/M5/M15/M30/H1。

#### `get_dominant_list(exchange) -> list[str]`

交易所的主连合约列表。

#### `get_instrument_trade_time(exchange, instrument_id, instant=None) -> dict`

带交易日的合约交易时段。

#### `get_product_trade_time(exchange, product_id, trading_day=None) -> dict`

品种交易时段(`product_id` 也可填合约代码)。

#### `get_avl_close_time(instrument_id) -> list[datetime]`

**从缓存**取合约当前时间之后的收盘时间序列(无缓存返回空)。

#### `get_close_time(instrument_id) -> list[str]`

**源码有但文档没提**!从缓存取**当前交易日所有**收盘时间字符串(格式 `HH:MM:SS`)。`KLineGenerator` 首次 tick 时调用(L569)。

#### `get_next_gen_time(exchange, instrument_id, tick_time, style) -> datetime`

根据合约 + 时间 + K 线周期,返回下一根 K 线生成时间。`KLineGenerator` 内部用。

#### `get_kline_snapshot(exchange, instrument_id) -> dict`(源码里用但 stub 未列出)

返回最新 K 线快照,格式:
```python
{
    "timestampHead": int,   # 毫秒时间戳, K 线起始
    "timestampTail": int,   # 毫秒时间戳, K 线最后 tick
    "tradingDay": str,
    "openPrice": float,
    "highestPrice": float,
    "lowestPrice": float,
    "closePrice": float,
    "volume": int,          # K 线期内成交量
    "totalVolume": int,     # 总成交量(至 tail 为止)
    "openInterest": int,
}
```

`KLineGenerator.__init__` 实例化时就拉一次(L293-294),防止后续 tick 和快照错位。

---

## 辅助函数

### `isdigit(value: str) -> bool`(L1047-1068)

判断字符串是否整数或小数(负号 + 多个点的各种 edge case 都处理)。

### `split_arbitrage_code(instrument_id) -> tuple[str|None, str|None]`(L1080-1094)

用正则 `[a-zA-Z]+\s(\w+)&(\w+)` 分割标准套利合约代码。

### `deprecated(new_func_name, log_func) -> Callable`(L1070-1078)

函数弃用提示装饰器。将来 `on_order_cancel` 可能被这个包装,启动时打印弃用警告。

### `is_backtesting() -> bool`(L1096-1098)

```python
return os.getenv("PYTHONGO_MODE") == "BACKTESTING"
```

macOS 上 `from pythongo.backtesting.fake_class import INFINIGO` 会触发 `pythongo/backtesting/__init__.py` 设这个环境变量,所以 `is_backtesting() → True`——`KLineGenerator` 会跳过快照拉取和 scheduler 启动。
