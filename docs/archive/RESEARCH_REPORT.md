# PythonGO Engine — Deep Research Report
*Generated: 2026-04-01 | Sources: 40+ | Confidence: High*

---

## Executive Summary

**PythonGO** 是无限易(InfiniTrader)内置的Python量化交易引擎，由量投科技开发。它通过C++绑定将交易所直连接口暴露给Python，支持中国全部期货/期权交易所。策略代码在无限易客户端内运行，同一套代码可在模拟盘和实盘之间无缝切换。

**飞书Webhook Bot** 是实现交易通知的最佳方案——零基础设施、单向推送、支持富文本卡片消息，完全满足开仓/加仓/减仓/平仓提醒需求。

---

## Part 1: PythonGO/无限易平台

### 1.1 平台概览

| 项目 | 详情 |
|------|------|
| 开发商 | 量投科技（上海）股份有限公司 |
| 官网 | https://infinitrader.quantdo.com.cn/ |
| PythonGO v2文档 | https://infinitrader.quantdo.com.cn/pythongo_v2 |
| QQ群 | 791595859 |
| Python版本 | 3.8+ |
| 依赖 | numpy, pandas, PyQt5, talib, scipy, statsmodels, pydantic |

### 1.2 支持的交易所

| 代码 | 名称 |
|------|------|
| SHFE | 上期所 |
| CZCE | 郑商所 |
| DCE | 大商所 |
| INE | 上海能源 |
| CFFEX | 中金所 |
| GFEX | 广期所 |
| SSE | 上海证券 |
| SZSE | 深圳证券 |

### 1.3 三种运行模式

| 模式 | 客户端 | Logo颜色 | 说明 |
|------|--------|---------|------|
| 模拟盘 | 模拟客户端 | 蓝色 | 测试用，QuantFair/SimNow账号 |
| 实盘 | PythonGO实盘客户端 | 紫色 | 需券商专用客户端+程序化报备 |
| Beta | Beta客户端 | 绿色 | 最新功能，同时支持模拟和实盘 |
| 回测 | 独立Python脚本 | N/A | pythongo.backtesting模块 |

**关键：同一套策略代码在所有模式下运行，切换模式只需登录不同客户端。**

### 1.4 架构与目录结构

```
InfiniTrader/
  pyStrategy/
    pythongo/
      base.py            # BaseStrategy, BaseParams, BaseState, Field
      infini.py           # C++桥接层 (sub/unsub, send_order, cancel_order)
      core.py             # MarketCenter API (K线数据, 主力合约)
      utils.py            # KLineGenerator, KLineProducer, Scheduler
      indicator.py        # 技术指标 (talib封装)
      option.py           # 期权模块
      ui/                 # K线图表UI
      classdef/           # 数据类: TickData, OrderData, TradeData, KLineData, Position
      backtesting/        # 回测引擎
    demo/                 # 内置示例策略
    self_strategy/        # 用户自定义策略 ← 我们的代码放这里
    instance_files/       # 策略实例持久化(JSON)
```

---

## Part 2: PythonGO 策略开发API

### 2.1 事件驱动回调模型

| 回调 | 触发时机 | 参数 |
|------|---------|------|
| `on_init()` | 实例创建/加载 | None |
| `on_start()` | 用户点击"运行" | None |
| `on_tick(tick)` | 每个行情tick | TickData |
| `on_order(order)` | 订单状态变更 | OrderData |
| `on_trade(trade, log)` | 订单成交 | TradeData |
| `on_cancel(order)` | 订单撤销 | CancelOrderData |
| `on_error(error)` | 错误 | dict (errCode, errMsg) |
| `on_contract_status(status)` | 合约状态变更 | InstrumentStatus |
| `on_stop()` | 用户点击"暂停" | None |

### 2.2 K线合成链

```
on_tick(tick) → KLineGenerator.tick_to_kline(tick) → callback(kline)     [K线完成]
                                                   → real_time_callback(kline) [每tick更新]
```

### 2.3 下单API

```python
# 开仓
order_id = self.send_order(
    exchange="SHFE",
    instrument_id="ag2406",
    volume=1,
    price=6000.0,
    order_direction="buy",     # "buy" / "sell"
    order_type="GFD",          # "GFD", "FAK", "FOK"
    market=False               # True = 市价单
)

# 平仓 (自动处理SHFE/INE平今平昨)
order_id = self.auto_close_position(
    exchange="SHFE",
    instrument_id="ag2406",
    volume=1,
    price=6100.0,
    order_direction="sell",
    shfe_close_first=False     # True = 优先平昨
)

# 撤单
self.cancel_order(order_id)
```

### 2.4 持仓查询

```python
pos = self.get_position("ag2406")
pos.position          # 总持仓
pos.net_position      # 净持仓
pos.long.position     # 多头总量
pos.short.position    # 空头总量
pos.long.open_avg_price    # 多头均价
pos.long.close_available   # 可平量

# 实时持仓 (无延迟)
pos = self.get_position("ag2406", simple=True)
```

### 2.5 技术指标

```python
# 通过 KLineGenerator.producer 访问
sma  = self.kline_generator.producer.sma(timeperiod=20)
ema  = self.kline_generator.producer.ema(timeperiod=12)
rsi  = self.kline_generator.producer.rsi(timeperiod=14)
k, d, j = self.kline_generator.producer.kdj()
macd, signal, hist = self.kline_generator.producer.macd()
atr  = self.kline_generator.producer.atr()
upper, mid, lower = self.kline_generator.producer.boll()

# 原始OHLCV数据 (numpy array)
closes = self.kline_generator.producer.close
opens  = self.kline_generator.producer.open
highs  = self.kline_generator.producer.high
lows   = self.kline_generator.producer.low
```

完整指标列表: `sma, ema, std, bbi, cci, rsi, adx, sar, kdj, kd, macd, macdext, atr, boll, keltner, donchian`

K线周期: `M1, M2, M3, M4, M5, M10, M15, M30, M45, H1, H2, H3, H4, D1`

### 2.6 策略模板示例 (V2 API)

```python
from pythongo.base import BaseParams, BaseState, BaseStrategy, Field
from pythongo.classdef import KLineData, TickData, OrderData, TradeData
from pythongo.core import KLineStyleType
from pythongo.utils import KLineGenerator

class Params(BaseParams):
    exchange: str = Field(default="", title="交易所代码")
    instrument_id: str = Field(default="", title="合约代码")
    fast_period: int = Field(default=5, title="快均线周期", ge=2)
    slow_period: int = Field(default=20, title="慢均线周期")
    kline_style: KLineStyleType = Field(default="M5", title="K线周期")
    max_position: int = Field(default=3, title="最大持仓")

class State(BaseState):
    order_id: int | None = Field(default=None, title="报单编号")

class MyStrategy(BaseStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.params_map = Params()
        self.state_map = State()
        self.kline_generator = None

    def on_start(self) -> None:
        self.kline_generator = KLineGenerator(
            real_time_callback=self.real_time_callback,
            callback=self.callback,
            exchange=self.params_map.exchange,
            instrument_id=self.params_map.instrument_id,
            style=self.params_map.kline_style,
        )
        self.kline_generator.push_history_data()
        super().on_start()

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)
        self.kline_generator.tick_to_kline(tick)

    def callback(self, kline: KLineData) -> None:
        """K线完成时调用 — 主要交易逻辑放这里"""
        fast = self.kline_generator.producer.sma(self.params_map.fast_period)
        slow = self.kline_generator.producer.sma(self.params_map.slow_period)
        # ... 交易逻辑 ...

    def real_time_callback(self, kline: KLineData) -> None:
        """每tick更新"""
        pass

    def on_order(self, order: OrderData) -> None:
        super().on_order(order)
        self.output("订单:", order)

    def on_trade(self, trade: TradeData, log=True) -> None:
        super().on_trade(trade, log)
        self.output("成交:", trade)

    def on_stop(self) -> None:
        super().on_stop()
```

---

## Part 3: 从回测到实盘注意事项

### 3.1 PythonGO特有的坑

| 问题 | 说明 | 解决方案 |
|------|------|---------|
| 客户端依赖 | 不能独立运行，关闭客户端策略停止 | 部署在云服务器(Windows VPS) |
| 大小写敏感 | 交易所代码和合约代码严格大小写 | 对照"实时行情"窗口确认 |
| SHFE/INE平今平昨 | 上期所/能源区分平今和平昨 | 用`auto_close_position()` |
| 持仓同步延迟 | `get_position()`成交后有延迟 | 用`simple=True`获取实时持仓 |
| K线边界问题 | 策略启动在分钟边界时K线可能错误 | 等几个tick后再开始交易 |
| 内存溢出 | 过多日志写入导致崩溃 | 控制`self.output()`频率 |
| 代码更新 | 修改后必须通过策略管理器重新加载 | 每次修改后重载策略 |
| MarketCenter限流 | `get_kline_data()`有AI限流，滥用会封IP | 谨慎使用，做好缓存 |

### 3.2 通用回测→实盘风险

- **滑点**: 回测假设理想成交，实盘有滑点
- **延迟**: 信号到执行的时间差
- **过拟合**: 历史最优参数可能实盘失效
- **市场冲击**: 大单对市场的影响
- **风控自建**: 框架只提供基础hook，止损/仓位限制需自己实现

### 3.3 实盘前置条件

1. 下载PythonGO实盘客户端（紫色logo）或券商定制版
2. 完成**程序化报备**（向券商申请）
3. 部署在Windows云服务器保证7×24稳定
4. 先在模拟盘充分测试

---

## Part 4: 飞书交易提醒Bot

### 4.1 方案选择

| 方案 | Webhook自定义机器人 | 自建应用Bot |
|------|-------------------|------------|
| 复杂度 | 极低 | 中等 |
| 设置 | 群聊添加机器人即可 | 需创建应用+管理员审批 |
| 方向 | 单向推送 | 双向交互 |
| 适用场景 | **交易提醒** ✅ | 多群/交互需求 |

**推荐: Webhook Bot** — 完全满足交易通知需求，零基础设施。

### 4.2 技术参数

| 参数 | 值 |
|------|-----|
| 频率限制 | 100次/分钟, 5次/秒 |
| 最大payload | 20 KB |
| 安全方式 | HMAC-SHA256签名 (推荐) |
| 消息类型 | 文本、富文本、图片、Interactive Card |

### 4.3 消息设计 — Interactive Card

```python
# 开仓通知卡片 (绿色header)
{
    "msg_type": "interactive",
    "card": {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "📈 开仓做多 - ag2406"},
            "template": "green"
        },
        "body": {
            "elements": [{
                "tag": "markdown",
                "content": "**品种**: ag2406\n**操作**: 开仓做多\n**价格**: 6,230.00\n**仓位**: 2手\n**止损**: 6,150.00\n\n**逻辑**: MA5上穿MA20+RSI>60+成交量放大"
            }]
        }
    }
}
```

颜色映射:
- 🟢 `green` = 开仓
- 🔵 `blue` = 加仓
- 🟠 `orange` = 减仓
- 🔴 `red` = 平仓

### 4.4 Python实现核心

```python
import json, time, hashlib, base64, hmac, requests
from dataclasses import dataclass
from enum import Enum

class TradeAction(Enum):
    OPEN = "开仓"
    ADD = "加仓"
    REDUCE = "减仓"
    CLOSE = "平仓"

ACTION_COLORS = {
    TradeAction.OPEN: "green",
    TradeAction.ADD: "blue",
    TradeAction.REDUCE: "orange",
    TradeAction.CLOSE: "red",
}

class FeishuTradingBot:
    def __init__(self, webhook_url: str, secret: str = None):
        self._webhook_url = webhook_url
        self._secret = secret

    def _gen_sign(self) -> tuple[str, str]:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            string_to_sign.encode(), digestmod=hashlib.sha256
        ).digest()
        return timestamp, base64.b64encode(hmac_code).decode()

    def send_trade_signal(self, symbol, action, direction, price, reasoning, **kw):
        color = ACTION_COLORS[action]
        payload = {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text",
                              "content": f"{action.value} {direction} - {symbol}"},
                    "template": color,
                },
                "body": {"elements": [{"tag": "markdown",
                    "content": f"**品种**: {symbol}\n**价格**: {price}\n**逻辑**: {reasoning}"}]},
            },
        }
        if self._secret:
            ts, sign = self._gen_sign()
            payload["timestamp"] = ts
            payload["sign"] = sign
        return requests.post(self._webhook_url, json=payload, timeout=10).json()
```

---

## Part 5: QBase_v2 → PythonGO 映射关系

| QBase_v2 概念 | PythonGO 对应 |
|--------------|--------------|
| 策略类 | 继承 `BaseStrategy` |
| 参数配置 | `BaseParams` + `Field()` (pydantic) |
| K线数据 | `KLineGenerator` + `producer.close/open/high/low` |
| 技术指标 | `producer.sma()`, `producer.rsi()` 等 (talib) |
| 下单 | `send_order()` / `auto_close_position()` |
| 持仓查询 | `get_position()` |
| 回调/事件 | `on_tick()` → `callback()` (K线完成) |
| 日志 | `self.output()` |
| 回测 | `pythongo.backtesting.engine.run()` |

---

## Key Sources

### PythonGO 官方文档
- 首页: https://infinitrader.quantdo.com.cn/
- V2文档: https://infinitrader.quantdo.com.cn/pythongo_v2
- BaseStrategy API: https://infinitrader.quantdo.com.cn/pythongo_v2/modules/pythongo_base
- KLineGenerator: https://infinitrader.quantdo.com.cn/pythongo_v2/modules/pythongo_utils
- 技术指标: https://infinitrader.quantdo.com.cn/pythongo_v2/modules/pythongo_indicator
- 数据类: https://infinitrader.quantdo.com.cn/pythongo_v2/modules/pythongo_classdef/position
- 示例: https://infinitrader.quantdo.com.cn/pythongo_v2/examples
- 客户端下载: https://infinitrader.quantdo.com.cn/pythongo_v2/client
- V1→V2迁移: https://infinitrader.quantdo.com.cn/pythongo_v2/v1_to_v2

### 飞书Bot文档
- Webhook机器人: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- 卡片消息: https://open.feishu.cn/document/feishu-cards/quick-start/send-message-cards-with-custom-bot
- 卡片设计器: https://open.feishu.cn/cardkit
- lark-oapi SDK: https://github.com/larksuite/oapi-sdk-python
- 频率限制: https://open.feishu.cn/document/faq/breaking-change/webhook-v2-robot-exceeds-frequency-limit-management
