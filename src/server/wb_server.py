#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端版 —— 一条命令跑完「授权（必要时）+ 签到 + 推送」，交给 cron 定时调用。

和 Windows 版的关系
------------------
Windows 版是 exe：GUI 配置，计划任务调 `WorkBuddyCheckin.exe --run`。
服务端版是脚本：没有界面、没有浏览器，全靠推送把授权链接送到手机上。

    python3 wb_server.py                  # 日常：该授权就授权，然后签到

配置跟着脚本走
--------------
默认读**本脚本同目录**的 config.json，日志与令牌也写在这个目录。
把 src/ 整个目录（wb_config.py / wb_notify.py / wb_oauth.py /
workbuddy_checkin.py / wb_server.py + config.json）拷到服务器上就能跑，
不认任何全局路径。用 `--config` 指定别处时，数据目录跟着配置文件走。

每次运行做什么
--------------
1. 检查授权：令牌不存在 / 损坏 / 会话失效 → 进入授权流程
2. 授权流程（最多 3 轮，指数退避）：
      申请会话 → 推送链接到企业微信/钉钉 → 等 180 秒
      等不到就隔 60 秒再来一轮（等 180 秒），再等 120 秒 → 第三轮
      等待期间用户随时可以回来改 config.json（每轮都会重读配置）
3. 拿到授权后立刻签到，并把结果推到同一批渠道
4. 全部失败则推一条失败通知，退出码非 0（cron 会因此报警）

退出码
------
    0   成功（含"今天已签到"）
    1   推送失败（--push-only）
    2   授权没完成 / 配置不可用
    3   网络或接口异常
    130 被 Ctrl+C 中断

零依赖，Python 3.8+，Linux / Windows 都能跑（不 import tkinter / wb_task）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 源码目录是分层的（server/ 与 common/ 平级），部署时是平铺 / 单文件 zipapp。
# 这里两条都挂上：源码态靠 ../common，单文件态靠 zip 内部的模块。
for _p in (str(Path(__file__).resolve().parent),
           str(Path(__file__).resolve().parent.parent / "common")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wb_config
import wb_notify
import wb_oauth
import workbuddy_checkin as core

# 配置、日志、令牌都放脚本所在目录。单文件 zipapp 里 __file__ 指向 .pyz 内部，
# 所以统一用 wb_config 算好的 SRC_DIR，不要自己从 __file__ 推。
HERE = wb_config.SRC_DIR

# 需要重新授权的几种情况（其余是网络/接口问题，重试授权没有意义）
AUTH_BROKEN = ("OAUTH_NOT_AUTHORIZED", "OAUTH_TOKEN_BAD", "OAUTH_SESSION_DEAD")

ENV_TEXT = {
    "OK": "可用",
    "OAUTH_NOT_AUTHORIZED": "未授权",
    "OAUTH_TOKEN_BAD": "令牌文件损坏",
    "OAUTH_SESSION_DEAD": "会话已失效",
    "OAUTH_NETWORK": "网络不通",
    "OAUTH_FAIL": "接口异常",
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def bind_data_dir(cfg_file: str) -> Path:
    """数据目录跟着配置文件走：config.json / checkin.log / auth_oauth.json 同层。

    每个对外入口都先调一次——这样单独调用内部函数（排查、二次开发）时，
    也不会把令牌写到别的地方去。
    """
    folder = Path(cfg_file).resolve().parent
    wb_config.set_data_dir(folder)
    return folder


# ---------------- 配置体检 / 生成模板 ----------------

def channel_rows(cfg: dict) -> list:
    """每个渠道一行：是否勾选、key 是否有效。**不打印 key 本身**。"""
    picked = [str(c) for c in (cfg.get("push_channels") or [])]
    rows = []
    for name, (title, resolve, _send) in wb_notify.CHANNELS.items():
        configured = bool(resolve(cfg))
        on = name in picked
        if on and configured:
            state = "可用"
        elif on:
            state = "已勾选，但 key 缺失或不合法"
        elif configured:
            state = "key 已填，但没勾选（不会推送）"
        else:
            state = "未启用"
        rows.append({"name": name, "title": title, "on": on,
                     "configured": configured, "state": state,
                     "usable": on and configured})
    return rows


def do_check(cfg: dict, config_file: str) -> int:
    bind_data_dir(config_file)
    say("配置文件：{}".format(config_file))
    say("数据目录：{}".format(wb_config.data_dir()))
    say("令牌文件：{}".format(core.oauth_token_path(cfg)))
    say("")

    info = wb_oauth.summary(core.oauth_token_path(cfg))
    if not info.get("present"):
        auth_text = "未授权（下次运行会自动发起授权）"
    elif info.get("error"):
        auth_text = "令牌异常：{}".format(info.get("error"))
    elif info.get("disabled") or info.get("expired"):
        auth_text = "已失效（下次运行会自动重新授权）"
    else:
        auth_text = "{}，有效期至 {}（剩 {} 天）".format(
            info.get("nickname") or "已授权",
            info.get("expires_text") or "?", info.get("days_left"))
    say("授权：{}".format(auth_text))
    say("")

    rows = channel_rows(cfg)
    for row in rows:
        say("  {}：{}".format(row["title"], row["state"]))
    usable = [r["name"] for r in rows if r["usable"]]
    say("")
    if not usable:
        say("✗ 没有可用的推送渠道。按上面的提示修配置（勾选 + 填有效 key）：")
        say("  1) 编辑 {}".format(config_file))
        say("  2) 再跑一次 --check")
        return 2
    say("✅ 可用渠道：{}".format("、".join(wb_notify.label(c) for c in usable)))

    if not info.get("present") or info.get("disabled") or info.get("expired"):
        say("⚠ 目前没有可用授权：下次运行会先推授权链接，等 {} 秒，".format(
            int(cfg.get("auth_wait_seconds") or 180)))
        say("  失败则指数退避重试（最多 {} 轮）。".format(
            int(cfg.get("auth_attempts") or 3)))
    else:
        say("下一步：python3 wb_server.py    # 直接签到")
    return 0


def do_init_config(target: str, force: bool) -> int:
    path = Path(target).expanduser().resolve() if target \
        else wb_config.config_path()
    if path.exists() and not force:
        say("✗ 文件已存在：{}".format(path))
        say("  要覆盖就加 --force（会丢掉现有 key）。")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wb_config.DEFAULT_CONFIG, ensure_ascii=False,
                               indent=2) + "\n", encoding="utf-8")
    say("✅ 已生成配置模板：{}".format(path))
    say("")
    say("接下来：")
    say("  1) 填 wecom_key（企业微信机器人 key）或 dingtalk_key")
    say("  2) 勾选要用的渠道：push_channels = [\"wecom\"]")
    say("  3) 体检：python3 wb_server.py --check")
    return 0


# ---------------- 推送 ----------------

def resolve_channels(cfg: dict, only: str = "") -> list:
    """要推到哪些渠道。only 指定单个渠道时用它，否则用配置里的选择。"""
    if only:
        return [only] if wb_notify.CHANNELS.get(only) and \
            wb_notify.CHANNELS[only][1](cfg) else []
    return wb_notify.selected_channels(cfg)


def push_text(cfg: dict, text: str, channels: List[str]) -> bool:
    ok_any = False
    for name in channels:
        title, _resolve, send = wb_notify.CHANNELS[name]
        ok, detail = send(cfg, text)
        say("  {} → {}".format(title, detail))
        ok_any = ok_any or ok
    return ok_any


def push_url(cfg: dict, url: str, channels: List[str],
             wait_seconds: int) -> bool:
    minutes = max(1, wait_seconds // 60)
    return push_text(cfg, core.build_auth_message(url, minutes), channels)


# ---------------- 授权 ----------------

def _int_opt(cli_value: Optional[int], cfg: dict, key: str, fallback: int,
             minimum: int = 0) -> int:
    value = cli_value
    if value is None:
        value = cfg.get(key, fallback)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = fallback
    return max(minimum, value)


def authorize(cfg_file: str, opts: argparse.Namespace) -> Dict[str, Any]:
    """推链接 → 等用户授权 → 失败则指数退避重试。

    每轮都重新读一遍 config.json：用户很可能就是在等待的这几分钟里，
    把机器人 key 补上、把渠道勾上的。第一轮没推出去，第二轮就推得出去了。
    """
    bind_data_dir(cfg_file)
    base = wb_config.load(cfg_file)
    attempts = _int_opt(opts.attempts, base, "auth_attempts", 3, 1)
    wait = _int_opt(opts.wait, base, "auth_wait_seconds", 180, 30)
    backoff = _int_opt(opts.backoff, base, "auth_backoff_seconds", 60)

    last = ""
    for i in range(1, attempts + 1):
        cfg = wb_config.load(cfg_file)
        channels = resolve_channels(cfg, opts.channel)
        say("")
        say("──────── 授权 第 {}/{} 轮 ────────".format(i, attempts))
        say("① 申请授权会话…")
        try:
            info = wb_oauth.begin_auth()
        except wb_oauth.OAuthError as e:
            last = "{}: {}".format(e.result, e)
            say("✗ {}".format(last))
        else:
            url = info.get("auth_url") or ""
            if not url:
                last = "服务端没有返回授权链接"
                say("✗ {}".format(last))
            else:
                say("")
                say("授权链接（也可手动转发）：")
                say("  " + url)
                say("")
                if channels:
                    say("② 推送到 {}…".format(
                        "、".join(wb_notify.label(c) for c in channels)))
                    if not push_url(cfg, url, channels, wait):
                        say("⚠ 所有渠道都推送失败，只能手动转发上面的链接。")
                else:
                    say("⚠ 没有可用的推送渠道（检查 config.json 的"
                        "push_channels 与 key），请手动转发上面的链接。")
                say("")
                say("③ 等待完成授权（最多 {} 秒）；这期间可以改 {}".format(
                    wait, cfg_file))
                say("   补配置，下一轮会自动重读。")

                def on_tick(elapsed, remain, _wait=wait):
                    step = 30 if _wait > 120 else 15
                    if elapsed and elapsed % step == 0:
                        say("  …已等 {} 秒，剩余约 {} 秒".format(
                            elapsed, max(0, remain)))

                try:
                    payload = wb_oauth.poll_token(
                        info["state"], timeout=wait, on_tick=on_tick)
                except wb_oauth.OAuthError as e:
                    last = "{}: {}".format(e.result, e)
                    say("✗ 本轮未完成：{}".format(last))
                else:
                    record = wb_oauth.build_record(payload)
                    if not record["auth"]["accessToken"]:
                        last = "授权响应里没有 accessToken"
                        say("✗ {}".format(last))
                    else:
                        path = core.oauth_token_path(cfg)
                        wb_oauth.save(record, path)
                        acct = record.get("account") or {}
                        say("")
                        say("④ ✅ 授权成功：{}（uid {}）".format(
                            acct.get("nickname") or "已登录账号",
                            acct.get("uid") or "?"))
                        say("   令牌已保存：{}".format(path))
                        return {"ok": True, "attempt": i,
                                "uid": acct.get("uid") or "",
                                "nickname": acct.get("nickname") or "",
                                "token_file": path}

        if i < attempts:
            delay = backoff * (2 ** (i - 1))
            say("")
            say("→ 第 {} 轮失败（{}）。{} 秒后重试…".format(i, last, delay))
            if delay:
                time.sleep(delay)
    return {"ok": False, "attempts": attempts, "message": last}


# ---------------- 主流程 ----------------

def notify_failure(cfg: dict, text: str, channels: List[str]) -> None:
    if not channels:
        return
    say("推送失败通知到 {}…".format(
        "、".join(wb_notify.label(c) for c in channels)))
    push_text(cfg, text, channels)


def run_checkin(cfg_file: str, opts: argparse.Namespace,
                existing: Optional[dict] = None) -> int:
    bind_data_dir(cfg_file)
    cfg = existing if existing is not None else wb_config.load(cfg_file)
    result = core.run_once(cfg, notify=True, progress=lambda m: say("· " + m))
    say("")
    say("结果：{}".format(result.get("result")))
    if opts.json:
        say(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return int(result.get("exit_code", 0))


def run_daily(cfg_file: str, opts: argparse.Namespace) -> int:
    bind_data_dir(cfg_file)
    cfg = wb_config.load(cfg_file)
    say("配置：{}".format(cfg_file))
    say("数据目录：{}".format(wb_config.data_dir()))
    say("① 检查授权…")

    env = core.detect_env(deep=True, cfg=cfg)
    code = env.get("code")
    say("   授权状态：{}（{}）".format(
        ENV_TEXT.get(code, code), env.get("message") or "—"))

    if code in AUTH_BROKEN:
        say("   需要先完成授权。")
        res = authorize(cfg_file, opts)
        if not res.get("ok"):
            cfg = wb_config.load(cfg_file)
            notify_failure(
                cfg,
                "【WorkBuddy 签到】❌ 服务端授权未完成\n"
                "已重试 {} 轮（指数退避），最后一次：{}\n"
                "签到已跳过。排查：python3 wb_server.py --check".format(
                    res.get("attempts"), res.get("message")),
                resolve_channels(cfg, opts.channel))
            say("")
            say("✗ 授权最终失败：{}".format(res.get("message")))
            return 2
        cfg = wb_config.load(cfg_file)
    elif code == "OAUTH_NETWORK":
        say("✗ 网络不通，未执行签到：{}".format(env.get("message")))
        return 3
    elif code != "OK":
        say("✗ 接口异常，未执行签到：{}".format(env.get("message")))
        return 3

    say("")
    say("② 执行签到…")
    return run_checkin(cfg_file, opts, existing=cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="服务端版 WorkBuddy 签到：检查授权 → 必要时推送授权链接 → "
                    "签到 → 推送结果（给 cron 用）",
        epilog=(
            "典型流程\n"
            "  1) python3 wb_server.py --init-config\n"
            "     填 wecom_key / dingtalk_key 与 push_channels\n"
            "  2) python3 wb_server.py --check\n"
            "  3) python3 wb_server.py          # 第一次会推授权链接，之后直接签到\n"
            "\n"
            "crontab（每天 09:30）\n"
            "  30 9 * * * cd /opt/wbcheckin && python3 wb_server.py "
            ">> checkin.log 2>&1\n"
            "\n"
            "配置、日志、令牌都放在脚本同目录，整个目录拷走就能换机器。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="",
                    help="配置文件路径（默认脚本同目录的 config.json）")
    ap.add_argument("--channel", choices=["wecom", "dingtalk"], default="",
                    help="强制只用某个推送渠道（默认按配置里的 push_channels）")
    ap.add_argument("--wait", type=int, default=None,
                    help="每轮等待授权的秒数（默认取配置 auth_wait_seconds=180）")
    ap.add_argument("--attempts", type=int, default=None,
                    help="最多授权几轮（默认取配置 auth_attempts=3）")
    ap.add_argument("--backoff", type=int, default=None,
                    help="轮次之间的退避基数秒数，按 1x/2x 增长"
                         "（默认取配置 auth_backoff_seconds=60）")
    ap.add_argument("--auth-only", action="store_true", help="只做授权，不签到")
    ap.add_argument("--checkin-only", action="store_true", help="只签到，不检查授权")
    ap.add_argument("--init-config", nargs="?", const="", default=None,
                    metavar="PATH", help="生成配置模板后退出（默认写到脚本目录）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --init-config：文件已存在也覆盖")
    ap.add_argument("--check", action="store_true",
                    help="体检授权与推送渠道（不联网、不签到）")
    ap.add_argument("--push-only", action="store_true",
                    help="只把 --url 推出去，不发起授权")
    ap.add_argument("--url", default="", help="配合 --push-only 使用")
    ap.add_argument("--version", action="store_true", help="显示版本后退出")
    ap.add_argument("--json", action="store_true", help="结果输出单行 JSON")
    args = ap.parse_args(argv)

    cfg_file = str(Path(args.config or (HERE / "config.json")).expanduser())
    # 数据目录跟着配置文件走：默认就是脚本目录
    wb_config.prepare(Path(cfg_file).resolve().parent)

    if args.version:
        say(wb_config.version_text())
        return 0

    if args.init_config is not None:
        return do_init_config(args.init_config, args.force)

    cfg = wb_config.load(cfg_file)
    if args.check:
        return do_check(cfg, cfg_file)

    channels = resolve_channels(cfg, args.channel)

    if args.push_only:
        if not args.url:
            say("✗ --push-only 需要配合 --url 指定链接")
            return 2
        if not channels:
            say("✗ 没有可用的推送渠道，先跑 --check 看看")
            return 2
        say("推送到 {}…".format("、".join(wb_notify.label(c) for c in channels)))
        return 0 if push_url(cfg, args.url, channels,
                             _int_opt(args.wait, cfg, "auth_wait_seconds",
                                      180, 30)) else 1

    try:
        if args.auth_only:
            res = authorize(cfg_file, args)
            if not res.get("ok"):
                say("✗ 授权失败：{}".format(res.get("message")))
                return 2
            return 0
        if args.checkin_only:
            return run_checkin(cfg_file, args)
        return run_daily(cfg_file, args)
    except KeyboardInterrupt:
        say("")
        say("已中断。令牌没有变化，重新跑一次即可。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
