#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 账号直连通道（OAuth 设备授权），零依赖、跨平台。

它是本项目**唯一**的登录通道（v2.0 起）：**用 CodeBuddy CLI 的设备授权
流程重新登录一次**，拿一份明文令牌，之后纯 HTTP 签到。全程不依赖
WorkBuddy 客户端进程，也不解密客户端的本地登录态。

设备授权的两个端点都是公共 HTTP 接口，`state` 只是一段服务端会话 id，
所以「在 A 机器生成链接、在 B 机器轮询拿令牌」在协议上是成立的——
服务端脚本（wb_auth_server.py）就是靠这一点工作的，见文件末尾的
「导入码」一节。

授权流程
--------
    1. POST /v2/plugin/auth/state?platform=CLI    → {state, authUrl}
    2. 浏览器打开 authUrl，用户确认登录（只需一次）
    3. GET  /v2/plugin/auth/token?state=<state>   → 轮询，code=0 拿到令牌
    4. 令牌落盘到 auth_oauth.json
    5. 之后每次运行：按需 refresh → 带 Bearer 直调 billing 接口

令牌续期
--------
accessToken 有效期约 28 天，refreshToken 约 30 天且**每次刷新都会轮换**，
所以刷新后必须立刻落盘。策略：过期前 5 天预刷新；调用遇 401 时刷新并重试一次。
refresh 也过期（服务端返回 12153）时，只能重新授权。

风险提示（不写在界面上，但文档必须保留）
--------------------------------------
官方对「脚本自动签到」持否定态度，检测到可能回收超额积分、严重时限制账号功能；
调用身份是 CLI 而非桌面端，服务端看到的风控特征不同。
使用与否请自行判断，风险自负。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

import wb_config

# ---- 端点 ----------------------------------------------------------------
# 授权服务是公共的（CN 区），与账号 domain 无关；billing 才按 domain 路由。
AUTH_STATE_URL = "https://copilot.tencent.com/v2/plugin/auth/state?platform=CLI"
AUTH_TOKEN_URL = "https://copilot.tencent.com/v2/plugin/auth/token?state="
REFRESH_URL_CN = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
REFRESH_URL_GLOBAL = "https://www.workbuddy.ai/v2/plugin/auth/token/refresh"

# 账号 domain 为空时的兜底（与客户端本地登录态里的 domain 保持一致）
DEFAULT_DOMAIN = "www.workbuddy.cn"

# 模拟 CLI 客户端身份（与社区实现一致；服务端据此判断调用方类型）
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

# 轮询返回码：11217 = 还没登录完；12153 = state 失效 / 会话已死
CODE_OK = 0
CODE_PENDING = 11217
CODE_SESSION_DEAD = 12153

# 会话失效的其他特征（服务端文案变化时兜底）
SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")

REFRESH_MARGIN = 5 * 24 * 3600   # 过期前 5 天预刷新（服务端可能提前撤销）
HTTP_TIMEOUT = 30
POLL_INTERVAL = 3

TOKEN_FILE_NAME = "auth_oauth.json"


class OAuthError(RuntimeError):
    """与主程序的 CheckinError 同构（.result / .exit_code / str()）。"""

    def __init__(self, result: str, message: str, exit_code: int = 3):
        super().__init__(message)
        self.result = result
        self.exit_code = exit_code


# ---- HTTP ----------------------------------------------------------------

def _request(method: str, url: str, headers: Optional[Dict[str, str]] = None,
             body: Any = None, timeout: int = HTTP_TIMEOUT
             ) -> Tuple[int, Dict[str, Any], str]:
    """发一个 JSON 请求，返回 (HTTP 状态码, 解析后的响应体, 原始文本)。

    不抛 HTTP 错误码异常——4xx/5xx 的响应体里通常带着业务 code，
    `code=12153` 这类关键信息只能从里面读。网络层失败才抛 OAuthError。
    """
    data = None
    m = (method or "GET").upper()
    if m in ("POST", "PUT", "PATCH"):
        data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=m)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", CLIENT_UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, str(v))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
    except Exception as e:
        raise OAuthError("OAUTH_NETWORK", "网络请求失败：{}".format(e), 3)
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        parsed = {}
    return status, (parsed if isinstance(parsed, dict) else {}), raw


# ---- 字段兼容 -------------------------------------------------------------
# 授权接口用 snake_case（access_token），刷新接口用 camelCase（accessToken），
# 两种都要认，否则换个接口就取不到值。

def _pick(d: Any, *names: str) -> Any:
    if not isinstance(d, dict):
        return None
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return v
    return None


def _jwt_payload(token: str) -> Dict[str, Any]:
    """解出 JWT 载荷（只读，不验签——仅用于取 uid / 昵称展示）。"""
    try:
        seg = str(token).split(".")[1]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg.encode("ascii"))
        out = json.loads(raw.decode("utf-8", "replace"))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _is_global(domain: str) -> bool:
    d = (domain or "").lower()
    return d == "workbuddy.ai" or d.endswith(".workbuddy.ai")


def billing_base(record: Dict[str, Any]) -> str:
    """billing 接口基址：优先用令牌里带的 domain。"""
    domain = str(_pick(record.get("auth") or {}, "domain") or "").strip()
    if not domain:
        domain = DEFAULT_DOMAIN
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain.rstrip("/")
    return "https://" + domain.rstrip("/")


def refresh_url(record: Dict[str, Any]) -> str:
    domain = str(_pick(record.get("auth") or {}, "domain") or "")
    return REFRESH_URL_GLOBAL if _is_global(domain) else REFRESH_URL_CN


# ---- 令牌记录 -------------------------------------------------------------

def token_path(cfg: Optional[Dict[str, Any]] = None,
               base_dir: Optional[str] = None) -> str:
    """令牌文件路径：配置项 oauth_token_file 优先，否则放程序目录。"""
    custom = str((cfg or {}).get("oauth_token_file") or "").strip()
    if custom:
        return os.path.abspath(os.path.expandvars(custom))
    return os.path.join(base_dir or str(wb_config.SCRIPT_DIR), TOKEN_FILE_NAME)


def build_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把授权/刷新响应统一成落盘格式。

    与社区脚本保持同构（{auth:{...}, account:{...}}），方便互相导导入。
    """
    access = str(_pick(payload, "accessToken", "access_token") or "")
    refresh = str(_pick(payload, "refreshToken", "refresh_token") or "")
    expires_in = _pick(payload, "expiresIn", "expires_in", "expires_at") or 0
    domain = str(_pick(payload, "domain", "billingDomain") or "")

    claims = _jwt_payload(access)
    account = {
        "uid": str(claims.get("uid") or claims.get("sub") or ""),
        "enterpriseId": str(claims.get("enterpriseId")
                            or claims.get("enterprise_id") or ""),
        "nickname": str(claims.get("nickname") or claims.get("name") or ""),
    }
    # 授权响应有时直接带账号信息，优先用它覆盖 JWT 里的
    for src in ("account", "user", "profile"):
        blob = payload.get(src)
        if isinstance(blob, dict):
            account["uid"] = str(blob.get("uid") or account["uid"])
            account["enterpriseId"] = str(
                blob.get("enterpriseId") or account["enterpriseId"])
            account["nickname"] = str(
                blob.get("nickname") or account["nickname"])

    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 0
    expires_at = int(time.time()) + expires_in if expires_in > 0 else 0

    return {
        "auth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires_at,
            "domain": domain,
        },
        "account": account,
        "disabled": False,
    }


def save(record: Dict[str, Any], path: str) -> None:
    """原子写回（先写临时文件再替换），避免刷新时断电导致令牌文件损坏。"""
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)   # POSIX 下收紧权限；Windows 上无效，忽略
    except OSError:
        pass


def load(path: str) -> Dict[str, Any]:
    """读取令牌；文件不存在或内容不完整时抛 OAUTH_NOT_LOGGED_IN。"""
    if not os.path.isfile(path):
        raise OAuthError(
            "OAUTH_NOT_LOGGED_IN",
            "尚未完成账号授权。请点「立即授权」完成一次浏览器登录。", 2)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise OAuthError("OAUTH_TOKEN_BAD", "令牌文件无法读取：{}".format(e), 2)
    if not isinstance(data, dict) or not str(
            _pick(data.get("auth") or {}, "accessToken") or ""):
        raise OAuthError("OAUTH_TOKEN_BAD",
                         "令牌文件缺少 accessToken，请重新授权。", 2)
    return data


def summary(path: str) -> Dict[str, Any]:
    """令牌概况（界面展示用，不返回令牌本身）。"""
    out: Dict[str, Any] = {"present": os.path.isfile(path), "path": path}
    try:
        data = load(path)
    except OAuthError as e:
        out["error"] = str(e)
        return out
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    expires_at = int(auth.get("expiresAt") or 0)
    out.update({
        "uid": acct.get("uid") or "",
        "nickname": acct.get("nickname") or "",
        "domain": auth.get("domain") or "",
        "expires_at": expires_at,
        "expires_text": (time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(expires_at))
                         if expires_at else ""),
        "days_left": (int((expires_at - time.time()) // 86400)
                      if expires_at else None),
        "expired": bool(expires_at and time.time() >= expires_at),
        "disabled": bool(data.get("disabled")),
    })
    return out


# ---- 导入码（服务端授权 -> 本机导入） -------------------------------------
#
# 服务端脚本拿到令牌后，用一段「导入码」把它交给 Windows 端：
#
#     WBAT1.<base64url(令牌 JSON)>
#
# 为什么是这种土办法：令牌是一次性凭证，必须**只落在真正执行签到的那台
# 机器上**。refreshToken 每次刷新都会轮换，两台机器各持一份就会互相踢掉
# （服务端返回 12153）。所以服务端拿完令牌**不落盘**，只把这串码交给运维，
# 由 Windows 端导入后独占。
#
# ⚠️ 导入码等同于令牌本身（只是 base64，不是加密）。别贴到群里、别进日志。

IMPORT_PREFIX = "WBAT1."


def export_code(record: Dict[str, Any]) -> str:
    """把令牌记录压成一行可粘贴的导入码。"""
    payload = json.dumps(record, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return IMPORT_PREFIX + base64.urlsafe_b64encode(
        payload).decode("ascii").rstrip("=")


def decode_code(code: str) -> Dict[str, Any]:
    """解析导入码；格式不对或没有 accessToken 时抛 OAUTH_IMPORT_BAD。"""
    s = str(code or "").strip()
    if s.startswith(IMPORT_PREFIX):
        s = s[len(IMPORT_PREFIX):]
    s = "".join(s.split())          # 容忍换行/空格（从聊天窗口复制常带）
    if not s:
        raise OAuthError("OAUTH_IMPORT_BAD", "导入码为空。", 2)
    pad = "=" * (-len(s) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(s + pad).decode("utf-8"))
    except Exception:
        raise OAuthError("OAUTH_IMPORT_BAD",
                         "导入码无法解析，可能被截断或复制不全。", 2)
    if not isinstance(data, dict) or not str(
            _pick(data.get("auth") or {}, "accessToken") or ""):
        raise OAuthError("OAUTH_IMPORT_BAD", "导入码里没有有效的令牌。", 2)
    return data


def save_code(code: str, path: str) -> Dict[str, Any]:
    """解析导入码并落盘（覆盖本机原令牌）。"""
    record = decode_code(code)
    record["disabled"] = False
    record.pop("note", None)
    save(record, path)
    return record


# ---- 授权流程 -------------------------------------------------------------

def begin_auth(timeout: int = HTTP_TIMEOUT) -> Dict[str, str]:
    """申请一个授权会话，返回 {state, auth_url}。"""
    status, body, raw = _request("POST", AUTH_STATE_URL, timeout=timeout)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if status == 200 and body.get("code") in (CODE_OK, None) and data.get("state"):
        return {"state": str(data.get("state")),
                "auth_url": str(data.get("authUrl") or data.get("auth_url") or "")}
    raise OAuthError(
        "OAUTH_STATE_FAILED",
        "申请授权失败：HTTP {} code={} {}".format(
            status, body.get("code"), (body.get("msg") or raw)[:80]), 3)


def poll_token(state: str, timeout: int = 900, interval: int = POLL_INTERVAL,
               on_tick: Optional[Callable[[int, int], None]] = None,
               should_stop: Optional[Callable[[], bool]] = None
               ) -> Dict[str, Any]:
    """等用户在浏览器里完成登录，拿到授权响应。

    on_tick(elapsed, remain) 用于界面刷进度；should_stop() 返回 True 时中断。
    返回原始授权响应体，调用方再用 build_record() 转成落盘格式。
    """
    total = max(30, int(timeout))
    deadline = time.time() + total
    started = time.time()
    url = AUTH_TOKEN_URL + state
    while time.time() < deadline:
        if should_stop and should_stop():
            raise OAuthError("OAUTH_CANCELLED", "已取消授权。", 2)
        status, body, raw = _request("GET", url, timeout=HTTP_TIMEOUT)
        code = body.get("code")
        if status == 200 and code == CODE_OK:
            data = body.get("data")
            if isinstance(data, dict) and _pick(data, "accessToken", "access_token"):
                return data
            raise OAuthError("OAUTH_BAD_RESPONSE",
                             "授权响应缺少令牌字段：{}".format(raw[:120]), 3)
        if code == CODE_SESSION_DEAD or status == 404:
            raise OAuthError("OAUTH_STATE_EXPIRED",
                             "授权会话已失效，请重新发起授权。", 2)
        # code=11217 或其它等待态：继续轮询
        if on_tick:
            on_tick(int(time.time() - started), int(deadline - time.time()))
        time.sleep(max(1, int(interval)))
    raise OAuthError("OAUTH_TIMEOUT",
                     "授权超时：{} 秒内未完成浏览器登录。".format(int(timeout)), 2)


def _open_browser(url: str) -> bool:
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def login(path: str, emit: Optional[Callable[[str], None]] = None,
          open_browser: bool = True, timeout: int = 900,
          should_stop: Optional[Callable[[], bool]] = None,
          on_tick: Optional[Callable[[int, int], None]] = None,
          on_url: Optional[Callable[[str], None]] = None
          ) -> Dict[str, Any]:
    """完整走一遍设备授权，成功后把令牌落盘并返回记录。

    on_url 在拿到授权链接的瞬间回调——界面要靠它立刻把链接显示出来，
    不必等 emit 的文案解析。
    """
    say = emit or (lambda _m: None)
    info = begin_auth()
    url = info["auth_url"]
    if on_url and url:
        on_url(url)
    say("请在浏览器完成登录：{}".format(url))
    if open_browser and url:
        say("已尝试自动打开浏览器" if _open_browser(url) else "自动打开失败，请手动复制链接")
    payload = poll_token(info["state"], timeout=timeout,
                         on_tick=on_tick, should_stop=should_stop)
    record = build_record(payload)
    if not record["auth"]["accessToken"]:
        raise OAuthError("OAUTH_BAD_RESPONSE", "授权未返回 accessToken。", 3)
    save(record, path)
    acct = record.get("account") or {}
    say("授权成功{}{}".format(
        "：" + acct["nickname"] if acct.get("nickname") else "",
        "（uid {}）".format(acct["uid"]) if acct.get("uid") else ""))
    return record


# ---- 令牌续期 -------------------------------------------------------------

def is_expired(record: Dict[str, Any], margin: int = REFRESH_MARGIN) -> bool:
    """是否已过期或即将过期（margin 为提前量秒数）。"""
    expires_at = int((record.get("auth") or {}).get("expiresAt") or 0)
    if not expires_at:
        return False        # 未知到期时间，不主动刷新，交给 401 触发
    return time.time() + margin > expires_at


def refresh(record: Dict[str, Any], path: str,
            emit: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """刷新令牌并落盘。失败抛 OAuthError（会话失效会标记 disabled）。"""
    say = emit or (lambda _m: None)
    token = str(_pick(record.get("auth") or {}, "refreshToken") or "")
    if not token:
        raise OAuthError("OAUTH_NO_REFRESH",
                         "令牌已过期且没有 refreshToken，请重新授权。", 2)
    acct = record.get("account") or {}
    headers = {
        "X-Refresh-Token": token,
        "X-Auth-Refresh-Source": "workbuddy",
    }
    if acct.get("enterpriseId"):
        headers["X-Enterprise-Id"] = str(acct["enterpriseId"])
    status, body, raw = _request("POST", refresh_url(record), headers=headers,
                                 body={})
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    code = body.get("code")
    msg = str(body.get("msg") or raw or "")

    dead = code == CODE_SESSION_DEAD or any(
        m in msg for m in SESSION_DEAD_MARKERS)
    if dead:
        record["disabled"] = True
        record["note"] = "session dead ({}), re-login required".format(code)
        save(record, path)
        raise OAuthError("OAUTH_SESSION_DEAD",
                         "登录会话已失效，请在设置里重新授权。", 2)

    payload = data or body
    access = str(_pick(payload, "accessToken", "access_token") or "")
    if status != 200 or not access:
        raise OAuthError(
            "OAUTH_REFRESH_FAILED",
            "令牌刷新失败：HTTP {} code={} {}".format(status, code, msg[:80]), 3)

    record["auth"]["accessToken"] = access
    new_refresh = str(_pick(payload, "refreshToken", "refresh_token") or "")
    if new_refresh:
        record["auth"]["refreshToken"] = new_refresh   # refreshToken 会轮换
    domain = str(_pick(payload, "domain") or "")
    if domain:
        record["auth"]["domain"] = domain
    expires_in = _pick(payload, "expiresIn", "expires_in")
    try:
        expires_in = int(expires_in or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        record["auth"]["expiresAt"] = int(time.time()) + expires_in
    record["disabled"] = False
    record.pop("note", None)
    save(record, path)
    say("令牌已刷新")
    return record


def ensure_valid(record: Dict[str, Any], path: str,
                 emit: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """快过期就刷新一次；刷新失败不中断（可能是网络抖动），交给调用重试。"""
    if not is_expired(record):
        return record
    say = emit or (lambda _m: None)
    say("令牌即将过期，正在续期…")
    try:
        return refresh(record, path, emit=say)
    except OAuthError as e:
        if e.result == "OAUTH_SESSION_DEAD":
            raise
        say("续期未成功（{}），继续尝试调用".format(e))
        return record


# ---- 会话对象（与 WBIPC 的 Session 同构） ---------------------------------

class OAuthSession:
    """把 OAuth 通道包装成 Session 的样子：只要 `fetch(method, path)`。

    业务层 run() 完全不知道有这层存在，两种模式共用同一套签到逻辑。
    """

    def __init__(self, path: str, emit: Optional[Callable[[str], None]] = None,
                 refresh_margin: int = REFRESH_MARGIN):
        self.path = path
        self.emit = emit or (lambda _m: None)
        self.record = load(path)
        if self.record.get("disabled"):
            raise OAuthError("OAUTH_SESSION_DEAD",
                             "登录会话已失效，请在设置里重新授权。", 2)
        if is_expired(self.record, margin=refresh_margin):
            self.record = ensure_valid(self.record, path, emit=self.emit)

    # -- 请求头 --
    def _headers(self) -> Dict[str, str]:
        auth = self.record.get("auth") or {}
        acct = self.record.get("account") or {}
        h = {"Authorization": "Bearer {}".format(auth.get("accessToken") or "")}
        if acct.get("uid"):
            h["X-User-Id"] = str(acct["uid"])
        if acct.get("enterpriseId"):
            h["X-Enterprise-Id"] = str(acct["enterpriseId"])
            h["X-Tenant-Id"] = str(acct["enterpriseId"])
        if auth.get("domain"):
            h["X-Domain"] = str(auth["domain"])
        return h

    def fetch(self, method: str, path: str,
              timeout: float = HTTP_TIMEOUT) -> Tuple[int, Dict[str, Any]]:
        """与 WBIPC Session.fetch 同签名：返回 (HTTP 状态码, 响应体)。"""
        url = billing_base(self.record) + path
        status, body, _raw = _request(method, url, headers=self._headers(),
                                      timeout=int(timeout))
        if self._needs_reauth(status, body):
            # 401 可能只是令牌被提前撤销：刷新后重试一次，仍失败才判定失效
            self.emit("接口拒绝访问，尝试续期后重试…")
            self.record = refresh(self.record, self.path, emit=self.emit)
            status, body, _raw = _request(method, url, headers=self._headers(),
                                          timeout=int(timeout))
            if self._needs_reauth(status, body):
                raise OAuthError("OAUTH_SESSION_DEAD",
                                 "授权已失效（HTTP {}），请重新授权。".format(status), 2)
        return status, body

    @staticmethod
    def _needs_reauth(status: int, body: Dict[str, Any]) -> bool:
        if status in (401, 403):
            return True
        code = body.get("code")
        return code in (401, 403, CODE_SESSION_DEAD)

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
