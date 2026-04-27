# Daily Report — PythonGO 实盘交易日报

每天根据无限易客户端导出的 `StraLog*.txt` 自动生成中文 HTML + PDF 日报,
内容包括:汇总卡片、所有 trade 表、分品种 pivot 公式 / 移动止损公式 / 入场出场原因 /
单品种 PnL 表,以及当天的盘面图。

## 用法

每天的固定流程:

```bash
# 1. 新建当天目录
mkdir -p reports/MM-DD

# 2. 把无限易导出的 log 和盘面截图放进去, 截图按品种命名
#    reports/MM-DD/StraLog*.txt
#    reports/MM-DD/AL.jpg
#    reports/MM-DD/CU.jpg
#    ... (每个品种一张)

# 3. 跑报告 (默认输出 HTML + PDF)
python3 tools/daily_report/generate_report.py --date YYYY-MM-DD --data-dir reports/MM-DD

# → 输出:
#    reports/MM-DD/YYYY-MM-DD.html
#    reports/MM-DD/YYYY-MM-DD.pdf
```

参数说明:

| 参数 | 说明 |
|------|------|
| `--date YYYY-MM-DD` | 目标交易日, 默认今天 |
| `--data-dir <path>` | 当天素材目录 (含 log + 图), 也是默认输出目录 |
| `--log <path>` | 显式指定 log 文件; 不传时自动找 data-dir 里 mtime 最新的 `StraLog*.txt` |
| `--out <path>` | 显式指定输出目录 |
| `--no-pdf` | 只生成 HTML, 不生成 PDF |

兼容老用法(log 在 `logs/` 下):

```bash
python3 tools/daily_report/generate_report.py --date 2026-04-27 --log "logs/StraLog(4).txt"
```

## 报告范围 — 中国期货交易日窗口

**T 日报告** = T-1 个交易日 21:00 → T 日 15:00,跨周末 / 节假日自动回溯到上一交易日 21:00。

| 目标日 | 窗口 |
|--------|------|
| 周二 | 周一 21:00 → 周二 15:00 |
| 周一 | 周五 21:00 → 周一 15:00 (跳过周末) |
| 节假日后首日 | 节前最后一个交易日 21:00 → 当日 15:00 |

节假日表手工维护在 `session_window.py::_HOLIDAYS_2026`,
精度容忍 ±1 天(对窗口范围影响可忽略)。

## 输入约定

`data-dir` 目录内必须包含:

- **log 文件**: `StraLog*.txt`(无限易导出的日志,会自动找 mtime 最新)
- **盘面截图**(可选): `{品种}.jpg` / `{品种}.png` 等,品种名大写
  - 例:`AL.jpg`, `CU.jpg`, `HC.jpg`, `JM.jpg`, `P.jpg`, `PP.jpg`, `AG.jpg`
  - 命名必须是单纯品种符号(1-4 个英文字母),会被识别并嵌入对应品种 section

## 报告结构

1. **当日汇总卡片**: 净盈亏 / 毛盈亏 / 滑点损益 / 平均滑点 / 已平仓+持有中 / 交易品种
2. **所有交易明细表**(按时间排序):时间 / 品种 / 方向 / 手数 / 入场价 / 出场价 / 毛盈亏 / 滑点损益(跳数)/ 净盈亏 / 状态
3. **每品种独立 section**:
   - 盘面图(如有)
   - 策略名 + 信号家族(V8 / V13)+ 关键参数
   - 入场公式(从源文件提取的真实公式)
   - 入场前置条件(全部满足才开仓)
   - 出场触发(任一即平仓:移动止损 / 硬止损 / 吊灯出场 / 信号反转)
   - **移动止损线生成方式**:`peak × (1 − trailing_pct%)`,tick 实时追踪 peak,分钟级判断
   - 开仓原因表:时间 / 手数 / 成交价 / 送单价 / raw signal / optimal / target / ATR / 当时指标值 / EXECUTE 原因
   - 平仓原因表:时间 / 手数 / 成交价 / 送单价 / 触发条件(移动止损 / 硬止损 / 信号反转)/ 详情
   - 本品种 trade 明细表(配对开-平):时间 / 方向 / 手数 / 入场成本 / 出场成本 / 毛盈亏 / 滑点损益 / 净盈亏

## 滑点定义(关键!)

**直接用 log 里的 `[滑点] N.Nticks` tag 数据**,这是策略自己用 `SlippageTracker` 计算的真滑点
(决策价 → 成交价偏离),不是任何重建公式。

### log 约定(SlippageTracker)

- **正数 = 不利**(策略付出 cost):买高于信号价 / 卖低于信号价
- **负数 = 有利**(策略获利):买低于信号价 / 卖高于信号价
- 单位:**每手 ticks**(per lot)
- 只在 `slip != 0` 时 log,`signal_px=0` 时(已被首次 fill 重置)不 log

### signal_price 来源(由策略代码决定)

- **入场**: `EXECUTE.price`(决策时刻的 bar close)
- **止损出场**: 触发时刻的 last tick price(≈ `[TRAIL_STOP][M1] price=X` 里的 X)
- **信号反转出场**: `EXECUTE.price`

### 报告内显示符号(已 flip!)

为了与金额方向一致(直观),报告里显示的跳数已 **flip log 原始符号**:

- **正跳数 = 有利**(损益正)
- **负跳数 = 不利**(损益负)

例:log `[滑点] -1.0ticks`(有利)→ 报告显示 `+1.0 跳`,损益为正。

### 损益计算

```
leg_slippage_pnl = advantage_ticks × tick_size × lots × multiplier
where advantage_ticks = -log_ticks  (正 = 有利)
```

### 同 oid 拆批共享

broker 把单 order 拆成多笔 ON_TRADE 时,SlippageTracker 只在第一笔 fill 后 log 一次
(因 `signal_px=0` 重置)。报告内会**自动把首笔 ticks 回填到同 oid 的所有 split fills**,
保证滑点计算覆盖完整成交量。

### 跳数显示约定(每笔 trade)

- 已平仓(2 腿):`+¥X (合 N.0 跳·均 M.0 跳)` — 合 = 进+出 signed sum,均 = 合/2
- 持有中(1 腿):`+¥X (N.0 跳)`
- 0 跳:`¥0 (0跳)`

## Pivot 公式来源

每个品种 section 顶部展示的 pivot 公式 / 入场条件 / 出场触发 / 移动止损公式,
**全部从 `src/{品种}/long/{文件}.py` 解析提取**(常量 + Params 默认值),不是手写。

修改策略源文件后,下次跑报告会自动反映新参数。

支持的策略家族:

| 家族 | 信号 | 品种 |
|------|------|------|
| **V8** | Donchian + ADX + PDI/MDI | AL / CU / HC |
| **V13** | Donchian + MFI | AG / P / PP / JM |

新增其他家族:在 `pivot_extractor.py::extract_pivot` 加分支,
或在 `_SYMBOL_DIR` 里添加品种对应的策略子目录。

## Trade 配对(FIFO)

按品种独立维护开仓队列,按 ON_TRADE 时序处理:

| direction | offset | 含义 | 操作 |
|-----------|--------|------|------|
| `'0'` | `'0'` | 买开 | push long_queue |
| `'1'` | `'1'`/`'3'` | 卖平多 | FIFO 取 long_queue (CTP: '1'=平今, '3'=平昨) |
| `'1'` | `'0'` | 卖开 | push short_queue (long-only 策略不出现) |
| `'0'` | `'1'`/`'3'` | 买平空 | FIFO 取 short_queue |

每对 OPEN+CLOSE = 一个 round-trip,带毛盈亏 + 滑点损益。
未配对 OPEN = 持有中,毛盈亏 = 0,只展示已发生的 entry-leg 滑点。

平仓量超出开仓队列累计的部分会被忽略(可能是策略外预存仓,不归算法策略管理)。

## 合约规格(乘数 / tick / 手续费)

直接读取 AlphaForge `~/Desktop/AlphaForge/alphaforge/data/specs.yaml`(覆盖 95 个品种,
含 fixed/ratio 两种手续费类型)。环境变量 `ALPHAFORGE_SPECS_PATH` 可覆盖路径。

找不到 specs.yaml 时,fallback 到 `specs.py::_FALLBACK_MULT/_TICK` 硬编码表。

**注意**: 当前报告**不展示手续费**(Simon 要求)。`commission()` 方法仍可用,
未来如需展示加上即可。

## PDF 生成

调用本机 Chrome / Chromium / Edge / Brave 的 headless 模式:

```python
chrome --headless --disable-gpu --no-pdf-header-footer \
       --print-to-pdf=output.pdf file:///path/to/report.html
```

CSS 已配 `@media print { .section { page-break-inside: avoid } }`,品种 section 不会被分页打断。

## 模块结构

```
tools/daily_report/
├── __init__.py
├── generate_report.py    # 主入口 (argparse + Chrome PDF)
├── log_parser.py         # 解析 StraLog 各类 event tag (含中文 [滑点])
├── session_window.py     # T-1 21:00 → T 15:00 窗口计算
├── specs.py              # 读 AlphaForge specs.yaml + fallback 表
├── pivot_extractor.py    # 解析策略源文件提取真实公式
├── trade_pairing.py      # FIFO 配对 + 滑点关联 (oid 维度)
└── render_html.py        # 白色主题极简风格 HTML 渲染
```

## 测试

`tests/test_daily_report.py` 共 32 个单元测试 + 端到端 smoke test:

```bash
python3 -m pytest tests/test_daily_report.py -v
```

覆盖:

- session window 边界(周末跳过 / 节假日 / 边界 contains)
- log parser 各类 tag(POS_DECISION / EXECUTE / EXEC_OPEN / EXEC_STOP / ON_TRADE / TRAIL_STOP / IND v8/v13 / SIGNAL / 中文 [滑点])
- 合约规格(fixed/ratio 手续费 / 未知品种 fallback)
- FIFO 配对(简单 round-trip / 持有中 / 拆批多腿)
- 滑点(log 不利 / log 有利 / 拆批共享 oid)
- 端到端 smoke test(用真实 4-27 log 跑通)

## 设计决策记录

### 为什么用 log [滑点] 而不是自己重算?

之前曾用 `(send_price - fill_price)` 自己算,但:

- `send_price`(EXEC_OPEN/EXEC_STOP price)是策略**主动让价后的限价**(已穿盘口),
  不是真"理想价"
- 例 AL EXEC_STOP @ 25035 (urgent 穿 5 跳) → ON_TRADE @ 25060,**自算成 +¥375 (5 跳)**
- 但实际 trail 触发时 close=25060,fill=25060 → **真滑点 = 0**
- 那 +¥375 只是"策略主动让 5 跳但市场没跑那么远"的虚假 alpha

策略本身的 `SlippageTracker` 用 `signal_price`(决策时 bar close 或 trail 触发时 last_price)
作为 reference,这是 industry standard "implementation shortfall" 算法,数据准确无歧义。

### 为什么图片要在 data-dir 内手动重命名为品种名?

OCR 识别图片标题不可靠且增加依赖。约定 `{SYMBOL}.jpg` 文件名最简单,Simon 已经熟悉这个流程。

### 为什么不展示手续费?

Simon 要求(交易频率低,手续费占比小,展示反而干扰主因素)。
`CommissionSpec` 数据仍维护着,需要时一行代码即可重新启用。

## 已知限制

- **节假日表手工维护到 2026 年**: `session_window.py::_HOLIDAYS_2026`,跨年需手动加
- **跨年没测**: T-1 跨年(如 1-2 报告应回溯到上一年 12-30)未做边界测试
- **平仓量超出开仓队列**: 当前忽略,未单独 log warning
- **多张盘面图**: 一个品种只支持一张图,文件名按品种唯一
