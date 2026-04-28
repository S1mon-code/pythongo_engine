# ICT v6 — PythonGO 移植

来源: `~/Desktop/ICT/ict_v3/` (~/Desktop/ICT 项目),
**ICT v6 (2022 Model state machine)** — Simon 自己 80+ 次 self-challenge 验证为
唯一 valid setup,在 NQ 上 cd-Sharpe +4.68 / 在 CN 8-product portfolio 上
**cd-Sharpe +8.31, CAGR +75.5%, 42/44 CN futures profitable**.

本目录把这套 ICT 框架移植到 PythonGO 实盘,**Phase 1 = 铁矿(I)reference 跑通版**。

---

## 策略 7 步 state machine

```
1. 等待 D1 bias               (bull / bear / neutral)
2. 等待 KZ 时间窗口            (CN: 09:00 / 13:30 / 21:00 开盘 30min)
3. 检测 sweep                  (流动性扫描, swing low/high 被 pierce + reclaim)
4. 等待 displacement + FVG     (强势反向 bar, body ≥ 1×ATR + FVG ≥ 0.2×ATR)
5. 计算 OTE 70.5% 限价         (OTE 区 = 62%-79% 回撤, 70.5% 是 sweet spot)
6. 限价单 fill                 (MVP 跳过 reactive entry confirmation)
7. R-ladder + chandelier trail (T1 +0.5R 平 1/3, T2 +1.5R 平 1/3, T3 +3R 平最后)
```

**风险管理**: 0.5% equity per trade, max 5 contracts, max 3 trades/day,
daily_stop_r=−2.0, daily_lock_r=+3.0, hard cutoff 14:50 / 22:50, max_hold 240×1m=4h.

---

## 文件结构

```
src/ICT/
├── __init__.py                      (空)
├── README.md                         本文件
├── modules/                          ICT 共享 primitives 子包
│   ├── __init__.py
│   ├── timezones.py                  CST 时区 helpers
│   ├── sessions_cn.py                CN kill zones + lunch break + hard cutoff
│   ├── structures.py                 ATR(14 Wilder) + 3-bar fractal swings
│                                       + sweep + displacement + FVG + engulfing
│   ├── bias.py                       D1 bias engine (no-lookahead)
│                                       + dealing range + PD-zone classification
│   └── state_machine.py              V6Config + V6StateMachine + R-ladder
│                                       + chandelier trail + per-day limits
└── I_Bidir_M1_ICT_v6.py              铁矿 (i2609) 1-min 双向策略
                                       双向 long+short, 完整 7 步 state machine
                                       含 takeover_lots, 自管持仓, 飞书通知
```

---

## 与现行 V8/V13/QExp 的关键差异

| 特性 | V8/V13 | QExp robust | **ICT v6** |
|------|--------|-------------|------------|
| 信号 | forecast (0-10) | binary fires | **state machine 6 状态** |
| Sizing | Carver vol target | max_lots once | **0.5% equity / stop_distance** |
| 周期 | H1 | M5/M15/M30 | **M1 + D1 bias** |
| Multi-TF | × | × | **★ D1 bias + M1 entry** |
| 出场 | trail + Chandelier + reversal | profit_target + hard | **R-ladder partials + chandelier trail** |
| Per-day | × | × | **★ max 3 / daily_stop_r / daily_lock_r** |
| Hard cutoff | × | × | **★ 14:50 / 22:50** |
| 双向 | long-only | long-only (HC short 单独) | **★ bidirectional 同策略** |
| 行数 | ~1500 | ~715 | ~1500 (含 modules) |

---

## 部署 (Windows 无限易)

```
pyStrategy/
├── pythongo/                          (无限易自带)
├── modules/                           (主 modules)
│   ├── session_guard.py
│   ├── pricing.py
│   ├── persistence.py
│   ├── feishu.py
│   ├── slippage.py
│   ├── heartbeat.py
│   ├── order_monitor.py
│   ├── error_handler.py
│   ├── rollover.py
│   ├── trading_day.py
│   └── contract_info.py
├── ICT/                               ★ ICT 子包整个 copy
│   ├── __init__.py
│   └── modules/
│       ├── timezones.py
│       ├── sessions_cn.py
│       ├── structures.py
│       ├── bias.py
│       └── state_machine.py
└── self_strategy/
    └── I_Bidir_M1_ICT_v6.py           ★ 策略文件
```

**关键**: ICT 子模块在 `pyStrategy/ICT/modules/` 而**非** `pyStrategy/modules/ict/`,跟主 modules 隔离。
策略 file 顶部有 sys.path hack 自动找到 ICT 子包。

### Params 面板必填

| 字段 | 默认 | 说明 |
|------|------|------|
| `instrument_id` | `i2609` | 铁矿主力 |
| `kline_style` | `M1` | **必须 1 分钟** (ICT v6 是 M1 框架) |
| `max_lots` | 5 | 硬上限 |
| `risk_per_trade_pct` | 0.005 | 0.5% |
| `max_trades_per_day` | 3 | |
| `enable_short_setups` | true | 双向 |
| `takeover_lots` | 0 | 0=新进程; >0=接管已有持仓 (跟其他策略一致) |

---

## MVP 简化(已 ship)vs Phase 2(未做)

### Phase 1 (本次 ship)
✓ Bidirectional long+short
✓ Single-swing fractal sweep (no EQL/EQH cluster priority)
✓ Bullish/Bearish displacement + 3-bar FVG
✓ OTE 70.5% limit (single-tier)
✓ R-ladder partials (33%/33%/runner)
✓ Chandelier trail (HH−1×ATR / LL+1×ATR)
✓ Per-day limits (max 3 / stop / lock)
✓ Hard cutoff (CN 14:50 / 22:50)
✓ D1 bias engine (no-lookahead, 1m → D1 resample)
✓ Takeover_lots 模式
✓ 完整自管持仓 + UI + 飞书

### Phase 2 (未做, 跑通验证后再加)
✗ EQL/EQH cluster priority sweep (单 swing fractal 已够 MVP)
✗ Reactive entry (engulfing + micro-MSS 确认) — MVP 直接 limit fill
✗ Multi-tier OTE (T1 50%/T2 79%) — MVP 用 single-tier 70.5%
✗ Strict MSS — MVP 用 displacement+FVG 即可
✗ Asian range size sanity filter
✗ Order-flow alignment filter (v7.1)
✗ HTF (W1+M1) bias filter (v8)
✗ Confluence Score gate (v8)
✗ Volume imbalance / Liquidity Void / PO3 gate (v9)

---

## 限制 & 已知 issue

1. **K 线 buffer 内存累积**: 1m 一年 ~24,000 bars × 4 数组,内存可控 (~MB 级);
   建议跟 18:00 强制清算 / 21:00 重启同步,buffer 自然 reset.
2. **D1 bias 来自 1m resample**: 当前进程内的 1m 历史 push 到 D1,D1 数据准确度受
   1m history 长度影响. 建议初始 push_history 至少 30 天(`30 × 24 × 60 = 43200` 1m bars)
   保证 D1 lookback_days=20 暖机充足.
3. **Reactive entry off**: MVP 直接在 OTE 70.5% 挂限价,不要求额外 engulfing 确认.
   实盘后如发现 false fills 增加再启用.
4. **首 tick takeover 模式无 ATR**: 接管时进程不知道历史 ATR,首 tick 兜底
   `entry_atr=0`,profit_target / chandelier 暂禁用直到下一根 bar close.

---

## 验证清单 (实盘启动)

1. ✅ 无限易 Params 面板填 `instrument_id=i2609`, `kline_style=M1`,
   留 `takeover_lots=0` (全新) 或 `>0` (接管下午仓位)
2. ✅ 启动后看 StraLog 出现:
   - `[ON_START] D1 bias built: N D1 bars, M non-neutral`
   - `[ON_START] state machine init: state=IDLE cur_idx=...`
   - `[ON_START 持仓] own_pos=N broker_pos=...`
   - 飞书 `**策略启动** I_Bidir_M1_ICT_v6 (ICT v6) ...`
3. ✅ 首 1m bar close 后:
   - `[ON_BAR 实盘] bar#1 close=X bias=bull/bear/neutral kz=DAY_OPEN_SB sm=IDLE`
4. ✅ Setup 触发(可能要等 sweep+displacement+FVG 全部满足,间歇出现):
   - `[PLACE_LIMIT] sweep@X disp@Y leg=Z`
   - 状态栏 `OTE / Stop / Target` 显示新值
5. ✅ 限价单 fill:
   - `[EXEC_OPEN] send_order buy/sell N手 @ X (passive) ...`
   - `[ON_TRADE] oid=X 'buy/sell' offset='0' price=Y vol=N`
   - `[OPEN] own_pos→N avg=X stop=Y`
6. ✅ R-ladder 触发:
   - `[EXEC_PARTIAL] sell N手 @ X R=0.5/1.5/3.0 reason=r_target_XR`
   - `[PARTIAL] own_pos→M new_stop=BE/X`
7. ✅ 出场触发:
   - `[CHANDELIER]` (持仓中 stop 上调) 或
   - `[EXEC_CLOSE]` (stop hit / max_hold / hard_cutoff)
   - `[CLOSE] own_pos→0 R=±X.XX`

---

## 测试覆盖

`tests/test_ict.py` 共 **25 个测试**:

- KZ / lunch / cutoff / can_trade (7)
- Wilder ATR / swings / sweep long+short / displacement (5)
- D1 bias warmup + zigzag uptrend (2)
- State machine init / buffer / no-bias / outside-KZ / position size / daily limit (6)
- Strategy file syntax + class name + takeover patches + imports + 自管 (5)

```bash
python3 -m pytest tests/test_ict.py -v   # 25 passed
python3 -m pytest tests/ -q              # 290 total (含 ICT)
```

---

## Phase 2 推广路线 (Simon 决策)

跑通验证 1-2 周后,可以:

1. 加 EQL/EQH cluster priority (源 ICT v3 已实现, port to `structures.py`)
2. 加 reactive entry (engulfing + micro-MSS in band)
3. 加 multi-tier OTE
4. 推广到 8-product portfolio: I + RB + HC + J + AU + AG + CU + 1 (待选)
   — 这 8 个在 ICT 原始研究里 cd-Sharpe +8.31 (CN portfolio)
5. 可选: Unicorn variant (Breaker+FVG overlap setup,同 ICT 主框架)
