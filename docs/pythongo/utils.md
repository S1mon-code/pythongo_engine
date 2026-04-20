# pythongo.utils + pythongo.core — 工具与数据

合并了 `pythongo.utils`(K 线合成器 / 定时器)和 `pythongo.core`(MarketCenter / Indicators)。

---

## KLineGenerator — 分钟级 K 线合成(**最重要**)

```python
from pythongo.utils import KLineGenerator

self.kline_generator = KLineGenerator(
    exchange=p.exchange,
    instrument_id=p.instrument_id,
    callback=self.callback,                 # K 线完成时推送
    style=p.kline_style,                    # M1/M3/H1/H4 ...
    real_time_callback=self.real_time_callback,  # 每 tick 推当前未完成 K(推 UI 用)
)
self.kline_generator.push_history_data()  # 启动时灌历史 K
```

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `exchange` / `instrument_id` / `style` | | 合约与周期 |
| `producer` | `KLineProducer` | **内置指标方法,含 OHLC 历史数据序列** |

### 方法

- `push_history_data()` — 推送历史 K 线到 callback(不需要历史数据可不调)
- `tick_to_kline(tick, push=False)` — 用 tick 合成 K 线。**`push` 参数不要填**
- `stop_push_scheduler()` — 停止「每交易时段最后一根 K」推送定时器(单例模式,一般用不到)

### KLineGeneratorSec — 秒级 K 线

```python
KLineGeneratorSec(callback, seconds=1)
```

### KLineGeneratorArb — 标准套利合约

两腿 tick 合成一个 tick,再走 `KLineGenerator.tick_to_kline`。**需要支持套利合约的站点环境**。

---

## KLineProducer — K 线生产器(含指标)

继承 `Indicators` 类,所以 `producer.sma()` / `producer.adx()` 等都可以直接调。

### 属性(OHLC 数据序列)

| 属性 | 类型 |
|------|------|
| `exchange` / `instrument_id` / `style` | |
| `kline_container` | `KLineContainer` |
| `open` | `list[np.float64]` |
| `close` | `list[np.float64]` |
| `high` | `list[np.float64]` |
| `low` | `list[np.float64]` |
| `volume` | `list[np.float64]` |
| `datetime` | `list[TypeDateTime]` |

⚠️ **没有 `open_interest` 字段**——OI 策略(V26/V14)必须在 `callback` 里从 `kline.open_interest` 手动收集到自己的序列。

### 方法

- `update(kline)` — 自动添加/插入/更新最后一根
- `append_data(kline)` / `insert_data(kline, index=-1)` / `update_last_kline(kline)`
- `worker()` — K 线数据 → KLineData 对象 → 更新序列 → 推到 callback

---

## KLineContainer — K 线容器

自动缓存实例本身,同 exchange + instrument 不重复获取。实例化后自动调 `init()`。

```python
KLineContainer(exchange, instrument_id, style)
```

方法:
- `get(exchange, instrument_id, style) -> list[dict]`
- `set(exchange, instrument_id, style, data)`
- `init(exchange, instrument_id, style)` — 获取 K 线并缓存

**数据不自动更新**——需要 `KLineGenerator` 合成后手动更新。

---

## Scheduler — 简易定时器

基于 `apscheduler.BackgroundScheduler`(非阻塞)。

```python
from pythongo.utils import Scheduler

scheduler = Scheduler(scheduler_name="my_timer")  # 传名字 = 单例模式(全局共享)
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
scheduler.stop()  # 停止后不可重启,需要重新实例化
```

⚠️ **单例模式**:传 `scheduler_name` 时,后续同名实例返回同一内存地址。在不同策略中管理同一定时器用。

### 方法
- `add_job(func, trigger, id=None, **kwargs)`
- `get_job(job_id) -> apscheduler.job.Job`
- `get_jobs() -> list[Job]`
- `remove_job(job_id)`
- `start()` / `stop()`

---

## Indicators — 技术指标类

`KLineProducer` 继承本类,所以 `producer.sma()` 等直接可用。默认参数(周期)都已设好,可直接调。

| 方法 | 指标 |
|------|------|
| `sma()` | 简单移动平均(SMA) |
| `ema()` | 指数平均(EXPMA) |
| `std()` | 标准差(StdDev) |
| `bbi()` | 多空指数(Bull And Bear Index) |
| `cci()` | 顺势指标 |
| `rsi()` | 相对强弱 |
| `adx()` | 平均趋向指数 |
| `sar()` | 抛物线指标(Parabolic SAR) |
| `kdj()` / `kd()` | 随机指标 |
| `macd()` | MACD |
| `macdext()` | 可控 MA 类型的 MACD(0=SMA, 1=EMA, 2=WMA, 3=DEMA, 4=TEMA, 5=TRIMA, 6=KAMA, 7=MAMA, 8=T3) |
| `atr()` | 真实波幅均值 |
| `boll()` | 布林线 |
| `keltner()` | 肯特纳通道 |
| `donchian()` | 唐奇安通道 |

💡 我们策略**手写 `_donchian` / `_atr` / `_adx_with_di` 纯 numpy 实现**,避免和 QBase 信号偏差(CLAUDE.md 明确记录)。

---

## CustomLogHandler — 自定义日志 Handler

当使用的 Python 库有自己的 `logging.Logger`,默认情况下这些日志不会被 PythonGO 捕获(PythonGO 走自己的界面日志通道)。

用法:给第三方库的 Logger 新增这个 Handler,让其日志进入 PythonGO。

参考实现:`Scheduler` 的 `__init__`。

---

## MarketCenter — 数据中心 API 🚨

### 🚨 速率限制警告

**所有 `get_*` 方法都有调用限制,由 AI 自动管控,频繁调用可能导致 IP 永久封禁**。

- 无具体次数上限
- AI 判定,不可申诉
- **规则:只在 `on_start` 拉一次 cache,不要在 `on_tick` / `on_bar` 循环调**

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

#### `get_next_gen_time(exchange, instrument_id, tick_time, style) -> datetime`

根据合约 + 时间 + K 线周期,返回下一根 K 线生成时间。
