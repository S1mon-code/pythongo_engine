"""错单流控工具 (2026-04-20).

PythonGO 官方 base.py 的 on_error 默认实现有自动流控:
- 任何错误都 trading=False
- errCode=0004 撤单错误时, Timer(limit_time=2s) 后自动恢复 trading

我们策略 override 了 on_error 但没调 super, 失去了 0004 流控 ——
连续撤单错误会刷屏飞书且没有冷却。本 module 把流控逻辑抽出来,
让每个策略 on_error 都能一行接入。

用法 (策略 on_error 里):
    from modules.error_handler import throttle_on_error

    def on_error(self, error):
        self.output(f"[错误] {error}")
        feishu("error", self.params_map.instrument_id, f"**异常**: {error}")
        throttle_on_error(self, error)
"""
from __future__ import annotations

from threading import Timer
from typing import Any


def throttle_on_error(strategy, error: dict[str, Any], cooldown_sec: float = 2.0) -> None:
    """0004 撤单错误自动流控.

    策略在 on_error 调用, 与现有 output/feishu 逻辑并存。

    行为:
    - errCode == "0004"(撤单错误) → trading=False, Timer(cooldown_sec)后恢复
    - 其他 errCode → 不改 trading(尊重策略原有行为,不引入额外 freeze)

    Args:
        strategy: BaseStrategy 实例(需有 .trading 属性)
        error: on_error 回调的 error dict, 应有 "errCode" 键
        cooldown_sec: 冷却秒数, 默认 2 (和 PythonGO base.limit_time 对齐)
    """
    err_code = str(error.get("errCode", "")).strip()
    if err_code != "0004":
        return

    # 0004 撤单错误: 短暂 freeze 然后恢复
    strategy.trading = False
    strategy.output(
        f"[0004 流控] 撤单错误冷却 {cooldown_sec}s, 期间 send_order 静默 return None"
    )

    def _restore():
        strategy.trading = True
        strategy.output("[0004 流控] 已恢复")

    Timer(cooldown_sec, _restore).start()
