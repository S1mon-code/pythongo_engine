import os
import sys
import traceback
from datetime import date, datetime
from importlib import machinery, reload
from typing import Any

from pythongo.infini import write_log

qt_origin_path = os.path.join(
    sys.base_prefix, "Lib", "site-packages", "PyQt5", "Qt5", "plugins"
)

if os.path.exists(qt_origin_path):
    #: 正确设置 QT 路径
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = qt_origin_path


def import_strategy(path: str) -> tuple[str, None]:
    """
    导入 Python 策略

    Args:
        path: 策略文件路径
    """

    try:
        file_name: str = os.path.basename(path)
        strategy_name = os.path.splitext(file_name)[0]
        machinery.SourceFileLoader(strategy_name, path).load_module()
        if hasattr(sys.modules[strategy_name], strategy_name):
            return "", getattr(sys.modules[strategy_name], strategy_name)
        return f"策略文件 {file_name} 中没有 {strategy_name} 类, 请检查", None
    except:
        return traceback.format_exc(), None


def reload_strategy() -> None:
    """重载策略"""
    ignore_modules = [
        "pythongo.ui",
        "pythongo.ui.crosshair",
        "pythongo.ui.drawer",
        "pythongo.ui.widget"
    ]

    for name, module in sys.modules.items():
        if name.startswith("pythongo") and name not in ignore_modules:
            reload(module)


def safe_datetime(time_str: str) -> datetime:
    """无限易使用此函数将时间字符串转为 datetime 对象"""
    __format = "%Y%m%d %H:%M:%S.%f"

    if all(time_str.split(" ")):
        return datetime.strptime(time_str, __format)

    _today = date.today().strftime("%Y%m%d")

    return datetime.strptime(f"{_today}{time_str}", __format)


def safe_call(py_func, py_args=()) -> Any | None:
    """
    无限易安全调用函数

    Args:
        py_func: `on_stop()`, `on_init()` 等各种方法的对象
        py_args: 参数
    """

    try:
        return py_func(*py_args)
    except:
        write_log(traceback.format_exc())
