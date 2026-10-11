#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到（WBIPC 宿主通道版，零依赖）

功能：
  1. 通过 WorkBuddy 客户端自带的 WBIPC 宿主通道调用官方签到接口
     （脚本只提交「方法+路径」，鉴权头由客户端注入，全程不接触令牌）
  2. 支持 Windows 计划任务无人值守执行：
     - 宿主未运行时自动拉起 WorkBuddy 客户端，等待就绪后签到
     - 若客户端是本脚本拉起的，签到完成后自动关闭（原本已开的不动）
  3. 结果推送到企业微信 / 钉钉群机器人（只填机器人 key，地址程序自动拼）

签到流程三步（幂等，重复执行安全）：
    1. POST /v2/billing/meter/checkin-activity-status   # 查：今天签了吗
    2. POST /v2/billing/meter/daily-checkin             # 领（code=10001 表示已签）
    3. POST /v2/billing/meter/checkin-activity-status   # 复核：真的签上了吗

用法：
  python workbuddy_checkin.py                # 执行每日签到（幂等）
  python workbuddy_checkin.py --check-only   # 只查状态，不领取
  python workbuddy_checkin.py --diagnose     # 环境自检
  python workbuddy_checkin.py --test-notify  # 发测试消息到已配置渠道
  以上均可加 --json 输出单行 JSON

配置：同目录 config.json（推送机器人 key、自动拉起客户端等）。

参考：WBIPC 模块为客户端官方自带（skills/library/workbuddy_ipc），
流程参考社区实践 https://cloud.tencent.com.cn/developer/article/2755383
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from win_quiet import quiet_kwargs


def app_dir() -> Path:
    """程序所在目录。

    打包成单文件 exe 后，__file__ 指向 PyInstaller 的临时解包目录——
    配置和日志会写进临时目录，进程一退出就丢。所以冻结态必须用 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


SCRIPT_DIR = app_dir()
LOG_PATH = SCRIPT_DIR / "checkin.log"    # 只记录结果摘要，无任何敏感信息
CONFIG_PATH = SCRIPT_DIR / "config.json"

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
IPC_CLIENT_ID = "buddy-checkin"
IPC_TIMEOUT_S = 30.0

# workbuddy_ipc 各候选路径的导入失败原因 / 发现过程（排查用，成功时为空）
IPC_IMPORT_ERRORS: list = []
IPC_SEARCH_TRACE: list = []

# 推送渠道的 URL 前缀：用户只需在配置里填 key，程序自己拼完整地址
WECOM_API_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
WECOM_API_HOST = "qyapi.weixin.qq.com"
DINGTALK_API_PREFIX = "https://oapi.dingtalk.com/robot/send"
DINGTALK_API_HOST = "oapi.dingtalk.com"

STATUS_KEYS = ("active", "today_checked_in", "today_credit",
               "daily_credit", "streak_days", "total_credits", "end_time")
CLAIM_KEYS = ("credit", "credits", "today_credit", "streak_days",
              "total_credits", "message")

DEFAULT_CONFIG: Dict[str, Any] = {
    # 推送：只填 key 即可，程序自动拼完整 webhook 地址；留空表示不推送
    "wecom_key": "",                # 企业微信机器人 key（webhook 链接 ?key= 后面的值）
    "dingtalk_key": "",             # 钉钉机器人 access_token（链接 access_token= 后面的值）
    "wecom_webhook": "",            # 兼容旧配置：可直接填完整地址（优先于 wecom_key）
    "dingtalk_webhook": "",         # 兼容：可直接填完整钉钉地址（优先于 dingtalk_key）
    "launch_workbuddy": True,       # 宿主未运行时自动拉起客户端
    "workbuddy_exe": "",            # 留空 = 自动探测安装路径
    "ipc_dir": "",                  # 留空 = 自动探测；填 workbuddy_ipc 的父目录（特殊安装方式兜底）
    "launch_timeout": 120,          # 拉起后等待 IPC 就绪的最长秒数
    "hide_window": True,            # 拉起后把客户端主窗口隐藏（后台运行，不打断别的活）
    "hide_window_timeout": 25,      # 等待主窗口出现并隐藏的最长秒数
    "close_after_checkin": True,    # 客户端由脚本拉起时，签到完成后自动关闭
    "close_timeout": 30,            # 优雅关闭等待进程退出的最长秒数
    "close_force": True,            # 优雅关闭超时后强制结束进程树（清干净残留）
    "ui_scale": 0,                  # 界面缩放：0=跟随系统 DPI，否则为百分比(100/125/150)
    "auto_env_check": True,         # 启动时自动做一次环境自检
    # 定时签到：由界面写入 Windows 计划任务，程序自身不常驻（占用为 0）
    "schedule_enabled": False,      # 是否已启用每日定时签到
    "schedule_times": ["09:00"],    # 每日触发时间点（可多个，逗号分隔填写）
    "schedule_jitter": 15,          # 随机抖动分钟数，0 = 准点执行
    "exit_after_apply": True,       # 应用定时设置后自动关闭本程序
}


class CheckinError(RuntimeError):
    def __init__(self, result: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.result = result
        self.exit_code = exit_code


# ---------------- 配置 ----------------

def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update({k: v for k, v in user_cfg.items()
                            if k in DEFAULT_CONFIG})
        except (OSError, ValueError):
            pass  # 配置损坏时退回默认值
    return cfg


# ---------------- 客户端定位（安装路径发现） ----------------

def find_workbuddy_exe(cfg: Optional[Dict[str, Any]] = None) -> str:
    """定位 WorkBuddy 客户端可执行文件；找不到返回空串。

    顺序：配置里指定的 → 正在运行的进程镜像路径 → 注册表(Uninstall/App
    Paths) → 常见安装目录。这样用户换了安装位置也不用改配置。
    """
    preferred = ""
    if cfg:
        preferred = str(cfg.get("workbuddy_exe") or "")
    try:
        import wb_host
        return wb_host.find_workbuddy_exe(preferred)
    except Exception:
        pass
    # wb_host 不可用时的最小兜底
    if preferred and os.path.isfile(preferred):
        return preferred
    for cand in (glob.glob("C:/Program Files*/WorkBuddy/WorkBuddy.exe")
                 + glob.glob("D:/Program Files*/WorkBuddy/WorkBuddy.exe")):
        return cand
    return ""


def install_root(cfg: Optional[Dict[str, Any]] = None) -> str:
    """客户端安装根目录（资源/内置模块发现用）。"""
    exe = find_workbuddy_exe(cfg)
    return os.path.dirname(os.path.abspath(exe)) if exe else ""


def workbuddy_installed(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return bool(find_workbuddy_exe(cfg))


# ---------------- workbuddy_ipc 模块发现 ----------------
# 注意：计划任务/开机自启环境没有 CODEBUDDY_BUILTIN_SKILLS_DIR。
#
# 这里必须「约定路径 + 有界递归兜底」两条腿走路：模块的父目录布局在不同
# 安装方式/版本下并不一致（安装目录里是 .../workbuddy-builtin/skills/library，
# 用户插件缓存里是 <厂商>/<插件>/<版本>/…），只写死几条相对路径的话，
# 换一台机器就会出现"明明装了客户端却找不到模块"（IPC_MODULE_NOT_FOUND）。

IPC_PKG = "workbuddy_ipc"

# 递归扫描时跳过的目录名（纯缓存/版本库，里面不可能有模块）。
# 用显式黑名单而不是「跳过所有点号目录」——.workbuddy 本身就是关键位置。
_SKIP_DIRS = frozenset((
    "node_modules", "__pycache__", "Cache", "GPUCache", "Code Cache",
    "CachedData", "logs", "temp", "tmp",
    ".git", ".svn", ".hg", ".venv", "venv",
))


def is_ipc_parent(path: Any) -> bool:
    """path 是否是 workbuddy_ipc 包的父目录。"""
    if not path:
        return False
    return os.path.isfile(os.path.join(str(path), IPC_PKG, "__init__.py"))


def _scan_ipc_parents(base: Any, max_depth: int = 9,
                      max_entries: int = 200000) -> List[str]:
    """在 base 下有界广度优先搜索，返回所有 workbuddy_ipc 包的父目录。

    实测代价：<安装目录> 约 20ms、插件缓存约 30ms，可以忽略。

    命中后**继续下探**：同一个 base 下可能并存多种布局
    （例如 plugins/workbuddy-builtin/skills 与其 skills/library 都放了包），
    提前收工会把后者漏掉——这是踩过的坑。
    """
    out: List[str] = []
    if not base or not os.path.isdir(base):
        return out
    seen = 0
    frontier = [(os.path.abspath(str(base)), 0)]
    while frontier and seen < max_entries:
        nxt = []
        for cur, depth in frontier:
            if is_ipc_parent(cur):
                out.append(cur)
            if depth >= max_depth:
                continue
            try:
                entries = os.scandir(cur)
            except OSError:
                continue
            with entries:
                for ent in entries:
                    seen += 1
                    if seen >= max_entries:
                        break
                    try:
                        if not ent.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if ent.name in _SKIP_DIRS:
                        continue
                    nxt.append((ent.path, depth + 1))
        frontier = nxt
    return out


def _install_dir_guesses() -> List[str]:
    """可能装 WorkBuddy 的目录（只保留真实存在的）。

    与 find_workbuddy_exe 的区别：这里不要求一定能找到 WorkBuddy.exe，
    目的是即使 exe 探测失败，也能靠目录约定把模块找回来。
    """
    cands: List[str] = []
    bases = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    local = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    if local:
        bases += [os.path.join(local, "Programs"), local]
    if appdata:
        bases.append(appdata)
    for base in bases:
        if not base:
            continue
        cands.append(os.path.join(base, "WorkBuddy"))
        cands.extend(sorted(glob.glob(os.path.join(base, "*", "WorkBuddy"))))
    for drv in ("C:", "D:", "E:", "F:", "G:"):
        for pf in ("Program Files", "Program Files (x86)"):
            cands.append(drv + "\\" + pf + "\\WorkBuddy")
        cands.append(drv + "\\WorkBuddy")

    out: List[str] = []
    seen = set()
    for c in cands:
        key = os.path.normcase(c)
        if key in seen or not os.path.isdir(c):
            continue
        seen.add(key)
        out.append(c)
    return out


# module_roots 的进程级缓存：同一次运行里目录不会变，没必要反复扫盘
# （安装目录 + 插件缓存的有界递归约 160ms，一启动就被 load_ipc 和
#  detect_env 各要一次）。key 用配置里的 ipc_dir，它是唯一能改变候选集合
# 的配置项；排查用的 probe_module_roots 会强制实扫，不吃缓存。
_MODULE_ROOTS_CACHE: Dict[str, List[str]] = {}


def module_roots(cfg: Optional[Dict[str, Any]] = None,
                 refresh: bool = False) -> List[str]:
    """workbuddy_ipc 可能的所在目录（按可信度排序）。"""
    cfg = load_config() if cfg is None else cfg
    key = str((cfg or {}).get("ipc_dir") or "")
    if not refresh and key in _MODULE_ROOTS_CACHE:
        return list(_MODULE_ROOTS_CACHE[key])
    roots = _compute_module_roots(cfg)
    _MODULE_ROOTS_CACHE[key] = list(roots)
    return roots


def _compute_module_roots(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """实际执行的探测（不要在热路径上直接调它，走 module_roots 吃缓存）。"""
    roots: List[str] = []
    seen = set()

    def add(path: Any) -> None:
        if not path:
            return
        try:
            p = os.path.abspath(
                os.path.expandvars(str(path).strip().strip('"')))
        except Exception:
            return
        key = os.path.normcase(p)
        if key in seen:
            return
        seen.add(key)
        roots.append(p)

    # 1) 配置里手工指定的（最高优先级，用来兜住探测不到的安装方式）
    add((cfg or {}).get("ipc_dir"))

    # 2) 环境变量
    for var in ("CODEBUDDY_BUILTIN_SKILLS_DIR", "WORKBUDDY_SKILLS_DIR"):
        add((os.environ.get(var) or "").strip())

    install = install_root(cfg)
    tail = ("resources", "app.asar.unpacked", "resources", "plugins",
            "workbuddy-builtin")

    # 3) 安装目录内的约定路径（命中最快，放前面）
    for base in ([install] if install else []) + _install_dir_guesses():
        builtin = os.path.join(base, *tail)
        for sub in ("skills/library", "skills", "resources/skills"):
            add(os.path.join(builtin, *sub.split("/")))
        add(builtin)

    # 4) 用户级插件缓存：实测存在多种布局，逐个 glob
    cache = os.path.join(os.path.expanduser("~"), ".workbuddy",
                         "plugins", "cache")
    for pat in ("workbuddy-builtin/skill-library/*",
                "workbuddy-builtin/skill-library/*/skills/library",
                "workbuddy-builtin/*/skill-library/*",
                "workbuddy-builtin/*/*/skills/library",
                "*/*/skills/library",
                "*/*/*/skills/library"):
        for hit in glob.glob(os.path.join(cache, *pat.split("/"))):
            add(hit)

    # 5) 有界递归兜底：上面所有约定路径都落空时，靠它把模块捞回来。
    #    放在最后，是为了让可信度高的约定路径排在前面。
    scan_bases: List[str] = []
    if install:
        scan_bases.append(install)
    scan_bases.extend(_install_dir_guesses())
    scan_bases.append(cache)
    scan_bases.append(os.path.join(os.path.expanduser("~"), ".workbuddy",
                                   "skills"))
    for base in scan_bases:
        for hit in _scan_ipc_parents(base):
            add(hit)

    return roots


def probe_module_roots(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """候选目录逐条体检（自检/排查用）。

    强制重新扫描（refresh=True）：排查时若吃了缓存，现场刚改的东西看不到。
    """
    out = []
    for r in module_roots(cfg, refresh=True):
        out.append({
            "path": r,
            "exists": os.path.isdir(r),
            "has_package": is_ipc_parent(r),
        })
    return out


def load_ipc(cfg: Optional[Dict[str, Any]] = None) -> Tuple[Any, str]:
    del IPC_IMPORT_ERRORS[:]
    del IPC_SEARCH_TRACE[:]
    roots = module_roots(cfg)
    for root in roots:
        if not os.path.isdir(os.path.join(root, IPC_PKG)):
            continue
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            import workbuddy_ipc
            return workbuddy_ipc, root
        except Exception as e:
            IPC_IMPORT_ERRORS.append(
                "{} -> {}: {}".format(root, type(e).__name__, e))
            try:
                sys.path.remove(root)
            except ValueError:
                pass
            sys.modules.pop("workbuddy_ipc", None)

    install = install_root(cfg)
    IPC_SEARCH_TRACE.append("候选目录 {} 个".format(len(roots)))
    IPC_SEARCH_TRACE.append("客户端安装目录 {}".format(
        install or "未检测到"))
    # 发现过程的细节只写进日志 / selftest.json：界面上摆一句"候选目录 11 个"
    # 对使用者没有任何意义，反而把真正的建议挤掉了。
    _log_event("内置模块未找到：{}".format("；".join(IPC_SEARCH_TRACE)))
    if IPC_IMPORT_ERRORS:
        _log_event("内置模块导入失败：{}".format(
            " | ".join(IPC_IMPORT_ERRORS[:3])))
    raise CheckinError(
        "IPC_MODULE_NOT_FOUND",
        "未找到内置模块 workbuddy_ipc。",
        2,
    )


# ---------------- WBIPC 会话 ----------------

class Session:
    """单连接内完成多次调用（一次握手，多次 invoke）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.ipc, self.module_path = load_ipc(cfg)
        try:
            self.client = self.ipc.connect(
                client_id=IPC_CLIENT_ID, client_kind="skill")
        except Exception as e:
            raise self._connect_error(e)

    @staticmethod
    def _connect_error(e: Exception) -> CheckinError:
        name = type(e).__name__
        msg = getattr(e, "message", None) or str(e)
        if name == "NotWorkBuddyEnv":
            return CheckinError(
                "NOT_IN_WORKBUDDY",
                "未连接到 WorkBuddy 宿主通道（客户端未运行或未登录）。", 2)
        if name == "EndpointUntrusted":
            return CheckinError(
                "ENDPOINT_UNTRUSTED",
                "宿主 IPC 端点无法自证身份，已中止。", 2)
        if name == "ConsentRequired":
            return CheckinError(
                "CONSENT_REQUIRED",
                "需要在 WorkBuddy 客户端完成授权后重试。", 2)
        return CheckinError("IPC_CONNECT_FAILED", "WBIPC 连接失败：{}".format(msg), 3)

    def fetch(self, method: str, path: str,
              timeout: float = IPC_TIMEOUT_S) -> Tuple[int, Dict[str, Any]]:
        try:
            pipe = self.client.get_pipe("wb.request")
            if "http.fetch" not in (getattr(pipe, "methods", None) or []):
                raise CheckinError(
                    "METHOD_UNAVAILABLE",
                    "宿主未开放 http.fetch 方法；请升级 WorkBuddy 客户端。", 2)
            result = pipe.invoke("http.fetch", {
                "method": method.upper(),
                "path": path,
            }, timeout=timeout)
        except CheckinError:
            raise
        except Exception as e:
            raise CheckinError("IPC_ERROR", "WBIPC 调用失败：{}".format(
                getattr(e, "message", None) or e), 3)
        if not isinstance(result, dict) or "status" not in result:
            raise CheckinError("INVALID_RESPONSE", "宿主返回格式异常。", 3)
        status = int(result.get("status", 0))
        body: Dict[str, Any] = {}
        b64 = result.get("body_b64")
        if b64:
            try:
                body = json.loads(base64.b64decode(b64).decode("utf-8"))
            except Exception:
                body = {}
        return status, body if isinstance(body, dict) else {}

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------- 宿主自动拉起 ----------------

def _progress(msg: str) -> None:
    """进度信息输出到 stderr：控制台可见，且不污染 --json 的 stdout。"""
    print("[checkin] {}".format(msg), file=sys.stderr, flush=True)


def _log_event(note: str) -> None:
    """关键事件写入日志（拉起/关闭等，便于事后排查）。"""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("{} {{\"result\": \"EVENT\", \"note\": \"{}\"}}\n".format(
                stamp, note.replace('"', "'")))
    except OSError:
        pass


def workbuddy_process_running() -> bool:
    """客户端进程是否在运行。

    优先走 wb_host 的 Win32 快照（打包成无控制台 exe 后最可靠），
    tasklist 仅作兜底。
    """
    try:
        import wb_host
        if wb_host.pids_of():
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL, **quiet_kwargs()).stdout
        return "WorkBuddy.exe" in (out or "")
    except Exception:
        return False  # 检测失败按未运行处理，交给重试逻辑兜底


def launch_workbuddy(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """拉起 WorkBuddy 客户端。返回 (是否成功, 说明)。

    用 os.startfile（资源管理器双击等效）而非 Popen：单实例转发、
    shell 环境等行为与用户手动启动完全一致，兼容性最好。
    """
    exe = find_workbuddy_exe(cfg)
    if not exe or not Path(exe).exists():
        return False, ("未找到 WorkBuddy.exe：客户端可能未安装，"
                       "或在设置里手动指定路径。")
    try:
        os.startfile(exe)  # noqa: 与双击启动等效
        return True, "已启动：" + exe
    except OSError as e:
        return False, "启动失败：{}".format(e)


def kill_workbuddy(force: bool) -> None:
    """结束 WorkBuddy 进程树。force=False 为优雅关闭，True 为强制。"""
    args = ["taskkill", "/IM", "WorkBuddy.exe", "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=20,
                       **quiet_kwargs())
    except Exception:
        pass


def wait_process_exit(timeout: int) -> bool:
    """等待 WorkBuddy 进程全部退出。返回是否已全部退出。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not workbuddy_process_running():
            return True
        time.sleep(2)
    return not workbuddy_process_running()


def close_workbuddy(cfg: Dict[str, Any]) -> str:
    """关闭 WorkBuddy 客户端（仅在本脚本自行拉起且签到成功时调用）。

    两段式：先优雅关闭（WM_CLOSE），超时后强制结束进程树——
    强杀可能只留下后台残留（无窗口、无 IPC 通道），反而导致下次
    无法拉起，所以必须清干净。返回结果说明文本。
    """
    kill_workbuddy(force=False)
    if wait_process_exit(int(cfg.get("close_timeout", 30))):
        return "WorkBuddy 已自动关闭（优雅退出）"
    if not cfg.get("close_force", True):
        return "优雅关闭超时且未启用强杀，WorkBuddy 仍在运行"
    kill_workbuddy(force=True)
    if wait_process_exit(15):
        return "WorkBuddy 已自动关闭（优雅超时后强制结束）"
    return "强制关闭后仍有进程残留，请手动检查"


def _endpoint_ready(ipc: Any) -> bool:
    """宿主 IPC 发现文件是否存在（存在通常意味着宿主通道可用）。"""
    try:
        path = ipc.discovery_path()
    except Exception:
        return False
    return bool(path and os.path.exists(path))


def hide_host_window(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """把客户端主窗口隐藏起来（进程继续跑，IPC 通道不受影响）。

    桌面端没有官方静默启动参数，只能在拉起后调 Win32 ShowWindow(SW_HIDE)。
    隐藏失败不算致命：日志记一笔，退回"窗口可见"继续签到。
    """
    if not cfg.get("hide_window", True):
        return False, "未启用（config.hide_window=false）"
    try:
        import wb_host
    except Exception as e:  # 理论上不会发生，兜底不影响主流程
        return False, "隐藏模块不可用：{}".format(e)
    return wb_host.hide_host_window(
        timeout=float(cfg.get("hide_window_timeout", 25)))


def connect_with_autolaunch(cfg: Dict[str, Any],
                            progress: Optional[Any] = None
                            ) -> Tuple[Session, bool]:
    """连接宿主；未就绪时按配置自动拉起客户端并轮询等待。

    返回 (session, launched)。launched=True 表示客户端是本脚本
    拉起的（签到成功后可按配置自动关闭）；原本已运行则 False。
    """
    emit = progress or _progress
    try:
        return Session(cfg), False
    except CheckinError as e:
        if not cfg.get("launch_workbuddy", True):
            raise
        if e.result not in ("NOT_IN_WORKBUDDY", "IPC_CONNECT_FAILED"):
            raise  # 授权/端点问题，拉起客户端也解决不了

    timeout = int(cfg.get("launch_timeout", 120))

    # 残留进程恢复：有 WorkBuddy 进程却没有 IPC 通道 = 后台残留
    # （单实例锁会让新拉起的实例立即退出，导致永远等不到就绪）。
    # 先清理残留，再走正常拉起。
    ipc, _ = load_ipc(cfg)
    if workbuddy_process_running() and not _endpoint_ready(ipc):
        emit("检测到 WorkBuddy 后台残留进程（无宿主通道），"
             "先结束残留进程再重新拉起...")
        _log_event("zombie cleanup: killing leftover processes")
        kill_workbuddy(force=True)
        if not wait_process_exit(15):
            raise CheckinError(
                "ZOMBIE_CLEANUP_FAILED",
                "后台残留进程清理失败，请手动在任务管理器结束"
                " WorkBuddy.exe 后重试。", 2)
        time.sleep(3)

    launched = False
    if not workbuddy_process_running():
        emit("WorkBuddy 未在运行，正在自动拉起客户端...")
        ok, why = launch_workbuddy(cfg)
        if not ok:
            _log_event("launch failed: " + why)
            raise CheckinError("LAUNCH_FAILED",
                               "自动拉起失败：{}".format(why), 2)
        launched = True
        if cfg.get("hide_window", True):
            emit("客户端已启动，正在隐藏窗口（后台运行，最长 {} 秒）...".format(
                int(cfg.get("hide_window_timeout", 25))))
            hid, note = hide_host_window(cfg)
            emit("窗口隐藏：{}".format(note))
            _log_event("hide window: {} -> {}".format(hid, note))
        emit("等待宿主通道就绪（最长 {} 秒）...".format(timeout))
        _log_event("launched workbuddy, waiting for IPC ready")

    # 等待循环：宿主就绪即返回；进程意外退出时自动重拉一次
    deadline = time.time() + timeout
    last_err: Optional[CheckinError] = None
    next_hint = time.time() + 15
    relaunched = 0
    while time.time() < deadline:
        time.sleep(5)
        if not workbuddy_process_running():
            if launched and relaunched < 1:
                relaunched += 1
                emit("客户端进程意外退出，重新拉起（第 {} 次）...".format(
                    relaunched))
                _log_event("relaunch #{}".format(relaunched))
                launch_workbuddy(cfg)
                if cfg.get("hide_window", True):
                    hide_host_window(cfg)
            elif not launched:
                err = CheckinError(
                    "APP_CLOSED", "WorkBuddy 进程在等待期间退出了。", 2)
                err.launched = False
                raise err
        try:
            sess = Session(cfg)
        except CheckinError as e:
            last_err = e
            if time.time() >= next_hint:
                emit("宿主尚未就绪（剩余 {} 秒），继续等待...".format(
                    int(deadline - time.time())))
                next_hint = time.time() + 15
            continue
        emit("宿主通道已就绪")
        return sess, launched
    err = last_err or CheckinError(
        "LAUNCH_TIMEOUT", "等待 WorkBuddy 宿主通道就绪超时。", 2)
    err.launched = launched  # 标记客户端是脚本拉起的，调用方结束后需关闭
    raise err


# ---------------- 业务流程 ----------------

def run(sess: Session, check_only: bool) -> Dict[str, Any]:
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


# ---------------- 消息推送（企业微信 / 钉钉） ----------------
#
# 用户只需填机器人 key（不是整条链接），地址由程序拼。同时兼容两种历史
# 写法：既接受裸 key，也接受 "key=xxx" 片段或直接粘整条 webhook 链接，
# 容错解析后仍然只往**官方域名**推送，避免 key 被引到第三方地址。

def _extract_key(raw: Any, param: str) -> str:
    """从用户输入里抽出 key。

    接受三种写法：① 直接填 key；② 填 "key=xxx"；③ 直接粘整条链接。
    看着像链接却抽不出参数时返回空串（不硬拼，避免误推到错误地址）。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.search(r"[?&]" + re.escape(param) + r"=([^&\s#]+)", s)
    if m:
        return m.group(1).strip()
    m = re.match(r"^" + re.escape(param) + r"=([^&\s#]+)", s)
    if m:
        return m.group(1).strip()
    if "://" in s or "/" in s or "?" in s:
        return ""
    return s


def wecom_key_of(cfg: Dict[str, Any]) -> str:
    """取出企业微信机器人 key（兼容旧配置里填整条链接的情况）。"""
    return (_extract_key(cfg.get("wecom_key"), "key")
            or _extract_key(cfg.get("wecom_webhook"), "key"))


def dingtalk_key_of(cfg: Dict[str, Any]) -> str:
    """取出钉钉机器人 access_token（兼容旧配置里填整条链接的情况）。"""
    return (_extract_key(cfg.get("dingtalk_key"), "access_token")
            or _extract_key(cfg.get("dingtalk_webhook"), "access_token"))


def wecom_resolve_url(cfg: Dict[str, Any]) -> str:
    """拼出企业微信机器人完整 webhook；未配置/非法时返回空串。"""
    raw = str(cfg.get("wecom_webhook") or "").strip()
    if raw.startswith(WECOM_API_PREFIX):
        return raw
    key = _extract_key(raw, "key") or _extract_key(cfg.get("wecom_key"), "key")
    if not key or key.lower().startswith("http"):
        return ""
    return WECOM_API_PREFIX + "?key=" + key


def dingtalk_resolve_url(cfg: Dict[str, Any]) -> str:
    """拼出钉钉机器人完整 webhook；未配置/非法时返回空串。"""
    raw = str(cfg.get("dingtalk_webhook") or "").strip()
    if raw.startswith(DINGTALK_API_PREFIX):
        return raw
    token = (_extract_key(raw, "access_token")
             or _extract_key(cfg.get("dingtalk_key"), "access_token"))
    if not token or token.lower().startswith("http"):
        return ""
    return DINGTALK_API_PREFIX + "?access_token=" + token


def _post_json(url: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, "推送失败：{}".format(e)
    # 两家返回体同构：errcode=0 表示成功
    if body.get("errcode") == 0:
        return True, "推送成功"
    return False, "推送失败：errcode={} {}".format(
        body.get("errcode"), body.get("errmsg"))


def wecom_push(cfg: Dict[str, Any], text: str) -> Tuple[bool, str]:
    """推送文本消息到企业微信群机器人。返回 (是否成功, 说明)。"""
    url = wecom_resolve_url(cfg)
    if not url:
        return False, "未配置企业微信机器人 key"
    return _post_json(url, {"msgtype": "text", "text": {"content": text}})


def dingtalk_push(cfg: Dict[str, Any], text: str) -> Tuple[bool, str]:
    """推送文本消息到钉钉群机器人。返回 (是否成功, 说明)。"""
    url = dingtalk_resolve_url(cfg)
    if not url:
        return False, "未配置钉钉机器人 access_token"
    return _post_json(url, {"msgtype": "text", "text": {"content": text}})


# (渠道名, 地址解析函数, 推送函数)
PUSH_CHANNELS: Tuple[Tuple[str, Callable[[Dict[str, Any]], str],
                           Callable[[Dict[str, Any], str], Tuple[bool, str]]],
                     ...] = (
    ("企业微信", wecom_resolve_url, wecom_push),
    ("钉钉", dingtalk_resolve_url, dingtalk_push),
)


def configured_channels(cfg: Dict[str, Any]) -> List[str]:
    """返回已配置好 key 的推送渠道名列表。"""
    return [name for name, resolve, _ in PUSH_CHANNELS if resolve(cfg)]


def push_all(cfg: Dict[str, Any],
             text: str) -> List[Tuple[str, bool, str]]:
    """向所有已配置渠道推送，未配置的自动跳过。

    返回 [(渠道名, 是否成功, 说明)]；一个渠道失败不影响其它渠道。
    """
    out: List[Tuple[str, bool, str]] = []
    for name, resolve, fn in PUSH_CHANNELS:
        if not resolve(cfg):
            continue
        ok, detail = fn(cfg, text)
        out.append((name, ok, detail))
    return out


def _notify_all(cfg: Dict[str, Any], result: Dict[str, Any],
                emit: Callable[[str], None]) -> List[Tuple[str, bool, str]]:
    """把签到结果推送到所有已配置渠道，并把每个渠道的成败写进运行日志。"""
    pushed = push_all(cfg, build_message(result))
    for name, ok, detail in pushed:
        emit("{}：{}".format(name, detail))
    if pushed:
        result["notify_results"] = [
            {"channel": n, "ok": ok, "detail": d} for n, ok, d in pushed]
    return pushed


def build_message(result: Dict[str, Any]) -> str:
    """把签到结果转成推送消息文本（各渠道共用同一份文案）。"""
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
    # 失败场景
    return ("【WorkBuddy 签到】❌ 执行失败：{}\n"
            "详情：{}\n"
            "时间：{}".format(code, result.get("message", ""), stamp))


# ---------------- 诊断 ----------------

def diagnose() -> Dict[str, Any]:
    cfg = load_config()
    out: Dict[str, Any] = {}
    out["client_exe"] = find_workbuddy_exe(cfg) or None
    out["install_root"] = install_root(cfg) or None
    out["module_roots"] = probe_module_roots(cfg)
    try:
        ipc, path = load_ipc(cfg)
        out["ipc_module"] = path
    except CheckinError as e:
        out["ipc_module"] = None
        out["error"] = str(e)
        out["ipc_import_errors"] = list(IPC_IMPORT_ERRORS)
        return out
    endpoint = getattr(ipc, "discovery_path", lambda: None)()
    out["endpoint_file"] = endpoint
    out["endpoint_exists"] = bool(endpoint and os.path.exists(endpoint))
    out["workbuddy_running"] = workbuddy_process_running()
    out["notify_channels"] = configured_channels(cfg)
    if not out["endpoint_exists"]:
        out["error"] = "wbipc endpoint.json 不存在（WorkBuddy 未运行？）"
        return out
    try:
        with Session(cfg) as sess:
            pipe = sess.client.get_pipe("wb.request")
            out["connected"] = True
            out["methods"] = list(getattr(pipe, "methods", None) or [])
            status, body = sess.fetch("POST", STATUS_PATH)
            out["status_api"] = {"http": status, "code": body.get("code")}
    except CheckinError as e:
        out["connected"] = False
        out["error"] = "{}: {}".format(e.result, e)
    except Exception as e:
        out["connected"] = False
        out["error"] = str(e)
    return out


# ---------------- 统一执行入口（GUI / 命令行共用） ----------------

def run_once(cfg: Optional[Dict[str, Any]] = None,
             check_only: bool = False,
             progress: Optional[Any] = None,
             notify: bool = True) -> Dict[str, Any]:
    """完整跑一次流程：连接 → 查 → 领 → 复核 → 推送 → 按需关闭客户端。

    GUI 和命令行走同一条路径，避免两套实现慢慢走偏。
    """
    cfg = cfg or load_config()
    emit = progress or _progress
    launched = False

    def finish(result: Dict[str, Any],
               err: Optional[CheckinError] = None) -> Dict[str, Any]:
        """客户端是本次拉起的 → 结束后关掉（只查状态时不动它）。"""
        if not (launched or getattr(err, "launched", False)):
            return result
        if check_only or not cfg.get("close_after_checkin", True):
            return result
        note = close_workbuddy(cfg)
        emit(note)
        append_log({"result": "HOST_CLOSED", "note": note})
        result["host_closed"] = note
        return result

    try:
        sess, launched = connect_with_autolaunch(cfg, progress=emit)
    except CheckinError as e:
        result = {"result": e.result, "message": str(e),
                  "exit_code": e.exit_code}
        append_log(result)
        if notify:
            _notify_all(cfg, result, emit)
        return finish(result, e)

    try:
        with sess:
            result = run(sess, check_only=check_only)
    except CheckinError as e:
        result = {"result": e.result, "message": str(e),
                  "exit_code": e.exit_code}

    append_log(result)
    if notify:
        _notify_all(cfg, result, emit)
    return finish(result)


# ---------------- 环境自检（存在 + 已登录） ----------------

def detect_env(deep: bool = True,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """检测客户端是否在跑、是否已登录、宿主通道是否通。

    deep=True 会真连一次 IPC 并调状态接口——这是唯一能确认"已登录"的办法，
    因为 account-snapshot.json 在登出后可能残留，光看文件会误判。

    返回的 code：
      OK                 一切正常
      NO_CLIENT          没装客户端（找不到 exe）
      CLIENT_NOT_RUNNING 装了但没在运行
      NO_ENDPOINT        进程在但没有宿主通道文件（启动中 / 卡死）
      NOT_LOGGED_IN      通道在但未登录
      IPC_FAIL           通道连接异常
      NO_HELPER          平台辅助模块缺失
      NO_MODULE          找不到 workbuddy_ipc 模块
    """
    cfg = cfg or load_config()
    out: Dict[str, Any] = {"code": "OK", "message": "", "steps": []}

    try:
        import wb_host
    except Exception as e:
        out.update(code="NO_HELPER",
                   message="Windows 辅助模块不可用：{}".format(e))
        return out

    pids = wb_host.pids_of()
    out["process_running"] = bool(pids)
    out["pids"] = pids
    out["steps"].append(
        ("客户端进程", "运行中（{} 个进程）".format(len(pids)) if pids else "未运行"))

    # 安装情况与客户端路径（与"是否在运行"分开判断：装了没开也要能认出来）
    exe = find_workbuddy_exe(cfg)
    out["exe_path"] = exe
    out["installed"] = bool(exe)
    out["steps"].append(
        ("客户端安装", exe if exe else "未找到 WorkBuddy.exe"))
    running_exe = ""
    try:
        running_exe = wb_host.running_exe_path()
    except Exception:
        pass
    if running_exe:
        out["running_exe_path"] = running_exe
        out["module_root"] = install_root(cfg)

    ep = wb_host.endpoint_path()
    out["endpoint"] = str(ep)
    out["endpoint_exists"] = ep.exists()
    out["steps"].append(
        ("宿主通道文件", "存在" if out["endpoint_exists"] else "不存在"))

    acct = wb_host.account_info()
    out["account"] = acct
    out["logged_in_snapshot"] = bool(acct)
    out["steps"].append(
        ("账号快照", "uid={}…".format(acct["uid"][:8]) if acct else "缺失"))

    if not pids:
        if exe:
            out.update(code="CLIENT_NOT_RUNNING",
                       message="已检测到客户端安装（{}），但当前没有运行。".format(
                           exe))
        else:
            out.update(code="NO_CLIENT",
                       message="未找到 WorkBuddy 客户端，请确认已正确安装。")
        return out
    if not out["endpoint_exists"]:
        out.update(code="NO_ENDPOINT",
                   message="客户端进程在运行，但宿主通道文件不存在"
                           "（可能仍在启动，或进程已卡死）。")
        return out

    if not deep:
        return out

    try:
        with Session(cfg) as sess:
            status, body = sess.fetch("POST", STATUS_PATH)
    except CheckinError as e:
        out["ipc_connected"] = False
        if e.result == "IPC_MODULE_NOT_FOUND":
            # 与"未登录"分开报：这是环境/安装问题，说成未登录会误导排查方向
            out["module_roots"] = probe_module_roots(cfg)
            out.update(code="NO_MODULE", message=str(e))
        elif e.result in ("NOT_IN_WORKBUDDY", "CONSENT_REQUIRED", "NO_MODULE"):
            out.update(code="NOT_LOGGED_IN",
                       message="宿主通道未就绪：{}".format(e))
        else:
            out.update(code="IPC_FAIL",
                       message="{}: {}".format(e.result, e))
        return out

    out["ipc_connected"] = True
    out["api_http"] = status
    out["api_code"] = body.get("code")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    out["status_data"] = {k: data[k] for k in STATUS_KEYS if k in data}

    if status in (401, 403) or body.get("code") in (401, 403, 10002):
        out.update(code="NOT_LOGGED_IN",
                   message="宿主通道可用，但接口拒绝访问——客户端未登录。")
        return out
    if status >= 400:
        out.update(code="IPC_FAIL",
                   message="状态接口返回 HTTP {} code={}".format(
                       status, body.get("code")))
        return out

    out["steps"].append(
        ("签到接口", "HTTP {} code={}".format(status, body.get("code"))))
    return out


# ---------------- 输出与日志 ----------------

def append_log(result: Dict[str, Any]) -> None:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(
                stamp, json.dumps(result, ensure_ascii=False)))
    except OSError:
        pass


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
    else:
        print(json.dumps(result, ensure_ascii=False))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WorkBuddy 每日签到（WBIPC 宿主通道版）")
    parser.add_argument("--check-only", action="store_true", help="仅查询今日签到状态")
    parser.add_argument("--diagnose", action="store_true", help="环境自检")
    parser.add_argument("--test-notify", action="store_true",
                        help="发送测试消息到所有已配置渠道（企业微信 / 钉钉）")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON")
    # 无界面签到入口：GUI/exe 被计划任务调起时用，行为与不带参数一致
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = load_config()

    if args.test_notify:
        pushed = push_all(
            cfg, "【WorkBuddy 签到】🔧 测试消息：推送配置正确，链路正常。")
        if not pushed:
            detail = "未配置任何推送渠道（请在设置里填企业微信 / 钉钉机器人 key）"
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

    # 走统一入口，和 GUI 完全同一条路径；只查状态时不推送，避免打扰
    result = run_once(cfg, check_only=args.check_only, progress=_progress,
                      notify=not args.check_only)
    render(result, args.json)
    return int(result.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
