"""合约规格 — 乘数 / tick / 手续费.

数据源: AlphaForge specs.yaml. 无依赖 alphaforge runtime, 直接读 YAML.
找不到时 fallback 到 pythongo_engine/src/modules/contract_info.py 的乘数表.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Locate AlphaForge specs.yaml
# ---------------------------------------------------------------------------

_DEFAULT_SPECS = Path.home() / "Desktop/AlphaForge/alphaforge/data/specs.yaml"


def _find_specs_path() -> Path | None:
    env = os.environ.get("ALPHAFORGE_SPECS_PATH")
    if env and Path(env).exists():
        return Path(env)
    if _DEFAULT_SPECS.exists():
        return _DEFAULT_SPECS
    return None


# ---------------------------------------------------------------------------
# Lightweight YAML parser (avoid PyYAML dep)
# specs.yaml 是 well-structured, 不需要完整 YAML; 用简单缩进解析.
# ---------------------------------------------------------------------------


def _parse_specs_yaml(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cur_symbol: str | None = None
    cur: dict | None = None
    cur_sub: dict | None = None
    sub_key: str | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#"):
            continue
        # 顶级 symbol: e.g. "AL:" no leading space
        if raw and raw[0].isalnum():
            line = raw.rstrip(":")
            cur_symbol = line.strip().rstrip(":")
            cur = {}
            out[cur_symbol] = cur
            cur_sub = None
            sub_key = None
            continue
        if cur is None:
            continue
        # 2-space indent: top-level key
        if raw.startswith("  ") and not raw.startswith("    "):
            stripped = raw.strip()
            if stripped.endswith(":"):
                sub_key = stripped[:-1]
                cur_sub = {}
                cur[sub_key] = cur_sub
                continue
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                cur[k.strip()] = _coerce(v.strip())
                cur_sub = None
                sub_key = None
                continue
        # 4-space indent: nested key under sub_key (e.g. commission.open)
        if raw.startswith("    ") and cur_sub is not None:
            stripped = raw.strip()
            if stripped.startswith("- "):  # list value (delivery_months)
                cur.setdefault(sub_key, []).append(_coerce(stripped[2:].strip()))
                continue
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                cur_sub[k.strip()] = _coerce(v.strip())
    return out


def _coerce(s: str):
    if s == "" or s.lower() in ("null", "none"):
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if re.match(r"^-?\d+$", s):
        return int(s)
    if re.match(r"^-?\d*\.?\d+([eE][-+]?\d+)?$", s):
        return float(s)
    return s.strip("'\"")


# ---------------------------------------------------------------------------
# Fallback multipliers (from src/modules/contract_info.py)
# ---------------------------------------------------------------------------

_FALLBACK_MULT = {
    "AL": 5, "CU": 5, "AG": 15, "AU": 1000,
    "RB": 10, "HC": 10, "FU": 10, "BU": 10, "RU": 10, "SP": 10,
    "I": 100, "JM": 60, "J": 100,
    "P": 10, "Y": 10, "M": 10, "A": 10, "B": 10, "C": 10, "CS": 10,
    "PP": 5, "L": 5, "V": 5, "EG": 10, "EB": 5, "PG": 20,
    "JD": 10, "LH": 16,
    "IF": 300, "IH": 300, "IC": 200, "IM": 200,
    "LC": 1, "SI": 5,
}

_FALLBACK_TICK = {
    "AL": 5, "CU": 10, "AG": 1, "AU": 0.02,
    "RB": 1, "HC": 1, "FU": 1, "BU": 2, "RU": 5, "SP": 2,
    "I": 0.5, "JM": 0.5, "J": 0.5,
    "P": 2, "Y": 2, "M": 1, "A": 1, "B": 1, "C": 1, "CS": 1,
    "PP": 1, "L": 1, "V": 1, "EG": 1, "EB": 1, "PG": 1,
    "JD": 1, "LH": 5,
    "LC": 50, "SI": 5,
}


@dataclass(frozen=True)
class CommissionSpec:
    type: Literal["fixed", "ratio"]
    open: float
    close_today: float
    close_yesterday: float


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    multiplier: float
    tick_size: float
    commission: CommissionSpec | None  # None → 手续费置 0

    def calc_commission(self, price: float, lots: int, *, offset: str) -> float:
        """offset: '0'=open, '1'=close_today, '3'=close_yesterday."""
        if self.commission is None:
            return 0.0
        c = self.commission
        if offset == "0":
            rate = c.open
        elif offset == "1":
            rate = c.close_today
        else:
            rate = c.close_yesterday
        if c.type == "fixed":
            return rate * lots
        # ratio: rate × notional
        notional = price * lots * self.multiplier
        return rate * notional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _symbol_root(instrument_id: str) -> str:
    """al2607 → AL, hc2510 → HC, ag2606 → AG."""
    s = re.match(r"^([A-Za-z]+)", instrument_id.strip())
    return s.group(1).upper() if s else instrument_id.upper()


_SPECS_CACHE: dict[str, dict] | None = None


def _load_specs() -> dict[str, dict]:
    global _SPECS_CACHE
    if _SPECS_CACHE is not None:
        return _SPECS_CACHE
    path = _find_specs_path()
    if path is None:
        _SPECS_CACHE = {}
        return _SPECS_CACHE
    try:
        _SPECS_CACHE = _parse_specs_yaml(path)
    except Exception:
        _SPECS_CACHE = {}
    return _SPECS_CACHE


def get_spec(symbol_or_instrument: str) -> ContractSpec:
    root = _symbol_root(symbol_or_instrument)
    specs = _load_specs()
    raw = specs.get(root, {})

    mult = raw.get("multiplier", _FALLBACK_MULT.get(root, 1))
    tick = raw.get("tick_size", _FALLBACK_TICK.get(root, 1))

    comm_raw = raw.get("commission") or {}
    if comm_raw and comm_raw.get("type") in ("fixed", "ratio"):
        comm = CommissionSpec(
            type=comm_raw["type"],
            open=float(comm_raw.get("open", 0.0)),
            close_today=float(comm_raw.get("close_today", 0.0)),
            close_yesterday=float(comm_raw.get("close_yesterday", 0.0)),
        )
    else:
        comm = None

    return ContractSpec(
        symbol=root,
        multiplier=float(mult),
        tick_size=float(tick),
        commission=comm,
    )
