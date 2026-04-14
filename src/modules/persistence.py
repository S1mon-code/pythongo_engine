"""状态持久化模块 — 原子写JSON.

用法:
    from modules.persistence import save_state, load_state
    save_state({"peak_equity": 1000000, "avg_price": 800.0}, name="MyStrategy")
    saved = load_state(name="MyStrategy")
"""
import os
import json
import time

STATE_DIR = "./state"


def save_state(data, name="default"):
    """原子写: temp → fsync → rename. 失败不影响交易."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, f"{name}_state.json")
        tmp = path + ".tmp"
        data["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception as e:
        print(f"[persistence] 保存失败 {name}: {type(e).__name__}: {e}")


def load_state(name="default"):
    """读主文件, 失败读备份. 返回dict或None."""
    for sfx in ("", ".bak"):
        p = os.path.join(STATE_DIR, f"{name}_state.json{sfx}")
        if os.path.exists(p):
            try:
                with open(p) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
    return None
