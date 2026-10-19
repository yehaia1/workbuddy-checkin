# -*- coding: utf-8 -*-
"""消息推送（企业微信 / 钉钉群机器人），零依赖、跨平台。

设计要点
--------
* 用户只填 **key**（不是整条链接），地址由本模块拼，且只拼到**官方域名**——
  避免 key 被误引到第三方地址。
* 同时兼容三种写法：裸 key、`key=xxx` 片段、整条 webhook 链接。
  看着像链接却抽不出参数时**拒发**，不硬拼。
* 两个渠道互相独立，一个失败不影响另一个。
* 服务端脚本（wb_auth_server.py）也复用这里，所以不能 import Windows 专有模块。
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from wb_config import PUSH_CHANNEL_LABELS

WECOM_API_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
DINGTALK_API_PREFIX = "https://oapi.dingtalk.com/robot/send"

# 渠道名 -> (显示名, 地址解析函数, 推送函数)。顺序即界面展示顺序。
CHANNELS: Dict[str, Tuple[str, Any, Any]] = {}


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
    return _extract_key(cfg.get("wecom_key"), "key")


def dingtalk_key_of(cfg: Dict[str, Any]) -> str:
    return _extract_key(cfg.get("dingtalk_key"), "access_token")


def wecom_url(cfg: Dict[str, Any]) -> str:
    """企业微信完整 webhook；未配置或非法时返回空串。"""
    key = wecom_key_of(cfg)
    if not key or key.lower().startswith("http"):
        return ""
    return WECOM_API_PREFIX + "?key=" + key


def dingtalk_url(cfg: Dict[str, Any]) -> str:
    """钉钉完整 webhook；未配置或非法时返回空串。"""
    token = dingtalk_key_of(cfg)
    if not token or token.lower().startswith("http"):
        return ""
    return DINGTALK_API_PREFIX + "?access_token=" + token


def _post_json(url: str, payload: Dict[str, Any],
               timeout: int = 15) -> Tuple[bool, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, "推送失败：{}".format(e)
    # 两家返回体同构：errcode=0 表示成功
    if body.get("errcode") == 0:
        return True, "推送成功"
    return False, "推送失败：errcode={} {}".format(
        body.get("errcode"), body.get("errmsg"))


def wecom_send(cfg: Dict[str, Any], text: str) -> Tuple[bool, str]:
    url = wecom_url(cfg)
    if not url:
        return False, "未配置企业微信机器人 key"
    return _post_json(url, {"msgtype": "text", "text": {"content": text}})


def dingtalk_send(cfg: Dict[str, Any], text: str) -> Tuple[bool, str]:
    url = dingtalk_url(cfg)
    if not url:
        return False, "未配置钉钉机器人 access_token"
    return _post_json(url, {"msgtype": "text", "text": {"content": text}})


CHANNELS = {
    "wecom": ("企业微信", wecom_url, wecom_send),
    "dingtalk": ("钉钉", dingtalk_url, dingtalk_send),
}


def selected_channels(cfg: Dict[str, Any]) -> List[str]:
    """用户在配置里勾选、且 key 填对了的渠道（保持固定顺序）。"""
    picked = [str(c) for c in (cfg.get("push_channels") or [])]
    return [name for name in CHANNELS
            if name in picked and CHANNELS[name][1](cfg)]


def label(name: str) -> str:
    return PUSH_CHANNEL_LABELS.get(name, name)


def push(cfg: Dict[str, Any], text: str,
         only: Optional[str] = None) -> List[Tuple[str, bool, str]]:
    """推送到所有已选渠道，返回 [(显示名, 是否成功, 说明)]。

    only 指定单个渠道（测试推送时用），不传则按配置全发。
    """
    out: List[Tuple[str, bool, str]] = []
    names = [only] if only else selected_channels(cfg)
    for name in names:
        meta = CHANNELS.get(name)
        if not meta:
            continue
        title, resolve, send = meta
        if not resolve(cfg):
            out.append((title, False, "未配置 {} 的 key".format(title)))
            continue
        ok, detail = send(cfg, text)
        out.append((title, ok, detail))
    return out
