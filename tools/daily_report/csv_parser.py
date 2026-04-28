"""无限易"实时回报"CSV 解析器.

CSV 来源: 无限易客户端 → 实时回报 → 信息导出.
编码: GBK (中文系统默认导出格式).

每行 = 一笔订单 (开/平仓 或 撤单), 41 列. 关键字段 (1-based 按表头序):
   1  序号 (broker 内部, 倒序)
   6  交易所 (DCE/SHFE)
   7  合约代码 (p2609)
   8  合约名称 (棕榈油2609)
   9  买卖 (买/卖)
  10  开平 (开仓/平仓/平今)
  11  报单价格
  12  报单数量
  13  报单状态 (全部成交/已成/部成部撤/撤单/已撤)
  14  成交均价
  15  成交数量
  16  撤单数量
  17  剩余数量
  18  报单时间 (HH:MM:SS)
  19  撤单时间
  20  成交时间 (HH:MM:SS)
  21  成交时间(日) (YYYYMMDD HH:MM:SS) ← 唯一含日期的字段
  22  手续费
  23  平仓盈亏 (broker 逐日盯市口径, 含隔夜价差)
  24  平仓盈亏(逐笔) (按本笔开仓价精确算)
  28  备注 (.py 文件绝对路径) → 反推策略 alias

CsvTrade 是单纯 DTO; 不做配对/盈亏推算 (那是 trade_pairing.pair_from_csv 的事).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# CSV 表头中文列名 (UTF-8 解码后) → 英文 key
_HEADER_MAP = {
    "序号": "seq",
    "交易所": "exchange",
    "合约代码": "instrument_id",
    "合约名称": "full_name",
    "买卖": "buy_sell",
    "开平": "offset_cn",
    "报单价格": "send_price",
    "报单数量": "send_lots",
    "报单状态": "status",
    "成交均价": "fill_price",
    "成交数量": "fill_lots",
    "撤单数量": "cancel_lots",
    "剩余数量": "remaining_lots",
    "报单时间": "send_time",
    "撤单时间": "cancel_time",
    "成交时间": "fill_time",
    "成交时间(日)": "fill_datetime",
    "手续费": "fee",
    "平仓盈亏": "broker_pnl",
    "平仓盈亏(逐笔)": "per_trade_pnl",
    "备注": "strategy_path",
}


# 状态分类
_FILLED_STATUSES = {"全部成交", "已成"}
_PARTIAL_CANCEL_STATUSES = {"部成部撤"}
_CANCELLED_STATUSES = {"撤单", "已撤", "已撤销"}


@dataclass(frozen=True)
class CsvTrade:
    """CSV 一行 = 一笔订单 (成交 / 部成部撤 / 撤单)."""

    seq: int
    exchange: str
    instrument_id: str            # p2609
    symbol_root: str              # P
    full_name: str                # 棕榈油2609
    side: str                     # "buy" | "sell"
    offset_cn: str                # 开仓 / 平仓 / 平今
    offset: str                   # '0' = 开仓, '1' = 平仓 (含平昨), '3' = 平今
    send_price: float | None      # 报价 (撤单可能 None)
    send_lots: int                # 报单数量
    status: str                   # "全部成交" | "撤单" | "部成部撤" 等
    is_filled: bool               # status ∈ {全部成交, 已成, 部成部撤(成交部分)}
    is_cancelled: bool            # 完全撤单 (无任何成交)
    fill_price: float | None      # 成交均价 (撤单 None)
    fill_lots: int                # 实际成交手数 (撤单 0)
    cancel_lots: int              # 撤单数量
    send_ts: datetime             # 报单时间 (绝对时间)
    fill_ts: datetime | None      # 成交时间 (绝对时间, 撤单 None)
    fee: float                    # 手续费 (元, 撤单 0)
    broker_pnl: float | None      # 平仓盈亏 (broker 盯市, 开仓为 None)
    per_trade_pnl: float | None   # 平仓盈亏 (逐笔, 开仓为 None)
    strategy_path: str            # 完整 .py 路径
    strategy_alias: str | None    # 路径中提取 (P_Long_1H_V13 → P)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSTRUMENT_ROOT_RE = re.compile(r"^([A-Za-z]+)")
_PY_FILE_RE = re.compile(r"\\([A-Za-z]+_[A-Za-z]+_[\w]+\.py)$")


def _root_from_instrument(instrument_id: str) -> str:
    m = _INSTRUMENT_ROOT_RE.match(instrument_id.strip())
    return m.group(1).upper() if m else instrument_id.upper()


def _alias_from_path(path: str) -> str | None:
    """从 broker CSV 备注栏的 .py 路径反推策略 alias.

    路径例: ...\\self_strategy\\P_Long_1H_V13_Donchian_MFI.py
              → 取首段 → "P"
            ...\\HC_Long_1H_V8_Donchian_ADX_Fil  ← broker 截断
              → 取首段 → "HC"
    """
    if not path:
        return None
    m = re.search(r"[\\/]([A-Za-z]+)_[A-Za-z]+_[\w]+", path)
    if m:
        return m.group(1).upper()
    return None


def _parse_float_or_none(s: str) -> float | None:
    s = (s or "").strip()
    if s in ("", "--", "-", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: str) -> int:
    s = (s or "").strip()
    if s in ("", "--", "-"):
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


def _resolve_calendar_dt(time_str: str, trading_day: datetime) -> datetime | None:
    """根据 trading_day + HH:MM:SS 推 calendar datetime.

    无限易交易日规则: 21:00 开始算下一交易日.
    所以 trading_day=4-28 包含的 calendar 时间是:
      - 4-27 21:00 ~ 23:59 (前夜盘段)
      - 4-28 00:00 ~ 02:30 (后夜盘段)
      - 4-28 09:00 ~ 15:00 (日盘)
    规则: 时间 >= 21:00 → calendar = trading_day - 1 天

    broker 的"成交时间(日)"列因不同交易所/不同时刻标法不一致, 不可靠;
    本函数只信任 HH:MM:SS, 自己推 calendar.
    """
    if not time_str:
        return None
    try:
        t = datetime.strptime(time_str.strip(), "%H:%M:%S").time()
    except ValueError:
        return None
    cal_date = trading_day - timedelta(days=1) if t.hour >= 21 else trading_day
    return cal_date.replace(hour=t.hour, minute=t.minute, second=t.second)


def _trading_day_from_filename(path: Path) -> datetime | None:
    """从 broker CSV 文件名提交易日: 实时回报(信息导出)_2026_04_28-15_12_43(1).csv → 4-28."""
    m = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", path.name)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _classify_status(status: str, fill_lots: int, cancel_lots: int) -> tuple[bool, bool]:
    """(is_filled, is_cancelled) — 同时为 True 表示部成部撤."""
    s = status.strip()
    is_filled = s in _FILLED_STATUSES or fill_lots > 0
    fully_cancelled = s in _CANCELLED_STATUSES and fill_lots == 0
    return is_filled, fully_cancelled


def _offset_code(offset_cn: str) -> str:
    """开仓→'0', 平今→'3', 平仓/平昨→'1'."""
    s = offset_cn.strip()
    if s == "开仓":
        return "0"
    if s == "平今":
        return "3"
    return "1"


def _read_with_encoding_fallback(path: Path) -> str:
    """CSV 以 GBK / GB18030 / UTF-8 顺序尝试解码."""
    for enc in ("gbk", "gb18030", "utf-8-sig", "utf-8"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_csv(
    path: str | Path,
    *,
    trading_day: str | None = None,
) -> list[CsvTrade]:
    """解析无限易实时回报 CSV.

    Args:
        path: csv 文件路径
        trading_day: 交易日 (YYYY-MM-DD); 未指定则从文件名推断
                     (broker 给的"成交时间(日)"列因交易所差异不可靠)

    Returns:
        按 fill_ts (撤单按 send_ts) 升序排序的 CsvTrade 列表.
    """
    p = Path(path)
    text = _read_with_encoding_fallback(p)
    rows = list(csv.DictReader(text.splitlines()))

    # 决定 trading_day
    td_dt: datetime | None = None
    if trading_day:
        try:
            td_dt = datetime.strptime(trading_day, "%Y-%m-%d")
        except ValueError:
            td_dt = None
    if td_dt is None:
        td_dt = _trading_day_from_filename(p)
    if td_dt is None:
        # 最后兜底: 从任意行的"成交时间(日)"取日期 (但不信任 trading_day 转换)
        for r in rows:
            dt_str = (r.get("成交时间(日)") or "").strip()
            if dt_str:
                try:
                    parsed = datetime.strptime(dt_str, "%Y%m%d %H:%M:%S")
                    # 若解析时间 >= 21:00, 实际 calendar = -1 天
                    td_dt = parsed if parsed.hour < 21 else parsed - timedelta(days=1)
                    td_dt = td_dt.replace(hour=0, minute=0, second=0)
                    # 但 trading_day 是 calendar_day + 1 (若 hour >= 21)
                    if parsed.hour >= 21:
                        td_dt = td_dt + timedelta(days=1)
                    break
                except (ValueError, IndexError):
                    continue
    if td_dt is None:
        raise ValueError(
            f"无法确定 CSV 交易日 (文件名 '{p.name}' 无日期, 也没传 trading_day 参数)"
        )

    fieldnames = list(rows[0].keys()) if rows else []
    header_to_eng: dict[str, str] = {
        cn: _HEADER_MAP[cn] for cn in fieldnames if cn in _HEADER_MAP
    }

    out: list[CsvTrade] = []
    for row_cn in rows:
        if not row_cn:
            continue
        # 中文 → 英文
        row = {header_to_eng[k]: v for k, v in row_cn.items() if k in header_to_eng}

        instrument_id = (row.get("instrument_id") or "").strip()
        if not instrument_id:
            continue

        side_cn = (row.get("buy_sell") or "").strip()
        side = "buy" if side_cn == "买" else "sell"
        offset_cn = (row.get("offset_cn") or "").strip()
        offset = _offset_code(offset_cn)

        fill_lots = _parse_int(row.get("fill_lots", "0"))
        cancel_lots = _parse_int(row.get("cancel_lots", "0"))
        status = (row.get("status") or "").strip()
        is_filled, is_cancelled = _classify_status(status, fill_lots, cancel_lots)

        # 时间: HH:MM:SS + trading_day → calendar datetime (broker 给的日期不可靠, 不用)
        send_t = (row.get("send_time") or "").strip()
        fill_t = (row.get("fill_time") or "").strip()

        send_ts = _resolve_calendar_dt(send_t, td_dt)
        if send_ts is None:
            continue
        fill_ts: datetime | None = None
        if is_filled:
            fill_ts = _resolve_calendar_dt(fill_t or send_t, td_dt)
            if fill_ts is None:
                fill_ts = send_ts

        out.append(
            CsvTrade(
                seq=_parse_int(row.get("seq", "0")),
                exchange=(row.get("exchange") or "").strip(),
                instrument_id=instrument_id,
                symbol_root=_root_from_instrument(instrument_id),
                full_name=(row.get("full_name") or "").strip(),
                side=side,
                offset_cn=offset_cn,
                offset=offset,
                send_price=_parse_float_or_none(row.get("send_price", "")),
                send_lots=_parse_int(row.get("send_lots", "0")),
                status=status,
                is_filled=is_filled,
                is_cancelled=is_cancelled,
                fill_price=_parse_float_or_none(row.get("fill_price", "")),
                fill_lots=fill_lots,
                cancel_lots=cancel_lots,
                send_ts=send_ts,
                fill_ts=fill_ts,
                fee=_parse_float_or_none(row.get("fee", "")) or 0.0,
                broker_pnl=_parse_float_or_none(row.get("broker_pnl", "")),
                per_trade_pnl=_parse_float_or_none(row.get("per_trade_pnl", "")),
                strategy_path=(row.get("strategy_path") or "").strip(),
                strategy_alias=_alias_from_path(row.get("strategy_path", "")),
            )
        )

    # 按时间排序: 成交按 fill_ts, 撤单按 send_ts
    out.sort(key=lambda t: t.fill_ts or t.send_ts)
    return out


def split_filled_and_cancelled(
    trades: list[CsvTrade],
) -> tuple[list[CsvTrade], list[CsvTrade]]:
    """分离 (有效成交, 完全撤单).

    部成部撤 (status='部成部撤' 且 fill_lots>0) 归入"有效成交".
    """
    filled = [t for t in trades if t.is_filled]
    cancelled = [t for t in trades if t.is_cancelled and not t.is_filled]
    return filled, cancelled
