"""飞书非阻塞通知模块.

用法:
    from modules.feishu import feishu
    feishu("open", "i2609", "**建仓** 3手 @ 800.0")
"""
import time
import threading
import requests

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a6aeb603-3d9f-40b5-a5a9-8f0a9cd3bf71"
ENABLED = True

COLORS = {
    "open": "green", "add": "blue", "reduce": "orange", "close": "red",
    "hard_stop": "carmine", "trail_stop": "red", "equity_stop": "carmine",
    "circuit": "carmine", "daily_stop": "carmine", "flatten": "orange",
    "error": "carmine",
    "start": "turquoise", "shutdown": "grey", "daily_review": "purple",
    "warning": "yellow", "info": "blue", "heartbeat": "indigo",
    "no_tick": "carmine", "rollover": "orange",
}
LABELS = {
    "open": "开仓", "add": "加仓", "reduce": "减仓", "close": "平仓",
    "hard_stop": "硬止损", "trail_stop": "移动止损", "equity_stop": "权益止损",
    "circuit": "熔断", "daily_stop": "单日止损", "flatten": "收盘清仓",
    "error": "异常",
    "start": "策略启动", "shutdown": "策略停止", "daily_review": "每日回顾",
    "warning": "预警", "info": "信息", "heartbeat": "心跳",
    "no_tick": "行情中断", "rollover": "换月提醒",
}


def _post(action, symbol, msg):
    c = COLORS.get(action, "grey")
    l = LABELS.get(action, action)
    try:
        resp = requests.post(WEBHOOK, json={
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"{l} | {symbol}"}, "template": c},
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                              "content": f"{msg}\n\n---\n*{time.strftime('%Y-%m-%d %H:%M:%S')}*"}}],
            },
        }, timeout=3)
        if resp.status_code != 200:
            print(f"[feishu] HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[feishu] 发送失败: {type(e).__name__}: {e}")


def feishu(action, symbol, msg):
    """非阻塞发送飞书卡片. daemon线程, 不影响交易."""
    if not ENABLED or not WEBHOOK:
        return
    threading.Thread(target=_post, args=(action, symbol, msg), daemon=True).start()
