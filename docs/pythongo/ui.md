# pythongo.ui — 带 UI 的策略模版

⚠️ **策略必须用本模块的 `BaseStrategy`**(不是 `pythongo.base.BaseStrategy`),否则没有 K 线图 UI。

```python
from pythongo.ui import BaseStrategy
```

自动处理 UI 逻辑(启动显示,暂停隐藏等)。

## BaseStrategy (UI 版本)

继承了 `pythongo.base.BaseStrategy`,增加:

| 属性 | 类型 | 说明 |
|------|------|------|
| `widget` | `KLWidget` | **K 线组件**(自动实例化) |
| `main_indicator_data` | `dict[str, float]` | **主图指标数据**(自己用 `@property` 定义) |
| `sub_indicator_data` | `dict[str, float]` | 副图指标数据(`@property` 定义) |

⚠️ **`main_indicator_data` 和 `sub_indicator_data` 不能直接赋值**,必须用 `@property` 或属性方法定义:

```python
class MyStrategy(BaseStrategy):
    @property
    def main_indicator_data(self):
        return {"forecast": self.state_map.forecast, "ma20": self._ma20}

    @property
    def sub_indicator_data(self):
        return {"rsi": self._rsi, "adx": self._adx}
```

## KLWidget — K 线组件

### recv_kline(data)

收取 K 线、价格信号、指标数据,更新到线图。

```python
def real_time_callback(self, kline: KLineData):
    self.widget.recv_kline({
        "kline": kline,
        "signal_price": 0.0,        # 信号触发价(可选)
        **self.main_indicator_data, # 展开主图指标
        # 副图指标也可以混在里面
    })
```

**data 字典**:
- `"kline"` → `KLineData`(必填)
- `"signal_price"` → `float`(信号触发时的价格,会在图上标记)
- 其他 key(如 `"MA"`, `"RSI"` 等)→ 指标值,对应到 `main_indicator_data` / `sub_indicator_data` 的展示

## 模块子文件(PythonGO 内部,不直接用)

- `crosshair.py` — 十字光标,鼠标移动显示 K 线坐标与数据
- `drawer.py` — 用 PyQt 构建 UI,绘画 K 线的主模块
- `widget.py` — 连接策略与 drawer 的桥梁,含 UI 基础策略模版 + KLWidget 简化库

## 我们代码中的使用模式

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
                "kline": kline, "signal_price": sp, **self.main_indicator_data,
            })
        except Exception:
            pass
```

## Portfolio / 多时间框架注意

Portfolio 策略同时跑 H1 + H4 两个 `KLineGenerator`。**只在 H4 的 `real_time_callback` 里推 `widget.recv_kline`**(图表只显示 H4),否则 UI 会刷新混乱。

CLAUDE.md 记录:
> 双时间框架: `kline_generator_h1` + `kline_generator_h4`,只有 H4 推图表
