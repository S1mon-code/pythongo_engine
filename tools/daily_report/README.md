# Daily Report — PythonGO 实盘交易日报

每天根据**无限易客户端导出的 broker CSV** (主) + `StraLog*.txt` (辅) 自动生成中文 HTML + PDF 日报。

**2026-04-30 标准**: broker CSV 是成交真值源, StraLog 提供决策上下文 + strategy 启动状态校准 (区分真 takeover / 手动单 / broker 异常)。**净盈亏统一用 strategy FIFO 配对** `(出场价 - 入场价) × 手数 × 乘数 + 滑点 - 手续费`, broker 盯市仅作对照参考 (含隔夜价差不反映策略实际操作)。

## 用法

```bash
# 1. 新建当天目录
mkdir -p reports/MM-DD

# 2. 把以下文件放进 reports/MM-DD/:
#    StraLog*.txt                                    ← 必需 (决策上下文 + ON_START 状态)
#    实时回报(信息导出)_YYYY_MM_DD-HH_MM_SS.csv      ← 必需 (broker 真值)
#    {品种}.jpg                                       ← 推荐 (盘面截图, 大写品种名)
#    image_map.json                                   ← 可选 (hash 命名图映射)

# 3. 跑报告 (默认 HTML + PDF)
python3 tools/daily_report/generate_report.py --date YYYY-MM-DD --data-dir reports/MM-DD
```

参数:

| 参数 | 说明 |
|------|------|
| `--date YYYY-MM-DD` | 目标交易日, 默认今天 |
| `--data-dir <path>` | 当天素材目录 (含 log + CSV + 图), 也是默认输出目录 |
| `--log <path>` | 显式指定 log 文件 |
| `--out <path>` | 显式指定输出目录 |
| `--no-pdf` | 只生成 HTML, 不生成 PDF |

## 数据源 — CSV 优先, log 兜底

工具会优先扫 `data-dir` 下名字含 `实时回报` 或 `信息导出` 的 `*.csv` 文件:

- **找到 CSV** → 走 broker 真值流程 (推荐)
- **没 CSV** → 走旧 log 流程 (fallback)

CSV 流程下:
- **配对**: 按 instrument_id FIFO + 自动找前一日 CSV 推 carryover 持仓
- **盈亏**: 策略 FIFO 配对 (主) + broker 报回真值 (对照)
- **手续费**: broker 精确到分
- **滑点**: 报单价 vs 成交均价直接算
- **撤单**: 单独成段 (OrderMonitor escalator 触发的)
- **决策上下文**: 当日 + 前日 StraLog 的 POS_DECISION / EXECUTE / IND / SIGNAL / TRAIL_STOP

## 盈亏口径 (★ 核心)

### 策略 FIFO 真值 (主指标)

PythonGO 是 FIFO 配对 — **必须用入场价和出场价配对算盈亏**, broker 报的"逐日盯市盈亏"含隔夜价差不反映策略实际操作。

```
毛盈亏  =  (出场价 - 入场价) × 手数 × 乘数      [long]
        =  (入场价 - 出场价) × 手数 × 乘数      [short]
滑点损益 = slip_ticks × tick_size × 手数 × 乘数  [正=有利]
净盈亏  = 毛盈亏 + 滑点损益 - 手续费
```

**例**: P 4-30 11:15 多 3 手, 入场 ¥9,840 → 出场 ¥9,838:
- 毛盈亏: (9838 - 9840) × 3 × 10 = **−¥60** (选时小亏)
- 滑点: 出场报 ¥9,828 成 ¥9,838 = +5 跳 × 2 × 3 × 10 = **+¥300** (卖高了)
- 手续费: ¥15.10
- **净盈亏: +¥224.90** (执行层把单子救回来)

主表/汇总卡片/品种段全部用此口径。**报告里"入场 > 出场但仍盈利"是合法的** — 滑点收益超过选时亏损时会出现。

### broker 盯市对照 (灰字参考)

broker CSV 的"平仓盈亏"列是**逐日盯市口径** (用昨结算价基准):
- 含隔夜价差 / 拆批分摊 / 底仓影响
- **不反映策略真实选时和执行**

报告里 broker 数字用灰字小号显示, 仅作"broker 视角参考"。

例: JM 4-30 22:00 (开 1292.5 → 平 1289):
- broker 盯市: **+¥4,230** (用昨结算约 1264, 差 25 × 3 × 60)
- 策略 FIFO: **−¥630** (实际策略亏)

## strategy startup 状态校准 (★ 核心)

CSV 是 broker 视角, 包含**手动操作 / 底仓自动平仓 / broker 异常成交** — 这些**不是 strategy 行为**, 不应该混入 round-trip。

工具用 strategy log 的 `[ON_START 恢复] own_pos / avg / peak` 校准:

```
strategy startup own_pos = 0 (该品种)
  ├─ prev_carryover 该品种 → 全部丢弃 (broker 异常残留 / 手动)
  ├─ 孤儿 takeover (entry.fill_price=0) → 过滤
  └─ 持有中 round-trip + 当日无 strategy ON_TRADE → 过滤 (手动单)

strategy startup own_pos > 0 (该品种)
  └─ prev_carryover 保留 + 注入前日 StraLog 决策上下文 (真接管)
```

**4-30 实证**: P 4-30 11:15 那 1 手"持有中"是 Simon 手动开的, 工具过滤掉。HC 4-29 末仓 4 手 @ 3403 是真 carryover, 保留。

## 隔夜接管 (carryover) 自动处理

`generate_report.py` 自动找前一日报告目录 (lexicographic 最近一个 < 当前):

```
reports/4-30 (今日)
    ↓
_find_prev_data_dir → reports/4-29
    ↓
_autoselect_csv → 实时回报...4-29.csv
    ↓
parse_csv → compute_carryover() FIFO 推过夜持仓
    ↓ {hc2610: {long: [(3403.0, 4 手, 04-29 14:15)]}}
    ↓
_autoselect_log → StraLog(8).txt (前日)
    ↓ parse_log → 给 carryover legs 注入 POS_DECISION/IND/forecast
    ↓
pair_from_csv(prev_carryover, prev_log_events, startup_states 校准)
    ↓
takeover RoundTrip with entry.fill_price=3403, decision/ind 全有
```

报告里显示:
> **开仓 (前日接管)**: 04-29 14:15:05 买入 4 手 @ ¥3,403.0
> **当时仓位决策**: 原始信号 raw=5.00 → Carver vol target 算出 5 手 → 目标 4 手
> **当时信号 (V8 Donchian+ADX)**: close=3402, ADX=33.1, PDI=28.2 > MDI=15.5 — 趋势确认 ✓

## 策略类型标签 (波段单 / 趋势单)

每个品种 section 标题旁加蓝色"波段单" / 紫色"趋势单"标签。

当前 (2026-04-30): 7 个 V8/V13 + 4 个 QExp 全是**波段单**。趋势单 (例如 ICT v6) 上线时只需在 `render_html.py::_STRATEGY_TYPES` dict 改一行映射即可。

## 输入约定

`data-dir` 目录可包含:

### 必需
- `StraLog*.txt`: 无限易导出的策略日志
- 实时回报 CSV: `实时回报(信息导出)_YYYY_MM_DD-HH_MM_SS(N).csv` (GBK 编码自动解码)

### 推荐
- 盘面截图 — 两种命名:
  1. **`{品种}.jpg`** (最简): `HC.jpg`, `P.jpg`, `PP.jpg`, ...
  2. **hash 命名 + image_map.json**:
     ```json
     {
       "HC": "8ee156d3....jpg",
       "P":  "e2080c41....jpg"
     }
     ```

## 报告结构

### 头部
- 标题 + 数据源 (StraLog + broker CSV)

### 当日汇总卡片 (6 张)
1. **净盈亏 (策略 FIFO)** — 主指标
2. **毛盈亏 (出 − 入)** — 选时表现
3. **broker 盯市 (对照)** — 灰字参考
4. 手续费
5. 已平仓 / 持有中 (含接管/撤单数)
6. 交易品种

### 所有交易明细主表
列: 时间 / 品种 / 方向 / 手数 / 入场价 / 出场价 / **毛盈亏** / **滑点** / 手续费 / **净盈亏** / broker对照(灰) / 状态

入场价显示规则:
- 真实价 (当日开仓) → 直接显示
- carryover (前日 CSV) → 显示价 + "(前日)" 灰字
- state.json 恢复 (CSV 缺前日) → 显示 avg + "(state恢复)" 灰字
- broker 异常 (无 strategy 入场) → "—"

### 当日撤单段
报单时间 / 品种 / 方向 / 开平 / 报价 / 手数 / 状态。OrderMonitor escalator 升级催出来的撤单。

### 每品种独立点评 (中文叙述, 2026-04-29 起)
- 盘面图
- **今日行情速览**: 涨跌幅 / 高低 / 收盘指标解读 (V8 ADX/PDI/MDI; V13 MFI)
- **本策略动作** (按时间逐笔):
  * 开仓: 时间 + 价 + 手数 + 滑点 + 仓位决策 (raw → Carver → buffer → target) + 信号详情 (V8/V13 阈值确认 ✓/△)
  * 平仓: 时间 + 价 + 滑点 + 触发条件 (移动止损/硬止损/ATR止盈/信号反转) + **盈亏公式** (毛+滑-fee=净) + broker 对照
  * 持仓中: 持仓过夜提示
- **今日总结**: 已平/持仓 + 策略 FIFO 毛盈亏 + 滑点 + 手续费 + 净盈亏 + broker 对照

## 报告范围 — 中国期货交易日窗口

**T 日报告** = T-1 个交易日 21:00 → T 日 15:00, 跨周末 / 节假日自动回溯到上一交易日 21:00。

| 目标日 | 窗口 |
|--------|------|
| 周二 | 周一 21:00 → 周二 15:00 |
| 周一 | 周五 21:00 → 周一 15:00 (跳过周末) |
| 节假日后首日 | 节前最后一个交易日 21:00 → 当日 15:00 |

节假日表手工维护在 `session_window.py::_HOLIDAYS_2026`。

## 时间戳处理

broker CSV 的 "成交时间(日)" 列因不同交易所标法不一致 (HC SHFE 标当日 calendar, P/PP DCE 标 trading_day) — **不可靠**。

工具改用 **HH:MM:SS + 文件名交易日推 calendar**:
- 时间 ≥ 21:00 → calendar = trading_day - 1 (前一日历日)
- 时间 < 21:00 → calendar = trading_day

## 滑点定义

CSV 流程: 直接 `(成交均价 - 报单价格)` 算 ticks, 按方向 flip 符号:
```
买单: send=9757, fill=9760  →  diff=+3 (买高了)  →  advantage = -3 跳 (不利)
卖单: send=9828, fill=9838  →  diff=+10 (卖高了)  →  advantage = +5 跳 (有利)
```
报告内显示**已 flip**: 正 = 有利 (与金额方向一致)。

## Trade 配对

### CSV 流程 (`pair_from_csv`)

按 **instrument_id** 分队列:

| status / 状态 | 含义 | 操作 |
|---|---|---|
| 全部成交 + 开仓 | push long_queue / short_queue (按 buy/sell) |
| 全部成交 + 平仓/平今 | FIFO 取对应方向开仓; 没找到 → 标 takeover (前日 CSV 推 / state.json 兜底 / 异常) |
| 撤单 | 不进 round_trips, 单独存 cancelled |

每对 OPEN+CLOSE = 一个 round-trip, 拆批按手数比例分摊手续费 / broker_pnl。

### 三层 startup state 校准 (2026-04-30+)

详见上面"strategy startup 状态校准"段。

## 合约规格 (乘数 / tick / 手续费)

直接读取 AlphaForge `~/Desktop/AlphaForge/alphaforge/data/specs.yaml`。环境变量 `ALPHAFORGE_SPECS_PATH` 可覆盖。找不到时 fallback 到 `specs.py::_FALLBACK_MULT/_TICK` 硬编码表。

## 模块结构

```
tools/daily_report/
├── __init__.py
├── generate_report.py    # 主入口 (CSV 优先 + image_map + carryover + startup 校准)
├── csv_parser.py         # ★ CSV 解析 (GBK + 41 列 + 21:00 边界 + compute_carryover)
├── log_parser.py         # ★ StraLog 解析 (各类 tag + parse_startup_states)
├── session_window.py     # T-1 21:00 → T 15:00 窗口计算
├── specs.py              # 读 AlphaForge specs.yaml + fallback 表
├── pivot_extractor.py    # 解析策略源文件 (旧版 pivot box, 新流程已弃用)
├── trade_pairing.py      # ★ pair_from_csv (含 prev_carryover + prev_log_events)
├── render_html.py        # ★ 中文叙述风格 + 策略 FIFO 主指标 + 类型标签
└── strategy_aliases.json # 策略 alias → 源文件路径 (11 个生产策略)
```

## 测试

```bash
python3 -m pytest tests/ -q
# → 302 passed
```

## 设计决策记录

### 为什么净盈亏改用策略 FIFO 而不是 broker 报回?

broker 报的"平仓盈亏"是**逐日盯市口径** (用昨结算价基准), 含隔夜价差。这反映 broker 端会计核算, **不反映策略实际开平的真实盈亏**。

PythonGO 是 FIFO 撮合 — 净盈亏必须用入场价和出场价配对算才是策略真实表现。

JM 4-30 22:00 实证: 开 1292.5 → 平 1289 (策略实际亏 ¥630), broker 显示 +¥4,230 (含 4-28 隔夜价差) 完全误导。

### 为什么"入场 > 出场但仍盈利"是合法的?

**净盈亏 = 毛盈亏 + 滑点 − 手续费**:
- 毛盈亏 < 0 (策略选时亏)
- 滑点 > 0 (执行层卖高/买低)
- 滑点 > |毛盈亏| + 手续费 → 净盈亏 > 0

P 4-30 11:15 实证: 入 9840 出 9838 (毛 −60), 出场报 9828 成 9838 (滑 +300), 减 fee 15.10 → 净 +224.90。

### 为什么用 strategy startup state 校准 broker CSV?

broker CSV 包含**手动开仓 / 底仓自动平 / broker 异常成交**等非策略行为。strategy log 的 `[ON_START 恢复] own_pos / avg / peak` 是 **strategy state.json 真值** — 用它校准能精确区分真接管 vs 异常事件。

P 4-30 11:15 那 1 手"持有中"实证: Simon 手动开的, broker CSV 报但 strategy 完全没动作 → 工具用 startup state P=0 + 当日无策略 ON_TRADE 把它过滤掉。

### 为什么 broker "成交时间(日)" 列不可靠?

观察发现同一时刻 (4-27 22:00) 不同交易所 broker 标法不一致 (HC 标 20260427, P 标 20260428). **对策**: 不信任该列, 自己用 `HH:MM:SS + 文件名交易日` 推 calendar (21:00+ 时间归前一日历日)。

### 为什么 image_map.json 是可选的?

无限易截图常用 hash 名 (`8ee156d3....jpg`) 不能被 `{品种}.jpg` 自动扫描。两条路:
1. 手动 mv 改名 (一次性)
2. 提供 image_map.json 把 hash 名映射到品种 (零改名)

工具优先读 image_map.json, fallback 到 `{品种}.jpg` 自动扫描。

### 为什么去掉旧版策略 pivot box?

2026-04-29 起, 每品种 section 改成中文叙述风格 (替代旧版 pivot box / 入场前置条件 / 移动止损公式 / 开仓原因表 / 平仓原因表 / 本品种交易明细)。原因: Simon 觉得旧版"详细策略介绍"太机械, 报告应该是"点评今天表现"。

新版每品种段落: 行情速览 + 逐笔叙述 (开仓决策理由 / 平仓触发条件 / 盈亏公式) + 今日总结。

## 已知限制

- **节假日表手工维护到 2026 年**: 跨年需手动加
- **broker_pnl 拆批分摊**: 工具按手数比例摊 (与 broker 内部精确分摊可能 < 1 分误差)
- **strategy startup state 必需**: 没有 strategy log 时, 校准过滤逻辑失效 (会把手动单 / 异常事件全部当成 takeover)
- **多张盘面图**: 一个品种只支持一张图, 文件名按品种唯一

## 未来改进方向

1. **strategy log 跟 broker CSV 用 oid 关联**: 现在用时间 + 价格 + 数量交叉匹配 (broker 端时钟可能比 strategy 慢 1-2 秒导致错配). 未来若 broker CSV 暴露 strategy oid 就能精确关联。
2. **趋势单 (ICT v6) 上线**: 修改 `_STRATEGY_TYPES["I"] = "trend"` 即可。
3. **多品种组合统计**: 加品种维度的胜率 / 平均盈亏 / 大赔/大赚.
4. **每周 / 每月 P&L 汇总报告**: 现在只有日报。
