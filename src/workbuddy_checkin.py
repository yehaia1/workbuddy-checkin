#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到 —— 账号直连核心逻辑（零依赖）。

只有一条登录通道：**账号直连**
------------------------------
用 CodeBuddy CLI 的设备授权流程换一份明文令牌存在本机，之后纯 HTTP 调接口。
全程不启动、不依赖 WorkBuddy 客户端（细节见 wb_oauth.py）。

签到三步（幂等，重复执行安全）
------------------------------
    1. POST /v2/billing/meter/checkin-activity-status   # 查：今天签了吗
    2. POST /v2/billing/meter/daily-checkin             # 领（code=10001 已签）
    3. POST /v2/billing/meter/checkin-activity-status   # 复核：真签上了吗

用法
----
本地运行统一走仓库根目录的 `run.py`（打包后的 exe 参数完全一致）：

    python run.py                          # 图形界面
    python run.py --run                    # 无界面执行一次（计划任务用）
    python run.py --check-only
    python run.py --auth                   # 本机浏览器授权（界面唯一入口）
    python run.py --test-notify
    python run.py --diagnose
    python run.py --version
  以上均可加 --json 输出单行 JSON

服务端（定时脚本）不是这套参数，见 src/wb_server.py。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import wb_config
import wb_notify
import wb_oauth

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

STATUS_KEYS = ("active", "today_checked_in", "today_credit",
               "daily_credit", "streak_days", "total_credits", "end_time")
CLAIM_KEYS = ("credit", "credits", "today_credit", "streak_days",
              "total_credits", "message")

# 兼容旧名字：外部脚本/回归检查里还在用 core.DEFAULT_CONFIG
DEFAULT_CONFIG = wb_config.DEFAULT_CONFIG


class CheckinError(RuntimeError):
    def __init__(self, result: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.result = result
        self.exit_code = exit_code


# 授权模块的异常与 CheckinError 同构（都有 .result / .exit_code），统一捕获
SESSION_ERRORS = (CheckinError, wb_oauth.OAuthError)


# ---------------- 配置 ----------------

def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    return wb_config.load(path)


def oauth_token_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    return wb_oauth.token_path(cfg or {}, base_dir=str(wb_config.data_dir()))


# ---------------- 进度与日志 ----------------

def _progress(msg: str) -> None:
    """进度写 stderr：控制台可见，且不污染 --json 的 stdout。"""
    print("[checkin] {}".format(msg), file=sys.stderr, flush=True)


def _log_event(note: str) -> None:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(wb_config.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write('{} {{"result": "EVENT", "note": "{}"}}\n'.format(
                stamp, note.replace('"', "'")))
    except OSError:
        pass


def append_log(result: Dict[str, Any]) -> None:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(wb_config.LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(
                stamp, json.dumps(result, ensure_ascii=False)))
    except OSError:
        pass


# ---------------- 会话 ----------------

def open_session(cfg: Dict[str, Any],
                 progress: Optional[Any] = None) -> wb_oauth.OAuthSession:
    """建立账号直连会话（令牌缺失/失效时抛 OAuthError）。"""
    emit = progress or _progress
    emit("读取本机授权令牌…")
    return wb_oauth.OAuthSession(oauth_token_path(cfg), emit=emit)


# ---------------- 业务流程 ----------------

def run(sess: wb_oauth.OAuthSession, check_only: bool) -> Dict[str, Any]:
    # 1) 查状态
    status, body = sess.fetch("POST", STATUS_PATH)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if status != 200 or body.get("code") not in (0, None):
        raise CheckinError("API_ERROR",
                           "状态接口返回异常 HTTP {} code={}".format(
                               status, body.get("code")), 4)
    brief = {k: data[k] for k in STATUS_KEYS if k in data}

    if check_only:
        return {"result": "STATUS", **brief}
    if not data.get("active", False):
        return {"result": "INACTIVE", **brief}
    if data.get("today_checked_in", False):
        return {"result": "ALREADY_CLAIMED", **brief}

    # 2) 领取（幂等；code=10001 表示今天已领过）
    c_status, c_body = sess.fetch("POST", CLAIM_PATH)
    c_data = c_body.get("data") if isinstance(c_body.get("data"), dict) else {}
    if c_status >= 400 and c_body.get("code") == 10001:
        return {"result": "ALREADY_CLAIMED", **brief}
    if c_status >= 400 or c_body.get("code") not in (0, None):
        # 领取报错：复核一次，可能实际已领成功
        r_status, r_body = sess.fetch("POST", STATUS_PATH)
        r_data = r_body.get("data") if isinstance(r_body.get("data"), dict) else {}
        if r_data.get("today_checked_in", False):
            return {"result": "ALREADY_CLAIMED",
                    **{k: r_data[k] for k in STATUS_KEYS if k in r_data}}
        raise CheckinError("API_ERROR",
                           "领取接口返回 HTTP {} code={}".format(
                               c_status, c_body.get("code")), 4)

    # 3) 复核：确认真的签上了
    v_status, v_body = sess.fetch("POST", STATUS_PATH)
    v_data = v_body.get("data") if isinstance(v_body.get("data"), dict) else {}
    if not v_data.get("today_checked_in", False):
        raise CheckinError("VERIFY_FAILED", "领取返回成功，但状态复核未通过。", 4)

    return {
        "result": "CLAIMED",
        **{k: c_data[k] for k in CLAIM_KEYS if k in c_data},
        **{k: v_data[k] for k in STATUS_KEYS if k in v_data},
    }


# ---------------- 消息文案 ----------------

def build_message(result: Dict[str, Any]) -> str:
    """签到结果 -> 推送文案（各渠道共用）。"""
    code = result.get("result")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if code == "CLAIMED":
        amount = result.get("credit", result.get("credits",
                       result.get("today_credit", "?")))
        return ("【WorkBuddy 签到】✅ 签到成功\n"
                "本次领取：+{} 积分\n"
                "连续签到：{} 天｜本期累计：{} 积分\n"
                "时间：{}".format(amount, result.get("streak_days", "?"),
                                  result.get("total_credits", "?"), stamp))
    if code == "ALREADY_CLAIMED":
        return ("【WorkBuddy 签到】ℹ️ 今日已签到，无需重复领取\n"
                "连续签到：{} 天｜本期累计：{} 积分\n"
                "时间：{}".format(result.get("streak_days", "?"),
                                  result.get("total_credits", "?"), stamp))
    if code == "INACTIVE":
        return ("【WorkBuddy 签到】⏸ 签到活动已结束，脚本自动跳过\n"
                "时间：{}".format(stamp))
    return ("【WorkBuddy 签到】❌ 执行失败：{}\n"
            "详情：{}\n"
            "时间：{}".format(code, result.get("message", ""), stamp))


def build_auth_message(url: str, minutes: int) -> str:
    """授权链接的推送文案（服务端脚本用它把链接发到群里）。"""
    return ("【WorkBuddy 签到】🔐 请完成账号授权\n"
            "1) 点开下面这条链接（手机上点开即可）\n"
            "2) 登录并确认授权\n"
            "有效期约 {} 分钟。\n"
            "{}".format(minutes, url))


# ---------------- 授权到期提醒 ----------------

# 令牌剩余天数 <= 这个值时提醒（配置项 expiry_warn_days 可改，0 = 关闭）
EXPIRY_WARN_DAYS = 2


def build_expiry_message(days: int, expires_text: str = "",
                         nickname: str = "") -> str:
    """到期提醒文案。days 为负数表示已经过期。"""
    if days < 0:
        state = "授权已过期"
        action = ("现在签到会失败，请打开程序点「立即授权」重新登录一次。")
    else:
        state = "授权还剩 {} 天".format(days)
        action = ("到期后签到会失败。程序会在到期前 5 天自动续期，"
                  "收到这条提醒说明自动续期没有成功，"
                  "请打开程序点「立即授权」重新登录一次。")
    return ("【WorkBuddy 签到】⚠️ {}\n"
            "账号：{}\n"
            "令牌到期：{}\n"
            "{}".format(state, nickname or "当前账号",
                        expires_text or "未知", action))


def check_expiry_notice(cfg: Optional[Dict[str, Any]] = None,
                        progress: Optional[Any] = None,
                        notify: bool = True) -> Optional[Dict[str, Any]]:
    """令牌快到期就推一条提醒；不需要提醒时返回 None。

    挂在**每次签到之后**：签到每天本来就会跑一次，顺手看一眼不增加任何调度；
    而且令牌真挂了下次签到就会失败，这条提醒只是提前一步。

    只认「有令牌但不健康」这一种情况——没令牌属于未授权，由界面横幅报，
    不必再往群里推一条。
    """
    cfg = cfg or load_config()
    emit = progress or _progress
    try:
        days_warn = int(cfg.get("expiry_warn_days", EXPIRY_WARN_DAYS) or 0)
    except (TypeError, ValueError):
        days_warn = EXPIRY_WARN_DAYS
    if days_warn <= 0:
        return None
    info = wb_oauth.summary(oauth_token_path(cfg))
    if not info.get("present") or info.get("error") or info.get("disabled"):
        return None
    days = info.get("days_left")
    if days is None or days > days_warn:
        return None

    expires_text = info.get("expires_text") or ""
    text = build_expiry_message(days, expires_text, info.get("nickname") or "")
    emit("授权只剩 {} 天（{}），发送续期提醒".format(days, expires_text or "?"))
    pushed = wb_notify.push(cfg, text) if notify else []
    for name, _ok, detail in pushed:
        emit("{}：{}".format(name, detail))
    out: Dict[str, Any] = {"days_left": days, "expires_text": expires_text}
    if pushed:
        out["notify_results"] = [{"channel": n, "ok": ok, "detail": d}
                                 for n, ok, d in pushed]
    return out


def notify_result(cfg: Dict[str, Any], result: Dict[str, Any],
                  emit: Callable[[str], None]) -> List[Tuple[str, bool, str]]:
    """把结果推到所有已选渠道，并把每个渠道的成败写进日志。"""
    pushed = wb_notify.push(cfg, build_message(result))
    for name, ok, detail in pushed:
        emit("{}：{}".format(name, detail))
    if pushed:
        result["notify_results"] = [
            {"channel": n, "ok": ok, "detail": d} for n, ok, d in pushed]
    return pushed


# ---------------- 统一执行入口（GUI / 命令行共用） ----------------

def run_once(cfg: Optional[Dict[str, Any]] = None,
             check_only: bool = False,
             progress: Optional[Any] = None,
             notify: bool = True) -> Dict[str, Any]:
    """完整跑一次：连 → 查 → 领 → 复核 → 推送 → 到期检查。

    GUI 和命令行走同一条路径，避免两套实现慢慢走偏。
    """
    cfg = cfg or load_config()
    emit = progress or _progress

    def finish(res: Dict[str, Any]) -> Dict[str, Any]:
        append_log(res)
        if notify:
            notify_result(cfg, res, emit)
            notice = check_expiry_notice(cfg, emit, notify=True)
            if notice:
                res["expiry_notice"] = notice
        return res

    try:
        sess = open_session(cfg, progress=emit)
    except SESSION_ERRORS as e:
        return finish({"result": e.result, "message": str(e),
                       "exit_code": e.exit_code})

    try:
        with sess:
            result = run(sess, check_only=check_only)
    except SESSION_ERRORS as e:
        result = {"result": e.result, "message": str(e),
                  "exit_code": e.exit_code}
    return finish(result)


# ---------------- 授权 ----------------

def login_oauth(cfg: Optional[Dict[str, Any]] = None,
                progress: Optional[Any] = None,
                timeout: Optional[int] = None,
                on_url: Optional[Callable[[str], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None
                ) -> Dict[str, Any]:
    """走一遍设备授权，把令牌落到本机（本机浏览器，界面唯一入口）。

    远程授权（把链接推到手机）不在这里：那属于服务端流程，
    由 wb_server.py 自己完成，见 docs/server.md。

    on_url(url)   拿到链接的瞬间回调（界面靠它立刻把链接显示出来）
    should_stop() 返回 True 时中断等待（用户点了取消）
    """
    cfg = cfg or load_config()
    emit = progress or _progress
    path = oauth_token_path(cfg)
    timeout = int(timeout or cfg.get("oauth_auth_timeout", 300) or 300)

    try:
        rec = wb_oauth.login(path, emit=emit, open_browser=True,
                             timeout=timeout, on_url=on_url,
                             should_stop=should_stop)
    except wb_oauth.OAuthError as e:
        result = {"result": e.result, "message": str(e),
                  "exit_code": e.exit_code}
        append_log(result)
        return result

    acct = rec.get("account") or {}
    auth = rec.get("auth") or {}
    result = {
        "result": "AUTHORIZED",
        "uid": acct.get("uid") or "",
        "nickname": acct.get("nickname") or "",
        "domain": auth.get("domain") or "",
        "expires_at": auth.get("expiresAt") or 0,
        "token_file": path,
    }
    append_log({"result": "AUTHORIZED",
                "note": "uid={} domain={}".format(result["uid"],
                                                  result["domain"])})
    return result


def import_token(code_or_file: str,
                 cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """导入服务端授权拿到的令牌（导入码或令牌文件路径）。"""
    cfg = cfg or load_config()
    raw = str(code_or_file or "").strip()
    if os.path.isfile(raw):
        # 也接受一个令牌文件（服务端 --save-token 导出的那份，或别的机器
        # 拷过来的 auth_oauth.json）。文件里是 JSON，导入码是 base64，两者都要认。
        try:
            with open(raw, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            raise CheckinError("IMPORT_FAILED", "读取令牌文件失败：{}".format(e), 2)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if not str((parsed.get("auth") or {}).get("accessToken") or ""):
                raise CheckinError("IMPORT_FAILED",
                                   "这个文件里没有 accessToken。", 2)
            record = parsed
        else:
            record = wb_oauth.decode_code(text)
    else:
        record = wb_oauth.decode_code(raw)
    path = oauth_token_path(cfg)
    wb_oauth.save(record, path)
    acct = record.get("account") or {}
    result = {
        "result": "IMPORTED",
        "uid": acct.get("uid") or "",
        "nickname": acct.get("nickname") or "",
        "token_file": path,
    }
    append_log({"result": "IMPORTED", "note": "uid={}".format(result["uid"])})
    return result


# ---------------- 环境自检 ----------------

def detect_env(deep: bool = True,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """检查授权状态；deep=True 会真调一次状态接口。

    返回的 code：
      OK                  授权有效、接口可用
      OAUTH_NOT_AUTHORIZED  本机还没有令牌
      OAUTH_TOKEN_BAD     令牌文件损坏
      OAUTH_SESSION_DEAD  会话已失效，需要重新授权
      OAUTH_NETWORK       网络不通
      OAUTH_FAIL          其它接口异常
    """
    cfg = cfg or load_config()
    out: Dict[str, Any] = {"code": "OK", "message": "", "steps": []}
    path = oauth_token_path(cfg)
    info = wb_oauth.summary(path)
    out["oauth_token"] = info
    out["token_file"] = path

    if not info.get("present"):
        out["steps"].append(("账号授权", "未授权"))
        out.update(code="OAUTH_NOT_AUTHORIZED",
                   message="尚未完成账号授权，需要先登录一次。")
        return out
    if info.get("error"):
        out["steps"].append(("账号授权", "令牌文件异常"))
        out.update(code="OAUTH_TOKEN_BAD", message=str(info["error"]))
        return out

    who = info.get("nickname") or "未命名账号"
    out["steps"].append(
        ("账号授权", "{}（uid {}…）".format(who, str(info.get("uid") or "")[:8])))
    days = info.get("days_left")
    out["steps"].append(
        ("令牌有效期", "{}（剩 {} 天）".format(info.get("expires_text") or "未知",
                                         days if days is not None else "?")))
    if info.get("disabled"):
        out.update(code="OAUTH_SESSION_DEAD", message="登录会话已失效，请重新授权。")
        return out
    if not deep:
        return out

    try:
        with wb_oauth.OAuthSession(path) as sess:
            status, body = sess.fetch("POST", STATUS_PATH)
    except SESSION_ERRORS as e:
        if e.result in ("OAUTH_SESSION_DEAD", "OAUTH_TOKEN_BAD"):
            out.update(code="OAUTH_SESSION_DEAD", message=str(e))
        elif e.result == "OAUTH_NETWORK":
            out.update(code="OAUTH_NETWORK", message=str(e))
        else:
            out.update(code="OAUTH_FAIL", message="{}: {}".format(e.result, e))
        return out

    out["api_http"] = status
    out["api_code"] = body.get("code")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    out["status_data"] = {k: data[k] for k in STATUS_KEYS if k in data}
    if status in (401, 403):
        out.update(code="OAUTH_SESSION_DEAD",
                   message="接口拒绝访问，授权已失效，请重新授权。")
        return out
    if status >= 400:
        out.update(code="OAUTH_FAIL",
                   message="状态接口返回 HTTP {} code={}".format(
                       status, body.get("code")))
        return out
    out["steps"].append(
        ("签到接口", "HTTP {} code={}".format(status, body.get("code"))))
    return out


# ---------------- 诊断 ----------------

def diagnose(path: Optional[str] = None) -> Dict[str, Any]:
    """输出一份「为什么签到不了」的证据快照（不含令牌内容）。"""
    cfg = load_config(path)
    out: Dict[str, Any] = {
        "version": wb_config.VERSION,
        "run_mode": wb_config.run_mode(),
        "data_dir": str(wb_config.data_dir()),
        "install_dir": str(wb_config.install_dir()),
        "config_path": str(wb_config.config_path(path)),
        "push_channels": wb_notify.selected_channels(cfg),
        "token_file": oauth_token_path(cfg),
    }
    out["oauth_token"] = wb_oauth.summary(out["token_file"])
    try:
        with wb_oauth.OAuthSession(out["token_file"]) as sess:
            out["billing_base"] = wb_oauth.billing_base(sess.record)
            status, body = sess.fetch("POST", STATUS_PATH)
            out["status_api"] = {"http": status, "code": body.get("code")}
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            out["status_data"] = {k: data[k] for k in STATUS_KEYS if k in data}
            out["connected"] = True
    except SESSION_ERRORS as e:
        out["connected"] = False
        out["error"] = "{}: {}".format(e.result, e)
    except Exception as e:
        out["connected"] = False
        out["error"] = str(e)
    try:
        import wb_task
        out["schedule"] = wb_task.query_fast()
    except Exception:
        pass
    return out


# ---------------- 输出 ----------------

def render(result: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    code = result.get("result")
    if code == "CLAIMED":
        amount = result.get("credit", result.get("credits",
                       result.get("today_credit", "?")))
        print("✅ 签到成功：+{} 积分（连续 {} 天，本期累计 {}）".format(
            amount, result.get("streak_days", "?"),
            result.get("total_credits", "?")))
    elif code == "ALREADY_CLAIMED":
        print("ℹ️ 今日已签到，无需重复领取（连续 {} 天，本期累计 {}）".format(
            result.get("streak_days", "?"), result.get("total_credits", "?")))
    elif code == "STATUS":
        print("今日签到状态：{}，连续 {} 天，本期累计 {} 积分".format(
            "已签到" if result.get("today_checked_in") else "未签到",
            result.get("streak_days", "?"), result.get("total_credits", "?")))
    elif code == "INACTIVE":
        print("当前没有进行中的签到活动")
    elif code in ("AUTHORIZED", "IMPORTED"):
        stamp = ""
        if result.get("expires_at"):
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(int(result["expires_at"])))
        print("✅ {}：{}（uid {}）{}".format(
            "授权成功" if code == "AUTHORIZED" else "授权已导入",
            result.get("nickname") or "已登录账号",
            result.get("uid") or "?",
            "，令牌有效期至 " + stamp if stamp else ""))
    else:
        print(json.dumps(result, ensure_ascii=False))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WorkBuddy 每日签到（账号直连）")
    parser.add_argument("--check-only", action="store_true", help="仅查询今日签到状态")
    parser.add_argument("--diagnose", action="store_true", help="环境自检")
    parser.add_argument("--test-notify", action="store_true",
                        help="发送测试消息到已选推送渠道")
    parser.add_argument("--auth", action="store_true",
                        help="本机浏览器授权（界面唯一入口）")
    parser.add_argument("--import-token", metavar="CODE_OR_FILE",
                        help="导入授权结果（导入码或令牌文件），令牌迁移/救急用")
    parser.add_argument("--version", action="store_true", help="显示版本后退出")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON")
    # 无界面签到入口：计划任务调起时用
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    if not wb_config.is_prepared():
        wb_config.prepare()
    args = parse_args(argv)

    if args.version:
        text = wb_config.version_text()
        print(json.dumps({"version": wb_config.VERSION,
                          "run_mode": wb_config.run_mode(),
                          "data_dir": str(wb_config.data_dir())},
                         ensure_ascii=False) if args.json else text)
        return 0

    cfg = load_config()

    if args.test_notify:
        pushed = wb_notify.push(
            cfg, "【WorkBuddy 签到】🔧 测试消息：推送配置正确，链路正常。")
        if not pushed:
            detail = "没有可用的推送渠道（请在「推送」里选渠道并填 key）"
            print(detail if not args.json else json.dumps(
                {"ok": False, "detail": detail}, ensure_ascii=False))
            return 1
        if args.json:
            print(json.dumps(
                {"ok": all(ok for _, ok, _ in pushed),
                 "results": [{"channel": n, "ok": ok, "detail": d}
                             for n, ok, d in pushed]}, ensure_ascii=False))
        else:
            for name, _ok, detail in pushed:
                print("{}：{}".format(name, detail))
        return 0 if all(ok for _, ok, _ in pushed) else 1

    if args.diagnose:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return 0

    if args.auth:
        result = login_oauth(cfg, progress=_progress)
        render(result, args.json)
        return int(result.get("exit_code", 0))

    if args.import_token:
        try:
            result = import_token(args.import_token, cfg)
        except SESSION_ERRORS as e:
            result = {"result": e.result, "message": str(e),
                      "exit_code": e.exit_code}
            append_log(result)
        render(result, args.json)
        return int(result.get("exit_code", 0))

    # 走统一入口，和 GUI 完全同一条路径；只查状态时不推送，避免打扰
    result = run_once(cfg, check_only=args.check_only, progress=_progress,
                      notify=not args.check_only)
    render(result, args.json)
    return int(result.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
