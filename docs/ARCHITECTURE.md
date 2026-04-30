# PythonGO Engine — Architecture & Development Spec

> **当前生产标准请见 [`STRATEGY_STANDARD_V2.md`](STRATEGY_STANDARD_V2.md)** (2026-04-24 起新策略必读).
>
> **2026-04-28 现状**: 12 个生产策略全部部署 (3 大家族):
> - V8 (Donchian + ADX, 1H, long): AL / CU / HC — ✅ 半年+ 实盘
> - V13 (Donchian + MFI, 1H, long): AG / JM / P / PP — ✅ 半年+ 实盘
> - QExp robust (binary fires + ATR target): AG_Mom (5min) / AG_VSv2 (5min) / I_Pull (15min) / HC_S (30min, short) — ⏸ 上线
> - ICT v6 (state machine + R-ladder + chandelier, 1m + D1 bias): I 铁矿 bidirectional — ⏸ Phase 1 试
>
> **核心基础设施**:
> - 14 active modules + 4 legacy (留作 tests 引用)
> - 88 品种合约规格 (含早盘 10:15-10:30 茶歇 + open_grace 30s + 14:50/22:50 hard cutoff)
> - takeover_lots 模式 (4 处 patch, 解决 18:00 强制清算 → 21:00 重启接管)
> - tools/daily_report/ 每日 HTML+PDF 实盘报告 (2026-04-30 标准定型):
>   broker CSV 主源 + StraLog 辅源, 净盈亏统一用策略 FIFO (出-入)×手数×乘数 +
>   滑点 − 手续费 (broker 盯市仅作对照, 含隔夜价差), 3 层 startup state 校准过滤
>   手动单/异常事件, 中文点评风格 + 波段单/趋势单类型标签 + 自动 carryover
> - 302 pytest 全绿
>
> **历史 session 记录**归档在 [`docs/archive/`](archive/), 含 SESSION_2026_04_17 (Phase 3/4 tick 止损 + ScaledEntry, 现已 legacy) +
> SESSION_2026_04_20 (Fleet-wide 30 文件修复 + PythonGO 源码审计) + 2 个 research.

## 项目总目标

将 QBase / QExp / ICT 等 research 项目里 backtested 验证过的量化策略,移植到 PythonGO 格式,部署到 Windows 无限易客户端实盘运行. 每个策略对应一个独立的 .py 文件,搭配共享 modules 提供风控/执行/监控基础设施.

**转换流程**: 研究端策略 → 移植成 PythonGO .py → 共享 modules 接基础设施 → 实盘部署 → daily_report 反馈

---

## 核心约束

1. **PythonGO要求：类名 = 文件名** — 如 `PortfolioIronLong.py` 内的类必须叫 `PortfolioIronLong`
2. .py文件放到 Windows 无限易 `pyStrategy/self_strategy/` 即可运行
3. 单文件自包含 — 所有逻辑在一个文件内，不import QBase/AlphaForge
4. 只依赖PythonGO内置包：numpy, requests + pythongo模块
5. 指标必须从QBase原样移植（纯numpy），不用talib（避免信号偏差）

---

## 当前生产策略 (12 个)

| 家族 | 品种 / Alias | 周期 | 方向 | 信号 | 部署 |
|------|-------------|------|------|------|------|
| **V8** Donchian + ADX | AL / CU / HC | 1H | long | forecast 0-10 + Carver vol target | ✅ 半年+ |
| **V13** Donchian + MFI | AG / JM / P / PP | 1H | long | 同 V8 | ✅ 半年+ |
| **QExp robust** | AG_Mom | 5min | long | 强阳线 body>1.5×ATR + cooldown 3 | ⏸ 4-28 |
| | AG_VSv2 | 5min | long | trend z>0.5 + ATR squeeze + 突破 | ⏸ 4-28 |
| | I_Pull | 15min | long | 强趋势 z>1.0 + 回撤 ≥1.5 ATR | ⏸ 4-28 |
| | HC_S | 30min | **short** | vol expansion + 跌破 | ⏸ 4-28 |
| **ICT v6** | I (铁矿) | 1m + D1 bias | bidirectional | sweep + displacement + FVG + OTE 70.5% + R-ladder | ⏸ 4-28 |

**风险参数差异**:
- V8/V13: max 5 contracts, hard 0.5%, trail 0.3%, daily review 15:15
- QExp: max 3 contracts, hard 2%, profit target 2×ATR, no trailing
- ICT: 0.5% equity per trade, max 5, R-ladder (0.5/1.5/3R), max 3 trades/day, daily_stop_r −2 / lock_r +3, hard cutoff 14:50/22:50

详细策略参数见各自源文件:
- V8: `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py`
- V13: `src/AG/long/AG_Long_1H_V13_Donchian_MFI.py`
- QExp: `src/qexp_robust/` + README
- ICT: `src/ICT/` + README

---

## PythonGO Template规范

从已验证的 `pythongo-strategy/` 项目提取：

### 类结构
```python
from pythongo.base import BaseParams, BaseState, Field
from pythongo.classdef import KLineData, OrderData, TickData, TradeData
from pythongo.ui import BaseStrategy          # 注意: ui版, 不是base版
from pythongo.utils import KLineGenerator
```

### KLineGenerator初始化
```python
def on_start(self):
    self.kline_generator = KLineGenerator(
        callback=self.callback,
        real_time_callback=self.real_time_callback,
        exchange=..., instrument_id=..., style=...
    )
    self.kline_generator.push_history_data()  # 必须在 super().on_start() 之前
    super().on_start()
```

### 下单（已验证可用）
```python
# 开仓/加仓
oid = self.send_order(exchange, instrument_id, volume, price,
                      order_direction="buy", market=True)

# 平仓/减仓
oid = self.auto_close_position(exchange, instrument_id, volume, price,
                               order_direction="sell", market=True)

# 持仓查询
pos = self.get_position(instrument_id).net_position
```

### 关键规则
- **Same-bar执行 (2026-04-14修复)**: 信号在当前bar产生后**立即提交TWAP执行**，不等下一根bar。
  - 旧行为: 信号存入`_pending`→下一根bar的callback开头才处理→多等一整根bar（H1=1小时延迟）
  - 新行为: `_on_bar`末尾检查`_pending`→止损立即`_execute()`/正常信号立即`_submit_twap()`→TWAP在当前bar的tick流中分批成交
  - 和QBase回测一致: Bar N收盘出信号→Bar N+1期间执行
  - `_on_bar`顶部仍保留`_pending`处理逻辑作为安全网（正常情况下不会触发）
- **非交易时段门控 (2026-04-17修复)**: 所有挂单/撤单必须在交易时段内完成。
  - 触发案例: 08:59 (SHFE AL开盘前1分钟) 策略尝试撤单, SHFE pre-opening拒绝 → `{'errCode': '0004', 'errMsg': '已撤单报单被拒绝SHFE:当前状态禁止此项操作'}`; 随后09:00 VWAP以0手异常完成
  - 根因: v8 inline VWAP + TWAP策略的 `_on_bar` 顶部撤单 + `_execute` 均未做 session check
  - 修复: 四层门控 (全部基于 `self._guard.should_trade()`):
    1. `_on_bar` 入口: 非交易时段直接 return, 不撤单/不挂单/不生成信号 (pending保留, 下根bar处理)
    2. `_on_tick_vwap`/twap模块 `check()`: tick级发单前检查 (TWAP策略走shared `modules/twap.py`的`is_in_session`)
    3. `_submit_vwap`/`_submit_twap`: 启动VWAP/TWAP窗口前检查
    4. `_execute`: 止损等immediate action发单前兜底防御
  - 覆盖范围: 22个策略文件 + 2个模板 (所有production Portfolio + 单策略 + VWAP变体v8 + Template_Long/Short)
- **每bar开头撤挂单**: `for oid in list(self.order_id): self.cancel_order(oid)` — **必须在session gate之后执行**
- **order_id追踪**: 用set追踪委托ID，在on_trade/on_order_cancel中清理
- **market=True**: 市价单，price参数仅用于显示
- **self.output()**: 不用print()

### API踩坑记录 (2026-04-02实测)
- **`get_account_fund_data("")` 会崩溃！** 正确用法：
  ```python
  investor = self.get_investor_data(1)
  account = self.get_account_fund_data(investor.investor_id)
  ```
- AccountData属性: balance, available, position_profit, close_profit, margin, commission, risk
- 建议在on_start中缓存investor_id，后续复用
- **`Field(title=...)` 不能含特殊字符** (逗号 `,` / 等号 `=` / 大于号 `>` / 括号 `(` `)`).
  `pythongo/base.py::__package_params` 把 title 当 key 反查 `model_fields`,含特殊字符
  时被截断 → `KeyError`. 详细说明只放代码注释,title 必须是简洁中文.
  ```python
  # ❌ 启动崩溃:
  takeover_lots: int = Field(default=0, title="启动接管手数(0=按state恢复, >0=手动接管)")
  # ✅ 正确:
  # takeover_lots: 启动时手动指定接管手数 (0=按 state 恢复; >0=手动接管, 覆盖 state)
  takeover_lots: int = Field(default=0, title="启动接管手数")
  ```
  (2026-04-27 takeover 推广实盘发现 + 7 文件修复)

### PythonGO数据特点
- **只提供tick数据** — K线全靠KLineGenerator从tick合成
- **KLineProducer有**: close, high, low, open, volume, datetime (numpy数组)
- **KLineProducer没有**: open_interest — 需要在callback中手动收集
- **KLineData有**: open_interest字段 — 每根K线完成时可读取

---

## 双频率架构

当前portfolio有daily和1H两个频率，需要两个KLineGenerator：

```
on_tick(tick)
  ├─ kline_gen_1h.tick_to_kline(tick)    → callback_1h(kline)
  └─ kline_gen_daily.tick_to_kline(tick) → callback_daily(kline)

callback_daily: v27 SuperTrend信号 → daily_forecast → daily_lots (每天更新一次)
callback_1h:    v18 TRIX+OI信号   → hourly_forecast → hourly_lots (每小时更新)

net_target = daily_lots + hourly_lots
实际执行: current_position → net_target, 差额下单
```

### Daily信号特点
- Daily bar完成时间: 15:00 (包含前一晚夜盘21:00-23:00 + 当日09:00-15:00)
- 每天只触发一次信号更新
- 使用continuous sizing (Vol Targeting公式每日重算)

### 1H信号特点
- 每小时触发一次
- 使用fixed entry sizing
- 需要OI数据 — 在callback中从KLineData.open_interest手动积累

### Net Position管理
- 所有频率汇总成一个net position
- daily多 + 1H空 = net position（可以自然对冲）
- 每次执行只看 `current_pos → net_target`，差额下单

---

## 止损体系（必须内置）

每个.py文件不管QBase那边怎么样，都必须自带以下止损：

### 1. 移动止损 (Trailing Stop)
```
peak_price 追踪持仓期间最高价
if close <= peak_price × (1 - trailing_pct):
    pending = "TRAIL_STOP"  → 下一bar平仓
```

### 2. 硬止损 (基于账户权益2%)
```
if 当前持仓浮亏 > 账户权益 × 2%:
    pending = "HARD_STOP"  → 下一bar平仓
```
- 基于**账户权益**，不是开仓价
- 这是绝对兜底，不受策略信号影响

### 3. Portfolio Stops (回撤止损)
```
drawdown = (equity - peak_equity) / peak_equity
-10%: 预警 (飞书通知)
-15%: 减仓至50%
-20%: 熔断平仓
```

### 4. 单日止损
```
daily_pnl = (equity - daily_start_equity) / daily_start_equity
-5%: 当日停止交易，全部平仓
```

### 优先级
止损 > 止盈 > 出场信号 > 加仓 > 建仓

---

## 仓位管理

### Vol Targeting公式 (Daily策略滚动加仓)
```
target_lots = (forecast/10) × (target_vol/realized_vol) × (capital/notional)

- forecast: 合并后的forecast [0, 20]
- realized_vol: (ATR × √annual_factor) / price
- notional: price × multiplier (一手名义价值)
- annual_factor: 252×4=1008 (1H) 或 252 (daily)
```
Daily策略每天重新计算target_lots。

### Carver Position Buffer (最小调仓阈值)
```
buffer_width = max(optimal_position × 10%, 0.5)
if optimal在 [current - buffer, current + buffer] 内:
    不交易
```
减少约50%交易次数，节省手续费。

---

## 9个运维问题解决方案

### 1. 每日P&L重置
**方案**: `当前时间+4小时`推算交易日

```python
# 21:00+4h=01:00(次日) → 正确归入下一交易日
# 09:00+4h=13:00(当日) → 正确归入当日
shifted = datetime.now() + timedelta(hours=4)
trading_day = shifted.strftime("%Y%m%d")
```
- P&L重置点: 09:00日盘开盘，不是午夜
- 夜盘P&L自动累入下一交易日
- 周五夜盘 → 周一交易日（需跳过周末）

### 2. 夜盘Daily K线
**方案**: PythonGO的`D1` KLineGenerator应自动处理

- Daily bar = 21:00(T-1) → 15:00(T)
- 完成时间: 15:00收盘
- 需在模拟盘验证PythonGO的D1行为是否正确
- 如不正确，自己从1H bar合成daily bar

### 3. 状态持久化
**方案**: 原子写JSON文件

```python
# 保存: 写临时文件 → fsync → rename (原子操作)
# 恢复: 读主文件，失败读备份
# 保存时机: 每笔成交后 + session边界(09:00/15:00/21:00)
# 保存内容: peak_equity, daily_start_equity, current_position, trading_day
```
文件位置: 无限易安装目录下 `pyStrategy/state/`

### 4. 换月提醒
**方案**: 初期手动换月，代码负责提醒

```python
# 到期前15个交易日: 飞书发提醒
# 到期前5个交易日: 飞书发紧急提醒
# 交割月: 个人投资者持仓限额为0手，必须提前平仓
```
- 铁矿石主力合约: 1-5-9月
- 后续可加OI交叉自动检测

### 5. 飞书不阻塞
**方案**: daemon线程 + 有界队列

```python
# 主线程: queue.put_nowait(msg)  ← 不阻塞，队列满则丢弃
# 后台线程: daemon=True, 从队列取消息发送
# 速率: 0.2秒/条 (飞书限5条/秒)
```
交易逻辑永远不被通知阻塞。

### 6. 订单管理
**方案**: 状态追踪 + 超时撤重

```python
# 每笔订单记录: order_id, status, volume, filled, created_time
# 30秒超时: 撤单并重发
# 涨跌停拒单: 飞书通知，等下一bar
# 部分成交: 下一bar自然修正(target-current差额)
```

### 7. 保证金变动
**方案**: 启动时查询 + 40%余量

```python
# 用 get_account_fund_data("").available 检查可用资金
# 下单前检查: 所需保证金 < 可用资金 × 60%
# 留40% buffer应对保证金率上调
```

### 8. 最小调仓阈值 (Carver Buffer)
**方案**: 10% buffer

```python
buffer = max(abs(optimal) * 0.10, 0.5)
if abs(optimal - current) <= buffer:
    不交易
```
- 10% buffer是Carver标准值
- 减少约50%交易次数
- 最小交易: 1手

### 9. 重启有持仓
**方案**: 信任实际持仓

```python
# on_start():
#   1. get_position() 查实际持仓
#   2. 读本地JSON恢复peak_equity等
#   3. 对比，不一致则信任broker + 飞书报警
#   4. push_history_data() warmup指标
#   5. warmup期间不交易 (trading=False until warmup complete)
```
关键原则: **永远信任broker实际持仓，不信自己记录的**

---

## QBase → PythonGO 指标移植清单

当前portfolio需要的指标（从QBase纯numpy实现原样移植）：

| 指标 | QBase文件 | 依赖 | 用于 |
|------|----------|------|------|
| `_ema` (SMA seed) | `indicators/_utils.py` | numpy | MACD, TRIX |
| `ema` (data[0] seed) | `indicators/trend/ema.py` | numpy | v6等 |
| `supertrend` | `indicators/trend/supertrend.py` | numpy | v27 (daily) |
| `trix` | `indicators/momentum/trix.py` | `_ema` | v18 (1h) |
| `oi_momentum` | `indicators/volume/oi_momentum.py` | numpy | v18 (1h) |
| `atr` (Wilder RMA) | `indicators/volatility/atr.py` | numpy | Chandelier Exit, Vol Targeting |
| `sma` | helper | numpy | 各处 |

注意: **不用talib**。QBase指标和talib在EMA seed、Bollinger ddof等细节上有差异，直接用talib会导致信号偏离回测结果。

---

## 文件结构

```
pythongo_engine/                     ← Mac开发仓库
├── docs/
│   ├── ARCHITECTURE.md              # 本文件
│   ├── MODULE_REFERENCE.md          # 模块参考手册
│   ├── RESEARCH_REPORT.md           # PythonGO + 飞书 deep research
│   └── OPERATIONAL_ISSUES_RESEARCH.md # 9个运维问题详细调研
│
├── src/                             # ★ 部署目录 — 整个复制到 self_strategy/
│   ├── TestFullModule.py            # 测试策略 (M1双均线 + 全模块)
│   ├── DailyV9_ROC_OBV.py          # 日线策略 (旧单文件版, 待更新)
│   ├── PortfolioIronLong.py         # Portfolio策略 (旧单文件版, 待更新)
│   └── modules/                     # ★ 通用运维模块 (所有策略共享)
│       ├── __init__.py
│       ├── feishu.py                # 飞书非阻塞通知
│       ├── persistence.py           # 状态持久化 (JSON原子写)
│       ├── trading_day.py           # 交易日检测 (+4小时法)
│       ├── risk.py                  # 止损体系 (6种止损)
│       ├── slippage.py              # 滑点记录
│       ├── heartbeat.py             # 心跳监控
│       ├── order_monitor.py         # 订单超时
│       ├── performance.py           # 绩效追踪
│       ├── rollover.py              # 换月提醒
│       └── position_sizing.py       # Vol Targeting + Carver Buffer
│
├── tests/                           # Mac上的单元测试
└── README.md
```

### 部署方式
```
pyStrategy/                      ← 无限易Python根目录
  pythongo/                      ← 框架(自带, 不动)
  modules/                       ← ★ 我们的模块放这里 (与pythongo同级)
    __init__.py
    feishu.py
    persistence.py
    ...
  self_strategy/
    TestFullModule.py            ← ★ 策略文件放这里
    DailyV9_ROC_OBV.py
```

策略文件中 `from modules.feishu import feishu` 即可使用。
modules/ 必须放在 pyStrategy/ 下 (与pythongo/同级), 不是self_strategy/下。

---

## .py文件内部结构 (Section划分)

```
Section 1: CONFIG         — 合约参数、策略权重、风控阈值、飞书配置
Section 2: INDICATORS     — QBase指标纯numpy移植
Section 3: SIGNALS        — 各策略信号逻辑 (纯函数)
Section 4: BLENDER        — Carver Signal Blending
Section 5: POSITION       — Vol Targeting + Carver Buffer
Section 6: RISK           — 移动止损 + 硬止损(2%权益) + Portfolio Stops
Section 7: OPERATIONS     — 状态持久化 + 交易日检测 + 换月提醒
Section 8: FEISHU         — 非阻塞飞书通知 (daemon线程)
Section 9: STRATEGY       — PythonGO主入口类 (BaseStrategy)
```

---

## 测试进度 (2026-04-02)

### 已测试通过 ✅
- [x] 基础交易: 开仓/加仓/平仓 (M1 MA3/MA7)
- [x] Next-bar规则 + 执行后return
- [x] 撤挂单 + order_id追踪
- [x] K线图表widget
- [x] 飞书非阻塞通知 (daemon线程)
- [x] 飞书卡片: 开仓/加仓/减仓/平仓/策略启动/策略停止
- [x] get_account_fund_data 正确调用 (investor_id)
- [x] 硬止损 (价格)
- [x] 移动止损
- [x] 权益止损 (2%)
- [x] Portfolio Stops (回撤 -10/-15/-20%)
- [x] 单日止损 (-5%)
- [x] 保证金检查
- [x] 文件写入 (os/json)

### 待测试 ⏳
- [ ] 状态持久化 (JSON保存→暂停→重启→恢复)
- [ ] 重启持仓恢复 (有仓暂停→重启)
- [ ] 交易日切换检测 (+4小时法)
- [ ] Carver 10% Buffer
- [ ] 每日08:00回顾推送

### 已知问题 ⚠️
- `get_account_fund_data("")` 传空字符串崩溃 → 必须传investor_id
- 执行pending后必须return，否则同bar重复生成信号

## 开发计划

### Phase 1: 模块验证 (进行中)
- [x] 基础交易验证
- [x] 飞书通知验证
- [x] 止损体系验证
- [ ] 持久化 + 重启恢复验证
- [ ] 交易日切换验证
- [ ] Carver Buffer验证

### Phase 2: 策略转换
- [ ] 替换v6/v7/v8/v9 → v27(daily) + v18(1h)
- [ ] 移植SuperTrend + TRIX + OI_Momentum指标
- [ ] 双KLineGenerator (D1 + H1)
- [ ] OI数组手动收集
- [ ] Net position管理
- [ ] DailyV9_ROC_OBV.py 更新为正确API

### Phase 3: 模拟盘全链路
- [ ] 验证D1 K线在夜盘的行为
- [ ] 验证OI数据可用性
- [ ] 信号对比: PythonGO vs QBase
- [ ] 全链路测试 (下单→止损→飞书→持久化→重启)

### Phase 4: 实盘
- [ ] 程序化报备
- [ ] Windows VPS + 自动重启
- [ ] 50%仓位试运行一个月
- [ ] 监控滑点 (fill_price vs signal_price)
