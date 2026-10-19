# -*- coding: utf-8 -*-
"""服务端版专用：本机开一个无头浏览器，把登录页里的**微信二维码**取出来。

为什么需要它
------------
设备授权的 `authUrl` 打开后是一个网页，登录动作发生在**那个网页里**：

    扫码 → 手机点「允许」→ 页面跳 /login/success
         → 页面自己调 GET /console/auth/login?platform=CLI&state=...
         → state 被标记成「已完成」
         → 任何一方拿 state 轮询 /v2/plugin/auth/token 才拿得到令牌

最后这一步是**页面自己发的**，带着页面自己的登录 cookie。所以：
把链接转发到手机上登录，登录态落在手机的浏览器里，服务端进程始终是个
旁观者，轮询会一直停在 11217（实测）。

换成另一种做法就成立了：**服务端自己开一个浏览器**去打开 authUrl，
把页面里的微信二维码取出来推给用户扫。扫码是微信内部完成的，页面在
服务端这个浏览器里跳转、回调，令牌就落到我们手上（实测 100 秒内 code=0）。

推什么给用户：**二维码图片的链接**
----------------------------------
微信 qrconnect 的二维码本身就是一张公网图片：

    https://open.weixin.qq.com/connect/qrcode/0813EhAu2z7Pkl2H

所以不需要上传图床、也不用在服务器上开 HTTP 口子把 PNG 暴露出去——
直接把这条链接发到企业微信 / 钉钉群即可。手机上点开就是二维码图片，
长按「识别图中二维码」直接跳授权页；电脑上点开则拿手机扫屏幕。
两家渠道都支持，不用为"钉钉不能发图片"做特殊处理。

依赖
----
playwright 只在**授权**时需要，日常签到不碰。所以：

* 本模块在函数里惰性 `import playwright`，没装也不影响 `import wb_qrlogin`；
* 打包成 pyz 时不会被静态扫描进闭包（AST 只看模块级导入），单文件体积不变；
* 浏览器按 chrome → msedge → `WB_BROWSER_PATH` / 常见系统路径 →
  playwright 自带 chromium 的顺序找，**有一处能起就行**。

    pip install playwright                    # 装库
    python -m playwright install --with-deps chromium   # Linux 服务器装浏览器
    export WB_BROWSER_PATH=/usr/bin/chromium  # 或用系统已有的 chromium

Linux 上「库装了、浏览器没装」是最常见的原因；其次是 **cron 用 root 跑，
浏览器却装在别的用户下**（`~/.cache/ms-playwright` 是按用户分的）。
这两种都会在启动浏览器前被拦下来，打一份可照做的报告后直接退出。

本模块也被服务端脚本复用，所以**不能 import 任何 Windows 专有东西**。
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 微信 qrconnect 二维码图片地址的特征片段：
#   /connect/qrcode/0813EhAu2z7Pkl2H
QR_MARK = "/connect/qrcode/"

# 图片链接的兜底基址（页面上给的是相对路径，要按所在 iframe 的地址拼）
QR_ORIGIN = "https://open.weixin.qq.com/"

PAGE_TIMEOUT_MS = 45000     # 打开登录页的超时
QR_WAIT_SECONDS = 35        # 在页面上等二维码出现的最长秒数
DOWNLOAD_TIMEOUT = 20

# 无头 Chrome 在 Linux 容器里当 root 跑必须关沙箱；其余几个是稳定性开关
LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--mute-audio",
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class QRError(RuntimeError):
    """取二维码失败：没装 playwright / 浏览器起不来 / 页面上没找到二维码。"""


# ---------------- 浏览器探测 ----------------
#
# 三种来源，按优先级：
#   1) playwright 的 chrome / msedge **channel**（借系统正式版）
#   2) 直接指到可执行文件（WB_BROWSER_PATH，或下面这些常见路径）
#   3) playwright 自己下载的 chromium（python -m playwright install chromium）
#
# Linux 服务器上最常踩的两个坑：
#   * 只 pip install playwright，没跑 playwright install → 三种都没有；
#   * cron 以 root 跑，浏览器却在别的用户下装的 —— 目录按用户分，
#     所以「手工跑正常、cron 里起不来」多半是这个。

def browsers_dir() -> Path:
    """playwright 放浏览器的目录（按用户区分）。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        return Path(env)
    home = Path.home()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    return home / ".cache" / "ms-playwright"


def bundled_dirs() -> List[str]:
    """playwright 浏览器目录里已有哪些（chromium-1187、firefox-…）。"""
    try:
        return sorted(p.name for p in browsers_dir().iterdir() if p.is_dir())
    except OSError:
        return []


def known_executables() -> List[str]:
    """可能存在的浏览器可执行文件（含 WB_BROWSER_PATH 覆盖）。"""
    paths: List[str] = []
    env = os.environ.get("WB_BROWSER_PATH", "").strip()
    if env:
        paths.append(env)
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)",
                                    r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if not base:
                continue
            for tail in (("Google", "Chrome", "Application", "chrome.exe"),
                         ("Microsoft", "Edge", "Application", "msedge.exe")):
                paths.append(os.path.join(base, *tail))
    elif sys.platform == "darwin":
        paths += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        # channel= 在 Linux 上找的就是 /opt 下这两个，所以也一并算进来
        paths += [
            "/opt/google/chrome/chrome",
            "/opt/microsoft/msedge/msedge",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
        ]
    return [p for p in paths if p]


def system_browser() -> str:
    """找到第一个真实存在的浏览器可执行文件，没有就返回空串。"""
    for path in known_executables():
        if os.path.isfile(path):
            return path
    return ""


def browser_ready() -> Tuple[bool, str]:
    """不启动浏览器，只判断「有没有一个能拿来用的 Chromium」。"""
    exe = system_browser()
    if exe:
        return True, "复用系统浏览器：{}".format(exe)
    hit = [n for n in bundled_dirs()
           if n.startswith("chromium") or n.startswith("chrome")]
    if hit:
        return True, "playwright 自带：{}".format(hit[-1])
    return False, "没有可用的 Chromium（{} 下没有 chromium-*）".format(
        browsers_dir())


def available() -> Tuple[bool, str]:
    """playwright 是否可用。返回 (是否可用, 不可用原因)。不启动浏览器。"""
    try:
        import playwright            # noqa: F401
        from playwright.sync_api import sync_playwright   # noqa: F401
    except Exception as e:
        return False, "未安装 playwright（{}）".format(str(e).strip()[:60])
    return True, ""


def install_hint() -> str:
    """给用户看的安装指引（只有在 available()/browser_ready() 为 False 时用）。"""
    return ("pip install playwright && python -m playwright install chromium\n"
            "    Linux 服务器缺依赖时用："
            "python -m playwright install --with-deps chromium\n"
            "    系统已装 chromium / chrome：设 "
            "WB_BROWSER_PATH=/usr/bin/chromium 直接复用\n"
            "    （Windows / macOS 有 Chrome 或 Edge 时只装库即可，"
            "不用下 chromium）")


def _short(err: Exception, limit: int = 110) -> str:
    """playwright 的报错是多行（带 Call log），取第一行就够定位了。"""
    first = (str(err).strip().splitlines() or [""])[0]
    first = first.replace("BrowserType.launch: ", "")
    return first[:limit] + ("…" if len(first) > limit else "")


def _launch_report(errors: List[Tuple[str, str]]) -> str:
    """把「四种浏览器都没起来」说清楚，并给出照着做就能修的命令。"""
    lines = ["起不了浏览器（{} 种来源都失败了）：".format(len(errors))]
    for label, msg in errors:
        lines.append("  · {} → {}".format(label, msg))
    lines.append("")

    dirs = bundled_dirs()
    lines.append("  playwright 浏览器目录：{}（{}）".format(
        browsers_dir(), "已装 " + "、".join(dirs) if dirs else "不存在或为空"))
    env_path = os.environ.get("WB_BROWSER_PATH", "").strip()
    lines.append("  WB_BROWSER_PATH：{}".format(
        env_path if env_path else "未设置"))
    lines.append("  当前用户：{}".format(
        os.environ.get("USERNAME") or os.environ.get("USER") or "?"))
    lines.append("")
    lines.append("  按顺序做，任一条通了就行：")
    lines.append("    1) 装 playwright 自带的 chromium（Linux 连同系统依赖）：")
    lines.append("         python -m playwright install --with-deps chromium")
    lines.append("       只装库不装浏览器是最常见的原因（本机机器上有 Chrome/Edge 才"
                 "能免这一步）")
    lines.append("    2) 机器上已经有 chromium / chrome，直接指给它：")
    lines.append("         export WB_BROWSER_PATH=/usr/bin/chromium")
    lines.append("    3) 浏览器装在别的用户下（手工跑正常、cron 里起不来就是它）：")
    lines.append("         用同一个用户跑，或 export PLAYWRIGHT_BROWSERS_PATH="
                 "<那个用户的>/.cache/ms-playwright")
    return "\n".join(lines)


class QRSession:
    """开浏览器 → 打开授权页 → 拿到二维码图片链接；**浏览器要保持打开**。

    用法：

        with wb_qrlogin.QRSession(url) as qr:      # qr.qr_url 就是二维码链接
            ...把链接推给用户...
            payload = wb_oauth.poll_token(state)   # 这期间浏览器必须活着
    """

    def __init__(self, auth_url: str, wait: int = QR_WAIT_SECONDS):
        self.auth_url = str(auth_url or "")
        self.wait = wait
        self.qr_url = ""            # 二维码**图片**的公网链接（推给用户的就是它）
        self.png: bytes = b""       # 顺带存一份原图，便于本地留档（可为空）
        self.channel = ""
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None

    # -- 生命周期 --
    def open(self) -> "QRSession":
        if not self.auth_url:
            raise QRError("授权链接为空")
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise QRError("未安装 playwright：{}".format(str(e).strip()[:80]))
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            raise QRError("playwright 启动失败：{}".format(str(e).strip()[:120]))

        # 依次试：chrome channel → msedge channel → 直接指到系统的可执行文件
        # → playwright 自带的 chromium。前两步在 Linux 上等价于找
        # /opt/google/chrome/chrome、/opt/microsoft/msedge/msedge。
        candidates: List[Tuple[str, Dict[str, Any]]] = [
            ("chrome（系统正式版）", {"channel": "chrome"}),
            ("msedge（系统正式版）", {"channel": "msedge"}),
        ]
        exe = system_browser()
        if exe:
            candidates.append((exe, {"executable_path": exe}))
        candidates.append(("playwright 自带 chromium", {}))

        errors: List[Tuple[str, str]] = []
        for label, extra in candidates:
            kw: Dict[str, Any] = {"headless": True, "args": list(LAUNCH_ARGS)}
            kw.update(extra)
            try:
                self._browser = self._pw.chromium.launch(**kw)
                self.channel = label
                break
            except Exception as e:
                errors.append((label, _short(e)))
        if self._browser is None:
            self.close()
            raise QRError(_launch_report(errors))

        self._ctx = self._browser.new_context(
            viewport={"width": 1100, "height": 900},
            device_scale_factor=2,      # 截图放大一倍，当链接失效时的备份图
            locale="zh-CN",
        )
        self._page = self._ctx.new_page()
        try:
            self._page.goto(self.auth_url, wait_until="domcontentloaded",
                            timeout=PAGE_TIMEOUT_MS)
        except Exception as e:
            first = (str(e).strip().splitlines() or [""])[0]
            self.close()
            raise QRError("打开授权页失败：{}".format(first[:120]))

        self._wait_qr()
        # 真正要推给用户的是链接；原图只是附带，拿不到也不影响
        if not self.qr_url and not self.png:
            self.close()
            raise QRError("登录页里没找到微信二维码（页面结构可能变了）")
        return self

    def close(self) -> None:
        """关浏览器。任何一步失败都不抛——收尾时不该盖掉真正的原因。"""
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._page = self._ctx = self._browser = self._pw = None

    def __enter__(self) -> "QRSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 取二维码 --
    def _wait_qr(self) -> None:
        """在页面（含 iframe）里找 /connect/qrcode/ 那张图，超时就放弃。

        页面刚 DOMContentLoaded 时二维码还没渲染，所以这里是**轮询**而不是
        等一个固定时长；iframe 也可能随跳转重建，取失败就重来。
        """
        deadline = time.time() + max(5, int(self.wait))
        while time.time() < deadline:
            try:
                for frame in self._page.frames:
                    for el in frame.query_selector_all("img"):
                        src = el.get_attribute("src") or ""
                        if QR_MARK not in src:
                            continue
                        url = urllib.parse.urljoin(
                            frame.url or QR_ORIGIN, src)
                        if not url.lower().startswith("http"):
                            continue
                        self.qr_url = url          # 链接到手，已经是可用的了
                        self.png = self._fetch(url)
                        if self.png:
                            return
                        try:                       # 下不到就截元素本身
                            shot = el.screenshot(type="png")
                        except Exception:
                            shot = b""
                        if shot:
                            self.png = shot
                        return                     # 链接有了就不必再等
            except Exception:
                pass          # 页面正在跳转时 frames 会抛，下一轮再来
            time.sleep(0.6)

    @staticmethod
    def _fetch(url: str) -> bytes:
        """把二维码原图（470x470）取下来当本地备份。

        拿不到就返回空——链接本身才是主产物，这里只是留个底。
        """
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                data = resp.read()
        except Exception:
            return b""
        if len(data) < 512:
            return b""
        if not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
            return b""
        return data
