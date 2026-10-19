# -*- coding: utf-8 -*-
"""独立运行入口 —— PyInstaller 只认这个文件，不要直接双击它。

源码运行不经过这里：界面是 `python src/desktop/checkin_gui.py`，
命令行是 `python src/common/workbuddy_checkin.py`。

冻结之后 `sys.frozen` 为真，`src/` 里的模块已经被 PyInstaller 收进
`_MEIPASS`，此时再往 sys.path 塞源码目录反而会把包内容搞乱，所以只在
源码态做路径引导（开发者直接 `python build/entry.py` 调试时才用得上）。
"""
from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("common", "desktop"):
        p = os.path.join(ROOT, "src", sub)
        if p not in sys.path:
            sys.path.insert(0, p)

import wb_config  # noqa: E402

# 冻结态的数据目录 = 当前用户的缓存目录（%LOCALAPPDATA%\WorkBuddyCheckin），
# 不写 exe 旁边 —— exe 放桌面、U 盘、只读目录都能正常跑。
# 顺便把旧版留在 exe 旁边的 config.json / auth_oauth.json 搬过来。
wb_config.prepare()

import checkin_gui  # noqa: E402


def main() -> int:
    return checkin_gui.main()


if __name__ == "__main__":
    sys.exit(main())
