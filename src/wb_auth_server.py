#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端授权脚本 —— 在服务器上发起账号授权，把链接推到手机，人不在电脑前也能签上。

它解决什么问题
--------------
Windows 那台机器可能没人守着（或者干脆没有浏览器）。与其让运维跑去点，
不如在服务器上生成授权链接，推到企业微信 / 钉钉群里，用户手机上点开登录，
授权结果再交回 Windows 端。

设备授权能不能跨机器？
----------------------
能。`POST /v2/plugin/auth/state` 拿到的 `state` 只是服务端一段会话 id，
`GET /v2/plugin/auth/token?state=xxx` 是**无状态轮询**，谁拿着 state 谁就能
取到令牌。所以「服务器生成链接 + 服务器轮询」在协议上是成立的。

但有一件事必须守住：**令牌只能有一份**
--------------------------------------
`refreshToken` 每次刷新都会轮换。如果服务器和 Windows 各存一份，两边各自
续期，先刷新的一方会让另一方的 refreshToken 立即失效（服务端返回 12153），
表现为「授权莫名其妙失效」。所以：
  * 本脚本默认**不落盘**，拿到令牌只输出一段一次性导入码；
  * Windows 端导入后独占这份令牌，之后所有续期都在那台机器上；
  * 只有你明确要求（--save-token）才会在服务器留一份，那时就别再导入到别处。

三种用法
--------
1) 默认：生成链接 → 推送 → 等待扫码 → 打印导入码（人工粘到 Windows 端）
       python src/wb_auth_server.py

2) 自动：授权后开一个一次性领取端口，Windows 端自己来取，全程无人工
       python src/wb_auth_server.py --handoff --port 8787
     Windows 端执行脚本打印出来的那条命令即可。

3) 只转发：把一条已有链接推到渠道（本机生成的链接也能借服务器发出去）
       python src/wb_auth_server.py --push-only --url https://copilot.tencent.com/login?...

首次部署先跑这两条（不联网、不创建授权会话）：
       python src/wb_auth_server.py --init-config     # 生成一份 config.json 模板
       python src/wb_auth_server.py --check           # 检查渠道与 key 是否可用

分工：**远程授权只属于服务端流程**
--------------------------------
本地版的界面只有「立即授权」（本机浏览器），没有"推到手机"、也没有"导入"。
要跨机器交接令牌，一律在命令行完成（--import-token / --pull-token），
因为这一步本来就是给无人值守的机器准备的。

零依赖，Python 3.7+，Linux / Windows 都能跑。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import wb_config
import wb_notify
import wb_oauth
import workbuddy_checkin as core


def say(msg: str = "") -> None:
    print(msg, flush=True)


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
    say("配置文件：{}".format(config_file))
    say("数据目录：{}".format(wb_config.SCRIPT_DIR))
    say("")
    rows = channel_rows(cfg)
    for row in rows:
        say("  {}：{}".format(row["title"], row["state"]))
    usable = [r["name"] for r in rows if r["usable"]]
    say("")
    if not usable:
        say("✗ 没有可用的推送渠道。按上面的提示修配置（勾选 + 填有效 key）后重跑：")
        say("    python src/wb_auth_server.py --config \"{}\" --check"
            .format(config_file))
        say("  还没有配置文件时先生成模板：python src/wb_auth_server.py --init-config")
        return 2
    say("✅ 可用渠道：{}".format("、".join(wb_notify.label(c) for c in usable)))
    say("下一步：python src/wb_auth_server.py"
        + ("" if len(usable) == 1 else " --channel " + usable[0]))
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
    say("  3) 体检：python src/wb_auth_server.py --config \"{}\" --check"
        .format(path))
    return 0


# ---------------- 推送 ----------------

def resolve_channels(cfg: dict, only: str = "") -> list:
    """要推到哪些渠道。only 指定单个渠道时用它，否则用配置里的选择。"""
    if only:
        return [only] if wb_notify.CHANNELS.get(only) and \
            wb_notify.CHANNELS[only][1](cfg) else []
    return wb_notify.selected_channels(cfg)


def push_url(cfg: dict, url: str, channels: list, timeout: int) -> bool:
    minutes = max(1, timeout // 60)
    text = core.build_auth_message(url, minutes)
    ok_any = False
    for name in channels:
        title, _resolve, send = wb_notify.CHANNELS[name]
        ok, detail = send(cfg, text)
        say("  {} → {}".format(title, detail))
        ok_any = ok_any or ok
    return ok_any


# ---------------- 一次性领取端口 ----------------

class Handoff:
    """授权成功后开一个一次性端口，让 Windows 端来取令牌。

    只做两件事：
        GET /token?k=<secret>   取令牌（取一次就作废）
        GET /status             看是否已取走（给排错用）
    secret 是启动时随机生成的，打印在控制台上。取走后 2 秒自动关服。
    """

    def __init__(self, record: dict, port: int, bind: str, timeout: int = 600):
        self.record = record
        self.secret = secrets.token_urlsafe(16)
        self.delivered = False
        self.stop = threading.Event()
        handler = self._make_handler()
        try:
            self.httpd = HTTPServer((bind, port), handler)
        except OSError:
            # 端口被占用时退回系统分配的临时端口，反正地址会打印出来
            self.httpd = HTTPServer((bind, 0), handler)
        self.port = self.httpd.server_address[1]

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, payload):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                key = (q.get("k") or [""])[0]
                if u.path == "/status":
                    self._reply(200, {"delivered": owner.delivered})
                    return
                if u.path != "/token":
                    self._reply(404, {"error": "not found"})
                    return
                if not key or not secrets.compare_digest(key, owner.secret):
                    self._reply(403, {"error": "bad secret"})
                    return
                if owner.delivered:
                    self._reply(410, {"error": "already delivered"})
                    return
                owner.delivered = True
                self._reply(200, {"token": owner.record})
                threading.Thread(target=owner._shutdown_later,
                                 daemon=True).start()

            def log_message(self, *a):
                pass

        return Handler

    def _shutdown_later(self):
        time.sleep(2)                      # 让响应先写出去
        self.stop.set()
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def serve(self, timeout: int) -> bool:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.httpd.timeout = 1
        say("")
        say("等待 Windows 端来领取（{} 秒后自动关闭）…".format(timeout))
        deadline = time.time() + timeout
        while time.time() < deadline and not self.delivered:
            time.sleep(1)
        if not self.delivered:
            say("超时无人领取，令牌未交出。重新跑一次脚本即可。")
        return self.delivered


def local_ip() -> str:
    """猜一个本机可被访问的 IP（不产生真实流量）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


# ---------------- 主流程 ----------------

def do_auth(cfg: dict, channels: list, timeout: int,
            handoff: bool, port: int, bind: str,
            handoff_timeout: int, save_token: str, as_json: bool) -> int:
    say("① 申请授权会话…")
    try:
        info = wb_oauth.begin_auth()
    except wb_oauth.OAuthError as e:
        say("✗ 申请授权失败：{}".format(e))
        return 3
    url = info.get("auth_url") or ""
    if not url:
        say("✗ 服务端没有返回授权链接。")
        return 3
    say("")
    say("授权链接：")
    say("  " + url)
    say("")

    if channels:
        say("② 推送授权链接到 {}…".format(
            "、".join(wb_notify.label(c) for c in channels)))
        if not push_url(cfg, url, channels, timeout):
            say("✗ 所有渠道都推送失败。链接仍可从上面手动复制。")
    else:
        say("⚠ 没有可用的推送渠道（检查 config.json 的 push_channels 与 key），"
            "请手动把链接发给用户。")

    say("")
    say("③ 等待用户打开链接并完成登录（最多 {} 分钟）…".format(
        max(1, timeout // 60)))

    def on_tick(elapsed, remain):
        if elapsed and elapsed % 15 == 0:
            say("  …已等待 {} 秒，剩余约 {} 秒".format(elapsed, max(0, remain)))

    try:
        payload = wb_oauth.poll_token(info["state"], timeout=timeout,
                                      on_tick=on_tick)
    except wb_oauth.OAuthError as e:
        say("✗ 授权失败：{}".format(e))
        return int(e.exit_code or 2)

    record = wb_oauth.build_record(payload)
    if not record["auth"]["accessToken"]:
        say("✗ 授权响应里没有 accessToken。")
        return 3

    acct = record.get("account") or {}
    auth = record.get("auth") or {}
    who = acct.get("nickname") or "已登录账号"
    say("")
    say("④ ✅ 授权成功：{}（uid {}）".format(who, acct.get("uid") or "?"))

    if save_token:
        wb_oauth.save(record, save_token)
        say("令牌已写入 {}（注意：这份令牌从此由这台机器独占，"
            "别再导入到别处，否则两边续期会互相踢掉）。".format(save_token))

    if handoff:
        h = Handoff(record, port=port, bind=bind)
        say("")
        say("一次性领取地址已就绪：")
        say("  http://{}:{}/token?k={}".format(local_ip(), h.port, h.secret))
        say("")
        say("在 Windows 端执行（exe 或源码任选其一）：")
        say('  WorkBuddy签到.exe --pull-token "http://{}:{}/token" "{}"'
            .format(local_ip(), h.port, h.secret))
        say('  python run.py --pull-token '
            '"http://{}:{}/token" "{}"'.format(local_ip(), h.port, h.secret))
        got = h.serve(handoff_timeout)
        if not got:
            return 4
        say("✅ 令牌已被 Windows 端取走，本进程不保留任何令牌。")
        return 0

    code = wb_oauth.export_code(record)
    if as_json:
        say(json.dumps({"result": "AUTHORIZED", "uid": acct.get("uid") or "",
                        "nickname": who, "import_code": code},
                       ensure_ascii=False))
        return 0

    say("")
    say("把下面这段一次性导入码交给 Windows 端，在那台机器上执行：")
    say('  WorkBuddy签到.exe --import-token "<粘贴下面的码>"')
    say('  python run.py --import-token "<粘贴下面的码>"     # 源码运行')
    say("")
    say(code)
    say("")
    say("⚠ 导入码等同于令牌本身（只是 base64），别贴群里、别进工单系统。")
    say("⚠ 本脚本没有在服务器保留令牌：Windows 端导入后由它独占续期。")
    say("⚠ 界面上的「账号授权」只有本机浏览器一条路，导入只能在命令行做。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="服务端发起 WorkBuddy 账号授权：生成链接 → 推到手机 → "
                    "等人登录 → 把令牌交给跑签到的那台 Windows",
        epilog=(
            "典型流程\n"
            "  1) python src/wb_auth_server.py --init-config\n"
            "     填 wecom_key / dingtalk_key 与 push_channels\n"
            "  2) python src/wb_auth_server.py --check\n"
            "  3) python src/wb_auth_server.py                # 人工取导入码\n"
            "     或 python src/wb_auth_server.py --handoff    # Windows 自动取件\n"
            "\n"
            "令牌只有一份：本脚本默认不落盘，交给 Windows 端独占续期。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="",
                    help="配置文件路径（默认数据目录的 config.json）")
    ap.add_argument("--channel", choices=["wecom", "dingtalk"], default="",
                    help="强制只用某个推送渠道（默认按配置里的 push_channels）")
    ap.add_argument("--timeout", type=int, default=600,
                    help="等待用户授权的秒数（默认 600）")
    ap.add_argument("--handoff", action="store_true",
                    help="授权成功后开一个一次性端口，由 Windows 端自动来取令牌")
    ap.add_argument("--port", type=int, default=8787,
                    help="--handoff 的监听端口（默认 8787，被占用就换一个）")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="--handoff 的监听地址（默认 0.0.0.0）")
    ap.add_argument("--handoff-timeout", type=int, default=600,
                    help="等 Windows 端来取令牌的秒数（默认 600）")
    ap.add_argument("--save-token", default="",
                    help="授权成功后把令牌存到这里（默认不存；见文件头说明）")
    ap.add_argument("--push-only", action="store_true",
                    help="只把 --url 推出去，不发起授权")
    ap.add_argument("--url", default="", help="配合 --push-only 使用")
    ap.add_argument("--init-config", nargs="?", const="", default=None,
                    metavar="PATH", help="生成配置模板后退出（默认写到数据目录）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --init-config：文件已存在也覆盖")
    ap.add_argument("--check", action="store_true",
                    help="只体检配置（渠道是否勾选、key 是否可用），不联网")
    ap.add_argument("--json", action="store_true", help="输出单行 JSON")
    args = ap.parse_args(argv)

    cfg_path = str(wb_config.config_path(args.config or None))

    if args.init_config is not None:
        return do_init_config(args.init_config, args.force)

    cfg = wb_config.load(args.config or None)
    if args.check:
        return do_check(cfg, cfg_path)

    channels = resolve_channels(cfg, args.channel)

    if args.push_only:
        if not args.url:
            say("✗ --push-only 需要配合 --url 指定链接")
            return 2
        if not channels:
            say("✗ 没有可用的推送渠道，先跑 --check 看看")
            return 2
        say("推送到 {}…".format("、".join(wb_notify.label(c) for c in channels)))
        return 0 if push_url(cfg, args.url, channels, args.timeout) else 1

    say("配置文件：{}".format(cfg_path))
    if not channels:
        say("⚠ 当前配置里没有可用的推送渠道，链接只能手动复制。"
            "（先跑 --check 看看缺什么）")

    try:
        return do_auth(cfg, channels, args.timeout, args.handoff, args.port,
                       args.bind, args.handoff_timeout, args.save_token,
                       args.json)
    except KeyboardInterrupt:
        say("")
        say("已中断，未交出任何令牌。重新跑一次脚本即可。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
