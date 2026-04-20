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
