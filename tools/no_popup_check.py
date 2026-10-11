# -*- coding: utf-8 -*-
"""回归检查：运行目标程序时，是否会弹出控制台黑框窗口。

背景
----
窗口程序（打包成 --windowed 的 exe，或源码态用 pythonw.exe）自身没有控制台。
它去拉 powershell.exe / tasklist.exe / taskkill.exe 这类**控制台程序**时，
Windows 找不到可继承的控制台，就会给子进程**新建一个控制台窗口** —— 就是那块黑框。

判定方式
--------
不用"肉眼看"，而是用 EnumWindows 枚举全系统可见顶层窗口，按类名挑出控制台窗口：

    ConsoleWindowClass            传统 conhost 控制台
    CASCADIA_HOSTING_WINDOW_CLASS Win11 交给 Windows Terminal 托管（很容易漏！）
    PseudoConsoleWindow           OpenConsole

⚠️ 两个必须注意的点
------------------
1. **本脚本自己必须用 pythonw.exe 运行**。用 python.exe 的话它自带控制台，
   子进程会继承控制台，**根本复现不出弹窗**。脚本会自检并警告。
2. **不要只匹配 ConsoleWindowClass**。Win11 上控制台归 Windows Terminal 托管，
   类名是 CASCADIA_HOSTING_WINDOW_CLASS，只匹配老类名会得出错误结论。

用法
----
    # 检查打包后的 exe（推荐）
    pythonw tools/no_popup_check.py --exe "dist\\WorkBuddy签到.exe" -- --sched-status

    # 不指定 --exe 时，自动在 dist/ 下找
    pythonw tools/no_popup_check.py -- --selftest

    # 结果同时写入 JSON
    pythonw tools/no_popup_check.py --exe ... --out result.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
                   "PseudoConsoleWindow")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetConsoleWindow.restype = wintypes.HWND


def snapshot() -> dict:
    """当前所有可见顶层窗口：hwnd -> {class, pid, title}。"""
    found = {}

    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        found[int(hwnd)] = {"class": cls.value, "pid": pid.value,
                            "title": title.value}
        return True

    user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


def find_exe(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_absolute() else (ROOT / p)
    for name in ("WorkBuddy签到.exe", "WorkBuddyCheckin.exe"):
        p = ROOT / "dist" / name
        if p.is_file():
            return p
    return None


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(
        description="检查运行目标程序时是否弹出控制台窗口")
    ap.add_argument("--exe", help="要检查的可执行文件（默认在 dist/ 下自动找）")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="等待目标程序结束的最长秒数（默认 180）")
    ap.add_argument("--out", default="no_popup_check.json",
                    help="结果 JSON 写入路径（默认 no_popup_check.json）")
    ap.add_argument("target_args", nargs="*",
                    help="传给目标程序的参数（建议放在 -- 之后）")
    args = ap.parse_args(argv)

    parent_console = int(kernel32.GetConsoleWindow() or 0)
    if parent_console:
        print("警告：本脚本当前有控制台，子进程会继承它，"
              "因此**复现不出弹窗**，结论不可信。")
        print("      请改用 pythonw.exe 运行本脚本。\n")

    exe = find_exe(args.exe)
    if not exe or not exe.is_file():
        print("找不到要检查的可执行文件，请用 --exe 指定。")
        return 2

    cmd = [str(exe)] + list(args.target_args)
    print("目标：{}".format(" ".join(cmd)))
    print("父进程有控制台：{}（0 表示无，符合真实场景）\n".format(parent_console))

    base = snapshot()
    hits: list = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            for h, v in snapshot().items():
                if h not in base and v["class"] in CONSOLE_CLASSES:
                    hits.append(v)
            time.sleep(0.03)

    watcher = threading.Thread(target=sampler, daemon=True)
    watcher.start()

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(exe.parent),
                              capture_output=True, timeout=args.timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc, code = None, None
    elapsed = time.time() - t0
    time.sleep(0.6)
    stop.set()
    watcher.join(timeout=2)

    result = {
        "target": cmd,
        "parent_has_console": parent_console,
        "parent_console_warning": bool(parent_console),
        "exit_code": code,
        "elapsed_sec": round(elapsed, 2),
        "console_window_count": len(hits),
        "console_windows": hits,
        "verdict": "PASS（未弹出控制台窗口）" if not hits
                   else "FAIL（出现了 {} 个控制台窗口）".format(len(hits)),
    }

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("耗时：{:.2f} 秒，退出码：{}".format(elapsed, code))
    print("期间新出现的控制台窗口：{} 个".format(len(hits)))
    for h in hits:
        print("  - class={class} pid={pid} title={title}".format(**h))
    print("\n结论：{}".format(result["verdict"]))
    print("结果已写入：{}".format(out))
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
