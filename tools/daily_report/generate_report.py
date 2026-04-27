"""Daily report 主入口.

推荐用法 (data-dir 模式 — 把当天的所有素材放一个文件夹):
    python tools/daily_report/generate_report.py --data-dir reports/4-27
    # → 自动找该目录下的 StraLog*.txt + {品种}.jpg/png 图,输出 2026-04-27.html 到该目录

或者老用法:
    python tools/daily_report/generate_report.py --date 2026-04-27 --log "logs/StraLog(4).txt"

默认:
    --date today
    --log: 自动找 data-dir (或 logs/) 里 mtime 最新的 StraLog*.txt
    --out: data-dir 本身 (或 reports/)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# 允许 `python tools/daily_report/generate_report.py` 直接运行
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "tools.daily_report"

from tools.daily_report.log_parser import parse_log
from tools.daily_report.pivot_extractor import extract_pivot
from tools.daily_report.render_html import render_report
from tools.daily_report.session_window import session_window_for
from tools.daily_report.trade_pairing import pair_trades


_REPO_ROOT = Path(__file__).resolve().parents[2]


_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _autoselect_log(logs_dir: Path) -> Path:
    candidates = sorted(
        (p for p in logs_dir.glob("StraLog*.txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"在 {logs_dir} 下找不到 StraLog*.txt")
    return candidates[0]


_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


def _find_chrome() -> str | None:
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """用 Chrome headless 把 HTML 转 PDF. 失败返回 False."""
    chrome = _find_chrome()
    if chrome is None:
        print("[WARN] 未找到 Chrome/Chromium, 跳过 PDF 生成", file=sys.stderr)
        return False
    try:
        subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                f"file://{html_path.resolve()}",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return pdf_path.exists()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[ERROR] PDF 生成失败: {e}", file=sys.stderr)
        return False


def _scan_symbol_images(data_dir: Path) -> dict[str, str]:
    """扫描目录下的 {品种}.{ext} 图片, 返回 {SYMBOL: 文件名} 映射 (相对 data_dir)."""
    out: dict[str, str] = {}
    for p in data_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in _IMG_EXTS:
            continue
        sym = p.stem.upper()
        # 只接受单纯品种名 (字母, 1-4 chars), 比如 AL/JM/PP/CU/AG, 排除 hash 名
        if 1 <= len(sym) <= 4 and sym.isalpha():
            out[sym] = p.name
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PythonGO daily algo trading report")
    ap.add_argument("--date", default=None,
                    help="目标交易日 (YYYY-MM-DD), 默认今天")
    ap.add_argument("--data-dir", default=None,
                    help="当天素材目录 (内含 StraLog*.txt 和 {品种}.jpg). "
                         "指定后 --log/--out 默认都从这里来")
    ap.add_argument("--log", default=None,
                    help="StraLog 文件路径; 默认自动找 logs/ 最新的")
    ap.add_argument("--out", default=None,
                    help="输出目录, 默认 reports/")
    ap.add_argument("--no-pdf", action="store_true",
                    help="只生成 HTML, 不生成 PDF")
    args = ap.parse_args(argv)

    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
        else date.today()
    )
    win = session_window_for(target)

    # data_dir 优先级: 显式 --data-dir > None
    data_dir: Path | None = None
    if args.data_dir:
        data_dir = Path(args.data_dir)
        if not data_dir.is_absolute():
            data_dir = _REPO_ROOT / data_dir
        if not data_dir.exists():
            print(f"[ERROR] data dir not found: {data_dir}", file=sys.stderr)
            return 2

    # log_path
    if args.log:
        log_path = Path(args.log)
    elif data_dir is not None:
        log_path = _autoselect_log(data_dir)
    else:
        log_path = _autoselect_log(_REPO_ROOT / "logs")
    if not log_path.is_absolute():
        log_path = _REPO_ROOT / log_path
    if not log_path.exists():
        print(f"[ERROR] log not found: {log_path}", file=sys.stderr)
        return 2

    # out_dir
    if args.out:
        out_dir = Path(args.out)
    elif data_dir is not None:
        out_dir = data_dir
    else:
        out_dir = _REPO_ROOT / "reports"
    if not out_dir.is_absolute():
        out_dir = _REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target.isoformat()}.html"

    # 图片扫描 (从 data_dir)
    sym_images = _scan_symbol_images(data_dir) if data_dir is not None else {}

    # 1. 解析 log + 过滤到交易日窗口
    all_events = parse_log(str(log_path))
    events_in_window = [ev for ev in all_events if win.contains(ev.event_ts)]
    print(f"[INFO] 解析 log: {log_path.name}")
    print(f"[INFO] 窗口: {win.start} → {win.end}")
    print(f"[INFO] 命中事件: {len(events_in_window)}")

    if not events_in_window:
        print(f"[WARN] 窗口内无事件; 跳过", file=sys.stderr)
        # 仍生成空报告
    # 2. 配对 trade
    trips = pair_trades(events_in_window)
    print(f"[INFO] Round-trips: {len(trips)} "
          f"(closed={sum(1 for r in trips if not r.is_open)}, "
          f"open={sum(1 for r in trips if r.is_open)})")

    # 3. 提取每品种 pivot
    #    - 包含有交易的品种 ∪ 有图片的品种 (CU 当天没开仓但有盘面图也展示)
    symbols = sorted({rt.symbol_root for rt in trips} | set(sym_images.keys()))
    pivots = {sym: extract_pivot(sym) for sym in symbols}
    print(f"[INFO] 品种: {symbols}")
    if sym_images:
        print(f"[INFO] 找到图片: {sorted(sym_images.keys())}")

    # 4. 渲染 HTML
    html_text = render_report(
        target_date=target,
        win_start=win.start,
        win_end=win.end,
        trips=trips,
        pivots=pivots,
        log_path=str(log_path),
        symbols=symbols,
        sym_images=sym_images,
    )
    out_path.write_text(html_text, encoding="utf-8")
    print(f"[OK] HTML 已生成: {out_path}")

    # PDF
    if not args.no_pdf:
        pdf_path = out_path.with_suffix(".pdf")
        if _html_to_pdf(out_path, pdf_path):
            print(f"[OK] PDF  已生成: {pdf_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
