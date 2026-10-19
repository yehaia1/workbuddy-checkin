# -*- coding: utf-8 -*-
"""一键把 checkin_gui.py 打包成单文件 exe（开发/发布用）。

为什么要包一层脚本：**打包用的解释器必须带 tkinter**。
用不带 tkinter 的解释器打包，PyInstaller 不报任何错，只是悄悄少收集
`_tkinter.pyd` 和 tcl/tk 运行时，打出来的 exe 一启动就
    ModuleNotFoundError: No module named 'tkinter'
直接崩。唯一线索是体积小掉约 3 MB（正常约 11 MB，坏版约 8 MB），
极易被忽略。所以这里先挑一个能 `import tkinter` 的解释器，打完再自检。

用法：
    python build_exe.py                   # 产出 dist/WorkBuddyCheckin.exe
    python build_exe.py --python <解释器>  # 指定解释器
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 运行时才用到的标准库（PyInstaller 静态分析有时漏掉，显式声明更稳）
HIDDEN_IMPORTS = [
    "base64", "binascii", "json", "re", "secrets", "socket", "ssl",
    "threading", "urllib.parse", "urllib.request", "urllib.error",
    "http.server", "http.client", "email", "email.utils",
    "_socket", "_ssl",
]


def _has_tkinter(exe: str) -> bool:
    try:
        r = subprocess.run(
            [exe, "-c", "import tkinter"],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def pick_interpreter() -> str:
    """挑一个带 tkinter 的解释器来打包。"""
    candidates = [sys.executable]
    here = Path(sys.executable).parent
    for name in ("python3.exe", "python.exe"):
        candidates.append(str(here / name))
    for pat in (
        r"C:\Users\*\AppData\Local\Programs\Python\Python3*\python3.exe",
        r"C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe",
        r"C:\Python3*\python.exe",
    ):
        candidates.extend(sorted(glob.glob(pat)))
    seen = set()
    for cand in candidates:
        if cand in seen or not os.path.isfile(cand):
            continue
        seen.add(cand)
        if _has_tkinter(cand):
            return cand
    raise SystemExit(
        "找不到带 tkinter 的 Python 解释器，无法打包。\n"
        "当前解释器：{}\n"
        "请安装官方 Python（勾选 tcl/tk）后重试，或用参数指定：\n"
        "    build_exe.py --python C:\\path\\to\\python.exe".format(sys.executable))


def build_env() -> dict:
    """打包环境（必要时带上本地隔离的 PyInstaller）。"""
    env = os.environ.copy()
    libs = ROOT / ".build" / "pylibs"
    if libs.is_dir():
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(libs) + (os.pathsep + cur if cur else "")
    return env


def verify_bundle(exe: Path) -> bool:
    """打包后校验：跑一次 --selftest，tkinter 缺失会立刻崩在这里。

    先删掉旧的自检产物再跑：文件存在不代表这次跑过，上一次留下的
    selftest.json 会让校验无条件通过（踩过）。
    """
    if not exe.is_file():
        return False
    stale = exe.parent / "selftest.json"
    try:
        stale.unlink()
    except OSError:
        pass
    try:
        r = subprocess.run([str(exe), "--selftest"], cwd=str(exe.parent),
                           capture_output=True, timeout=300,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print("自检执行失败：{}".format(e))
        return False
    if r.returncode == 0 and stale.is_file():
        return True
    print("自检输出：{}".format(
        (r.stderr or r.stdout or b"").decode("utf-8", "replace")[:500]))
    return False


def main(argv: "list[str]") -> int:
    if "--python" in argv:
        interpreter = argv[argv.index("--python") + 1]
        if not _has_tkinter(interpreter):
            print("指定的解释器没有 tkinter，无法打包：{}".format(interpreter))
            return 2
    else:
        interpreter = pick_interpreter()

    if interpreter != sys.executable:
        print("当前解释器不带 tkinter，自动改用：{}".format(interpreter))
    print("打包解释器：{}".format(interpreter))

    args = [
        interpreter, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", "WorkBuddyCheckin",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / ".build" / "pyi" / "work"),
        "--specpath", str(ROOT / ".build" / "pyi"),
    ]
    for m in HIDDEN_IMPORTS:
        args += ["--hidden-import", m]
    args.append(str(ROOT / "checkin_gui.py"))

    print("PyInstaller:", " ".join(args[:4]), "...")
    try:
        subprocess.check_call(args, cwd=str(ROOT), env=build_env())
    except subprocess.CalledProcessError as e:
        print("\n打包失败，退出码 {}".format(e.returncode))
        print("若提示没有 pyinstaller：")
        print("  <python> -m pip install --target .build/pylibs pyinstaller")
        return e.returncode

    exe = ROOT / "dist" / "WorkBuddyCheckin.exe"
    print("\n完成：{}".format(exe))
    if not verify_bundle(exe):
        print("！！校验未通过：exe 里可能没有 tkinter，别发布这一版")
        return 3
    print("校验通过：tkinter 已打包")
    print("提示：改中文名直接复制改名即可，例如 dist/WorkBuddy签到.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
