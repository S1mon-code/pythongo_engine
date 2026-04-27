# 启动接管模式 (Takeover)

## 背景

无限易客户端每天 **18:00 强制清算**,所有策略进程退出。Simon 在 **21:00 夜盘开盘前**手动重开。

问题:broker 端持有混合仓位:

- **底仓**:历史 / 手动 / 其他用途的仓位 — 策略**不该碰**
- **下午策略已开仓**:如下午 JM 突破开了 4 手 — 重开后**应继续由策略管理**

如果让策略启动时全盘接管 broker 仓位 (`broker_pos`),会误把底仓当成自己的;
如果完全不接管,下午仓位失控、止损 / 信号反转都不再生效。

## 解决方案 — Params `takeover_lots`

启动时通过无限易 UI 在 Params 面板填一个数字,告诉策略**它应该负责多少手**。

```
Params 面板:
  takeover_lots = 4    ← 启动接管手数(0=按state恢复, >0=手动接管)
```

`takeover_lots > 0` 时,策略会:

1. 强制 `self._own_pos = takeover_lots`(覆盖 `state.json` 恢复值)
2. 清空 `_my_oids`(历史 oid 已失效)
3. 设 `_takeover_pending = True`,首个有效 tick 进来时:
   - `self.avg_price = tick.last_price`
   - `self.peak_price = tick.last_price`
   - `_risk.peak_price` 自动由 `update_peak_trough_tick` 在持仓 sign 切换时用 first tick 初始化
4. **不主动决策 / 不 startup_eval** — 等下一个 K 线 close 自然进 `_on_bar` 决策
5. 飞书推送 `[TAKEOVER 启动]` 通知,显示接管手数 + 底仓估算

`takeover_lots = 0`(默认)时 — 完全等同旧行为(从 `state.json` 恢复 / 全新进程)。

## 流程示例 — JM 当日

```
13:30  下午策略开 JM 4 手 @ 1280
14:30  其他来源开 2 手底仓 @ 1285  (broker 总仓 = 6 手)
18:00  强制清算 → 进程退出, broker 仍有 6 手
21:00  Simon 重开 JM 策略, Params 填 takeover_lots = 4

       策略启动后:
         _own_pos = 4
         broker_pos = 6 (底仓 2 手不归策略管)
         首 tick last_price = 1278  →  avg = peak = 1278
         peak_price 从 1278 起 tick-level 追踪

       后续:
         tick price = 1273  (下跌 0.39%)
           → 触发 hard_stop?  hard 0.5% × 1278 = 1271.6, 还未触发
           → 触发 trail_stop? peak 1278, line = 1278 × 0.997 = 1274.2
             1273 ≤ 1274.2 → TRAIL 触发, 平 4 手
         broker 端净仓: 6 - 4 = 2  (底仓不动) ✓

         或: H1 bar close, 信号 = 0  → target = 0  → 平 4 手 (信号反转)
```

## 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| `avg_price` 兜底 | 启动时 `last_price` | 简单, 持仓几小时内偏差小, hard_stop 0.5% 不会误触发 |
| `peak_price` 兜底 | 启动时 `last_price` | 同上, trail 重新追踪只会更保守 |
| 与 startup_eval 关系 | 跳过, 等下个 bar 自然决策 | 手动接管是明确指令, 不让策略立刻动 |
| 与 state.json 关系 | 完全 override | manually specified 优先级最高 |
| Performance tracker | 不计入 (无真实 entry 历史) | 数据干净, 重启后新 round-trip 才统计 |

## 平仓时的"底仓不动"保证

- 策略 close 时只发 `vol = self._own_pos`(=4),broker 总仓位减 4
- CTP / SHFE 内部 FIFO 平今 / 平昨的选择,不影响"策略关闭量 = `_own_pos`"
- 数学上:`broker_净持仓 - 策略关闭量 = 原broker仓 - _own_pos = 底仓` ✓

不管 broker 优先平今还是平昨,**净持仓减少量 = 策略意图**,底仓数量不变。

## 已实施范围 (2026-04-27)

**全部 7 个生产策略**(JM 先做验证,其他 6 个一并推广):

| 家族 | 文件 |
|------|------|
| **V8** (Donchian + ADX) | `src/AL/long/AL_Long_1H_V8_Donchian_ADX_Filter.py` |
| | `src/CU/long/CU_Long_1H_V8_Donchian_ADX_Filter.py` |
| | `src/HC/long/HC_Long_1H_V8_Donchian_ADX_Filter.py` |
| **V13** (Donchian + MFI) | `src/AG/long/AG_Long_1H_V13_Donchian_MFI.py` |
| | `src/JM/long/JM_Long_1H_V13_Donchian_MFI.py` |
| | `src/P/long/P_Long_1H_V13_Donchian_MFI.py` |
| | `src/PP/long/PP_Long_1H_V13_Donchian_MFI.py` |

7 个文件 4 处 patch 完全一致(模板对齐),232/232 pytest 全绿,`ast` 语法全部通过。

## 验证清单 (Simon 21:00 启动时)

- [ ] Params 面板填入 `takeover_lots = N`(N = 下午开的实际手数)
- [ ] 启动后查看 StraLog:
  - [ ] `[ON_START TAKEOVER] 手动接管 N 手 (覆盖 state, broker_pos=...)` 日志出现
  - [ ] `[ON_START 持仓] own_pos=N broker_pos=...` 数字一致
  - [ ] 飞书收到 `[TAKEOVER 启动]` 通知
- [ ] 等首 tick:
  - [ ] `[TAKEOVER FIRST TICK] avg_price=peak_price=X.XX own_pos=N` 出现
- [ ] 状态栏 UI:
  - [ ] `own_pos = N`,`broker_pos = N + 底仓`
  - [ ] `avg_price` / `peak_price` ≈ 启动时 last_price
- [ ] 后续 trail_stop 触发或信号反转触发时:
  - [ ] 平 N 手,broker 端剩下底仓不变

## 推广跟踪

| 品种 | 实施 | 实盘验证 |
|------|------|---------|
| JM | ✅ 2026-04-27 | ⬜ 待 21:00 实盘验证 |
| AL | ✅ 2026-04-27 | ⬜ 同上 |
| CU | ✅ 2026-04-27 | ⬜ 同上 |
| HC | ✅ 2026-04-27 | ⬜ 同上 |
| AG | ✅ 2026-04-27 | ⬜ 同上 |
| P  | ✅ 2026-04-27 | ⬜ 同上 |
| PP | ✅ 2026-04-27 | ⬜ 同上 |

## 文件改动清单 (每个文件 4 处, 完全相同 patch)

1. `Params` — 加 `takeover_lots: int = Field(default=0, ...)`
2. `__init__` — 加 `self._takeover_pending = False`
3. `on_start` — 在 `[ON_START 持仓]` 日志后,`if self._own_pos == 0:` 前插入 takeover override 分支
4. `on_tick` — 在 `super().on_tick()` 之后插入首 tick 兜底逻辑
