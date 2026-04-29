"""StraLog 解析器.

新格式 (2026-04-24+) line 样例:
    [2026-04-27 10:00:00.105] [StraLog] [info] [2026-04-27 10:00:00] [AL] \
        [POS_DECISION] optimal=4 (raw=4.34) own_pos=0 → target=3 broker_pos=3 ...

抽出关键事件: POS_DECISION / EXECUTE / EXEC_OPEN / EXEC_STOP / ON_TRADE
            / TRAIL_STOP / IND / SIGNAL / ON_ORDER_CANCEL / 启动
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


# [2026-04-27 10:00:00.105] [StraLog] [info] [2026-04-27 10:00:00] [AL] [TAG] body
# tag 可能是英文 (POS_DECISION / ON_TRADE / EXEC_OPEN ...) 或中文 ([滑点])
_LINE_RE = re.compile(
    r"^\[(?P<wall>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\] "
    r"\[StraLog\] \[\w+\] "
    r"\[(?P<event_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"\[(?P<symbol>[A-Za-z0-9_]+)\] "
    r"\[(?P<tag>[\w一-鿿]+)\](?:\[[\w一-鿿]+\])* "
    r"(?P<body>.*)$"
)


@dataclass(frozen=True)
class LogEvent:
    wall_ts: datetime
    event_ts: datetime
    symbol: str        # AL / CU / JM / ...
    tag: str           # POS_DECISION / EXECUTE / ON_TRADE / ...
    body: str
    raw: str


def parse_line(line: str) -> LogEvent | None:
    m = _LINE_RE.match(line.rstrip("\n"))
    if m is None:
        return None
    try:
        wall = datetime.strptime(m.group("wall"), "%Y-%m-%d %H:%M:%S")
        event_ts = datetime.strptime(m.group("event_ts"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return LogEvent(
        wall_ts=wall,
        event_ts=event_ts,
        symbol=m.group("symbol"),
        tag=m.group("tag"),
        body=m.group("body"),
        raw=line.rstrip("\n"),
    )


def parse_log(path: str) -> Iterator[LogEvent]:
    with open(path, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            ev = parse_line(line)
            if ev is not None:
                yield ev


# ---------------------------------------------------------------------------
# Body parsers — 各 tag 的字段提取
# ---------------------------------------------------------------------------

_POS_DECISION_RE = re.compile(
    r"optimal=(?P<optimal>-?\d+) \(raw=(?P<raw>-?\d+\.?\d*)\) "
    r"own_pos=(?P<own_pos>-?\d+) → target=(?P<target>-?\d+) "
    r"broker_pos=(?P<broker_pos>-?\d+) "
    r"\(capital=(?P<capital>[\d.]+) atr=(?P<atr>[\d.]+)\)"
)

_EXECUTE_RE = re.compile(
    r"action=(?P<action>\w+) price=(?P<price>-?\d+\.?\d*) "
    r"own_pos=(?P<own_pos>-?\d+) target=(?P<target>\S+) "
    r"reason=(?P<reason>.*)"
)

_EXEC_OPEN_RE = re.compile(
    r"send_order (?P<side>buy|sell) (?P<lots>\d+)手 @ (?P<price>-?\d+\.?\d*)"
)

_EXEC_OPEN_OID_RE = re.compile(r"send_order 返回 oid=(?P<oid>-?\d+)")

_EXEC_STOP_RE = re.compile(
    r"(?P<reason>\w+) auto_close (?P<side>buy|sell) (?P<lots>\d+)手 @ (?P<price>-?\d+\.?\d*)"
)

_ON_TRADE_RE = re.compile(
    r"oid=(?P<oid>-?\d+) direction='(?P<direction>[01])' offset='(?P<offset>[013])' "
    r"price=(?P<price>-?\d+\.?\d*) vol=(?P<vol>\d+)"
)

_TRAIL_STOP_RE = re.compile(
    r"(?:M1 )?(?:price=)?(?P<close>-?\d+\.?\d*) <= 移损线(?P<line>-?\d+\.?\d*)"
)

_IND_V8_RE = re.compile(
    r"DC_U=(?P<dc_u>[\d.]+)\s+DC_M=(?P<dc_m>[\d.]+)\s+DC_L=(?P<dc_l>[\d.]+)"
    r"(?:\s+Chandelier=(?P<chandelier>[\d.]+))?"
    r".*?ADX=(?P<adx>[\d.]+)\s+PDI=(?P<pdi>[\d.]+)\s+MDI=(?P<mdi>[\d.]+)"
    r".*?close=(?P<close>[\d.]+)"
)

_IND_V13_RE = re.compile(
    r"DC_U=(?P<dc_u>[\d.]+)\s+DC_M=(?P<dc_m>[\d.]+)\s+DC_L=(?P<dc_l>[\d.]+)"
    r"(?:\s+Chandelier=(?P<chandelier>[\d.]+))?"
    r".*?MFI=(?P<mfi>[\d.]+)"
    r".*?close=(?P<close>[\d.]+)"
)

_SIGNAL_RE = re.compile(r"raw=(?P<raw>-?\d+\.?\d*) forecast=(?P<forecast>-?\d+\.?\d*)")

_SLIP_RE = re.compile(r"(?P<ticks>-?\d+\.?\d*)\s*ticks")

# [ON_START 恢复] own_pos=4 avg=3379.0 peak=3403.5 my_oids=7
_ON_START_RECOVER_RE = re.compile(
    r"own_pos=(?P<own_pos>-?\d+)\s+avg=(?P<avg>[\d.]+)\s+peak=(?P<peak>[\d.]+)"
)


# [ON_START 恢复] 整行匹配 (因为 tag "ON_START 恢复" 含空格, 不被主正则覆盖)
_STARTUP_LINE_RE = re.compile(
    r"\[(?P<event_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
    r"\[(?P<symbol>[A-Za-z0-9_]+)\] \[ON_START 恢复\] "
    r"own_pos=(?P<own_pos>-?\d+)\s+avg=(?P<avg>[\d.]+)\s+peak=(?P<peak>[\d.]+)"
)


def parse_startup_states(log_path: str) -> dict[str, dict]:
    """从 strategy log 抽每个品种 [ON_START 恢复] 的 own_pos / avg_price / peak_price.

    用于孤儿 takeover 时区分:
      - strategy 启动时 own_pos > 0 → 真接管 (用 avg_price 当入场价)
      - strategy 启动时 own_pos = 0 → broker 异常 (此笔与 strategy 无关)

    Returns: {symbol_root: {"own_pos": int, "avg_price": float, "peak_price": float, "ts": datetime}}
    """
    out: dict[str, dict] = {}
    with open(log_path, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            m = _STARTUP_LINE_RE.search(line)
            if not m:
                continue
            sym = m.group("symbol").upper()
            if sym in out:
                continue   # 取每品种首次启动 (当日交易日开盘)
            try:
                ts = datetime.strptime(m.group("event_ts"), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            out[sym] = {
                "own_pos": int(m.group("own_pos")),
                "avg_price": float(m.group("avg")),
                "peak_price": float(m.group("peak")),
                "ts": ts,
            }
    return out


def parse_pos_decision(body: str) -> dict | None:
    m = _POS_DECISION_RE.search(body)
    if not m:
        return None
    return {
        "optimal": int(m.group("optimal")),
        "raw": float(m.group("raw")),
        "own_pos": int(m.group("own_pos")),
        "target": int(m.group("target")),
        "broker_pos": int(m.group("broker_pos")),
        "capital": float(m.group("capital")),
        "atr": float(m.group("atr")),
    }


def parse_execute(body: str) -> dict | None:
    m = _EXECUTE_RE.search(body)
    if not m:
        return None
    target_raw = m.group("target")
    target_int: int | None = None
    if target_raw not in ("None", ""):
        try:
            target_int = int(target_raw)
        except ValueError:
            target_int = None
    return {
        "action": m.group("action"),
        "price": float(m.group("price")),
        "own_pos": int(m.group("own_pos")),
        "target": target_int,
        "reason": m.group("reason").strip(),
    }


def parse_exec_open(body: str) -> dict | None:
    """处理 EXEC_OPEN 两种 body: send_order 或 send_order 返回 oid."""
    m_oid = _EXEC_OPEN_OID_RE.search(body)
    if m_oid:
        return {"kind": "oid", "oid": int(m_oid.group("oid"))}
    m_send = _EXEC_OPEN_RE.search(body)
    if m_send:
        return {
            "kind": "send",
            "side": m_send.group("side"),
            "lots": int(m_send.group("lots")),
            "price": float(m_send.group("price")),
        }
    return None


def parse_exec_stop(body: str) -> dict | None:
    m = _EXEC_STOP_RE.search(body)
    if not m:
        return None
    return {
        "reason": m.group("reason"),
        "side": m.group("side"),
        "lots": int(m.group("lots")),
        "price": float(m.group("price")),
    }


def parse_on_trade(body: str) -> dict | None:
    m = _ON_TRADE_RE.search(body)
    if not m:
        return None
    return {
        "oid": int(m.group("oid")),
        "direction": m.group("direction"),  # '0'=buy, '1'=sell
        "offset": m.group("offset"),         # '0'=open, '1'=close_today, '3'=close_yesterday
        "price": float(m.group("price")),
        "vol": int(m.group("vol")),
    }


def parse_trail_stop(body: str) -> dict | None:
    m = _TRAIL_STOP_RE.search(body)
    if not m:
        return None
    return {
        "close": float(m.group("close")),
        "line": float(m.group("line")),
    }


def parse_ind(body: str) -> dict | None:
    # V13 has MFI; V8 has ADX. Try V13 first to avoid "MFI" being missed in V8 path.
    m = _IND_V8_RE.search(body)
    if m:
        out = {
            "kind": "v8",
            "dc_u": float(m.group("dc_u")),
            "dc_m": float(m.group("dc_m")),
            "dc_l": float(m.group("dc_l")),
            "adx": float(m.group("adx")),
            "pdi": float(m.group("pdi")),
            "mdi": float(m.group("mdi")),
            "close": float(m.group("close")),
        }
        if m.group("chandelier"):
            out["chandelier"] = float(m.group("chandelier"))
        return out
    m = _IND_V13_RE.search(body)
    if m:
        out = {
            "kind": "v13",
            "dc_u": float(m.group("dc_u")),
            "dc_m": float(m.group("dc_m")),
            "dc_l": float(m.group("dc_l")),
            "mfi": float(m.group("mfi")),
            "close": float(m.group("close")),
        }
        if m.group("chandelier"):
            out["chandelier"] = float(m.group("chandelier"))
        return out
    return None


def parse_signal(body: str) -> dict | None:
    m = _SIGNAL_RE.search(body)
    if not m:
        return None
    return {"raw": float(m.group("raw")), "forecast": float(m.group("forecast"))}


def parse_slip(body: str) -> float | None:
    """[滑点] N.Nticks → ticks 数 (策略 SlippageTracker 约定: 正数=不利, 负数=有利).

    单位是 per lot (每手). 例:
      P 卖出 fill=9819, signal=9821, tick=2 → ticks=(9821-9819)/2 = +1.0 (不利)
    """
    m = _SLIP_RE.search(body)
    if not m:
        return None
    return float(m.group("ticks"))
