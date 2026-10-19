#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把服务端版打成一个**单文件脚本**：`dist/server/wb_server.pyz`。

用法
----
    python3 build/make_server.py                  # 产出 dist/server/wb_server.pyz
    python3 build/make_server.py --out D:/tmp/wb_server.pyz

为什么是单文件
--------------
服务端部署最怕"这个文件忘了拷"。`src/` 是分层的（exe 版有自己的界面和计划任务
代码，服务端不该带），所以这里把服务端真正用到的模块收进一个 zipapp：

    python3 wb_server.pyz --init-config
    python3 wb_server.pyz --check
    python3 wb_server.pyz                 # 授权（必要时）+ 签到 + 推送

* 模块在 zip 内部，`import` 照常工作，天然隔离（不会和 `load` / `main`
  这类的同名函数打架）。
* 靠 `import` 闭包自动收集（AST 只看模块级导入），不会漏文件。
* 闭包里出现 `checkin_gui` / `wb_task` / `win_quiet`（tkinter、计划任务、
  弹窗抑制）直接报错——那是 exe 版的东西。
* 压缩存放，产物约 60 KB；只依赖 Python 3.8+ 标准库。
* 配置、日志、令牌仍然放**脚本所在目录**（`wb_config` 认得出自己在 zip 里，
  会退到 `sys.argv[0]` 的目录），`--init-config` 生成 `config.json`。
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path
from typing import List, Set

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
DEFAULT_OUT = ROOT / "dist" / "server" / "wb_server.pyz"

ENTRY = "wb_server"
# 服务端可能用到的源码目录（按层放）
SRC_DIRS = ("common", "server")

# exe 版专用，出现在闭包里说明依赖写错了
FORBIDDEN = ("checkin_gui", "wb_task", "win_quiet")

MAIN_PY = """\
# -*- coding: utf-8 -*-
# 由 build/make_server.py 生成，不要手改。
import sys

import wb_server

if __name__ == "__main__":
    sys.exit(wb_server.main())
"""


def module_files() -> dict:
    """模块名 -> 文件路径（common/ 与 server/ 里所有 .py）。"""
    out = {}
    for sub in SRC_DIRS:
        for p in sorted((SRC / sub).glob("*.py")):
            out[p.stem] = p
    return out


def local_imports(path: Path) -> Set[str]:
    """静态解析一个文件里 import 了哪些本项目模块。

    只看**模块级**导入（含顶层 if/try）：函数体内的 import 是惰性的，
    单文件包里不一定执行到，不该算成必需依赖。
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

    names: Set[str] = set()
    for node in walk(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # 相对导入在单文件里没有意义
                names.add(node.module.split(".")[0])
    return names


def collect(entry: str = ENTRY) -> List[str]:
    """从入口开始算 import 闭包，入口排最前。"""
    files = module_files()
    seen: Set[str] = set()
    queue = [entry]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = files.get(mod)
        if f is None:
            raise SystemExit("✗ 找不到模块：{}（在 {} 里）".format(
                mod, " / ".join(SRC_DIRS)))
        for dep in local_imports(f):
            if dep in files and dep not in seen:
                queue.append(dep)

    bad = sorted(seen & set(FORBIDDEN))
    if bad:
        raise SystemExit(
            "✗ 服务端入口依赖了 exe 版专用模块：{}\n"
            "  这几个模块用了 tkinter / Windows 计划任务，服务器上跑不了。\n"
            "  请把那处导入改成函数内惰性导入（并加 os.name == \"nt\" 或 try 兜底）。"
            .format("、".join(bad))
        )
    rest = sorted(seen - {entry})
    return [entry] + rest


def build(out: Path) -> List[str]:
    files = module_files()
    mods = collect()
    stage = ROOT / ".build" / "serverpkg"

    # 1) 准备一个干净的暂存目录（zip 内部是平铺的）
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for mod in mods:
        dst = stage / (mod + ".py")
        # 统一 LF，免得体积/校验和随平台变化
        dst.write_text(files[mod].read_text(encoding="utf-8"),
                       encoding="utf-8", newline="\n")
    (stage / "__main__.py").write_text(MAIN_PY, encoding="utf-8", newline="\n")

    # 2) 打成一个可执行 zip
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    zipapp.create_archive(str(stage), target=str(out),
                          interpreter="/usr/bin/env python3", compressed=True)
    return mods


def verify(out: Path) -> str:
    """在产物所在目录跑一次 --version：能跑通才说明闭包完整。"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", str(out), "--version"],
        cwd=str(out.parent), capture_output=True, text=True, timeout=60, env=env,
    )
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "WorkBuddyCheckin" not in text:
        raise SystemExit("✗ 产物跑不起来（退出码 {}）：\n{}".format(
            proc.returncode, text))
    return text.splitlines()[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把服务端版打成单文件 wb_server.pyz（不含 GUI / Windows 模块）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="产物路径（默认 dist/server/wb_server.pyz）")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    default=True, help="跳过打完后的冒烟")
    args = ap.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    mods = build(out)
    print("入口：{}".format(mods[0]))
    print("打进包里的模块（{} 个）：{}".format(len(mods), "、".join(mods)))
    if args.verify:
        print("  ✓ 冒烟通过：{}".format(verify(out)))

    size = out.stat().st_size / 1024
    print("完成：{}（{:.1f} KB，单个文件）".format(out, size))
    print("服务器上只要这一个文件；配置由 {} --init-config 生成。".format(
        out.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
