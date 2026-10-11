# -*- coding: utf-8 -*-
"""让子进程不再弹出黑色控制台窗口。

问题现象
--------
程序打包成「无控制台」的 windowed exe（源码态用 pythonw.exe 跑也一样）时，
它自己去拉 powershell.exe / tasklist.exe / taskkill.exe 这类**控制台程序**，
Windows 找不到可以继承的控制台，就会**给子进程新建一个控制台窗口**——
用户看到的就是"点一下按钮，冒出一个黑框闪一下"。

任务本身执行是正常的，纯粹是窗口不该出现。

解决办法
--------
给 subprocess 加两个参数：
  * creationflags = CREATE_NO_WINDOW (0x08000000)
      —— 从根上不分配控制台，管道读写一切照旧
  * startupinfo.dwFlags |= STARTF_USESHOWWINDOW; wShowWindow = SW_HIDE
      —— 双保险，万一某些系统仍创建了窗口也保持隐藏

注意：CREATE_NO_WINDOW 与 DETACHED_PROCESS 互斥，不能同时用。
本模块只解决「父进程无控制台」时的弹窗；子进程的输出仍然走管道，
不受影响（capture_output / stdout=PIPE 均正常）。
"""
from __future__ import annotations

import os
import subprocess

# 0x08000000：为子进程创建时不分配控制台（Python 3.7+ 自带该常量）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_IS_WINDOWS = os.name == "nt"


def quiet_kwargs() -> dict:
    """返回可直接展开进 subprocess.run/Popen 的「不弹窗」参数。

    用法：
        subprocess.run(cmd, capture_output=True, **quiet_kwargs())
    非 Windows 平台返回空字典，调用处无需分支判断。
    """
    if not _IS_WINDOWS:
        return {}
    kwargs = {"creationflags": CREATE_NO_WINDOW}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:            # pragma: no cover - 极端环境下退化为只加 flag
        pass
    return kwargs
