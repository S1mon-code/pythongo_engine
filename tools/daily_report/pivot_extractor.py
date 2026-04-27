"""提取每个策略的 pivot point — 真正的入场出场公式.

从 src/{品种}/long/*.py 解析:
- 信号常量 (DC_PERIOD, ADX_THRESHOLD, MFI_FLOOR, ...)
- generate_signal() 公式 docstring
- Params 里的 hard_stop_pct / trailing_pct 默认值

返回结构化数据供 HTML 渲染.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PivotSpec:
    symbol: str
    strategy_name: str           # AL_Long_1H_V8_Donchian_ADX_Filter
    family: str                  # V8 / V13
    constants: dict              # {"DC_PERIOD": 40, "ADX_THRESHOLD": 22.0, ...}
    entry_formula: str           # 自然语言/伪公式
    entry_conditions: list[str]  # ["ADX > 22", "PDI > MDI", "close > DC_M"]
    exit_pivots: list[str]       # 各种出场 pivot 描述
    trail_stop_formula: str      # 移动止损线生成公式
    hard_stop_pct: float
    trailing_pct: float


_SYMBOL_DIR = {
    "AL": "AL/long", "CU": "CU/long", "HC": "HC/long",
    "AG": "AG/long", "JM": "JM/long", "P": "P/long", "PP": "PP/long",
}


def _read_strategy_file(symbol: str) -> tuple[str, str] | None:
    """返回 (strategy_name, file_text). 找不到 returns None."""
    sub = _SYMBOL_DIR.get(symbol.upper())
    if sub is None:
        return None
    folder = _REPO_ROOT / "src" / sub
    if not folder.exists():
        return None
    # 取 V8/V13 主策略文件 (排除 backup/legacy)
    candidates = sorted(
        f for f in folder.glob("*.py")
        if "_V8" in f.name or "_V13" in f.name
        if not f.name.endswith(".bak")
    )
    if not candidates:
        return None
    f = candidates[0]
    return f.stem, f.read_text(encoding="utf-8")


def _extract_constants(text: str, names: list[str]) -> dict:
    out: dict = {}
    for name in names:
        m = re.search(rf"^{name}\s*=\s*(-?\d+\.?\d*)", text, re.MULTILINE)
        if m:
            val = m.group(1)
            out[name] = float(val) if "." in val else int(val)
    return out


def _extract_field_default(text: str, field_name: str) -> float | None:
    m = re.search(
        rf"{field_name}:\s*float\s*=\s*Field\(default=(-?\d+\.?\d*)",
        text,
    )
    if m:
        return float(m.group(1))
    m = re.search(
        rf"{field_name}:\s*int\s*=\s*Field\(default=(-?\d+)",
        text,
    )
    if m:
        return float(m.group(1))
    return None


def extract_pivot(symbol: str) -> PivotSpec | None:
    info = _read_strategy_file(symbol)
    if info is None:
        return None
    strategy_name, text = info
    family = "V8" if "_V8" in strategy_name else "V13" if "_V13" in strategy_name else "?"

    if family == "V8":
        consts = _extract_constants(
            text,
            ["DC_PERIOD", "ADX_PERIOD", "ADX_THRESHOLD", "SIGNAL_SCALE",
             "CHANDELIER_PERIOD", "CHANDELIER_MULT", "FORECAST_SCALAR",
             "FORECAST_CAP", "MAX_LOTS"],
        )
        entry_formula = (
            f"信号 = clip((close − DC_M) / (DC_U − DC_M) × {consts.get('SIGNAL_SCALE', 1.5)}, 0, 1)"
        )
        entry_conds = [
            f"ADX > {consts.get('ADX_THRESHOLD', 22.0)}(趋势强度过阈值)",
            "PDI > MDI(多头方向占优)",
            "close > DC_M(收盘在 Donchian 中轴之上)",
        ]
    elif family == "V13":
        consts = _extract_constants(
            text,
            ["DC_PERIOD", "MFI_PERIOD", "MFI_FLOOR", "BREAKOUT_WEIGHT",
             "CHANDELIER_PERIOD", "CHANDELIER_MULT", "FORECAST_SCALAR",
             "FORECAST_CAP", "MAX_LOTS"],
        )
        bw = consts.get("BREAKOUT_WEIGHT", 0.6)
        floor = consts.get("MFI_FLOOR", 50.0)
        entry_formula = (
            f"通道位置 pos = (close − DC_L) / (DC_U − DC_L); "
            f"通道信号 don_sig = clip((pos − 0.5) × 3, 0, 1) 当 pos > 0.5,否则 0; "
            f"资金流信号 mfi_sig = clip((MFI − {floor}) / 30, 0, 1) 当 MFI > {floor},否则 0; "
            f"综合信号 = {bw} × don_sig + {1 - bw:.1f} × mfi_sig"
        )
        entry_conds = [
            "通道位置 pos > 0.5(close 在 Donchian 通道上半部)",
            f"MFI > {floor}(资金净流入)",
        ]
    else:
        return None

    hard = _extract_field_default(text, "hard_stop_pct") or 0.5
    trail = _extract_field_default(text, "trailing_pct") or 0.3

    # 出场 pivot 列表(中文)
    chand_period = consts.get("CHANDELIER_PERIOD", 22)
    chand_mult = consts.get("CHANDELIER_MULT", 2.58 if family == "V8" else 3.0)
    exit_pivots = [
        (f"移动止损 (TRAIL_STOP):当前价 ≤ 移损线 → 平仓"
         f"(分钟级判断,降噪)"),
        (f"硬止损 (HARD_STOP):tick 级,当前价 ≤ 入场均价 × (1 − {hard}%) → 立即平仓"),
        (f"吊灯出场 (Chandelier):close < 持仓期间最高价({chand_period}根) − {chand_mult}×ATR"),
        ("信号反转 (SIGNAL_REVERSAL):POS_DECISION optimal=0 → 平仓"),
    ]

    # 移动止损线生成方式 — 详细说明
    trail_stop_formula = (
        f"持仓开始后,每个 tick 实时更新 peak_price = max(peak_price, last_price);"
        f"\n移损线 = peak_price × (1 − {trail}%) = peak_price × {1 - trail/100:.5f};"
        f"\n判断频率:每分钟首个 tick(同一分钟只判一次,降噪);"
        f"\n触发条件:last_price ≤ 移损线 → 立即穿盘口平仓 (urgent)。"
        f"\n持仓重置点:开仓 / 加仓 / 减仓 / 平仓 → peak 重新初始化为入场价。"
    )

    return PivotSpec(
        symbol=symbol.upper(),
        strategy_name=strategy_name,
        family=family,
        constants=consts,
        entry_formula=entry_formula,
        entry_conditions=entry_conds,
        exit_pivots=exit_pivots,
        trail_stop_formula=trail_stop_formula,
        hard_stop_pct=hard,
        trailing_pct=trail,
    )
