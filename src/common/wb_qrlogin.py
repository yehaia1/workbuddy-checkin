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
* 优先借系统已装的 Chrome / Edge（`channel=`），最后才用 playwright 自带的
  chromium —— Windows 上通常不用额外下载浏览器。

    pip install playwright                    # 装库
    python -m playwright install chromium     # 只有借不到 Chrome/Edge 才需要

本模块也被服务端脚本复用，所以**不能 import 任何 Windows 专有东西**。
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# 微信 qrconnect 二维码图片地址的特征片段：
#   /connect/qrcode/0813EhAu2z7Pkl2H
QR_MARK = "/connect/qrcode/"

# 图片链接的兜底基址（页面上给的是相对路径，要按所在 iframe 的地址拼）
QR_ORIGIN = "https://open.weixin.qq.com/"

PAGE_TIMEOUT_MS = 45000     # 打开登录页的超时
QR_WAIT_SECONDS = 35        # 在页面上等二维码出现的最长秒数
DOWNLOAD_TIMEOUT = 20

# 依次尝试的浏览器：前两个借系统已装的，最后一个用 playwright 自带的
CHANNELS: Tuple[Optional[str], ...] = ("chrome", "msedge", None)

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


def available() -> Tuple[bool, str]:
    """playwright 是否可用。返回 (是否可用, 不可用原因)。不启动浏览器。"""
    try:
        import playwright            # noqa: F401
        from playwright.sync_api import sync_playwright   # noqa: F401
    except Exception as e:
        return False, "未安装 playwright（{}）".format(str(e).strip()[:60])
    return True, ""


def install_hint() -> str:
    """给用户看的安装指引（只有在 available() 为 False 时才该展示）。"""
    return ("pip install playwright && python -m playwright install chromium\n"
            "    （Windows 有 Chrome / Edge 时只装库即可，不用下 chromium）")


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

        errors: List[str] = []
        for ch in CHANNELS:
            try:
                kw: Dict[str, Any] = {"headless": True, "args": list(LAUNCH_ARGS)}
                if ch:
                    kw["channel"] = ch
                self._browser = self._pw.chromium.launch(**kw)
                self.channel = ch or "playwright-chromium"
                break
            except Exception as e:
                first = (str(e).strip().splitlines() or [""])[0]
                errors.append("{}: {}".format(ch or "bundled", first[:70]))
        if self._browser is None:
            self.close()
            raise QRError("起不了浏览器（{}）".format("；".join(errors)[:200]))

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
