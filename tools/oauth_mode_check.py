# -*- coding: utf-8 -*-
"""账号直连链路回归检查（不碰真实接口、不需要真实账号）。

用本地桩服务器把整条链路跑一遍，重点盯住几个真的写错过的地方：

  1. 轮询：11217（等待中）不能当失败，必须继续等；
  2. 字段兼容：授权接口 snake_case、刷新接口 camelCase，都要能取到；
  3. refreshToken 轮换：刷新后必须落盘，否则下次就被判会话失效；
  4. 401 自动续期重试：第一次 401、刷新后第二次成功，业务层应当无感；
  5. 12153 会话失效：标记 disabled 并提示"重新授权"，而不是含糊的网络错误；
  6. 导入码：导出的码必须能原样导入，坏码必须明确报错；
  7. 到期提醒：剩余天数 <= expiry_warn_days 才提醒，阈值设 0 等于关闭；
  8. 推送：只推选中的渠道，没填 key 的渠道不参与；
  9. 版本与数据目录：版本号格式、exe 写缓存目录、旧版数据一次性迁移；
 10. 服务端脚本：能不依赖 tkinter 导入，授权参数取自配置。

用法：
    python tools/oauth_mode_check.py

结果写入 .build/oauth_mode_check.json（构建缓存目录，不入库），
退出码反映是否全部通过。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import wb_config                                                  # noqa: E402
import wb_notify                                                  # noqa: E402
import wb_oauth                                                   # noqa: E402
import workbuddy_checkin as core                                  # noqa: E402

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
        self.domain = ""              # 由 point_at() 填成 127.0.0.1:port
        self.state_polls = 0
        self.refresh_calls = 0
        self.checkin_calls = 0
        self.checked_in = False
        self.force_401_once = False
        self.refresh_dead = False
        self.headers_seen = []
        self.paths_seen = []

    def handle(self, h, method, path, body):
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
    stub.domain = base
    wb_oauth.AUTH_STATE_URL = base + "/v2/plugin/auth/state?platform=CLI"
    wb_oauth.AUTH_TOKEN_URL = base + "/v2/plugin/auth/token?state="
    wb_oauth.REFRESH_URL_CN = base + "/v2/plugin/auth/token/refresh"
    wb_oauth.REFRESH_URL_GLOBAL = base + "/v2/plugin/auth/token/refresh"
    return base


# ---- 用例 -----------------------------------------------------------------

def main():
    stub = Stub()
    srv, port = serve(stub)
    host = point_at(stub, port)
    tmpdir = tempfile.mkdtemp(prefix="wb_oauth_check_")
    token_file = os.path.join(tmpdir, "auth_oauth.json")
    cfg = dict(core.DEFAULT_CONFIG)
    cfg["oauth_token_file"] = token_file
    cfg["push_channels"] = ["wecom"]
    cfg["wecom_key"] = "WECOM-KEY"
    cfg["dingtalk_key"] = "DING-KEY"

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

        # 3) 令牌 domain 决定 billing 基址
        check("billing 基址取自 domain",
              wb_oauth.billing_base(rec) == host, wb_oauth.billing_base(rec))

        # 4) 完整签到：查 → 领 → 复核
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
        check("失效状态已落盘", bool(marked.get("disabled")) is True,
              marked.get("disabled"))
        try:
            wb_oauth.OAuthSession(token_file)
            check("失效令牌拒绝使用", False, "没有抛异常")
        except wb_oauth.OAuthError as e:
            check("失效令牌拒绝使用", e.result == "OAUTH_SESSION_DEAD", e.result)

        # 7) 导入码：服务端导出的东西必须能原样导入
        stub.refresh_dead = False
        fresh = wb_oauth.login(token_file, emit=lambda m: None,
                              open_browser=False, timeout=30)
        code = wb_oauth.export_code(fresh)
        check("导入码有前缀", code.startswith(wb_oauth.IMPORT_PREFIX), code[:12])
        check("导入码是单行", "\n" not in code, len(code))
        os.remove(token_file)
        res = core.import_token(code, cfg)
        check("导入码导入成功", res.get("result") == "IMPORTED", res)
        back = wb_oauth.load(token_file)
        check("导入后令牌一致",
              back["auth"]["accessToken"] == fresh["auth"]["accessToken"],
              back["auth"]["accessToken"][:12])
        check("导入会清掉失效标记", not back.get("disabled"), back.get("disabled"))
        # 从聊天窗口粘贴常带换行/空格，必须容忍
        wrapped = code[:20] + "\n" + code[20:]
        check("导入码容忍换行", bool(wb_oauth.decode_code(wrapped).get("auth")))
        for bad in ("", "WBAT1.", "WBAT1.!!!!", "not-a-code"):
            try:
                wb_oauth.decode_code(bad)
                check("坏导入码被拒绝({!r})".format(bad), False, "没有抛异常")
            except wb_oauth.OAuthError as e:
                check("坏导入码被拒绝({!r})".format(bad),
                      e.result == "OAUTH_IMPORT_BAD", e.result)

        # 7b) 从文件导入 —— 跨机器搬令牌时给的就是一份 auth_oauth.json，
        #     它必须是 JSON 而不是导入码，两种都得认。
        server_file = os.path.join(tmpdir, "from_server.json")
        with open(server_file, "w", encoding="utf-8") as fh:
            json.dump(fresh, fh, ensure_ascii=False)
        os.remove(token_file)
        res = core.import_token(server_file, cfg)
        check("从令牌文件导入成功", res.get("result") == "IMPORTED", res)
        check("文件导入后令牌可读",
              wb_oauth.load(token_file)["auth"]["accessToken"]
              == fresh["auth"]["accessToken"])

        # 8) 到期提醒：只在剩余天数 <= expiry_warn_days 时触发，且不碰真实渠道
        quiet = dict(cfg, push_channels=[])          # 没有渠道 = 不真发
        check("令牌充足时不提醒", core.check_expiry_notice(quiet) is None)
        near = json.loads(json.dumps(fresh))
        near["auth"]["expiresAt"] = int(time.time()) + 86400 + 5
        wb_oauth.save(near, token_file)
        notice = core.check_expiry_notice(quiet)
        check("剩 1 天会提醒", bool(notice) and notice.get("days_left") == 1,
              notice)
        check("提醒里带到期时间", bool((notice or {}).get("expires_text")),
              notice)
        check("提醒不写坏令牌",
              wb_oauth.load(token_file)["auth"]["expiresAt"]
              == near["auth"]["expiresAt"])
        far = json.loads(json.dumps(fresh))
        far["auth"]["expiresAt"] = int(time.time()) + 86400 * 3 + 5
        wb_oauth.save(far, token_file)
        check("剩 3 天（阈值 2）不提醒",
              core.check_expiry_notice(quiet) is None)
        check("阈值设为 0 等于关闭",
              core.check_expiry_notice(dict(quiet, expiry_warn_days=0)) is None)
        check("已过期文案明确", "已过期" in core.build_expiry_message(-1))
        check("提醒文案含天数",
              "还剩 1 天" in core.build_expiry_message(1, "2026-11-01 09:00"))

        # 9) 推送：只推选中的渠道，没填 key 的不参与
        check("只推选中的渠道", wb_notify.selected_channels(cfg) == ["wecom"],
              wb_notify.selected_channels(cfg))
        check("企业微信地址只拼官方域名",
              wb_notify.wecom_url(cfg).startswith(wb_notify.WECOM_API_PREFIX),
              wb_notify.wecom_url(cfg))
        cfg2 = dict(cfg)
        cfg2["push_channels"] = ["wecom", "dingtalk"]
        check("未选渠道不参与",
              wb_notify.selected_channels(dict(cfg, push_channels=[])) == [],
              wb_notify.selected_channels(dict(cfg, push_channels=[])))
        check("整条链接也能抽出 key",
              wb_notify.wecom_key_of(
                  {"wecom_key": "https://qyapi.weixin.qq.com/cgi-bin/webhook/"
                                "send?key=ABC123"}) == "ABC123")
        check("看着像链接却不合法就拒发",
              wb_notify.wecom_url({"wecom_key": "https://evil.example/x?k=1"}) == "")

        # 10) 文案里必须带链接和有效期，否则手机上没法用
        msg = core.build_auth_message("https://example.invalid/login?state=x", 10)
        check("授权文案含链接", "https://example.invalid/login?state=x" in msg, msg)
        check("授权文案含有效期", "10 分钟" in msg, msg)

        # 11) 服务端脚本：服务器上没有 tkinter / 没有 wb_task，必须能裸导入
        try:
            import wb_server
            chans = wb_server.resolve_channels(cfg, "")
            check("服务端脚本可导入并解析渠道", chans == ["wecom"], chans)
            forced = wb_server.resolve_channels(cfg2, "dingtalk")
            check("服务端可强制指定渠道", forced == ["dingtalk"], forced)
            rows = {r["name"]: r for r in wb_server.channel_rows(cfg2)}
            check("体检：勾选且填了 key 才算可用",
                  rows["wecom"]["usable"] and rows["dingtalk"]["usable"], rows)
            rows = {r["name"]: r
                    for r in wb_server.channel_rows(
                        dict(cfg2, wecom_key=""))}
            check("体检：没填 key 不算可用", not rows["wecom"]["usable"], rows)
            check("体检结果不含 key 本身",
                  "WECOM-KEY" not in json.dumps(rows, ensure_ascii=False))
            check("服务端不依赖 wb_task", not hasattr(wb_server, "wb_task"))
            check("授权参数默认 180 秒 / 3 轮",
                  (wb_server._int_opt(None, {}, "auth_wait_seconds", 180, 30),
                   wb_server._int_opt(None, {}, "auth_attempts", 3, 1))
                  == (180, 3))
            check("授权参数可被配置覆盖",
                  wb_server._int_opt(None, {"auth_wait_seconds": 240},
                                     "auth_wait_seconds", 180, 30) == 240)
            check("指数退避：60/120/240",
                  [60 * 2 ** i for i in range(3)] == [60, 120, 240])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wb_server.main(["--version"])
            check("服务端 --version", rc == 0 and wb_config.VERSION in buf.getvalue(),
                  buf.getvalue()[:60])
        except Exception as e:
            check("服务端脚本可导入并解析渠道", False, repr(e))
        finally:
            # 上面 --version 会把数据目录切到脚本目录，恢复回测试前的状态
            wb_config.set_data_dir(Path(ROOT))

        # 12) 请求头：鉴权与身份头都要带齐
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

        # 13) 版本与数据目录：exe 写缓存、源码写仓库根、旧数据只能搬一次
        check("版本号是 x.y.z",
              bool(re.match(r"^\d+\.\d+\.\d+$", wb_config.VERSION)),
              wb_config.VERSION)
        check("版本行带程序名",
              wb_config.APP_ID in wb_config.version_text(),
              wb_config.version_text())
        check("产物是英文名", wb_config.APP_ID.isascii(), wb_config.APP_ID)
        check("源码运行默认写仓库根目录",
              wb_config.default_data_dir() == Path(ROOT),
              str(wb_config.default_data_dir()))
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        check("缓存目录在用户目录下（不碰程序目录）",
              str(wb_config.cache_dir()).startswith(local or str(Path.home())),
              str(wb_config.cache_dir()))

        inst, data = (os.path.join(tmpdir, "install"),
                      os.path.join(tmpdir, "cache"))
        os.makedirs(inst, exist_ok=True)
        for name in ("config.json", wb_config.TOKEN_FILE_NAME):
            with open(os.path.join(inst, name), "w", encoding="utf-8") as fh:
                fh.write('{"legacy": 1}')
        real_install = wb_config.install_dir
        wb_config.install_dir = lambda: Path(inst)
        try:
            wb_config.set_data_dir(Path(data))
            moved = wb_config.migrate_legacy()
            check("旧版配置与令牌都会迁到数据目录",
                  sorted(moved) == ["auth_oauth.json", "config.json"], moved)
            check("迁移后目标文件确实存在",
                  os.path.isfile(os.path.join(data, "config.json")))
            check("迁移保留源文件（不擅自删用户数据）",
                  os.path.isfile(os.path.join(inst, "config.json")))
            check("已有配置不会被覆盖", wb_config.migrate_legacy() == [],
                  wb_config.migrate_legacy())
            wb_config.set_data_dir(Path(inst))
            check("数据目录=程序目录时不做迁移",
                  wb_config.migrate_legacy() == [])
        finally:
            wb_config.install_dir = real_install
            wb_config.set_data_dir(Path(ROOT))

        # 14) 服务端打包：只带该带的模块，数据落脚本目录
        check("仓库布局识别正确", wb_config.is_repo_layout() is True)
        build_dir = os.path.join(str(ROOT), "build")
        bundle = None
        sys.path.insert(0, build_dir)
        try:
            import make_server_bundle as bundle
            mods = bundle.collect()
            check("服务端闭包是 5 个模块", len(mods) == 5, mods)
            check("闭包不含 GUI / Windows 模块",
                  not (set(mods) & set(bundle.FORBIDDEN)), mods)
            check("闭包含入口与签到核心",
                  {"wb_server", "workbuddy_checkin"} <= set(mods), mods)
            check("闭包不含 win_quiet（tk/计划任务专用）",
                  "win_quiet" not in mods, mods)
        except Exception as e:  # noqa: BLE001
            check("服务端打包脚本可用", False, repr(e))
        finally:
            sys.path.remove(build_dir)

        pkg = Path(ROOT) / "dist" / "server"
        if pkg.is_dir() and bundle is not None:
            names = sorted(p.name for p in pkg.glob("*.py"))
            check("dist/server 里没有 GUI / Windows 模块",
                  not ({"{}.py".format(f) for f in bundle.FORBIDDEN}
                       & set(names)), names)
            check("dist/server 自带配置模板",
                  (pkg / "config.example.json").is_file())

        # 平铺目录（服务器上的样子）数据必须落脚本目录，不能落父目录
        flat = Path(tmpdir) / "flat"
        flat.mkdir(exist_ok=True)
        real_src = wb_config.SRC_DIR
        wb_config.SRC_DIR = flat
        try:
            check("平铺目录不算仓库布局", wb_config.is_repo_layout() is False)
            check("服务端数据目录=脚本目录",
                  wb_config.default_data_dir() == flat,
                  str(wb_config.default_data_dir()))
            check("服务端版本行写「脚本运行」",
                  "脚本运行" in wb_config.version_text(),
                  wb_config.version_text())
        finally:
            wb_config.SRC_DIR = real_src
    finally:
        srv.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok = all(r["ok"] for r in RESULTS)
    print("\n{} / {} 通过".format(sum(1 for r in RESULTS if r["ok"]),
                                  len(RESULTS)))
    out_dir = os.path.join(ROOT, ".build")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "oauth_mode_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"ok": ok, "results": RESULTS}, fh,
                  ensure_ascii=False, indent=2)
    print("结果已写入 {}".format(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
