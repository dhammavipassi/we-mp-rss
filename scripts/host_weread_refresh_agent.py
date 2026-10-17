#!/usr/bin/env python3
"""宿主机微信读书 Cookie 刷新代理（常驻，由 launchd 启动）。

为什么需要它
------------
- 微信读书的登录态 profile 由 macOS 钥匙串加密，容器内 Linux Chromium **解密不了**，
  因此浏览器刷新动作必须在宿主机用本机 Chrome 完成。
- 但用户希望「点击执行 / 定时任务」时，若 Cookie 过期就**自动刷新**再同步文章，
  而不是手动跑到宿主机敲命令。
- 解法：宿主机常驻一个轻量 HTTP 代理。容器在同步前调用其 ``/refresh`` 端点，
  代理用本机 Chrome 做无头刷新（登录态失效时再弹窗扫码），结果写回数据卷
  ``wx.lic``；容器随后读到最新 Cookie 去同步文章。

刷新策略（verify-first，尽量不启动浏览器）
----------------------------------------
1. 先验证当前 wx.lic 里的 Cookie 是否仍能拉到数据；有效则直接返回（秒级，不启动浏览器）。
2. 无效则做**无头刷新**（复用持久化 profile，通常秒级~数十秒）。
3. 无头仍失败（登录态过期）→ 走 Web UI 扫码通道：调容器 ``/weread/qr/code``
   出码 → ``open`` 弹到默认浏览器 → 系统通知 → 后台轮询 ``wx.lic``，
   扫码写回即恢复；登录页随服务端续码自动更新。HTTP 立即返回 ``needs_scan``。
   冷却期 ``WEREAD_QR_COOLDOWN_S`` 内不重复弹窗。QR 通道不可用才回退
   playwright 可见窗口扫码。

容器侧通过环境变量 ``WEREAD_REFRESH_AGENT_URL``（默认
``http://host.docker.internal:9876/refresh``）找到本代理。

注意：本文件运行在**宿主机**（macOS），使用宿主机 Python venv + Playwright + 本机 Chrome，
不是容器内的 Linux Chromium。
"""
import os
import sys
import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 日志实时落盘（launchd 场景 stdout 重定向为文件时避免全缓冲滞后）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# 让脚本能 import 项目的 core 包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 指向宿主机上容器挂载的数据卷 wx.lic / profile（与容器 /app/data 同一物理目录）
os.environ.setdefault("WEREAD_LIC_PATH", "/Users/yangqing/wechat-rss-data/wx.lic")
os.environ.setdefault(
    "WEREAD_PROFILE_DIR",
    "/Users/yangqing/wechat-rss-data/weread-chrome-profile",
)

from core.weread_cookie_refresh import (  # noqa: E402
    refresh_weread_cookie,
    _verify_cookie,
    _load_weread_data,
)

HOST = os.environ.get("WEREAD_AGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEREAD_AGENT_PORT", "9876"))

# Web UI 扫码通道（容器 /api/v1/wx/weread/qr/*：服务器出码+轮询+直写 wx.lic）
WEMPRSS_API = os.environ.get("WEMPRSS_API", "http://127.0.0.1:8002/api/v1/wx")
WEMPRSS_STATIC = os.environ.get(
    "WEMPRSS_STATIC", "http://127.0.0.1:8002/static/weread_qrcode.png")
WEMPRSS_USER = os.environ.get("WEMPRSS_USER", "admin")
WEMPRSS_PASS = os.environ.get("WEMPRSS_PASS", "admin@123")
QR_WAIT_S = float(os.environ.get("WEREAD_QR_WAIT_S", "420"))
QR_REGEN_S = float(os.environ.get("WEREAD_QR_REGEN_S", "100"))
QR_COOLDOWN_S = float(os.environ.get("WEREAD_QR_COOLDOWN_S", "1800"))
LIC_PATH = os.environ.get(
    "WEREAD_LIC_PATH", "/Users/yangqing/wechat-rss-data/wx.lic")
QR_MARK = os.path.join(os.path.dirname(LIC_PATH), ".qr_prompted_ts")
# 事故级去重标记：存在期间不再向 OmniFocus 推「扫码登录」任务，
# Cookie 恢复验证通过时清除——同一事故只推一条，恢复后再坏才推下一条
ALERT_MARK = os.path.join(os.path.dirname(LIC_PATH), ".cookie_alert_active")

# 串行化刷新，避免并发（如 07:55 定时 + 手动执行同时触发）争抢同一 profile
_refresh_lock = threading.Lock()
_qr_lock = threading.Lock()
_qr_thread = None


def do_refresh() -> dict:
    """执行一次「确保 Cookie 新鲜」的逻辑。

    返回: ``{"ok": bool, "refreshed": bool, "needs_scan": bool, "message": str}``
    """
    # 1) 当前 Cookie 仍有效 → 无需启动浏览器，直接返回
    _, data = _load_weread_data()
    cookie = (data.get("cookie") or "").strip()
    if cookie and _verify_cookie(cookie):
        _clear_alert_mark()
        return {
            "ok": True,
            "refreshed": False,
            "needs_scan": False,
            "message": "Cookie 仍有效，无需刷新",
        }

    # 2) 需要刷新：无头优先（cooldown_hours=0 强制真刷新，绕过 6h 冷却误判）
    try:
        ok = refresh_weread_cookie(verbose=True, headless_only=True, cooldown_hours=0)
    except Exception as e:
        ok = False
        print(f"[agent] 无头刷新异常: {e}")

    if ok:
        _clear_alert_mark()
        return {
            "ok": True,
            "refreshed": True,
            "needs_scan": False,
            "message": "Cookie 已自动刷新",
        }

    # 3) 无头失败：登录态已死 → Web UI 二维码通道（服务器出码+直写 wx.lic，
    #    不依赖弹出的浏览器窗口；窗口找不到/码过期的问题不存在）
    global _qr_thread
    with _qr_lock:
        if (_qr_thread is not None and _qr_thread.is_alive()) or _qr_recently_prompted():
            return {
                "ok": False,
                "refreshed": False,
                "needs_scan": True,
                "message": "Cookie 已过期，二维码已弹出等待扫码（冷却期内不重复弹）",
            }
        _qr_thread = threading.Thread(target=_qr_login_flow, daemon=True)
        _qr_thread.start()
    return {
        "ok": False,
        "refreshed": False,
        "needs_scan": True,
        "message": "Cookie 已过期，登录二维码已弹出到默认浏览器，微信扫码即可",
    }


def _http_json(url: str, token: str = "", method: str = "GET", data=None):
    """轻量 JSON 请求（宿主 urllib，无第三方依赖）。"""
    import urllib.request
    body = None
    headers = {}
    if data is not None:
        body = data
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[agent] QR API 请求失败 {url}: {e}")
        return None


def _api_token() -> str:
    resp = _http_json(
        f"{WEMPRSS_API}/auth/login", method="POST",
        data=f"username={WEMPRSS_USER}&password={WEMPRSS_PASS}".encode())
    return ((resp or {}).get("data") or {}).get("access_token", "")


def _qr_recently_prompted() -> bool:
    try:
        return time.time() - os.path.getmtime(QR_MARK) < QR_COOLDOWN_S
    except OSError:
        return False


def _notify(title: str, body: str):
    """macOS 系统通知（锁屏也进通知中心），不依赖 OmniFocus GUI。"""
    import subprocess
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Glass"'],
            timeout=10, capture_output=True)
    except Exception:
        pass


def _clear_alert_mark():
    try:
        os.remove(ALERT_MARK)
    except OSError:
        pass


def _push_omnifocus_scan_needed():
    """扫码类「需人工」事件推 OmniFocus——系统通知易错过，任务列表是收口处。

    事故级去重：同一失效事故只推一条（ALERT_MARK 存在期间不重复推），
    Cookie 恢复验证通过后清除标记；推送成功才创建标记。
    """
    import subprocess
    push_bin = os.path.expanduser(
        "~/Github_projects/_scripts/push_omnifocus_task.sh")
    if not os.path.isfile(push_bin):
        return
    if os.path.exists(ALERT_MARK):
        print("[agent] OmniFocus 本事故已推过，跳过重复推送")
        return
    try:
        result = subprocess.run(
            [push_bin,
             "--title", "需处理 · 微信读书 Cookie · 扫码登录",
             "--note",
             "公众号管道正文抓取依赖 WeRead 登录态，当前 Cookie 已失效。\n"
             "登录二维码已弹到默认浏览器，微信扫码+手机确认后管道自动恢复。",
             "--project", "管道故障",
             "--flag", "--replace"],
            timeout=600, capture_output=True)
        if result.returncode == 0:
            Path(ALERT_MARK).touch()
        else:
            print(f"[agent] OmniFocus 推送失败: exit={result.returncode}")
    except Exception as e:
        print(f"[agent] OmniFocus 推送失败: {e}")


def _qr_login_flow():
    """出码 → 弹默认浏览器 → 系统通知 → 轮询 wx.lic → 过期自动补码。"""
    import subprocess
    lic_mtime0 = 0.0
    try:
        lic_mtime0 = os.path.getmtime(LIC_PATH)
    except OSError:
        pass
    token = _api_token()
    if not token:
        print("[agent] QR 通道不可用（登录失败），回退弹窗扫码")
        try:
            refresh_weread_cookie(verbose=True, cooldown_hours=0)
        except Exception as e:
            print(f"[agent] 后台扫码刷新异常: {e}")
        return

    def _new_qr_and_open():
        resp = _http_json(f"{WEMPRSS_API}/weread/qr/code", token=token)
        uid = (((resp or {}).get("data") or {}).get("uid") or "")
        if not uid:
            return False
        subprocess.run(
            ["open", f"http://127.0.0.1:{PORT}/login"],
            timeout=10, capture_output=True)
        print(f"[agent] 二维码已弹出到默认浏览器 (uid={uid})，等待扫码…")
        return True

    if not _new_qr_and_open():
        print("[agent] QR 出码失败，回退弹窗扫码")
        try:
            refresh_weread_cookie(verbose=True, cooldown_hours=0)
        except Exception as e:
            print(f"[agent] 后台扫码刷新异常: {e}")
        return

    try:
        Path(QR_MARK).touch()
    except OSError:
        pass
    _notify("微信读书登录已过期", "二维码已弹到浏览器，微信扫码+手机确认即可")
    _push_omnifocus_scan_needed()

    deadline = time.time() + QR_WAIT_S
    last_regen = time.time()
    while time.time() < deadline:
        time.sleep(5)
        try:
            if os.path.getmtime(LIC_PATH) > lic_mtime0:
                _, data = _load_weread_data()
                cookie = (data.get("cookie") or "").strip()
                if cookie and _verify_cookie(cookie):
                    print("[agent] 扫码成功，Cookie 已写回并验证通过")
                    _clear_alert_mark()
                    _notify("微信读书已恢复", "Cookie 已更新，公众号管道自动恢复")
                    return
        except OSError:
            pass
        if time.time() - last_regen > QR_REGEN_S:
            # 续发当前二维码，登录页自动刷新图片。
            if _http_json(f"{WEMPRSS_API}/weread/qr/code", token=token):
                last_regen = time.time()
                print("[agent] 二维码已续期，登录页自动显示当前二维码")
    try:
        os.utime(QR_MARK, (0, 0))
    except OSError:
        pass
    print("[agent] 扫码等待超时，下次巡检再试")


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refresh(self):
        with _refresh_lock:
            result = do_refresh()
        self._send(200, result)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/login":
            image_url = json.dumps(WEMPRSS_STATIC).replace("<", "\\u003c")
            body = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>微信读书登录</title><body><h1>微信读书登录</h1>"
                    "<p>请用微信扫码并确认。二维码自动更新，请扫描当前显示的二维码。</p>"
                    "<img id='qr' alt='微信读书登录二维码' width='300' height='300'>"
                    "<script>const src=" + image_url + ";const img=document.getElementById('qr');"
                    "function refresh(){img.src=src+(src.includes('?')?'&':'?')+'ts='+Date.now();}"
                    "refresh();setInterval(refresh,5000);</script></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/health", ""):
            self._send(200, {"ok": True, "message": "agent alive"})
        elif path == "/refresh":
            self._refresh()
        else:
            self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        # /refresh 同时支持 POST（容器 urllib 默认 POST）
        self._refresh()

    def log_message(self, *args):
        # 避免把每个请求打到 stderr（已在 do_refresh 内打印关键日志）
        pass


def main():
    os.makedirs(os.path.dirname(os.environ.get("WEREAD_LIC_PATH", "")) or ".", exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"[agent] 微信读书 Cookie 刷新代理已启动: http://{HOST}:{PORT}/refresh")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[agent] 代理已停止")


if __name__ == "__main__":
    main()
