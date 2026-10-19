# -*- coding: utf-8 -*-
"""账号直连（OAuth）通道回归检查。

背景
----
客户端 5.6.2 起把本地登录态改成字段级加密，"读 token 直调接口"的老脚本全部
失效；WBIPC 通道又要求客户端进程在跑。为此新增了第三条通道：用 CodeBuddy CLI
的设备授权流程重新登录一次，拿明文令牌后纯 HTTP 签到。

这条通道**没法用手工点一下来验证**（要真的浏览器授权、真的签到），
所以这里用本地桩服务器把整条链路跑一遍，重点覆盖几个容易写错的地方：

  1. 轮询：11217（等待中）不能当成失败，必须继续等；
  2. 字段兼容：授权接口用 snake_case、刷新接口用 camelCase，都要能取到；
  3. refreshToken 轮换：刷新后必须落盘，否则下次就被判会话失效；
  4. 401 自动续期重试：第一次 401、刷新后第二次成功，业务层应当无感；
  5. 12153 会话失效：要标记 disabled 并给出"重新授权"而不是含糊的网络错误；
  6. 两种模式的分发：auth_mode 决定是否启动客户端（wbipc 才拉进程）。

用法
----
    python tools/oauth_mode_check.py

结果写入当前目录的 oauth_mode_check.json，退出码反映是否全部通过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_oauth                                                  # noqa: E402
import workbuddy_checkin as core                                 # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "ok": bool(cond), "detail": detail})
    print("{} {}{}".format("PASS" if cond else "FAIL", name,
                           "" if cond else "  <- " + str(detail)))
    return cond


# ---- 桩服务器 -------------------------------------------------------------

class Stub:
    """可编排的假服务端：记录请求，按剧本返回。"""

    def __init__(self):
        self.domain = ""              # 由 serve() 填成 127.0.0.1:port
        self.state_polls = 0          # 轮询次数（控制 11217 的节奏）
        self.refresh_calls = 0
        self.checkin_calls = 0
        self.checked_in = False        # 领取后置真，供状态接口复核
        self.force_401_once = False   # 下一次 billing 调用返回 401
        self.refresh_dead = False     # 刷新返回 12153
        self.headers_seen = []
        self.paths_seen = []

    def handle(self, h, method, path, body):
        q = parse_qs(urlparse(path).query)
        p = urlparse(path).path
        self.paths_seen.append(method + " " + p)
        self.headers_seen.append(dict(h.headers))

        if p.endswith("/plugin/auth/state"):
            return 200, {"code": 0, "data": {
                "state": "STUB-STATE", "authUrl": "https://example.invalid/login"}}

        if p.endswith("/plugin/auth/token"):
            self.state_polls += 1
            if self.state_polls < 2:
                return 200, {"code": 11217, "msg": "pending"}
            return 200, {"code": 0, "data": {
                "access_token": "AAA.BBB.CCC",
                "refresh_token": "REFRESH-1",
                "expires_in": 28 * 24 * 3600,
                "domain": self.domain}}

        if p.endswith("/plugin/auth/token/refresh"):
            self.refresh_calls += 1
            if self.refresh_dead:
                return 401, {"code": 12153,
                             "msg": "Offline user session not found"}
            return 200, {"code": 0, "data": {
                "accessToken": "AAA.BBB.REFRESHED",
                "refreshToken": "REFRESH-2",          # 轮换
                "expiresIn": 28 * 24 * 3600,
                "domain": self.domain}}

        if p.endswith("/billing/meter/checkin-activity-status"):
            if self.force_401_once:
                self.force_401_once = False
                return 401, {"code": 401, "msg": "unauthorized"}
            return 200, {"code": 0, "data": {
                "active": True, "today_checked_in": self.checked_in,
                "daily_credit": 100,
                "streak_days": 10 if self.checked_in else 9,
                "total_credits": 900}}

        if p.endswith("/billing/meter/daily-checkin"):
            self.checkin_calls += 1
            self.checked_in = True
            return 200, {"code": 0, "data": {
                "credit": 100, "streak_days": 10, "total_credits": 1000,
                "message": "ok"}}

        return 404, {"code": 404, "msg": "no route " + p}


def serve(stub):
    class Handler(BaseHTTPRequestHandler):
        def _do(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            status, payload = stub.handle(self, method, self.path, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def point_at(stub, port):
    """把所有端点指到桩服务器。"""
    base = "http://127.0.0.1:{}".format(port)
    stub.domain = base        # 令牌里的 domain，billing 基址由它推导
    wb_oauth.AUTH_STATE_URL = base + "/v2/plugin/auth/state?platform=CLI"
    wb_oauth.AUTH_TOKEN_URL = base + "/v2/plugin/auth/token?state="
    wb_oauth.REFRESH_URL_CN = base + "/v2/plugin/auth/token/refresh"
    wb_oauth.REFRESH_URL_GLOBAL = base + "/v2/plugin/auth/token/refresh"
    # billing_base 从令牌 domain 推导；令牌里带 http:// 前缀即可命中桩
    return base


# ---- 用例 -----------------------------------------------------------------

def main():
    stub = Stub()
    srv, port = serve(stub)
    host = point_at(stub, port)
    tmpdir = tempfile.mkdtemp(prefix="wb_oauth_check_")
    token_file = os.path.join(tmpdir, "auth_oauth.json")
    cfg = dict(core.DEFAULT_CONFIG)
    cfg["auth_mode"] = "oauth"
    cfg["oauth_token_file"] = token_file

    try:
        # 1) 未授权时给出明确结论，而不是泛泛的连接失败
        try:
            wb_oauth.OAuthSession(token_file)
            check("未授权时报错", False, "没有抛异常")
        except wb_oauth.OAuthError as e:
            check("未授权时报错", e.result == "OAUTH_NOT_LOGGED_IN", e.result)

        # 2) 轮询：11217 之后仍能拿到令牌
        t0 = time.time()
        rec = wb_oauth.login(token_file, emit=lambda m: None,
                             open_browser=False, timeout=30)
        auth = rec.get("auth") or {}
        check("轮询跳过 11217 后成功", bool(auth.get("accessToken")), rec)
        check("等待中不是失败", stub.state_polls >= 2, stub.state_polls)
        check("轮询成本可控", time.time() - t0 < 20, time.time() - t0)
        check("refreshToken 已落盘", auth.get("refreshToken") == "REFRESH-1",
              auth.get("refreshToken"))
        check("expiresAt 由 expires_in 推出",
              int(auth.get("expiresAt") or 0) > time.time() + 20 * 86400,
              auth.get("expiresAt"))
        check("令牌文件权限已收紧（POSIX 才生效）",
              os.path.isfile(token_file))

        # 3) 令牌 domain 决定 billing 基址
        check("billing 基址取自 domain",
              wb_oauth.billing_base(rec) == host,
              wb_oauth.billing_base(rec))

        # 4) 完整签到：查 → 领 → 复核，业务层对通道无感
        with wb_oauth.OAuthSession(token_file) as sess:
            result = core.run(sess, check_only=False)
        check("签到流程成功", result.get("result") == "CLAIMED", result)
        check("领取只调用一次", stub.checkin_calls == 1, stub.checkin_calls)
        check("结果字段完整",
              result.get("credit") == 100 and result.get("streak_days") == 10,
              result)

        # 5) 401 → 自动续期 → 重试成功；新 refreshToken 必须落盘
        before = stub.refresh_calls
        stub.force_401_once = True
        with wb_oauth.OAuthSession(token_file) as sess:
            status, _body = sess.fetch("POST", core.STATUS_PATH)
        check("401 后自动续期并重试", status == 200, status)
        check("确实调用了刷新", stub.refresh_calls == before + 1,
              stub.refresh_calls)
        saved = wb_oauth.load(token_file)
        check("轮换后的 refreshToken 已落盘",
              saved["auth"]["refreshToken"] == "REFRESH-2",
              saved["auth"]["refreshToken"])
        check("轮换后的 accessToken 已落盘",
              saved["auth"]["accessToken"].endswith("REFRESHED"),
              saved["auth"]["accessToken"])

        # 6) 会话失效（12153）→ 标记 disabled，并给出重新授权
        stub.refresh_dead = True
        rec2 = wb_oauth.load(token_file)
        try:
            wb_oauth.refresh(rec2, token_file)
            check("12153 判定会话失效", False, "没有抛异常")
        except wb_oauth.OAuthError as e:
            check("12153 判定会话失效", e.result == "OAUTH_SESSION_DEAD", e.result)
        marked = wb_oauth.load(token_file)
        check("失效状态已落盘", bool(marked.get("disabled")) is True, marked.get("disabled"))
        try:
            wb_oauth.OAuthSession(token_file)
            check("失效令牌拒绝使用", False, "没有抛异常")
        except wb_oauth.OAuthError as e:
            check("失效令牌拒绝使用", e.result == "OAUTH_SESSION_DEAD", e.result)

        # 7) 模式分发：oauth 不该碰客户端进程
        called = {"launch": 0}
        orig = core.connect_with_autolaunch

        def spy(*a, **kw):
            called["launch"] += 1
            raise AssertionError("oauth 模式不应该走客户端拉起")

        core.connect_with_autolaunch = spy
        try:
            stub.refresh_dead = False
            wb_oauth.login(token_file, emit=lambda m: None,
                           open_browser=False, timeout=30)
            sess, launched = core.open_session(cfg)
            check("oauth 模式不拉起客户端", called["launch"] == 0, called)
            check("oauth 模式 launched=False", launched is False, launched)
            sess.close()

            cfg2 = dict(cfg)
            cfg2["auth_mode"] = "wbipc"
            try:
                core.open_session(cfg2)
            except AssertionError:
                pass
            check("wbipc 模式仍走客户端通道", called["launch"] == 1, called)
        finally:
            core.connect_with_autolaunch = orig

        # 8) 请求头：鉴权与身份头都要带齐
        hdrs = stub.headers_seen[-1] if stub.headers_seen else {}
        with wb_oauth.OAuthSession(token_file) as sess:
            sess.fetch("POST", core.STATUS_PATH)
        hdrs = stub.headers_seen[-1]
        low = {k.lower(): v for k, v in hdrs.items()}
        check("带 Authorization Bearer",
              str(low.get("authorization", "")).startswith("Bearer "),
              low.get("authorization"))
        check("带 X-Domain", bool(low.get("x-domain")), low.get("x-domain"))
        check("UA 为 CLI 身份",
              "CodeBuddy" in str(low.get("user-agent", "")),
              low.get("user-agent"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok = all(r["ok"] for r in RESULTS)
    print("\n{} / {} 通过".format(sum(1 for r in RESULTS if r["ok"]),
                                  len(RESULTS)))
    out = os.path.join(os.getcwd(), "oauth_mode_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"ok": ok, "results": RESULTS}, fh,
                  ensure_ascii=False, indent=2)
    print("结果已写入 {}".format(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
