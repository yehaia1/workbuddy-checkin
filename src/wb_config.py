# -*- coding: utf-8 -*-
"""配置与路径 —— 全项目唯一的配置来源。

三种运行方式，数据目录各不相同（这是刻意的，见 docs/architecture.md）：

    独立运行（exe）   %LOCALAPPDATA%\\WorkBuddyCheckin   ← 用户缓存目录
    本地源码运行       仓库根目录
    服务端脚本         wb_server.py 所在目录（配置跟着脚本走）

为什么 exe 不把配置写在 exe 旁边：用户可能把 exe 放在桌面、U 盘、
只读目录，甚至 Program Files 下面 —— 写完要么被安全软件拦，要么下次
换个位置就"配置丢了"。写进当前用户的缓存目录，exe 放哪都能跑。

本模块也被服务端脚本复用，所以**不能 import 任何 Windows 专有东西**
（tkinter / ctypes.windll 之类）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

APP_ID = "WorkBuddyCheckin"          # 目录名 / 计划任务名 / 产物名，全英文
APP_TITLE = "WorkBuddy 签到"
VERSION = "3.0.0"                    # 唯一版本号来源，打包时也读这里
RELEASE_DATE = "2026-10-19"

TOKEN_FILE_NAME = "auth_oauth.json"

# 推送渠道名（内部标识 -> 界面文案）
PUSH_CHANNEL_LABELS = {"wecom": "企业微信", "dingtalk": "钉钉"}


SRC_DIR = Path(__file__).resolve().parent

# 由 set_data_dir() 决定；未指定时按运行方式取默认值
_DATA_DIR: Optional[Path] = None
_DATA_ORIGIN = ""

DEFAULT_CONFIG: Dict[str, Any] = {
    # ---- 推送 ----
    # 只推送选中的渠道；留空 = 不推送。key 留空的渠道会被自动跳过。
    "push_channels": ["wecom"],
    "wecom_key": "",                # 企业微信机器人：webhook 链接里 key= 后面那段
    "dingtalk_key": "",             # 钉钉机器人：链接里 access_token= 后面那段
    # ---- 授权 ----
    "oauth_token_file": "",         # 留空 = 数据目录下 auth_oauth.json
    "oauth_auth_timeout": 300,      # 本机浏览器授权等待上限（秒）
    # 服务端自动授权：等多久（秒）/ 最多几轮 / 轮次间的退避基数（秒，指数增长）
    "auth_wait_seconds": 180,
    "auth_attempts": 3,
    "auth_backoff_seconds": 60,
    # 令牌剩余天数 <= 该值时，每次签到后额外推一条续期提醒（界面不暴露，
    # 需要改就改 config.json；设 0 = 关闭提醒）
    "expiry_warn_days": 2,
    # ---- 定时（仅 Windows 版使用）----
    "schedule_enabled": False,      # 是否已写入 Windows 计划任务
    "schedule_times": ["09:00"],    # 每日触发时间点，可多个
    "schedule_jitter": 15,          # 随机抖动分钟数，0 = 准点
    "exit_after_apply": True,       # 应用定时后自动退出本程序
}

# v1.x 遗留的配置项：加载时忽略，保存时顺手从文件里剔掉，
# 免得用户看着 config.json 里一堆没用的键发懵。
LEGACY_KEYS = (
    "auth_mode", "workbuddy_exe", "ipc_dir", "launch_workbuddy",
    "launch_timeout", "hide_window", "hide_window_timeout",
    "close_after_checkin", "close_timeout", "close_force",
    "auto_env_check", "ui_scale", "wecom_webhook", "dingtalk_webhook",
)


# ---------------- 路径 ----------------

def run_mode() -> str:
    """"frozen" = 独立运行（exe），"source" = 本地源码运行。"""
    return "frozen" if getattr(sys, "frozen", False) else "source"


def is_repo_layout() -> bool:
    """是不是在开发仓库里跑（而不是服务端那种平铺目录）。

    区分这两种「源码运行」很必要：

    * 仓库：`<repo>/src/wb_config.py`，数据落在**仓库根**（`src/` 的上一级）
    * 服务端：`<dir>/wb_server.py` + `<dir>/wb_config.py` 平铺，数据落在
      **脚本目录本身**

    判据用仓库根的标志物（`run.py` / `.git` / `build/`），不看目录名——
    服务端目录可能叫任何名字，也可能就叫 `src`。
    """
    root = SRC_DIR.parent
    return any((root / mark).exists() for mark in ("run.py", ".git", "build"))


def install_dir() -> Path:
    """程序本体所在目录。

    * 独立运行：exe 所在目录（exe 本体，**不在这里写任何数据**）
    * 源码运行：`src/` 目录
    """
    if run_mode() == "frozen":
        return Path(sys.executable).resolve().parent
    return SRC_DIR


def cache_dir() -> Path:
    """当前用户的缓存目录（独立运行的默认数据目录）。

    Windows 用 `%LOCALAPPDATA%`（没有就用 `%APPDATA%`），
    其它系统退到 XDG 缓存，方便在 Linux 上做冒烟测试。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_ID
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_ID.lower()


def default_data_dir() -> Path:
    """没显式指定时用哪个目录。

    * 独立运行（exe）：用户缓存目录
    * 仓库源码运行：仓库根目录
    * 服务端平铺目录：**脚本所在目录**（配置跟着脚本走）
    """
    if run_mode() == "frozen":
        return cache_dir()
    return SRC_DIR.parent if is_repo_layout() else SRC_DIR


def data_dir() -> Path:
    """数据目录：配置、日志、令牌都放这里。"""
    return _DATA_DIR if _DATA_DIR is not None else default_data_dir()


def data_origin() -> str:
    """数据目录来源，给人看的（界面上写"数据目录（用户缓存目录）"）。"""
    if _DATA_ORIGIN:
        return _DATA_ORIGIN
    if run_mode() == "frozen":
        return "用户缓存目录"
    if is_repo_layout() and data_dir() == SRC_DIR.parent:
        return "仓库根目录"
    return "脚本目录"


def set_data_dir(path: Path) -> Path:
    """指定数据目录，并同步模块级常量。

    `CONFIG_PATH` / `LOG_PATH` / `DATA_DIR` 是被各处直接读的模块属性，
    所以这里连同它们一起重绑（消费方都是运行时属性查找，不会拿到旧值）。
    """
    global _DATA_DIR, _DATA_ORIGIN, DATA_DIR, CONFIG_PATH, LOG_PATH
    target = Path(path).expanduser().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    _DATA_DIR = target
    if target == cache_dir():
        _DATA_ORIGIN = "用户缓存目录"
    elif target == SRC_DIR.parent:
        _DATA_ORIGIN = "仓库根目录"
    elif target == SRC_DIR:
        _DATA_ORIGIN = "脚本目录"
    else:
        _DATA_ORIGIN = "指定目录"
    DATA_DIR = target
    CONFIG_PATH = target / "config.json"
    LOG_PATH = target / "checkin.log"
    return target


def migrate_legacy(verbose: Optional[Callable[[str], None]] = None) -> List[str]:
    """把旧版留在程序旁边的配置 / 令牌搬进缓存目录（只搬一次）。

    v2.x 及更早的 exe 把 config.json、auth_oauth.json 写在 exe 旁边。
    v3.0 改成缓存目录后，如果不搬，用户会遇到"升级一下就要重新配 key、
    重新授权"。这里只在**目标不存在**时复制，源文件保留不删——
    用户自己确认没问题后再手工删掉。

    服务端脚本（数据目录就是脚本目录）和源码运行不会走到这里。
    """
    target = data_dir()
    if target == install_dir():
        return []
    moved: List[str] = []
    for name in ("config.json", TOKEN_FILE_NAME):
        src = install_dir() / name
        dst = target / name
        if not src.is_file() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            moved.append(name)
        except OSError:
            continue
    if moved and verbose:
        verbose("已把旧版数据从 {} 迁移到 {}".format(install_dir(), target))
    return moved


def prepare(explicit: Optional[Path] = None,
            verbose: Optional[Callable[[str], None]] = None) -> Path:
    """入口第一件事：定下数据目录 → 补目录 → 旧版一次性迁移。

    explicit 给了就用它（服务端脚本传脚本目录，或 --config 的上级目录），
    没给就按运行方式取默认值（exe = 缓存，源码 = 仓库根）。
    """
    set_data_dir(explicit if explicit is not None else default_data_dir())
    migrate_legacy(verbose)
    return data_dir()


def is_prepared() -> bool:
    """入口是否已经定过数据目录（避免下层入口把服务端指定的目录覆盖掉）。"""
    return _DATA_DIR is not None


# 初始值：入口调用 prepare() 之后会被重绑
DATA_DIR = default_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "checkin.log"


def version_text() -> str:
    """版本行（`--version` 与日志共用）。"""
    if run_mode() == "frozen":
        mode = "独立运行"
    elif is_repo_layout():
        mode = "源码运行"
    else:
        mode = "脚本运行"  # 服务端那种平铺目录
    return "{} {} ({})　{}".format(APP_ID, VERSION, RELEASE_DATE, mode)


# ---------------- 配置读写 ----------------

def config_path(path: Optional[str] = None) -> Path:
    """配置文件路径（服务端脚本可显式指定另一份）。"""
    return Path(path).expanduser().resolve() if path else CONFIG_PATH


def _derive_push_channels(raw: Dict[str, Any]) -> list:
    """老配置里没有 push_channels，就按填了哪个 key 推断。"""
    out = []
    if str(raw.get("wecom_key") or raw.get("wecom_webhook") or "").strip():
        out.append("wecom")
    if str(raw.get("dingtalk_key") or raw.get("dingtalk_webhook") or "").strip():
        out.append("dingtalk")
    return out


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """读配置；文件缺失或损坏时退回默认值（不让程序因为配置坏掉而崩）。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg["schedule_times"] = list(DEFAULT_CONFIG["schedule_times"])
    cfg["push_channels"] = list(DEFAULT_CONFIG["push_channels"])
    p = config_path(path)
    try:
        user = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(user, dict):
        return cfg

    if "push_channels" not in user:
        channels = _derive_push_channels(user)
    else:
        raw = user.get("push_channels")
        channels = [str(c) for c in raw] if isinstance(raw, (list, tuple)) else []
    for k, v in user.items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    cfg["push_channels"] = [c for c in channels if c in PUSH_CHANNEL_LABELS]

    times = cfg.get("schedule_times")
    if isinstance(times, str):
        times = times.replace("，", ",").split(",")
    cfg["schedule_times"] = [str(t).strip() for t in (times or []) if str(t).strip()]
    return cfg


def save_patch(patch: Dict[str, Any], path: Optional[str] = None) -> Path:
    """只改传入的键，其余原样保留（绝不整份重写，避免冲掉别人的配置）。"""
    p = config_path(path)
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    for k in LEGACY_KEYS:
        cur.pop(k, None)
    cur.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def ensure_file(path: Optional[str] = None) -> Path:
    """首次运行落一份配置文件，用户才知道该改哪个文件。"""
    p = config_path(path)
    if not p.exists():
        save_patch(dict(DEFAULT_CONFIG), path=str(p))
    return p


def token_path(cfg: Optional[Dict[str, Any]] = None,
               base_dir: Optional[str] = None) -> str:
    """令牌文件路径：配置项优先，留空则放数据目录。"""
    custom = str((cfg or {}).get("oauth_token_file") or "").strip()
    if custom:
        return os.path.abspath(os.path.expandvars(custom))
    return os.path.join(str(base_dir or data_dir()), TOKEN_FILE_NAME)
