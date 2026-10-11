# -*- coding: utf-8 -*-
"""后台拉起探针 —— 验证"客户端被拉起来但窗口藏起来、IPC 照常可用"是否成立。

为什么需要单独一个探针：
  当前会话就跑在 WorkBuddy 客户端里面，主程序没法自己把客户端关掉再测
  （关掉等于把自己杀了）。所以把这件有破坏性的事单独拎出来，由你在
  方便的时候手动跑。

三种模式（默认只读，绝不改东西）：

  1. 只读报告：
        python probe_hidden_launch.py
     列出当前 WorkBuddy 进程与所有顶层窗口的可见性状态。

  2. 隐藏/恢复实测（对**正在运行**的客户端操作，不杀进程）：
        python probe_hidden_launch.py --test-hide
     找到主窗口 → 隐藏 → 报告 → 恢复。用来确认 ShowWindow 这条路通不通。

  3. 完整链路实测（要求客户端**已关闭**）：
        python probe_hidden_launch.py --full
     拉起客户端 → 尽快隐藏 → 记录"窗口闪现时长" → 验证窗口已隐藏且
     WBIPC 通道仍可用（真调一次状态接口）→ 最后留在隐藏态。
     想把它调回来：python probe_hidden_launch.py --restore

用法提示：Windows 上直接 `python probe_hidden_launch.py --full` 即可。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import wb_host
import workbuddy_checkin as core


def hr(title: str) -> None:
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def report_snapshot() -> dict:
    snap = wb_host.snapshot()
    print("客户端进程：{}".format(
        "运行中 {}".format(snap["pids"]) if snap["process_running"] else "未运行"))
    ep = wb_host.endpoint_path()
    print("宿主通道文件：{}（{}）".format(
        "存在" if ep.exists() else "不存在", ep))
    acct = wb_host.account_info()
    print("账号快照：{}".format(
        "uid={}… nickname={}".format(acct["uid"][:8], acct.get("nickname"))
        if acct else "缺失"))
    print("\n顶层窗口（共 {} 个）：".format(len(snap["windows"])))
    if not snap["windows"]:
        print("  （无）")
    for w in snap["windows"]:
        print("  hwnd={:<10} visible={:<5} minimized={:<5} owner={:<5} title={}".format(
            w["hwnd"], str(w["visible"]), str(w["minimized"]),
            str(w["owner"]), w["title"][:48] or "(无标题)"))
    snap["endpoint_exists"] = ep.exists()
    snap["account"] = acct
    return snap


def test_hide_current() -> int:
    """对正在运行的客户端做一次 隐藏 → 恢复，验证 Win32 路径可用。"""
    hr("模式 2：隐藏/恢复实测（不杀进程）")
    snap = report_snapshot()
    if not snap["process_running"]:
        print("\n客户端没在运行。请改用 --full 做完整链路测试。")
        return 2
    win = wb_host.main_window()
    if not win:
        print("\n没找到可作为主窗口的窗口，无法测试。")
        return 3
    print("\n选中的主窗口：hwnd={} title={!r}".format(win["hwnd"], win["title"]))
    print("→ 隐藏…")
    ok, note = wb_host.hide_host_window(timeout=10)
    print("  结果：{} {}".format("成功" if ok else "失败", note))
    time.sleep(2)
    after = [w for w in wb_host.list_windows(win["pid"])
             if w["hwnd"] == win["hwnd"]]
    visible = after[0]["visible"] if after else None
    print("  隐藏后窗口 visible={}".format(visible))
    print("  IPC 是否仍可用：", end="")
    env = core.detect_env(deep=True)
    print(env["code"])
    print("→ 恢复窗口…")
    ok2, note2 = wb_host.show_host_window()
    print("  结果：{} {}".format("成功" if ok2 else "失败", note2))
    print("\n结论：{}".format(
        "Win32 隐藏可行，且不影响 IPC。" if ok and env["code"] == "OK"
        else "隐藏或 IPC 有问题，见上面输出。"))
    return 0 if (ok and env["code"] == "OK") else 1


def test_full() -> int:
    hr("模式 3：完整链路实测（要求客户端已关闭）")
    if wb_host.pids_of():
        print("客户端正在运行（PID {}）。".format(wb_host.pids_of()))
        print("这会干扰测量：单实例锁会让新实例直接退出，测不出'拉起'过程。")
        print("请先在托盘退出 WorkBuddy，再重跑本命令。")
        return 2

    cfg = core.load_config()
    print("使用配置：exe={} hide_window={}".format(
        cfg.get("workbuddy_exe"), cfg.get("hide_window")))
    print("\n[1/5] 拉起客户端…")
    t0 = time.time()
    ok, why = core.launch_workbuddy(cfg)
    print("  {} {}".format("OK" if ok else "FAIL", why))
    if not ok:
        return 3

    print("[2/5] 轮询等待主窗口出现（每 100ms 一次）…")
    win, t_appear = None, None
    while time.time() - t0 < 120:
        w = wb_host.main_window()
        if w and w["title"]:
            win, t_appear = w, time.time()
            break
        time.sleep(0.1)
    if not win:
        print("  超时：120 秒内没等到主窗口。")
        return 4
    print("  窗口出现：hwnd={} title={!r}（耗时 {:.2f}s）".format(
        win["hwnd"], win["title"], t_appear - t0))

    print("[3/5] 立刻隐藏…")
    hid, note = wb_host.hide_host_window(timeout=10, pids=wb_host.pids_of())
    t_hidden = time.time()
    print("  {} {}".format("OK" if hid else "FAIL", note))
    print("  ★ 窗口闪现时长（从进程启动到隐藏成功）：{:.2f} 秒".format(
        t_hidden - t0))

    print("[4/5] 校验窗口确实不可见…")
    time.sleep(1.5)
    still = [w for w in wb_host.list_windows(win["pid"])
             if w["hwnd"] == win["hwnd"]]
    visible = still[0]["visible"] if still else "窗口已销毁"
    print("  visible = {}".format(visible))

    print("[5/5] 校验 IPC 通道仍可用（真调一次状态接口）…")
    env = core.detect_env(deep=True)
    print("  code = {}".format(env["code"]))
    for name, val in env.get("steps", []):
        print("    {}：{}".format(name, val))
    if env.get("status_data"):
        print("  状态数据：{}".format(json.dumps(
            env["status_data"], ensure_ascii=False)))

    hr("结论")
    feasible = hid and env["code"] == "OK"
    print("后台拉起可行性：{}".format("成立 ✅" if feasible else "不成立 ❌"))
    print("代价：客户端窗口会闪现约 {:.1f} 秒（官方没有静默启动参数，"
          "绕不过去）。".format(max(0.0, t_hidden - t0)))
    print("客户端现在处于隐藏状态、进程仍在运行。要把它调回来：")
    print("  python probe_hidden_launch.py --restore")
    return 0 if feasible else 1


def do_restore() -> int:
    hr("恢复客户端窗口")
    ok, note = wb_host.show_host_window()
    print("{} {}".format("OK" if ok else "FAIL", note))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 后台拉起探针")
    ap.add_argument("--test-hide", action="store_true",
                    help="对正在运行的客户端做隐藏/恢复实测（不杀进程）")
    ap.add_argument("--full", action="store_true",
                    help="完整链路实测（要求客户端已关闭）")
    ap.add_argument("--restore", action="store_true",
                    help="恢复客户端主窗口显示")
    args = ap.parse_args()

    if args.restore:
        return do_restore()
    if args.test_hide:
        return test_hide_current()
    if args.full:
        return test_full()

    hr("模式 1：只读报告")
    report_snapshot()
    print("\n想继续验证？")
    print("  隐藏/恢复实测（安全，不杀进程）：python probe_hidden_launch.py --test-hide")
    print("  完整链路实测（需先关客户端）：  python probe_hidden_launch.py --full")
    return 0


if __name__ == "__main__":
    sys.exit(main())
