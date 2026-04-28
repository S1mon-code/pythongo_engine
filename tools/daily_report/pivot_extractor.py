"""提取每个策略的 pivot point — 真正的入场出场公式.

支持 3 大 family:
  - V8  (Donchian + ADX)        — src/{品种}/long/*_V8_*.py
  - V13 (Donchian + MFI)        — src/{品种}/long/*_V13_*.py
  - QExp (binary signal + ATR)  — src/qexp_robust/*.py (4 个子 family)

Strategy alias 映射放在 strategy_aliases.json. 日报按 alias 找 file → file 自动识别 family.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALIASES_PATH = Path(__file__).resolve().parent / "strategy_aliases.json"


@dataclass(frozen=True)
class PivotSpec:
    """策略 pivot 公式 + 出场规则.

    适用 V8 / V13 / QExp 三大 family. QExp 没有 trailing stop, 用 profit_target 代替.
    """
    alias: str                       # 无限易 strategy_name (e.g. "AL", "AG_Mom")
    symbol: str                      # 品种代码 (e.g. "AL", "AG")
    strategy_name: str               # 文件名 stem (e.g. "AL_Long_1H_V8_Donchian_ADX_Filter")
    family: str                      # V8 / V13 / QExp_Mom / QExp_VSv2 / QExp_Pull / QExp_HVB
    bias: str                        # "long" 或 "short"
    constants: dict                  # {"DC_PERIOD": 40, ...}
    entry_formula: str
    entry_conditions: list[str]
    exit_pivots: list[str]
    # V8/V13 用 trail_stop_formula; QExp 用 profit_target_formula. 不适用时为空字符串.
    trail_stop_formula: str
    profit_target_formula: str
    hard_stop_pct: float
    trailing_pct: float              # QExp 时为 0
    profit_target_atr_mult: float    # V8/V13 时为 0


# ─────────────────────────────────────────────────────────────────────────────
# Alias loading
# ─────────────────────────────────────────────────────────────────────────────


def _load_aliases() -> dict[str, str]:
    if not _ALIASES_PATH.exists():
        return {}
    raw = json.loads(_ALIASES_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _extract_constants(text: str, names: list[str]) -> dict:
    out: dict = {}
    for name in names:
        m = re.search(rf"^{name}\s*=\s*(-?\d+\.?\d*)", text, re.MULTILINE)
        if m:
            val = m.group(1)
            out[name] = float(val) if "." in val else int(val)
    return out


def _extract_field_default(text: str, field_name: str) -> float | None:
    m = re.search(rf"{field_name}:\s*float\s*=\s*Field\(default=(-?\d+\.?\d*)", text)
    if m:
        return float(m.group(1))
    m = re.search(rf"{field_name}:\s*int\s*=\s*Field\(default=(-?\d+)", text)
    if m:
        return float(m.group(1))
    return None


def _detect_family(strategy_name: str, text: str) -> str | None:
    """从 file stem + 内容自动识别 family."""
    if "_V8" in strategy_name:
        return "V8"
    if "_V13" in strategy_name:
        return "V13"
    # QExp family — 看 import 的 signal class
    if "MomentumContinuationSignal" in text:
        return "QExp_Mom"
    if "VolSqueezeBreakoutLongV2Signal" in text:
        return "QExp_VSv2"
    if "PullbackStrongTrendSignal" in text:
        return "QExp_Pull"
    if "HighVolBreakdownShortSignal" in text:
        return "QExp_HVB"
    return None


def _detect_symbol(strategy_name: str) -> str:
    """文件名前缀提取品种代码 (AL_Long_..., AG_Long_..., HC_Short_...)."""
    m = re.match(r"^([A-Z]+)_", strategy_name)
    return m.group(1) if m else ""


# ─────────────────────────────────────────────────────────────────────────────
# V8 / V13 pivot 提取 (现有逻辑)
# ─────────────────────────────────────────────────────────────────────────────


def _build_v8(text: str) -> tuple[dict, str, list[str]]:
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
    return consts, entry_formula, entry_conds


def _build_v13(text: str) -> tuple[dict, str, list[str]]:
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
    return consts, entry_formula, entry_conds


def _build_v8v13_exits(family: str, consts: dict, hard: float, trail: float) -> tuple[list[str], str]:
    chand_period = consts.get("CHANDELIER_PERIOD", 22)
    chand_mult = consts.get("CHANDELIER_MULT", 2.58 if family == "V8" else 3.0)
    exit_pivots = [
        f"移动止损 (TRAIL_STOP):当前价 ≤ 移损线 → 平仓(分钟级判断,降噪)",
        f"硬止损 (HARD_STOP):tick 级,当前价 ≤ 入场均价 × (1 − {hard}%) → 立即平仓",
        f"吊灯出场 (Chandelier):close < 持仓期间最高价({chand_period}根) − {chand_mult}×ATR",
        "信号反转 (SIGNAL_REVERSAL):POS_DECISION optimal=0 → 平仓",
    ]
    trail_formula = (
        f"持仓开始后,每个 tick 实时更新 peak_price = max(peak_price, last_price);"
        f"\n移损线 = peak_price × (1 − {trail}%) = peak_price × {1 - trail/100:.5f};"
        f"\n判断频率:每分钟首个 tick(同一分钟只判一次,降噪);"
        f"\n触发条件:last_price ≤ 移损线 → 立即穿盘口平仓 (urgent)。"
        f"\n持仓重置点:开仓 / 加仓 / 减仓 / 平仓 → peak 重新初始化为入场价。"
    )
    return exit_pivots, trail_formula


# ─────────────────────────────────────────────────────────────────────────────
# QExp pivot 提取 (4 个 sub-family)
# ─────────────────────────────────────────────────────────────────────────────


def _build_qexp_mom(text: str) -> tuple[dict, str, list[str]]:
    """MomentumContinuationSignal — 强阳线连续."""
    consts = {
        "atr_lookback": 20, "body_atr_mult": 1.5, "body_to_range_min": 0.6,
        "cooldown_bars": 3,
    }
    entry_formula = (
        f"body = close − open;  range = high − low;  atr = ATR({consts['atr_lookback']});\n"
        f"body_to_range = body / range\n"
        f"FIRES iff: close > open AND body > {consts['body_atr_mult']}×atr "
        f"AND body_to_range >= {consts['body_to_range_min']} AND cooldown >= {consts['cooldown_bars']} bars"
    )
    entry_conds = [
        f"上涨 bar (close > open)",
        f"body > {consts['body_atr_mult']} × ATR(20)",
        f"body / range >= {consts['body_to_range_min']}(收盘在 range top {int((1-consts['body_to_range_min'])*100)}%)",
        f"距上次触发 >= {consts['cooldown_bars']} bars",
    ]
    return consts, entry_formula, entry_conds


def _build_qexp_vsv2(text: str) -> tuple[dict, str, list[str]]:
    """VolSqueezeBreakoutLongV2 — squeeze + breakout + trend filter."""
    consts = {
        "atr_lookback": 20, "atr_history_lookback": 60, "squeeze_ratio": 0.8,
        "breakout_lookback": 20, "body_to_range_min": 0.5,
        "trend_z_min": 0.5, "cooldown_bars": 5,
    }
    entry_formula = (
        f"squeeze = cur_ATR / mean(60 hist ATR);  z_60 = trend_zscore(log_returns)\n"
        f"FIRES iff: z_60 > +{consts['trend_z_min']} AND squeeze <= {consts['squeeze_ratio']} "
        f"AND close > 20-bar high AND body/range >= {consts['body_to_range_min']} "
        f"AND cooldown >= {consts['cooldown_bars']} bars"
    )
    entry_conds = [
        f"trend z_60 > +{consts['trend_z_min']}(uptrend filter)",
        f"cur_ATR / mean(60 hist ATR) <= {consts['squeeze_ratio']}(squeeze)",
        f"close > 20-bar high(excl current)(breakout)",
        f"body / range >= {consts['body_to_range_min']}(强收盘)",
        f"距上次触发 >= {consts['cooldown_bars']} bars",
    ]
    return consts, entry_formula, entry_conds


def _build_qexp_pull(text: str) -> tuple[dict, str, list[str]]:
    """PullbackStrongTrend — 强趋势回撤入场."""
    consts = {
        "lookback": 20, "atr_lookback": 20, "k_atr": 1.5, "recent_high_lookback": 8,
        "trend_z_min": 1.0, "cooldown_bars": 5,
    }
    entry_formula = (
        f"z_60 = trend_zscore(log_returns);  rolling_max = max(highs[-21:-1]);\n"
        f"pullback_atr_units = (rolling_max - close) / atr;  bars_since_max = {consts['lookback']-1} - argmax\n"
        f"FIRES iff: z_60 > +{consts['trend_z_min']} AND bars_since_max <= {consts['recent_high_lookback']} "
        f"AND pullback_atr_units >= {consts['k_atr']} AND cooldown >= {consts['cooldown_bars']} bars"
    )
    entry_conds = [
        f"trend z_60 > +{consts['trend_z_min']}(强 uptrend, 比 v2 +0.5 更严)",
        f"20-bar rolling max 在最近 {consts['recent_high_lookback']} bar 内(最近创新高)",
        f"(rolling_max − close) / ATR >= {consts['k_atr']}(深度回撤至少 {consts['k_atr']} ATR)",
        f"距上次触发 >= {consts['cooldown_bars']} bars",
    ]
    return consts, entry_formula, entry_conds


def _build_qexp_hvb(text: str) -> tuple[dict, str, list[str]]:
    """HighVolBreakdownShort — vol 扩张 + 跌破 = panic-selling SHORT."""
    consts = {
        "atr_lookback": 20, "atr_history_lookback": 60, "vol_expansion_min": 1.3,
        "breakdown_lookback": 20, "body_to_range_min": 0.5, "cooldown_bars": 5,
    }
    entry_formula = (
        f"vol_ratio = cur_ATR / mean(60 hist ATR);  body = open − close (positive on bear)\n"
        f"FIRES iff (SHORT 入场): vol_ratio >= {consts['vol_expansion_min']} "
        f"AND close < 20-bar low AND close < open AND body/range >= {consts['body_to_range_min']} "
        f"AND cooldown >= {consts['cooldown_bars']} bars"
    )
    entry_conds = [
        f"cur_ATR / mean(60 hist ATR) >= {consts['vol_expansion_min']}(vol 扩张 = regime change)",
        f"close < 20-bar low(excl current)(breakdown)",
        f"close < open AND body / range >= {consts['body_to_range_min']}(强阴线,不是 wick)",
        f"距上次触发 >= {consts['cooldown_bars']} bars",
    ]
    return consts, entry_formula, entry_conds


def _build_qexp_exits(bias: str, hard: float, atr_mult: float) -> tuple[list[str], str]:
    """QExp 出场: ATR profit target + 2% hard stop. 无 trailing."""
    if bias == "long":
        exit_pivots = [
            f"ATR 止盈:当前价 ≥ 入场均价 + {atr_mult} × 入场 ATR → 立即平仓 (urgent)",
            f"硬止损:tick 级,当前价 ≤ 入场均价 × (1 − {hard}%) → 立即平仓 (urgent)",
            "其他:无 trailing,无 Chandelier,无信号反转(QExp 是 binary signal,持有到任一出场触发)",
        ]
        formula = (
            f"开仓时记录 entry_price = avg_price, entry_atr = ATR(N).\n"
            f"profit_target = entry_price + {atr_mult} × entry_atr (固定值, 持仓期间不变)\n"
            f"hard_stop_line = entry_price × (1 − {hard}%)\n"
            f"触发条件:每 tick 检查, last_price ≥ profit_target → 平仓"
        )
    else:  # short
        exit_pivots = [
            f"ATR 止盈:当前价 ≤ 入场均价 − {atr_mult} × 入场 ATR → 立即买平 (urgent)",
            f"硬止损:tick 级,当前价 ≥ 入场均价 × (1 + {hard}%) → 立即买平 (urgent)",
            "其他:无 trailing,无 Chandelier,无信号反转(QExp 是 binary signal,持有到任一出场触发)",
        ]
        formula = (
            f"开仓时记录 entry_price = avg_price, entry_atr = ATR(N).\n"
            f"profit_target = entry_price − {atr_mult} × entry_atr (固定值, 持仓期间不变)\n"
            f"hard_stop_line = entry_price × (1 + {hard}%)\n"
            f"触发条件:每 tick 检查, last_price ≤ profit_target → 买平"
        )
    return exit_pivots, formula


# ─────────────────────────────────────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────────────────────────────────────


_QEXP_BUILDERS = {
    "QExp_Mom": _build_qexp_mom,
    "QExp_VSv2": _build_qexp_vsv2,
    "QExp_Pull": _build_qexp_pull,
    "QExp_HVB": _build_qexp_hvb,
}


def extract_pivot(alias: str) -> PivotSpec | None:
    """按 alias (无限易 strategy_name) 提取 pivot.

    自动识别 family (V8 / V13 / QExp_*), 解析对应公式 + 出场规则.
    aliases 配置在 strategy_aliases.json.
    """
    aliases = _load_aliases()
    rel_path = aliases.get(alias)
    if rel_path is None:
        # 向后兼容: alias 没在 json 里, 尝试当 symbol 用 (V8/V13)
        return _legacy_symbol_lookup(alias)

    file_path = _REPO_ROOT / rel_path
    if not file_path.exists():
        return None
    text = file_path.read_text(encoding="utf-8")
    strategy_name = file_path.stem
    family = _detect_family(strategy_name, text)
    if family is None:
        return None

    symbol = _detect_symbol(strategy_name)
    bias = "short" if "_Short_" in strategy_name else "long"
    hard = _extract_field_default(text, "hard_stop_pct") or (
        2.0 if family.startswith("QExp_") else 0.5
    )

    if family == "V8":
        consts, entry_formula, entry_conds = _build_v8(text)
        trail = _extract_field_default(text, "trailing_pct") or 0.3
        exit_pivots, trail_formula = _build_v8v13_exits(family, consts, hard, trail)
        return PivotSpec(
            alias=alias, symbol=symbol, strategy_name=strategy_name, family=family, bias=bias,
            constants=consts, entry_formula=entry_formula, entry_conditions=entry_conds,
            exit_pivots=exit_pivots, trail_stop_formula=trail_formula,
            profit_target_formula="", hard_stop_pct=hard, trailing_pct=trail,
            profit_target_atr_mult=0.0,
        )

    if family == "V13":
        consts, entry_formula, entry_conds = _build_v13(text)
        trail = _extract_field_default(text, "trailing_pct") or 0.3
        exit_pivots, trail_formula = _build_v8v13_exits(family, consts, hard, trail)
        return PivotSpec(
            alias=alias, symbol=symbol, strategy_name=strategy_name, family=family, bias=bias,
            constants=consts, entry_formula=entry_formula, entry_conditions=entry_conds,
            exit_pivots=exit_pivots, trail_stop_formula=trail_formula,
            profit_target_formula="", hard_stop_pct=hard, trailing_pct=trail,
            profit_target_atr_mult=0.0,
        )

    # QExp 4 个 family
    builder = _QEXP_BUILDERS.get(family)
    if builder is None:
        return None
    consts, entry_formula, entry_conds = builder(text)
    atr_mult = _extract_field_default(text, "profit_target_atr_mult") or 2.0
    exit_pivots, profit_formula = _build_qexp_exits(bias, hard, atr_mult)
    return PivotSpec(
        alias=alias, symbol=symbol, strategy_name=strategy_name, family=family, bias=bias,
        constants=consts, entry_formula=entry_formula, entry_conditions=entry_conds,
        exit_pivots=exit_pivots, trail_stop_formula="",
        profit_target_formula=profit_formula, hard_stop_pct=hard, trailing_pct=0.0,
        profit_target_atr_mult=atr_mult,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 向后兼容: alias 没在 json 里时, 按 symbol root 找 V8/V13
# ─────────────────────────────────────────────────────────────────────────────


_LEGACY_SYMBOL_DIR = {
    "AL": "AL/long", "CU": "CU/long", "HC": "HC/long",
    "AG": "AG/long", "JM": "JM/long", "P": "P/long", "PP": "PP/long",
}


def _legacy_symbol_lookup(symbol: str) -> PivotSpec | None:
    sub = _LEGACY_SYMBOL_DIR.get(symbol.upper())
    if sub is None:
        return None
    folder = _REPO_ROOT / "src" / sub
    if not folder.exists():
        return None
    candidates = sorted(
        f for f in folder.glob("*.py")
        if ("_V8" in f.name or "_V13" in f.name) and not f.name.endswith(".bak")
    )
    if not candidates:
        return None
    f = candidates[0]
    text = f.read_text(encoding="utf-8")
    strategy_name = f.stem
    family = _detect_family(strategy_name, text)
    if family is None:
        return None
    bias = "long"
    hard = _extract_field_default(text, "hard_stop_pct") or 0.5
    trail = _extract_field_default(text, "trailing_pct") or 0.3

    if family == "V8":
        consts, entry_formula, entry_conds = _build_v8(text)
    elif family == "V13":
        consts, entry_formula, entry_conds = _build_v13(text)
    else:
        return None
    exit_pivots, trail_formula = _build_v8v13_exits(family, consts, hard, trail)

    return PivotSpec(
        alias=symbol.upper(), symbol=symbol.upper(), strategy_name=strategy_name,
        family=family, bias=bias, constants=consts,
        entry_formula=entry_formula, entry_conditions=entry_conds,
        exit_pivots=exit_pivots, trail_stop_formula=trail_formula,
        profit_target_formula="", hard_stop_pct=hard, trailing_pct=trail,
        profit_target_atr_mult=0.0,
    )
