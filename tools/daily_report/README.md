# Daily Report — PythonGO 实盘交易日报

每天根据**无限易客户端导出的 broker CSV** (主) + `StraLog*.txt` (辅) 自动生成中文 HTML + PDF 日报。

**2026-04-28+ 新流程**: broker CSV 是成交真值源 (盈亏/手续费/撤单 broker 报回), StraLog 提供决策上下文 (信号/风控/原因)。CSV 缺失时自动 fallback 到旧 log 流程。

## 用法

每天的固定流程:

```bash
# 1. 新建当天目录
mkdir -p reports/MM-DD

# 2. 把以下文件放进 reports/MM-DD/:
#    StraLog*.txt                                    ← 必需 (决策上下文)
#    实时回报(信息导出)_YYYY_MM_DD-HH_MM_SS.csv      ← 推荐 (broker 真值)
#    {品种}.jpg                                       ← 可选 (盘面截图, 大写品种名)
#       e.g. HC.jpg / P.jpg / PP.jpg / JM.jpg / AL.jpg / CU.jpg / AG.jpg
#    image_map.json                                   ← 可选 (hash 命名图映射)

# 3. 跑报告 (默认输出 HTML + PDF)
python3 tools/daily_report/generate_report.py --date YYYY-MM-DD --data-dir reports/MM-DD

# → 输出:
#    reports/MM-DD/YYYY-MM-DD.html
#    reports/MM-DD/YYYY-MM-DD.pdf
```

参数:

| 参数 | 说明 |
|------|------|
| `--date YYYY-MM-DD` | 目标交易日, 默认今天 |
| `--data-dir <path>` | 当天素材目录 (含 log + CSV + 图), 也是默认输出目录 |
| `--log <path>` | 显式指定 log 文件; 不传时自动找 data-dir 里 mtime 最新的 `StraLog*.txt` |
| `--out <path>` | 显式指定输出目录 |
| `--no-pdf` | 只生成 HTML, 不生成 PDF |

## 数据源 — CSV 优先, log 兜底

工具会优先扫 `data-dir` 下名字含 `实时回报` 或 `信息导出` 的 `*.csv` 文件:

- **找到 CSV** → 走 broker 真值流程 (推荐):
  - 配对: 按 instrument_id FIFO + 隔夜接管自动识别
  - 盈亏: broker 报回的真实平仓盈亏 (含隔夜价差)
  - 手续费: broker 精确到分
  - 滑点: 报单价 vs 成交均价直接算 (不依赖策略 log)
  - 撤单: 单独成段 (今天 OrderMonitor 升级催出来的)
  - StraLog 仍用作决策上下文 (POS_DECISION / EXECUTE / IND / SIGNAL / TRAIL_STOP)
- **没 CSV** → 走旧 log 流程:
  - 用 `[滑点] N.N ticks` log 行计算滑点
  - ON_TRADE log 配对 round-trip
  - 平仓盈亏 = (出 − 入) × 手数 × 乘数

## 报告范围 — 中国期货交易日窗口

**T 日报告** = T-1 个交易日 21:00 → T 日 15:00, 跨周末 / 节假日自动回溯到上一交易日 21:00。

| 目标日 | 窗口 |
|--------|------|
| 周二 | 周一 21:00 → 周二 15:00 |
| 周一 | 周五 21:00 → 周一 15:00 (跳过周末) |
| 节假日后首日 | 节前最后一个交易日 21:00 → 当日 15:00 |

节假日表手工维护在 `session_window.py::_HOLIDAYS_2026`。

## 输入约定

`data-dir` 目录内可以包含:

### 必需

- **log 文件**: `StraLog*.txt` (无限易导出的日志, 自动找 mtime 最新)

### 推荐 (CSV 主流程)

- **broker CSV**: `实时回报(信息导出)_YYYY_MM_DD-HH_MM_SS(N).csv`
  - 编码 GBK, 工具自动解码
  - 41 列: 序号 / 投资者 / 报单编号 / 交易所 / 合约 / 买卖 / 开平 / 报单价 / 成交均价 / 状态 /
    手续费 / 平仓盈亏 / 平仓盈亏(逐笔) / 报单时间 / 成交时间 / 策略路径 / 等
  - 时间从文件名推交易日 (broker"成交时间(日)"列因不同交易所标法不一致, 不可靠)
  - 21:00+ 时间自动归到前一日历日 (无限易交易日规则)

### 可选

- **盘面截图**: 两种命名方式
  1. **直接命名 `{品种}.jpg`** (最简): `HC.jpg`, `P.jpg`, `PP.jpg`, `AL.jpg`, ...
     必须是 1-4 个英文字母, 自动识别
  2. **hash 命名 + image_map.json**: 适合无限易截图工具直接保存的 hash 名

```json
{
  "HC": "8ee156d3f1bd98bac847821a6e685c78.jpg",
  "P":  "e2080c416a70383bdf596286f004cf1a.jpg",
  "PP": "95d25b7c37fc0b17a7e856281036a53d.jpg"
}
```

## 报告结构

### CSV 流程 (broker 真值)

1. **当日汇总卡片** (6 张):
   - 净盈亏 (broker − 手续费) ★ 主指标
   - broker 平仓盈亏 (含隔夜价差)
   - 逐笔盈亏 (按本笔开仓价)
   - 手续费总额
   - 已平仓 / 持有中 (含接管 / 撤单数)
   - 交易品种

2. **所有交易明细表** (按时间):
   时间 / 品种 / 方向 / 手数 / 入场价 / 出场价 / **broker盈亏** / **逐笔盈亏** / **手续费** / 净盈亏 / 状态
   (隔夜接管的孤儿平仓显示橘色 "隔夜接管" 标签, 入场价为 "—")

3. **当日撤单段** (3 笔示例):
   报单时间 / 品种 / 方向 / 开平 / 报价 / 手数 / 状态
   通常是 OrderMonitor escalator 触发的撤单重发, 单独可见

4. **每品种独立 section** (同 log 流程):
   - 盘面图 (如有, 含 image_map.json 映射)
   - 策略 pivot box (家族 + 参数 + 入场公式 + 入场前置 + 出场触发 + 移动止损/ATR 止盈公式)
   - 开仓原因表 (raw signal / optimal / target / ATR / 当时指标值)
   - 平仓原因表 (移动止损 / 硬止损 / ATR 止盈 / 信号反转 + 详情)
   - 本品种交易明细表

### log 流程 (CSV 缺失时)

汇总卡片改为: 净盈亏 / 毛盈亏 / 滑点损益 / 平均滑点 / 已平仓+持有中 / 交易品种
主表改为: 时间 / 品种 / 方向 / 手数 / 入场价 / 出场价 / 毛盈亏 / 滑点损益 (跳数) / 净盈亏 / 状态

## 隔夜接管的孤儿平仓 (CSV 流程独有)

当 broker CSV 出现"今日平仓但 FIFO 队列里找不到对应今日开仓"时, 标 `is_takeover=True`:

- 入场价 = "—" (真开仓在昨天, CSV 没有)
- gross_pnl = broker_pnl (broker 报回的真值, 含隔夜价差)
- 主表 "状态" 列显示橘色 "隔夜接管" 标签
- 净盈亏汇总照常计入

例: 4-28 启动时 state.json 接管了 JM 2 手, 21:16 移动止损触发, broker 报 −540 元 — 这笔在旧
log 流程下会被 `pair_trades` 完全忽略 (没今日开仓配对), CSV 流程下完整入账。

## 滑点定义

### CSV 流程

直接 `(报单价 - 成交均价)` 算 ticks, 按方向 flip 符号:
```
买单: send=9757, fill=9760  →  diff=+3 (买高了, 不利)  →  advantage = -3 ticks
卖单: send=9757, fill=9767  →  diff=+10 (卖高了, 有利)  →  advantage = +10 ticks
```
报告内显示**已 flip**: 正跳数 = 有利 (与金额方向一致)。

### log 流程

直接用 log 里的 `[滑点] N.N ticks` tag 数据 (策略 `SlippageTracker` 计算的真滑点)。

- log 约定: 正 = 不利, 负 = 有利 (signal_price 视角)
- 报告内已 flip: 正 = 有利
- 同 oid 拆批 fills 共享首笔 ticks (broker 拆批同价成交)

## Pivot 公式来源

每个品种 section 顶部的 pivot 公式 / 入场条件 / 出场触发 / 移动止损公式,
**全部从 `src/{品种}/long/{文件}.py` 解析提取** (常量 + Params 默认值), 不是手写。
修改策略源文件后, 下次跑报告会自动反映新参数。

支持的策略家族:

| 家族 | 信号 | 品种 |
|------|------|------|
| **V8** | Donchian + ADX + PDI/MDI | AL / CU / HC |
| **V13** | Donchian + MFI | AG / P / PP / JM |
| **QExp robust** | Momentum / VolSqueeze / Pullback / HighVolBreakdown | AG_Mom / AG_VSv2 / I_Pull / HC_S |

新增其他家族: 在 `pivot_extractor.py::extract_pivot` 加分支。

## Trade 配对

### CSV 流程 (`pair_from_csv`)

按 **instrument_id** 分队列 (避免 hc2510 / hc2511 跨合约混):

| status / 状态 | 含义 | 操作 |
|---|---|---|
| 全部成交 + 开仓 | push long_queue / short_queue (按 buy/sell) |
| 全部成交 + 平仓/平今 | FIFO 取对应方向开仓; **没找到 → 标 takeover** |
| 撤单 | 不进 round_trips, 单独存 cancelled 列表 |

每对 OPEN+CLOSE = 一个 round-trip, 拆批按手数比例分摊手续费 / broker_pnl。

### log 流程 (`pair_trades`, 旧版)

按 ON_TRADE 时序处理: direction='0' offset='0' 买开 / direction='1' offset∈('1','3') 卖平多 / 等。
平仓量超出开仓队列累计的部分会被忽略 (可能是策略外预存仓, 或 takeover — 这种情况下用 CSV)。

## 合约规格 (乘数 / tick / 手续费)

直接读取 AlphaForge `~/Desktop/AlphaForge/alphaforge/data/specs.yaml` (覆盖 95 个品种,
含 fixed/ratio 两种手续费类型)。环境变量 `ALPHAFORGE_SPECS_PATH` 可覆盖路径。
找不到时 fallback 到 `specs.py::_FALLBACK_MULT/_TICK` 硬编码表。

**注意**: log 流程下不展示手续费; CSV 流程下展示 broker 报回的精确手续费 (元)。

## PDF 生成

调用本机 Chrome / Chromium / Edge / Brave 的 headless 模式:

```bash
chrome --headless --disable-gpu --no-pdf-header-footer \
       --print-to-pdf=output.pdf file:///path/to/report.html
```

CSS 已配 `@media print { .section { page-break-inside: avoid } }`, 品种 section 不会被分页打断。

## 模块结构

```
tools/daily_report/
├── __init__.py
├── generate_report.py    # 主入口 (argparse + Chrome PDF + CSV 优先 + image_map.json)
├── csv_parser.py         # ★ 无限易"实时回报"CSV 解析器 (GBK + 41 列 + 21:00 边界)
├── log_parser.py         # 解析 StraLog 各类 event tag (含中文 [滑点])
├── session_window.py     # T-1 21:00 → T 15:00 窗口计算
├── specs.py              # 读 AlphaForge specs.yaml + fallback 表
├── pivot_extractor.py    # 解析策略源文件提取真实公式
├── trade_pairing.py      # FIFO 配对 (pair_trades log 版 + pair_from_csv broker 版)
├── render_html.py        # 白色主题极简风格 HTML 渲染 (双流程兼容)
└── strategy_aliases.json # 策略 alias → 源文件路径 (11 个生产策略)
```

## 测试

```bash
python3 -m pytest tests/ -q
```

302 个 pytest 测试全绿, 涵盖 session window / log parser / specs / FIFO 配对 / 滑点。

## 设计决策记录

### 为什么从 log 流程切到 CSV 流程? (2026-04-28)

- **隔夜接管的孤儿平仓**: log 流程下 JM 21:16 trail_stop 平仓被 `pair_trades` 忽略 (没今日开仓配对), CSV 流程精确入账 broker 真值
- **手续费**: log 没有, CSV 精确到分
- **撤单**: log 流程很难提出来, CSV 直接 status 列分类
- **盈亏 ground truth**: broker 报回的逐日盯市盈亏比策略层估算精确 (含隔夜价差)
- **解析稳定**: CSV 是 41 列固定结构, log regex 在格式微调时容易失效

### 为什么 broker "成交时间(日)" 列不可靠?

观察发现: 同一时刻 (4-27 22:00) 不同交易所 broker 标法不一致 (HC 标 20260427, P 标 20260428).
**对策**: 不信任该列, 自己用 `HH:MM:SS + 文件名交易日` 推 calendar (21:00+ 时间归前一日历日)。

### 为什么 image_map.json 是可选的?

无限易截图保存常用 hash 名 (e.g. `8ee156d3f1bd98bac847821a6e685c78.jpg`), 不能直接被
`{品种}.jpg` 自动扫描。两条路:
1. 用户手动 mv 改名 (一次性, 简单)
2. 提供 image_map.json 把 hash 名映射到品种 (零改名, 需要每天写 json)

工具优先读 image_map.json, fallback 到 `{品种}.jpg` 自动扫描。

### 为什么用 log [滑点] 而不是自己重算? (log 流程)

之前曾用 `(send_price - fill_price)` 自己算, 但 `send_price` 是策略**主动让价后的限价**
(已穿盘口), 不是真"理想价"。策略本身的 `SlippageTracker` 用 `signal_price` (决策时 bar
close 或 trail 触发时 last_price) 作为 reference, 这是 industry standard "implementation
shortfall" 算法。

CSV 流程下不用 log 滑点, 直接 (send_price - fill_price) 也能用 — 因为 CSV 的 send_price
是 broker 真实报单价 (而非策略主动让价后的内部限价), 等同 signal_price 的角色。

## 已知限制

- **节假日表手工维护到 2026 年**: `session_window.py::_HOLIDAYS_2026`, 跨年需手动加
- **跨年没测**: T-1 跨年 (如 1-2 报告应回溯到上一年 12-30) 未做边界测试
- **多张盘面图**: 一个品种只支持一张图, 文件名按品种唯一
- **CSV 无 ag_pnl 拆分**: 当一笔平仓在 broker 端拆批 (e.g. PP 11:29 拆 1+3) 时, 工具按手数
  比例分摊 broker_pnl / 手续费, 与 broker 内部精确分摊可能有 < 1 分误差
