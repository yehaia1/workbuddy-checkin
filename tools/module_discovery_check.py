# -*- coding: utf-8 -*-
"""模块发现回归检查：证明「非标准安装布局」也能找到 workbuddy_ipc。

背景
----
有用户反馈：本机跑得好好的，换一台**已安装并登录客户端**的电脑就报

    {"result": "IPC_MODULE_NOT_FOUND", ...}

原因是 workbuddy_ipc 的父目录在不同安装方式/版本下布局并不一致，早期版本
只探测几条写死的相对路径，一旦对不上就整体失效。

本工具在临时目录里**合成几种真实存在的布局**，逐个验证发现逻辑能否找到，
并在"全都找不到"时打印升级后的报错信息（应当带上候选目录数量与安装目录，
便于远程定位问题）。

用法
----
    python tools/module_discovery_check.py

结果写入当前目录的 module_discovery_check.json，并以退出码反映是否全部通过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import workbuddy_checkin as core  # noqa: E402


PKG = core.IPC_PKG

# 合成的布局：名字 -> workbuddy_ipc 包所在目录（相对安装根）
LAYOUTS = {
    "标准安装（skills/library）":
        "resources/app.asar.unpacked/resources/plugins/workbuddy-builtin"
        "/skills/library",
    "变体安装（skills 直挂）":
        "resources/app.asar.unpacked/resources/plugins/workbuddy-builtin"
        "/skills",
    "未知布局（深层嵌套，旧版必失手）":
        "resources/app.asar.unpacked/out/builtin/skills/library",
    "插件缓存式（厂商/插件/版本/…）":
        ".workbuddy/plugins/cache/acme/some-plugin/1.2.3/skills/thing",
}


def make_pkg(parent: Path, with_init: bool = True) -> Path:
    d = parent / PKG
    d.mkdir(parents=True, exist_ok=True)
    if with_init:
        (d / "__init__.py").write_text("# fake package\n", encoding="utf-8")
    return d


def build_tree(base: Path) -> dict:
    """按 LAYOUTS 造出目录树，返回 {布局名: 包父目录}。"""
    made = {}
    for name, rel in LAYOUTS.items():
        parent = base / Path(rel)
        make_pkg(parent)
        made[name] = str(parent)
    # 干扰项：同名目录但没有 __init__.py，不应被当成模块
    decoy = base / "resources/app.asar.unpacked/resources/plugins/decoy"
    make_pkg(decoy, with_init=False)
    made["[干扰项] 无 __init__.py"] = str(decoy)
    return made


def main() -> int:
    report = {"layouts": [], "not_found_message": None, "passed": True}
    tmp = Path(tempfile.mkdtemp(prefix="wb_modcheck_"))
    try:
        made = build_tree(tmp)
        found = set(core._scan_ipc_parents(str(tmp)))

        for name, parent in made.items():
            expect = not name.startswith("[干扰项]")
            ok = (parent in found) == expect
            report["layouts"].append({
                "layout": name,
                "parent": parent,
                "expect_found": expect,
                "actually_found": parent in found,
                "ok": ok,
            })
            if not ok:
                report["passed"] = False
            mark = "PASS" if ok else "FAIL"
            print("[{}] {:<32} {}".format(mark, name,
                                          "找到" if parent in found else "未找到"))

        # 强制「全部落空」，检查报错信息是否可定位。
        # 三个来源都要掐掉：目录猜测、exe 探测、以及真实机器上的缓存 glob。
        class _NoGlob:
            @staticmethod
            def glob(*_a, **_k):
                return []

        real_guesses = core._install_dir_guesses
        real_install = core.install_root
        real_glob = core.glob
        real_scan = core._scan_ipc_parents
        core._install_dir_guesses = lambda: []
        core.install_root = lambda cfg=None: ""
        core.glob = _NoGlob
        core._scan_ipc_parents = lambda *a, **k: []
        core.os.environ.pop("CODEBUDDY_BUILTIN_SKILLS_DIR", None)
        try:
            core.load_ipc({})
        except core.CheckinError as e:
            report["not_found_message"] = str(e)
            print("\n模拟「全找不到」时的报错：\n  " + str(e))
            if not ("候选目录" in str(e) and "客户端安装目录" in str(e)):
                report["passed"] = False
                print("  ^^ 报错信息缺少定位信息，FAIL")
        finally:
            core._install_dir_guesses = real_guesses
            core.install_root = real_install
            core.glob = real_glob
            core._scan_ipc_parents = real_scan

        out = Path.cwd() / "module_discovery_check.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("\n结果 -> {}".format(out))
        print("总体：" + ("全部通过" if report["passed"] else "存在失败项"))
        return 0 if report["passed"] else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
