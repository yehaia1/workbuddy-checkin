# -*- coding: utf-8 -*-
"""把「独立运行」版本打包成单文件 exe（发布用，与逻辑源码分开）。

本脚本只负责构建，不掺业务逻辑：
    ROOT/src/common/   两边共用（配置、授权、推送、签到核心）
    ROOT/src/desktop/  exe 版专用（界面、计划任务、弹窗抑制）
    ROOT/build/entry.py 冻结态入口（PyInstaller 从这里进）
    ROOT/dist/          产物

为什么要包一层脚本：**打包用的解释器必须带 tkinter**。
用不带 tkinter 的解释器打包，PyInstaller 不报任何错，只是悄悄少收集
`_tkinter.pyd` 和 tcl/tk 运行时，打出来的 exe 一启动就
    ModuleNotFoundError: No module named 'tkinter'
直接崩。唯一线索是体积小掉约 3 MB（正常约 11 MB，坏版约 8 MB），
极易被忽略。所以这里先挑一个能 `import tkinter` 的解释器，打完再自检。

用法：
    python build/build_exe.py                    # 产出 dist/WorkBuddyCheckin.exe
    python build/build_exe.py --python <解释器>   # 指定解释器

产物只有一个英文名：`dist/WorkBuddyCheckin.exe`（版本信息写进 exe 属性）。
不再生成中文名副本 —— 中文名在命令行、计划任务、日志里都会被编码折腾。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SRC_COMMON = SRC / "common"
SRC_DESKTOP = SRC / "desktop"
ENTRY = ROOT / "build" / "entry.py"
EXE_NAME = "WorkBuddyCheckin"

# 旧版本曾额外产出中文名 exe，升级后要清掉，免得两版混在一起
LEGACY_NAMES = ("WorkBuddy签到.exe",)

# 运行时才用到的标准库（PyInstaller 静态分析有时漏掉，显式声明更稳）
HIDDEN_IMPORTS = [
    "base64", "binascii", "json", "re", "secrets", "socket", "ssl",
    "threading", "urllib.parse", "urllib.request", "urllib.error",
    "http.server", "http.client", "email", "email.utils",
    "_socket", "_ssl", "tkinter", "tkinter.ttk",
    "wb_config", "wb_notify", "wb_oauth", "wb_task", "win_quiet",
    "workbuddy_checkin", "checkin_gui",
]


def app_version() -> str:
    """版本号唯一来源：src/common/wb_config.py 的 VERSION。"""
    if str(SRC_COMMON) not in sys.path:
        sys.path.insert(0, str(SRC_COMMON))
    try:
        import wb_config  # noqa: WPS433（构建期临时导入，只为读版本）
        return str(wb_config.VERSION)
    except Exception as e:
        raise SystemExit("读不到版本号（src/common/wb_config.py）：{}".format(e))


def write_version_file(version: str) -> Path:
    """生成 PyInstaller 的版本资源，右键属性里能看到版本、产品名。"""
    nums = [int(n) for n in re.findall(r"\d+", version)][:4]
    while len(nums) < 4:
        nums.append(0)
    nums = nums[:4]
    path = ROOT / ".build" / "pyi" / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# UTF-8\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        "    filevers=({v}), prodvers=({v}),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('040904B0', [\n"
        "        StringStruct('CompanyName', 'WorkBuddyCheckin'),\n"
        "        StringStruct('FileDescription', 'WorkBuddy daily check-in'),\n"
        "        StringStruct('FileVersion', '{version}'),\n"
        "        StringStruct('InternalName', '{name}'),\n"
        "        StringStruct('OriginalFilename', '{name}.exe'),\n"
        "        StringStruct('ProductName', '{name}'),\n"
        "        StringStruct('ProductVersion', '{version}')])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n".format(v=", ".join(str(n) for n in nums), version=version,
                     name=EXE_NAME),
        encoding="utf-8")
    return path


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
        "    python build/build_exe.py --python C:\\path\\to\\python.exe".format(
            sys.executable))


def build_env() -> dict:
    """打包环境（必要时带上本地隔离的 PyInstaller）。"""
    env = os.environ.copy()
    libs = ROOT / ".build" / "pylibs"
    if libs.is_dir():
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(libs) + (os.pathsep + cur if cur else "")
    return env


def selftest_file() -> Path:
    """exe 的自检结果写在哪：数据目录（用户缓存），不是 exe 旁边。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else (Path.home() / ".cache")
    return root / EXE_NAME / "selftest.json"


def verify_bundle(exe: Path) -> bool:
    """打包后校验：跑一次 --selftest，确认是冻结态且 tkinter 没缺。

    先删掉旧的自检产物再跑：文件存在不代表这次跑过，上一次留下的
    selftest.json 会让校验无条件通过（踩过）。所以除了退出码，还要看
    `frozen` 字段——源码态自检也会生成同样的文件。

    v3.0 起数据目录是用户缓存，selftest.json 不再落在 exe 旁边。
    """
    if not exe.is_file():
        return False
    stale = selftest_file()
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
    if r.returncode != 0 or not stale.is_file():
        print("自检输出：{}".format(
            (r.stderr or r.stdout or b"").decode("utf-8", "replace")[:500]))
        print("期望的自检结果：{}（存在={}）".format(stale, stale.is_file()))
        return False
    try:
        data = json.loads(stale.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("自检结果读不出来：{}".format(e))
        return False
    if data.get("frozen") is not True:
        print("自检结果不是冻结态，产物可能并没有真正打包")
        return False
    tok = data.get("token") or {}
    if not tok.get("present"):
        auth = "未授权"
    elif tok.get("disabled") or tok.get("expired"):
        auth = "已失效"
    else:
        auth = "有效（剩 {} 天）".format(tok.get("days_left"))
    print("自检通过：v{}，{}，推送 {}".format(
        data.get("version"), auth,
        "、".join(data.get("push_channels") or []) or "未配置"))
    return True


def main(argv: "list[str]") -> int:
    version = app_version()
    if "--python" in argv:
        interpreter = argv[argv.index("--python") + 1]
        if not _has_tkinter(interpreter):
            print("指定的解释器没有 tkinter，无法打包：{}".format(interpreter))
            return 2
    else:
        interpreter = pick_interpreter()

    if interpreter != sys.executable:
        print("当前解释器不带 tkinter，自动改用：{}".format(interpreter))
    print("版本：v{}".format(version))
    print("打包解释器：{}".format(interpreter))
    print("源码目录：{}、{}".format(SRC_COMMON, SRC_DESKTOP))
    print("入口：{}".format(ENTRY))

    version_file = write_version_file(version)
    print("版本资源：{}".format(version_file))

    args = [
        interpreter, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", EXE_NAME,
        "--version-file", str(version_file),
        "--paths", str(SRC_COMMON),
        "--paths", str(SRC_DESKTOP),
        # 先出到 .build/out 再复制进 dist：dist 里的 exe 可能正在运行，
        # 直接往里写会 PermissionError（改时间戳/校验和那一步最容易踩）。
        "--distpath", str(ROOT / ".build" / "out"),
        "--workpath", str(ROOT / ".build" / "pyi" / "work"),
        "--specpath", str(ROOT / ".build" / "pyi"),
    ]
    for m in HIDDEN_IMPORTS:
        args += ["--hidden-import", m]
    args.append(str(ENTRY))

    print("PyInstaller:", " ".join(args[:4]), "...")
    try:
        subprocess.check_call(args, cwd=str(ROOT), env=build_env())
    except subprocess.CalledProcessError as e:
        print("\n打包失败，退出码 {}".format(e.returncode))
        print("若提示没有 pyinstaller：")
        print("  <python> -m pip install --target .build/pylibs pyinstaller")
        return e.returncode

    staged = ROOT / ".build" / "out" / (EXE_NAME + ".exe")
    if not staged.is_file():
        print("打包结束但没找到产物：{}".format(staged))
        return 3
    exe = ROOT / "dist" / (EXE_NAME + ".exe")
    exe.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(staged), str(exe))
    except OSError as e:
        print("复制到 dist 失败（exe 可能正在运行）：{}".format(e))
        return 4

    # 清掉旧版本留下的中文名 exe：两个文件并存时，用户 / 计划任务
    # 很容易打开的是那个不再更新的旧文件。
    for legacy in LEGACY_NAMES:
        old = exe.with_name(legacy)
        if old.is_file():
            try:
                old.unlink()
                print("已删除旧的中文名产物：{}".format(old))
            except OSError as e:
                print("旧产物删不掉（请手动删）：{} {}".format(old, e))

    print("\n完成：{}".format(exe))
    if not verify_bundle(exe):
        print("！！校验未通过：exe 里可能没有 tkinter，别发布这一版")
        return 5
    print("打包完成：v{}".format(version))
    return 0



if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
