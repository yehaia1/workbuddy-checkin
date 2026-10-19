#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把服务端版需要的文件收进一个目录，别把 GUI 和 Windows 模块带去服务器。

用法
----
    python3 build/make_server_bundle.py            # 输出 dist/server/
    python3 build/make_server_bundle.py --out D:/tmp/wb   # 换输出目录

为什么要这个脚本
----------------
`src/` 里混着三类东西：

    wb_server.py                    服务端入口
    wb_config / wb_oauth / wb_notify / workbuddy_checkin
                                    两边共用（禁止 import Windows 专有模块）
    checkin_gui / wb_task / win_quiet
                                    只有 exe 版才用（tkinter、计划任务、
                                    CREATE_NO_WINDOW）

服务端部署时如果整个 `src/` 拷过去，会把 tkinter 界面、Windows 计划任务代码
一起带上服务器。虽然不会被执行，但看的人会以为要在服务器上开界面。
所以这里按 import 闭包**自动**收集，既不漏文件，也不多带。

产物
----
    dist/server/
        <闭包里的 .py>             通常 5 个
        config.example.json        配置模板（不含任何 key）
        README.txt                 部署步骤（三行）
        _bundle.json               本脚本生成的清单，下次打包据此清理

只清理「上一次由本脚本放进来的 .py」，**不动** `config.json` /
`auth_oauth.json` / `checkin.log` —— 那可能是用户已经授权过的线上数据。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
DEFAULT_OUT = ROOT / "dist" / "server"

ENTRY = "wb_server.py"

# 服务端绝对不该出现的模块（真的出现了说明依赖写错了，直接失败）
FORBIDDEN = ("checkin_gui", "wb_task", "win_quiet")

MANIFEST = "_bundle.json"

README_TEXT = """WorkBuddy 每日签到 —— 服务端版
==============================

本目录就是服务端的全部文件，不需要安装 Python 包以外的任何依赖。

1) 生成配置
   python3 wb_server.py --init-config
   # 编辑 config.json：填 wecom_key（企业微信机器人 key）
   #                 或 dingtalk_key，并把 push_channels 勾上

2) 体检（不联网、不签到，只检查配置与令牌）
   python3 wb_server.py --check

3) 第一次运行（没有授权会自动推链接，等 3 分钟，期间可以在微信里点开链接扫码，
   拿到信息后回来改 config.json，脚本下一轮会重读配置）
   python3 wb_server.py

4) 挂定时任务（每天 09:30）
   30 9 * * * cd {dir} && python3 wb_server.py >> checkin.log 2>&1

配置（config.json）、日志（checkin.log）、令牌（auth_oauth.json）都写在本目录，
整个目录拷走即可换机器。**不要在两台机器上同时跑签到**：refreshToken 每次刷新
都会轮换，两边各持一份会互相踢掉。
"""


def local_imports(path: Path, toplevel_only: bool = True) -> Set[str]:
    """静态解析一个文件里 import 了哪些**本项目**的模块名。

    默认只看**模块级**导入（含顶层 if/try 里的）：函数体内的 import 是惰性的，
    服务端不一定会执行到——比如 `workbuddy_checkin.diagnose()` 里查计划任务
    那段 `import wb_task`，服务器上永远走不到，不该把 wb_task 算成依赖。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        raise SystemExit("✗ 解析失败 {}：{}".format(path.name, e))

    def walk(nodes):
        """遍历顶层语句，遇到函数/类就停（不进函数体）。"""
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                yield node
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                continue
            elif isinstance(node, (ast.If, ast.Try, ast.With,
                                   ast.For, ast.While)):
                for child in walk(node.body):
                    yield child
                for child in walk(getattr(node, "orelse", []) or []):
                    yield child
                for h in getattr(node, "handlers", []) or []:
                    for child in walk(h.body):
                        yield child
                for child in walk(getattr(node, "finalbody", []) or []):
                    yield child

    roots = walk(tree.body) if toplevel_only else ast.walk(tree)
    names: Set[str] = set()
    for node in roots:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level>0）在服务端这种平铺目录里用不到，忽略
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def collect(entry: str = ENTRY) -> List[str]:
    """从入口开始算 import 闭包，返回要复制的文件名（排序后）。"""
    known = {p.stem for p in SRC.glob("*.py")}
    seen: Set[str] = set()
    queue = [Path(entry).stem]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = SRC / (mod + ".py")
        if not f.exists():
            raise SystemExit("✗ 找不到模块文件：{}".format(f))
        for dep in local_imports(f):
            if dep in known and dep not in seen:
                queue.append(dep)

    bad = sorted(seen & set(FORBIDDEN))
    if bad:
        raise SystemExit(
            "✗ 服务端入口依赖了 Windows 专有模块：{}\n"
            "  这几个模块只能在 exe 版用，请把它们从 wb_server / "
            "workbuddy_checkin 的导入里摘掉（或改成函数内惰性导入 + try）。"
            .format("、".join(bad))
        )
    # 入口排最前，其余按名字排，方便看
    rest = sorted(seen - {Path(entry).stem})
    return [Path(entry).stem] + rest if Path(entry).stem in seen else rest


def previous_files(out: Path) -> List[str]:
    """上次打包放进来的文件（只删这些，免得误删用户数据）。"""
    mf = out / MANIFEST
    if not mf.exists():
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    files = data.get("files") or []
    return [str(f) for f in files if str(f).endswith(".py")]


def write_example(out: Path) -> Path:
    """配置模板：直接复用 wb_config.DEFAULT_CONFIG，保证键名不会写歪。"""
    sys.path.insert(0, str(SRC))
    import wb_config  # noqa: E402

    p = out / "config.example.json"
    p.write_text(json.dumps(wb_config.DEFAULT_CONFIG, ensure_ascii=False,
                            indent=2) + "\n", encoding="utf-8")
    return p


def verify(out: Path) -> None:
    """在产物目录里跑 --version：能跑通才说明闭包是完整的。"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 别在交付目录里留 __pycache__
    proc = subprocess.run(
        [sys.executable, "-B", str(out / ENTRY), "--version"],
        cwd=str(out), capture_output=True, text=True, timeout=60, env=env,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or "WorkBuddyCheckin" not in text:
        raise SystemExit(
            "✗ 产物跑不起来（退出码 {}）：\n{}".format(proc.returncode, text)
        )
    print("  ✓ 冒烟通过：{}".format(text.strip().splitlines()[-1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="收集服务端版需要的文件（不含 GUI 与 Windows 模块）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="输出目录（默认 dist/server）")
    ap.add_argument("--verify", dest="verify", action="store_true",
                    default=True, help="收完之后跑一次 --version（默认开）")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="跳过冒烟")
    args = ap.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    mods = collect()
    print("服务端依赖闭包（{} 个）：{}".format(len(mods), "、".join(mods)))

    # 1) 清理上一次的产物（只删 .py，不碰配置/令牌/日志）
    removed = []
    for name in previous_files(out):
        f = out / name
        if f.exists():
            f.unlink()
            removed.append(name)
    if removed:
        print("  清掉上次的：{}".format("、".join(removed)))

    # 2) 复制
    copied = []
    for mod in mods:
        src = SRC / (mod + ".py")
        dst = out / (mod + ".py")
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8",
                       newline="\n")  # 统一 LF
        copied.append(dst.name)

    # 3) 附带件
    example = write_example(out)
    (out / "README.txt").write_text(
        README_TEXT.format(dir=str(out).replace("\\", "/")),
        encoding="utf-8", newline="\n")

    (out / MANIFEST).write_text(json.dumps({
        "entry": ENTRY,
        "files": copied,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print("  写出：{}".format("、".join(copied)))
    print("  附带：config.example.json、README.txt")

    # 4) 冒烟
    if args.verify:
        verify(out)

    total = sum((out / n).stat().st_size for n in copied)
    print("完成：{}（{} 个 .py，{:.1f} KB）".format(out, len(copied), total / 1024))
    print("别把整个 src/ 拷到服务器，这个目录就够了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
