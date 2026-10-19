# -*- coding: utf-8 -*-
"""配置与路径 —— 全项目唯一的配置来源。

v2.0 起只剩「账号直连」一条通道，配置项随之砍掉一半：
客户端路径、自动拉起、隐藏窗口、各种等待超时、界面缩放，全部删除。
剩下来的只有三块，正好对应界面上那三块：授权 / 定时 / 推送。

同时这份模块被服务端脚本（wb_auth_server.py）复用，所以**不能 import
任何 Windows 专有东西**（tkinter / ctypes.windll 之类）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

APP_TITLE = "WorkBuddy 签到"
VERSION = "2.1"

TOKEN_FILE_NAME = "auth_oauth.json"

# 推送渠道名（内部标识 -> 界面文案）
PUSH_CHANNEL_LABELS = {"wecom": "企业微信", "dingtalk": "钉钉"}


SRC_DIR = Path(__file__).resolve().parent


def run_mode() -> str:
    """"frozen" = 独立运行（exe），"source" = 本地源码运行。"""
    return "frozen" if getattr(sys, "frozen", False) else "source"


def app_dir() -> Path:
    """数据目录（配置、日志、令牌都放这里）。

    * 独立运行：`__file__` 在 PyInstaller 的临时解包目录里，写进去会随进程
      退出一起消失，所以必须用 exe 所在目录。
    * 本地源码运行：`src/` 的上级目录，也就是仓库根目录 —— 不能把运行时
      文件写进源码树里，否则配置、日志和 .py 混在一起，也很难区分
      哪份配置属于哪次运行。
    """
    if run_mode() == "frozen":
        return Path(sys.executable).resolve().parent
    return SRC_DIR.parent


SCRIPT_DIR = app_dir()
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "checkin.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    # ---- 推送 ----
    # 只推送选中的渠道；留空 = 不推送。key 留空的渠道会被自动跳过。
    "push_channels": ["wecom"],
    "wecom_key": "",                # 企业微信机器人：webhook 链接里 key= 后面那段
    "dingtalk_key": "",             # 钉钉机器人：链接里 access_token= 后面那段
    # ---- 授权 ----
    "oauth_token_file": "",         # 留空 = 程序目录下 auth_oauth.json
    "oauth_auth_timeout": 300,      # 等待扫码/浏览器登录的上限（秒）
    # ---- 定时 ----
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
    """令牌文件路径：配置项优先，留空则放程序（脚本）目录。"""
    custom = str((cfg or {}).get("oauth_token_file") or "").strip()
    if custom:
        return os.path.abspath(os.path.expandvars(custom))
    return os.path.join(str(base_dir or SCRIPT_DIR), TOKEN_FILE_NAME)
