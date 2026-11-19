# -*- coding: utf-8 -*-
"""多账号列表 —— `config.json` 里 `accounts` 的唯一读写入口。

一条账号记录就是一份令牌文件：

    {"uid": "<账号唯一标识>", "token_file": "auth_<uid>.json",
     "nickname": "<授权时记下的昵称快照>"}

约定
----
* **归属只看授权结果里的 uid**：uid 已存在 = 重新授权（覆盖原条目），
  不存在 = 新增。命令行 `--account` 只做“目标校验”，不决定写哪个文件——
  否则用户在“想续期”时扫了另一个号，会静默覆盖掉原来的令牌。
* uid 读不出来（旧令牌不是 JWT / 文件损坏）时用 `"legacy"` 占位，
  这只允许出现在**本机还没有任何账号**的情况；重新授权成功后会被换成真实 uid。
* `token_file` 在数据目录内时存相对文件名，旧版自定义的绝对路径原样保留，
  这样整个数据目录可以整体搬走。
* 旧版（单账号）配置里没有 `accounts` 键，读的时候按 `auth_oauth.json`
  （或 `oauth_token_file` 指的那份）合成第一个账号，`ensure()` 时才落盘。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import wb_config
import wb_oauth

# uid 缺失时的占位键（只用于旧数据，新授权不允许）
LEGACY_UID = "legacy"


# ---------------- 路径 ----------------

def _safe_uid(uid: str) -> str:
    """把 uid 收敛成安全的文件名片段（防路径分隔符、通配符、超长名字）。"""
    out = "".join(c if (c.isalnum() or c in "._-") else "_"
                  for c in str(uid or "")).strip("._-")
    return out[:64]


def default_token_file(uid: str) -> str:
    """账号默认令牌文件名（相对数据目录）。"""
    return "auth_{}.json".format(_safe_uid(uid) or "account")


def _store_path(raw: str) -> str:
    """落盘用的路径：数据目录内的存相对名，其它位置存绝对路径。"""
    p = Path(os.path.expandvars(str(raw or "").strip())).expanduser()
    if not p.is_absolute():
        p = Path(wb_config.data_dir()) / p
    p = p.resolve()
    try:
        return str(p.relative_to(Path(wb_config.data_dir()).resolve()))
    except ValueError:
        return str(p)


def token_file_of(acct: Optional[Dict[str, Any]],
                  base_dir: Optional[str] = None) -> str:
    """账号令牌文件的绝对路径（相对路径挂在数据目录下）。"""
    base = Path(base_dir or wb_config.data_dir())
    raw = str((acct or {}).get("token_file") or "").strip()
    if not raw:
        return str(base / wb_config.TOKEN_FILE_NAME)
    p = Path(os.path.expandvars(raw)).expanduser()
    return str(p if p.is_absolute() else base / p)


# ---------------- 读取 ----------------

def _normalize(raw: Any) -> List[Dict[str, str]]:
    """把配置里的数组洗成统一结构（坏条目直接丢掉，不让界面崩）。"""
    out: List[Dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip() or LEGACY_UID
        out.append({
            "uid": uid,
            "token_file": (str(item.get("token_file") or "").strip()
                           or default_token_file(uid)),
            "nickname": str(item.get("nickname") or "").strip(),
        })
    return out


def _legacy_account(cfg: Optional[Dict[str, Any]]
                    ) -> Optional[Dict[str, str]]:
    """旧配置里那份唯一令牌，合成成第一个账号（只读，不落盘）。"""
    raw = str((cfg or {}).get("oauth_token_file") or "").strip()
    path = token_file_of({"token_file": raw} if raw else {})
    if not os.path.isfile(path):
        return None
    info = wb_oauth.summary(path)
    return {
        "uid": str(info.get("uid") or "").strip() or LEGACY_UID,
        "token_file": _store_path(path),
        "nickname": str(info.get("nickname") or "").strip(),
    }


def list_accounts(cfg: Optional[Dict[str, Any]] = None
                   ) -> List[Dict[str, str]]:
    """当前账号列表（副本，调用方随便改都不会污染配置）。"""
    cfg = cfg if cfg is not None else wb_config.load()
    if isinstance(cfg.get("accounts"), list):
        return _normalize(cfg["accounts"])
    legacy = _legacy_account(cfg)
    return [legacy] if legacy else []


def ensure(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """入口调用一次：把旧单账号数据落成 `accounts`（只迁移一次）。

    只要配置里已经有 `accounts` 键（哪怕空数组）就不再迁移——
    否则用户删光账号后，那份没人用的 old 令牌会“复活”。
    """
    cfg = cfg if cfg is not None else wb_config.load()
    if isinstance(cfg.get("accounts"), list):
        return _normalize(cfg["accounts"])
    accounts = list_accounts(cfg)
    cfg["accounts"] = [dict(a) for a in accounts]
    try:
        # 全新安装时先把完整默认配置落盘（用户才知道该改哪个文件），
        # 再补上迁移出来的账号；否则 config.json 里只会孤零零一个 accounts。
        if not wb_config.config_path().exists():
            wb_config.ensure_file()
        wb_config.save_patch({"accounts": cfg["accounts"]})
    except OSError:
        # 只读目录（比如程序装在 Program Files 下）：内存里照样能用，
        # 下次有写权限时再落盘，不让迁移把整个程序卡死。
        pass
    return accounts


# ---------------- 定位 ----------------

def _locate(accounts: List[Dict[str, str]], spec: str) -> int:
    """按「1 起的序号」或「完整 uid」找下标，找不到返回 -1。"""
    s = str(spec or "").strip()
    if not s:
        return -1
    if s.isdigit():
        idx = int(s) - 1
        return idx if 0 <= idx < len(accounts) else -1
    for i, acct in enumerate(accounts):
        if acct.get("uid") == s:
            return i
    return -1


def resolve(spec: str, cfg: Optional[Dict[str, Any]] = None
            ) -> Optional[Dict[str, str]]:
    """按 uid 或序号取一条账号；没找到返回 None。"""
    accounts = list_accounts(cfg)
    idx = _locate(accounts, spec)
    return accounts[idx] if idx >= 0 else None


# ---------------- 授权结果合并 ----------------

def _describe(acct: Dict[str, str]) -> str:
    """错误提示里怎么称呼一个账号：昵称优先，否则 uid 前 8 位。"""
    uid = str(acct.get("uid") or "")
    nick = str(acct.get("nickname") or "").strip()
    short = uid[:8] + ("…" if len(uid) > 8 else "")
    return "{}（{}）".format(nick, short) if nick else short


def merge_authorized(record: Dict[str, Any], target: str = "",
                     cfg: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """把一次授权结果并入账号列表（新增或覆盖）。

    record  授权 / 导入得到的令牌记录（含 accessToken 与 account.uid）
    target  `--account` 指定的目标（uid 或序号）；空 = 按 uid 自动合并
    返回 {action: created|updated, uid, nickname, token_file}

    校验不通过时抛 OAuthError，**此时磁盘上的令牌一个字都不会动**。
    """
    cfg = cfg if cfg is not None else wb_config.load()
    accounts = list_accounts(cfg)
    acct_info = record.get("account") or {}
    uid = str(acct_info.get("uid") or "").strip()
    nickname = str(acct_info.get("nickname") or "").strip()

    # 1) 目标校验：扫错号时立刻停手，不覆盖原令牌
    target = str(target or "").strip()
    tgt_idx = _locate(accounts, target) if target else -1
    if target and tgt_idx < 0:
        raise wb_oauth.OAuthError(
            "OAUTH_ACCOUNT_NOT_FOUND",
            "找不到账号「{}」：确认序号或完整 uid 后再试。".format(target), 2)
    if target and uid and accounts[tgt_idx].get("uid") not in ("", LEGACY_UID):
        if accounts[tgt_idx]["uid"] != uid:
            raise wb_oauth.OAuthError(
                "OAUTH_WRONG_ACCOUNT",
                "本次要更新的是 {}，但授权的是 {}；为避免覆盖，未保存。".format(
                    _describe(accounts[tgt_idx]),
                    nickname or uid), 2)

    # 2) 没有 uid 就无法确定归属。只允许“本机还没有账号”当 legacy 收下；
    #    已经有账号时拒绝，免得把别人的令牌写进现有条目。
    if not uid:
        if accounts and not (target and accounts[tgt_idx].get("uid") == LEGACY_UID):
            raise wb_oauth.OAuthError(
                "OAUTH_NO_UID",
                "授权响应里没有账号标识（uid），无法确定归属，未保存。", 3)
        uid = LEGACY_UID

    # 3) 定位条目：uid 命中 = 更新；否则新增（或补齐 legacy 条目）
    idx = _locate(accounts, uid)
    action = "updated" if idx >= 0 else "created"
    if idx < 0 and target:
        idx = tgt_idx          # legacy 条目授权成功：沿用原条目，只换 uid
        action = "updated"

    if idx >= 0:
        token_file = accounts[idx]["token_file"] or default_token_file(uid)
        nickname = nickname or accounts[idx].get("nickname") or ""
    else:
        # 首个账号沿用旧配置里自定义的令牌路径，升级后文件位置不变
        custom = str((cfg or {}).get("oauth_token_file") or "").strip()
        token_file = _store_path(custom) if (not accounts and custom) \
            else default_token_file(uid)

    path = token_file_of({"token_file": token_file})

    # 4) 先落令牌，再改列表：反过来会出现“列表有账号、文件不存在”
    try:
        wb_oauth.save(record, path)
    except OSError as e:
        raise wb_oauth.OAuthError(
            "ACCOUNT_TOKEN_SAVE_FAILED", "令牌写入失败：{}".format(e), 2)

    entry = {"uid": uid, "token_file": _store_path(path), "nickname": nickname}
    if idx >= 0:
        accounts[idx] = entry
    else:
        accounts.append(entry)

    cfg["accounts"] = [dict(a) for a in accounts]
    try:
        wb_config.save_patch({"accounts": cfg["accounts"]})
    except OSError as e:
        raise wb_oauth.OAuthError(
            "ACCOUNT_SAVE_FAILED",
            "令牌已保存到 {}，但账号列表写入失败：{}".format(path, e), 2)

    return {"action": action, "uid": uid, "nickname": nickname,
            "token_file": path}


def remove(uid: str, cfg: Optional[Dict[str, Any]] = None,
           delete_file: bool = True) -> Dict[str, Any]:
    """从列表删掉一个账号。

    令牌文件**只在数据目录内**才删：旧版自定义到别处的路径可能还有别的用途，
    擅自删除用户目录外的文件风险太大，那种情况只移除条目、保留文件。
    """
    cfg = cfg if cfg is not None else wb_config.load()
    accounts = list_accounts(cfg)
    idx = _locate(accounts, uid)
    if idx < 0:
        raise wb_oauth.OAuthError(
            "OAUTH_ACCOUNT_NOT_FOUND", "找不到账号：{}".format(uid), 2)
    gone = accounts.pop(idx)
    path = token_file_of(gone)

    base = Path(wb_config.data_dir()).resolve()
    removed_file = False
    if delete_file:
        try:
            if Path(path).resolve().parent == base and os.path.isfile(path):
                os.remove(path)
                removed_file = True
        except OSError:
            pass

    cfg["accounts"] = [dict(a) for a in accounts]
    wb_config.save_patch({"accounts": cfg["accounts"]})
    return {"uid": gone["uid"], "nickname": gone.get("nickname") or "",
            "token_file": path, "removed_file": removed_file}

