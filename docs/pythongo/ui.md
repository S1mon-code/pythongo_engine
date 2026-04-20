# pythongo.ui — 带 UI 的策略模版

**源码**:`pythongo/ui/widget.py`(336 行)+ `drawer.py` / `crosshair.py`(纯 PyQt 绘图)+ `__init__.py`

⚠️ **策略必须用本模块的 `BaseStrategy`**(不是 `pythongo.base.BaseStrategy`),否则没有 K 线图 UI。

```python
from pythongo.ui import BaseStrategy
```

## UI BaseStrategy — 重写了 base 的生命周期

**源码**:`pythongo/ui/widget.py` L21-102

```python
class BaseStrategy(BaseStrategy):   # 同名覆盖 pythongo.base.BaseStrategy
    qt_thread: Thread = None
    qt_gui_support: "QtGuiSupport" = None
    
    def __init__(self):
        super().__init__()
        self.widget: KLWidget = None
```

### 重写的回调

| 回调 | UI 版本做了什么 |
|------|--------------|
| `on_init()` | `super().on_init()` + `self.init_widget()` 创建 KLWidget |
| `on_start()` | `super().on_start()` + 若 widget 存在 `widget.load_data_signal.emit()` 载入历史 K 线到图表 |
| `on_stop()` | `super().on_stop()` + `self.close_gui()` 隐藏窗口 |

### 新增属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `widget` | `KLWidget` | K 线组件(自动实例化) |
| `main_indicator_data` | `@property → dict[str, float]` | **主图指标数据**(子类 override) |
| `main_indicator` | `@property → list[str]` | 主图指标名(dict keys) |
| `sub_indicator_data` | `@property → dict[str, float]` | 副图指标数据 |
| `sub_indicator` | `@property → list[str]` | 副图指标名 |

### 默认 indicator data 为空

```python
# widget.py L33-40
@property
def main_indicator_data(self) -> dict[str, float]:
    return {}

@property
def sub_indicator_data(self) -> dict[str, float]:
    return {}
```

**子类必须 override** 才有指标。

### QT 线程启动条件(L105-106)

```python
if current_thread() is not main_thread():
    BaseStrategy.start_qt_gui_support()  # 启动 QT 子线程
```

**只有非主线程导入时才启 QT**(防止交互 shell 意外启)。

### `start_qt` 的实现(L87-102)

启动 QApplication + 加载 qdarkstyle + style.qss,实例化 `QtGuiSupport`。

---

## QtGuiSupport — QT 辅助类

**源码**:`pythongo/ui/widget.py` L109-135

```python
class QtGuiSupport(QtCore.QObject):
    init_widget_signal = QtCore.pyqtSignal(object)
    hide_signal = QtCore.pyqtSignal(object)
    
    def __init__(self):
        super().__init__()
        self.widget_container: dict[str, KLWidget] = {}
        self.init_widget_signal.connect(self.init_strategy_widget)
        self.hide_signal.connect(self.hide_strategy_widget)
```

### `init_strategy_widget(strategy)` — 按 strategy_name 缓存 widget

```python
def init_strategy_widget(self, s: BaseStrategy) -> None:
    if self.widget_container.get(s.strategy_name) is None:
        s.widget = KLWidget(s)
        self.widget_container[s.strategy_name] = s.widget
    else:
        s.widget = self.widget_container[s.strategy_name]  # 复用
        self.widget_container[s.strategy_name].strategy = s
    s.widget.kline_widget.set_title(s.params_map.instrument_id)
```

**同名策略重启复用旧 widget**,所以关闭再开启不会累积窗口。

### `hide_strategy_widget(strategy)` — 隐藏窗口

`on_stop` 时触发。

---

## KLWidget — K 线组件

**源码**:`pythongo/ui/widget.py` L138-335

```python
class KLWidget(QWidget):
    update_kline_signal = QtCore.pyqtSignal(dict)
    load_data_signal = QtCore.pyqtSignal()
    set_xrange_event_signal = QtCore.pyqtSignal()
    
    def __init__(self, strategy: BaseStrategy, parent=None):
        super().__init__(parent)
        self.strategy = strategy
        self.kline_widget = KLineWidget()        # 实际绘图组件
        self.init_ui()
        self.klines: list[dict] = []             # K 线数据缓存
        self.all_indicator_data = defaultdict(list)  # 所有指标数据
        
        self.update_kline_signal.connect(self.update_kline)
        self.load_data_signal.connect(self.load_kline_data)
        self.set_xrange_event_signal.connect(self.kline_widget.set_xrange_event)
        self._first_add = True
```

### 关键方法:`recv_kline(data)` 双路径

```python
# widget.py L181-200
def recv_kline(self, data: dict[str, float | KLineData]) -> None:
    if self.strategy.trading:
        # 实盘:异步 Qt signal emit,UI 线程更新
        self.update_kline_signal.emit(data)
    else:
        # 历史/暂停:直接累积到本地数组
        if self._first_add:
            self.clear()
            self._first_add = False
        self.klines.append(data["kline"].to_json())
        for s in (self.main_indicator + self.sub_indicator):
            self.all_indicator_data[s].append(data[s])
    
    self.update_bs_signal(data.get("signal_price", 0))
```

### `data` 必须包含所有指标值

```python
for s in (self.main_indicator + self.sub_indicator):
    self.all_indicator_data[s].append(data[s])  # ⚠️ data[s] 必须存在
```

**如果 `main_indicator_data = {"forecast": x}`**,那调 `recv_kline(data)` 时 `data` **必须包含 `"forecast"` key**,否则 KeyError。

策略代码模式:

```python
def _push_widget(self, kline, sp=0.0):
    try:
        self.widget.recv_kline({
            "kline": kline,
            "signal_price": sp,
            **self.main_indicator_data  # ← 展开,确保 key 齐全
        })
    except Exception:
        pass  # ⚠️ 吞异常,防止 widget 未初始化时(on_start 前 push_history)抛
```

### `update_kline(data)` — 实盘更新路径(L203-234)

```python
def update_kline(self, data):
    kline = data["kline"]
    
    # 检测丢数据:新 K 时间在已有两根之间
    if (
        len(self.klines) >= 2
        and (self.klines[-2]["datetime"] < kline.datetime < self.klines[-1]["datetime"])
    ):
        """插入到倒数第二位"""
        self.klines.insert(-1, kline.to_json())
        self.kline_widget.insert_kline(kline)
        for indicator_name in self.main_indicator:
            self.all_indicator_data[indicator_name].insert(-1, data[indicator_name])
        self.update_indicator_data(new_data=True)
        return
    
    is_new_kline = self.kline_widget.update_kline(kline)  # 新 K or 更新最后一根
    
    if is_new_kline:
        self.klines.append(kline.to_json())
    else:
        self.klines[-1] = kline.to_json()
    
    self.update_indicator_data(data, new_data=is_new_kline)
    
    self.kline_widget.update_candle_signal.emit()
    self.renew_main_indicator()
    self.renew_sub_indicator()
```

### `load_kline_data()` — 历史数据载入图表(L245-262)

```python
def load_kline_data(self):
    if self._first_add is False:
        """只有调用过 recv_kline 才需重新载入"""
        pdData = pd.DataFrame(self.klines).set_index("datetime")
        pdData["open_interest"] = pdData["open_interest"].astype(float)
        self.kline_widget.load_data(pdData)
        ...
```

**依赖 pandas**。一次性把累积的 `klines` 数组画到图上。

### `closeEvent` 防误关(L329-335)

```python
def closeEvent(self, evt):
    if self.strategy.trading:
        QMessageBox.warning(None, "警告", "策略启动时无法关闭，暂停时会自动关闭！")
    else:
        self.hide()
    evt.ignore()  # ⭐ 永远不真关闭,只隐藏
```

策略跑着时关窗口会弹警告。必须点暂停才能关。

---

## 我们代码的使用模式

```python
from pythongo.ui import BaseStrategy

class AL_Long_1H_V8_Donchian_ADX_Filter(BaseStrategy):
    def __init__(self):
        super().__init__()
        # ...

    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast}

    def real_time_callback(self, kline: KLineData):
        self._push_widget(kline)

    def _push_widget(self, kline, sp=0.0):
        try:
            self.widget.recv_kline({
                "kline": kline, "signal_price": sp,
                **self.main_indicator_data,
            })
        except Exception:
            pass
```

## Portfolio / 多时间框架注意

Portfolio 策略同时跑 H1 + H4 两个 `KLineGenerator`。**只在 H4 的 `real_time_callback` 里推 `widget.recv_kline`**(图表只显示 H4),否则 UI 会刷新混乱。

CLAUDE.md 记录:
> 双时间框架: `kline_generator_h1` + `kline_generator_h4`,只有 H4 推图表

---

# drawer.py — K 线图主绘制模块

**源码**:`pythongo/ui/drawer.py`(697 行)

## `KLineWidget` — 主 widget

3 个子图垂直堆叠:
- **主图**(`kline_plot_item`)— K 线 + 主图指标 + 买卖箭头,最小高度 350
- **成交量子图**(`vol_plot_item`)— 成交量柱图,最大高度 150
- **底部子图**(`bottom_chart`)— 默认持仓量,或副图指标

### 关键常量

```python
kline_count = 60  # 默认显示 60 根 K 线窗口

# 涨红跌绿(中国习惯,和西方相反)
up_color   = (255, 113, 113)  # 红色,上涨
down_color = (  0, 176,  26)  # 绿色,下跌

# 指标颜色循环 deque(6 种)
colors = ["#FFFFFF", "#FFF100", "#FF37E5", "#C18AFF", "#B6FF1A", "#368FFF"]
```

**超过 6 个指标颜色会循环**——不会冲突但视觉不易区分。

### 信号(pyqtSignal)

| 信号 | 参数 | 连接到 |
|------|------|--------|
| `update_candle_signal` | — | `update_candle` |
| `add_buy_sell_signal` | `int`(array index) | `add_new_bs_signal` |
| `draw_marks_signal` | — | `draw_marks` |

### `update_kline(kline) -> bool`

```python
# drawer.py L579-582
is_new_kline = not (
    len(self.datas) > 0
    and kline.datetime == self.datas[-1].datetime.astype(datetime.datetime)
)
```

**通过 datetime 相同与否判断**:
- `callback` bar close 时 datetime 不同 → 追加新 K
- `real_time_callback` 每 tick 时 datetime 相同 → 更新最后一根

返回 `is_new_kline` 供上层决定 `klines.append` vs `klines[-1] = `。

### 买卖信号箭头:signal_price 正上负下

```python
# drawer.py L530-534 add_new_bs_signal
arrow = pg.ArrowItem(
    pos=(index, self.datas[index]["low" if price > 0 else "high"]),
    angle=90 if price > 0 else -90,                          # ⭐ 正向上,负向下
    brush=(168, 101, 243) if price > 0 else (255, 234, 90),  # 买紫,卖黄
)
```

| `signal_price` | 箭头方向 | 锚点 | 颜色 |
|----------------|---------|-----|------|
| `> 0` | ↑(买) | K 线 `low` 下 | 紫色 `(168, 101, 243)` |
| `< 0` | ↓(卖) | K 线 `high` 上 | 黄色 `(255, 234, 90)` |

**我们策略 `_execute` 的 return**:
- 开仓/加仓 → `return price`(正,向上箭头)
- 平仓/止损 → `return -price`(负,向下箭头)
- REDUCE → `return -price`(减仓也是卖)

### `CandlestickItem` — K 线图形对象

- 用 `QPicture` 缓存提升性能(只重画可视区)
- **下跌绿实心,上涨红空心**(中国习惯)

### `load_data(datas: pd.DataFrame)` — 载入历史 K 线

依赖 pandas DataFrame,datetime 为 index。**成交量染色同步 K 线涨跌**:

```python
# L647-648
df.loc[datas["open"].values <= datas["close"].values, "open"] = 0   # 上涨
df.loc[datas["open"].values > datas["close"].values, "close"] = 0   # 下跌
```

### `init_xrange_event()` — Y 轴自动自适应

```python
def viewXRangeChanged(low, high, *args):
    view_range = view.viewRange()
    xmin, xmax = ...
    ymin = min(self.datas[xmin:xmax][low])
    ymax = max(self.datas[xmin:xmax][high])
    view.setYRange(ymin, ymax)
```

**可视 X 范围变化时,Y 轴自动根据可见区 low/high 缩放**——所以拖图时 Y 轴会跟着变。

### `clear_data()`

策略重启时通过 `KLWidget.recv_kline` 的 `_first_add` 路径调用。

---

# crosshair.py — 十字光标

**源码**:`pythongo/ui/crosshair.py`(252 行)

## `Crosshair(QtCore.QObject)`

挂载到 3 个子图(主 / 成交量 / 底部),鼠标移动时:
- 竖线穿 3 个子图同步
- 水平线只在当前子图显示
- 右上角 HTML text:所有指标当前值
- 左上角 HTML text:O/H/L/C + 成交量 + 持仓量 + 信号价

### 颜色规则

```python
up_color   = "#FF7171"  # 涨红
down_color = "#00B01A"  # 跌绿

def get_color(self, value: float, close: float) -> str:
    return self.up_color if value > close else self.down_color
```

**O/H/L/C 每个字段独立染色**——根据 **当根 vs 上根 close** 的大小关系。

### `__text_info` 左上(主图)

```
日期
2026-04-20
时间
10:30:00
开盘价       ← 按和上根 close 比较染色
3,500.5
最高价
3,510.0
最低价
3,495.0
收盘价
3,505.0
成交量
12,345
持仓量
567,890
成交价       ← 如果这根 K 有信号
85.0
```

### `__text_sig` 右上 HTML

所有主图指标当前值,按 `indicator_color_map` 染色。字体 18px。

### `__text_sub_sig` 副图右上

副图指标(同主图格式)。

### `__text_volume` 成交量子图右上

```
VOL: 12345
```

### 鼠标事件节流

```python
self.proxy = pg.SignalProxy(
    signal=parent.scene().sigMouseMoved,
    rateLimit=60,          # 每秒最多 60 次更新
    slot=self.__mouse_moved
)
```

**60 Hz 节流**,避免高频移动时卡顿。

---

## 模块整体结构

| 文件 | 作用 |
|------|------|
| `ui/__init__.py` | 从 `widget` 导出 `BaseStrategy` + `KLWidget` |
| `ui/widget.py` | UI `BaseStrategy` + `KLWidget`(连接策略和 drawer)+ `QtGuiSupport` |
| `ui/drawer.py` | `KLineWidget` — K 线绘图主模块(PyQt + pyqtgraph) |
| `ui/crosshair.py` | `Crosshair` — 十字光标(依赖 KLineWidget) |
| `ui/style.qss` | Qt 样式表(窗口外观) |
| `ui/infinitrader.png` | 窗口图标 |
