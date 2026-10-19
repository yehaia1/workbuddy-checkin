# -*- coding: utf-8 -*-
"""独立运行入口 —— PyInstaller 只认这个文件，不要直接双击它。

它和根目录的 `run.py` 是**两种运行方式**，刻意分开：

    run.py            本地源码运行（要 Python、有控制台、随时改代码）
    build/entry.py    冻结运行（打成 exe，目标机器不需要 Python）

冻结之后 `sys.frozen` 为真，`src/` 里的模块已经被 PyInstaller 收进
`_MEIPASS`，此时再往 sys.path 塞源码目录反而会把包内容搞乱，所以只在
源码态做路径引导。
"""
from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    # 只有开发者直接 `python build/entry.py` 才会走到这里
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SRC = os.path.join(ROOT, "src")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)

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
