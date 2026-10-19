#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地运行入口（源码态）。

这个文件只做两件事：把 `src/` 挂进模块搜索路径、调用真正的入口。
不掺任何业务逻辑，也不参与打包 —— 打包走 `build/entry.py`。

为什么要有它
------------
源码态和独立态的运行方式完全不同，混在一个入口里最容易出问题：

    本地运行（本文件）          独立运行（build/entry.py → exe）
    ----------------------      --------------------------------
    需要 Python + tkinter       目标机器什么都不用装
    配置写在仓库根目录          配置写在 exe 旁边
    有控制台，print 直接可见    窗口程序，没有控制台
    改一行代码立刻生效          每次都要重新打包

所以两个入口分开：`run.py` 永远从 `src/` 加载代码，`build/entry.py`
只做冻结态的启动。同一份逻辑，两种运行方式，互不干扰。

用法
----
    python run.py                  # 图形界面（首次运行会在根目录生成 config.json）
    python run.py --run --json     # 无界面签到一次（调试计划任务用）
    python run.py --selftest       # 环境自检，结果写到根目录 selftest.json
    python run.py --sched-status   # 查看计划任务状态
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

try:
    import tkinter  # noqa: F401
except ImportError:
    # 这是「本地运行」唯一的硬性依赖；不想装 Python 就用打包好的 exe
    sys.exit(
        "当前 Python 没有 tkinter，无法显示图形界面。\n"
        "  * 装官方 Python 时勾选 tcl/tk，或\n"
        "  * 改用独立运行版本：dist/WorkBuddy签到.exe\n"
        "只跑签到（--run）不需要界面，但本入口仍然要求完整 Python 环境。")

import checkin_gui  # noqa: E402  （必须在 sys.path 之后导入）


def main() -> int:
    return checkin_gui.main()


if __name__ == "__main__":
    sys.exit(main())
