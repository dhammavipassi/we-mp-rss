"""微信读书 Cookie 刷新（全部在宿主机执行，容器只消费结果）。

架构：微信读书登录态 profile 由 macOS 钥匙串加密，容器内 Linux Chromium 解密不了，
因此**所有浏览器操作（首次扫码 + 每日自动刷新）都在宿主机用本机 Chrome 完成**，
刷新成功后把明文 Cookie 写回数据卷的 wx.lic；容器（jobs/mps.add_job）只读取 wx.lic
的 Cookie 同步文章，不在容器内启动浏览器。

调用方：scripts/refresh_weread_cookie.py（GUI 扫码 / --headless 每日自动，由 launchd 触发）。

流程：
1. 用本机 Chrome（Playwright 驱动）打开配置的公众号主页 URL（reader 页，形如
   https://weread.qq.com/web/mp/reader/xxxx，**不要配成带 bookId 的 /web/mp/articles
   接口地址**——接口地址会被重定向且不是给人看的页面）；
2. 主页内部会发出 /web/mp/articles 请求，从该请求头提取最新 Cookie（回退
   context.cookies 拼接）；
3. 写回 wx.lic 的 weread_data，并记录 cookie_refresh_last_ts（冷却用）；
4. 若 headless_only=False 且取不到有效 Cookie，弹可见窗口提示扫码登录，等待后更新。

注意：
- 直接读写 wx.lic（WEREAD_LIC_PATH 指定，默认 ./data/wx.lic）。
- 持久化 profile 目录由 WEREAD_PROFILE_DIR 指定，默认 ~/.cache/we-mp-rss/weread-chrome-profile；
  应与容器挂载的数据卷指向同一物理目录（如 /Users/yangqing/wechat-rss-data/weread-chrome-profile），
  由本机 Chrome 持有登录态，宿主刷新时复用。
- 宿主机调用一般不传 force_bundled（使用 wx.lic 中配置的 browser_path 指向的本机 Chrome）。
"""
import os
import json
import re
import time

import yaml

DEFAULT_LIC_PATH = os.environ.get("WEREAD_LIC_PATH", "./data/wx.lic")
DEFAULT_PROFILE_DIR = os.environ.get(
    "WEREAD_PROFILE_DIR",
    os.path.expanduser("~/.cache/we-mp-rss/weread-chrome-profile"),
)


def _read_lic(lic_path: str = DEFAULT_LIC_PATH) -> dict:
    """直接读取 wx.lic（YAML），避免引入 core.config 的重依赖链。"""
    if not os.path.exists(lic_path):
        return {}
    with open(lic_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_lic(lic_path: str, doc: dict):
    with open(lic_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)


def _load_weread_data(lic_path: str = DEFAULT_LIC_PATH):
    """返回 (doc, data)，doc 为整个 YAML 文档，data 为 weread_data 字典。"""
    doc = _read_lic(lic_path)
    data = doc.get("weread_data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    data = dict(data)
    accounts = data.get("accounts")
    if isinstance(accounts, list) and accounts:
        data.update({key: accounts[0].get(key, "") for key in ("cookie", "vid", "name", "ticket")})
    return doc, data


def _save_cookie(cookie: str, name: str = "", lic_path: str = DEFAULT_LIC_PATH):
    """把最新 cookie 写回 wx.lic 的 weread_data（保留文档其他部分）。

    同时同步 vid 字段：配置页与 /weread 状态接口用 weread_data.vid 判断是否已配置，
    若只更新 cookie 而不更新 vid，换账号扫码后会残留上一个账号的 vid。
    """
    doc, data = _load_weread_data(lic_path)
    data["cookie"] = cookie
    vid = extract_vid(cookie)
    if vid:
        data["vid"] = vid
    if name:
        data["name"] = name
    accounts = data.get("accounts")
    if isinstance(accounts, list) and accounts:
        entry = next((account for account in accounts if str(account.get("vid", "")) == vid), None)
        if entry is None:
            entry = {"vid": vid, "name": name}
            accounts.append(entry)
        entry["cookie"] = cookie
        if name:
            entry["name"] = name
        for key in ("cookie", "vid", "name", "ticket"):
            data.pop(key, None)
    data["cookie_refresh_last_ts"] = time.time()
    doc["weread_data"] = data
    _write_lic(lic_path, doc)


def extract_vid(cookie: str) -> str:
    """从 Cookie 字符串中提取 wr_vid。"""
    for item in (cookie or "").split(";"):
        item = item.strip()
        if item.startswith("wr_vid="):
            return item[len("wr_vid="):].strip()
    return ""


def _dedupe_cookie(cookie: str) -> str:
    """去掉重复键（浏览器注入种子后再回抓可能产生重复 wr_vid/wr_skey 等），保留首次出现。

    重复键会导致服务端取到第一个（可能是过期）值，故保存与注入前都需去重。
    """
    kept = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if "=" in item:
            k, _, v = item.partition("=")
            k = k.strip()
            if k and k not in kept:
                kept[k] = f"{k}={v.strip()}"
    return "; ".join(kept.values())


_VERIFY_BOOK_FALLBACK = "MP_WXS_3528995129"


def _verify_probe_book_ids(lic_path: str) -> list[str]:
    """探针用 bookId：优先取 wx.lic 里配置的 cookie_refresh_url 中的 MP_WXS_*，

    再补一个内置样例兜底（配置的公众号若停更无 reviewId，还有第二个候选可验）。
    """
    _, data = _load_weread_data(lic_path)
    m = re.search(r"MP_WXS_\d+", str(data.get("cookie_refresh_url") or ""))
    ids = [m.group(0)] if m else []
    if _VERIFY_BOOK_FALLBACK not in ids:
        ids.append(_VERIFY_BOOK_FALLBACK)
    return ids


def _verify_cookie(cookie: str, lic_path: str = DEFAULT_LIC_PATH) -> bool:
    """实打实走一遍生产抓取路径，确认 Cookie 真能拉到正文。

    两段验证：先用 /api/mp/cover 取该号最新一篇的 reviewId（验证基础登录态），
    再请求 /web/mp/content 拉正文（验证内容接口权限）。半失效 Cookie 会让 cover
    照常返回而 /web/mp/* 接口返回 -2012 或空体——只验 cover 会把死 Cookie 误判成活。
    缺少 requests 或验证网络失败时返回 False，不保存未经验证的 Cookie。
    """
    try:
        import requests
    except ImportError:
        return False
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://weread.qq.com/",
    }
    proxy_url = os.environ.get("WEREAD_BROWSER_PROXY", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        review_id = ""
        for book_id in _verify_probe_book_ids(lic_path):
            r = None
            # SR 代理对 CN 域名偶发 503（Tunnel connection failed），重试吸收抖动
            for _attempt in range(3):
                try:
                    r = requests.get(
                        "https://weread.qq.com/api/mp/cover",
                        params={"bookId": book_id},
                        headers=headers,
                        proxies=proxies,
                        timeout=30,
                    )
                    break
                except Exception:
                    r = None
                    time.sleep(1.5)
            if r is None:
                continue
            try:
                j = r.json()
            except Exception:
                return False
            if not isinstance(j, dict):
                return False
            code = j.get("errCode", j.get("errcode", 0))
            if code:  # 任何非零 errCode 均表示无效（含 -2012/-2041 等）
                return False
            review_id = str(j.get("reviewId") or "").strip()
            if review_id:
                break
        if not review_id:
            # 没有正文可验证，不能据公开封面认定登录态有效。
            return False
        c = None
        for _attempt in range(3):
            try:
                c = requests.get(
                    "https://weread.qq.com/web/mp/content",
                    params={"reviewId": review_id},
                    headers={**headers, "Accept": "text/html,application/xhtml+xml,*/*"},
                    proxies=proxies,
                    timeout=30,
                )
                break
            except Exception:
                c = None
                time.sleep(1.5)
        if c is None:
            return False
        if c.status_code != 200 or not c.text.strip():
            return False
        try:
            c.json()
        except ValueError:
            # 非 JSON = 真文章 HTML。新版 WeRead 页面形态多样：常规文 js_content、
            # 短文 text_page_info、图集 picture_page_info_list、客户端渲染壳
            # js_content_container——任一标记即视为有效正文
            # （旧版只认 id="js_content"，会把新版页面误判为无效，实测）。
            text = c.text
            return any(
                m in text
                for m in (
                    "js_content",
                    "rich_media_content",
                    "text_page_info",
                    "picture_page_info_list",
                )
            )
        # 正文接口的 JSON 响应是错误/状态消息，不是 HTML 正文。
        return False
    except Exception:
        return False


def _extract_cookie_from_page(page, context, url: str) -> str:
    """优先从公众号接口请求头取 Cookie，回退 context.cookies 拼接。"""
    captured = {}

    def _on_request(request):
        if any(path in request.url for path in ("/api/mp/cover", "/web/mp/content", "/web/mp/articles")):
            captured["cookie"] = request.all_headers().get("cookie", "")

    page.on("request", _on_request)
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        print(f"[refresh] 打开页面异常: {e}")
    # 优先使用 network 请求头中的 Cookie
    cookie = captured.get("cookie", "").strip()
    if cookie:
        return cookie
    # 回退：直接用 context 的 cookie jar 拼接
    try:
        cookies = context.cookies("https://weread.qq.com")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


RENEWAL_URL = "https://weread.qq.com/web/login/renewal"


def _renew_session(cookie: str, timeout: int = 20) -> str:
    """用 wr_rt 调 /web/login/renewal 刷新服务端会话，返回刷新后的 Cookie 串；失败返回 ""。

    微信读书 web 会话的 wr_skey 短时有效（续期响应 Max-Age≈90 分钟），wr_rt
    （refresh token）有效期约一年。浏览器登录态靠页面周期性调 renewal 维持——
    本函数把同一机制搬到宿主：无需浏览器、无需扫码即可续期，是长期稳定的主路径。
    """
    import re as _re

    try:
        import requests
    except ImportError:
        return ""
    m_rt = _re.search(r"wr_rt=([^;]+)", cookie)
    m_vid = _re.search(r"wr_vid=([^;]+)", cookie)
    if not m_rt or not m_vid:
        return ""
    proxy_url = os.environ.get("WEREAD_BROWSER_PROXY", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://weread.qq.com",
        "Referer": "https://weread.qq.com/",
    })
    # 登录 Cookie 必须注入 session 的 cookie jar（domain=weread.qq.com），
    # 否则服务端按游客处理（-2013）。
    s.cookies.set("wr_rt", m_rt.group(1), domain="weread.qq.com", path="/")
    s.cookies.set("wr_vid", m_vid.group(1), domain="weread.qq.com", path="/")
    r = None
    for _attempt in range(3):
        try:
            r = s.post(RENEWAL_URL, data={"rq": "%2Fweb%2Fbook%2Fread"},
                       proxies=proxies, timeout=timeout)
            break
        except Exception:
            r = None
            time.sleep(1.5)
    if r is None:
        return ""
    try:
        j = r.json()
    except Exception:
        return ""
    if not isinstance(j, dict) or j.get("succ") != 1:
        return ""
    # 续期可能下发新 wr_skey（也可能同值仅刷新 TTL）：以响应 jar 为准合并更新。
    # 注意：服务端会带回 wr_pf=undefined / NaN 这类 JS 哨兵值，属非法 Cookie，
    # 写入后会毒化整条 Cookie 串（正文验证 3/3 失败，实测），必须过滤。
    _INVALID = {"undefined", "NaN", "null", ""}
    pairs = {}
    for part in cookie.split(";"):
        part = part.strip()
        if part and "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
    for c in s.cookies:
        if c.value and c.name and c.value not in _INVALID:
            pairs[c.name] = c.value
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def refresh_weread_cookie(verbose: bool = True, headless_only: bool = False,
                          force_bundled: bool = False, cooldown_hours: float = 6.0) -> bool:
    """执行一次 Cookie 自动刷新。成功更新（或确认仍新鲜）返回 True，否则 False。

    headless_only: True 时只做无头刷新，不在登录失效时弹窗扫码（容器内使用）。
    force_bundled: True 时忽略 browser_path，使用 Playwright 自带 Chromium
                   （容器内没有宿主机 Chrome 路径，且需与数据卷共享 profile）。
    cooldown_hours: 距离上次成功刷新不足该时长且已有 Cookie 时，视为仍有效，跳过刷新。
    """
    lic, data = _load_weread_data()
    url = (data.get("cookie_refresh_url") or "").strip()
    browser_path = (data.get("browser_path") or "").strip()
    browser_type = (data.get("browser_type") or "chrome").strip() or "chrome"

    if not url:
        if verbose:
            print("[refresh] 未配置 cookie_refresh_url，跳过自动刷新（请在微信读书配置页填写）")
        return False

    # 冷却：上次刷新成功且在冷却期内，且已有 Cookie，则视为仍有效，跳过
    if cooldown_hours and cooldown_hours > 0:
        last_ts = data.get("cookie_refresh_last_ts") or 0
        has_cookie = bool((data.get("cookie") or "").strip())
        if has_cookie and last_ts and (time.time() - float(last_ts)) < cooldown_hours * 3600:
            # 冷却期内：只有经真实接口验证 Cookie 仍有效才跳过刷新；
            # 否则（已过期 / 旧时间戳是假阳性）必须强制刷新，不能被旧时间戳卡住。
            if _verify_cookie((data.get("cookie") or "").strip()):
                if verbose:
                    print(f"[refresh] Cookie 在冷却期内（{cooldown_hours:g}h）且仍有效，跳过刷新")
                return True
            if verbose:
                print("[refresh] Cookie 在冷却期内但已失效，强制刷新")

    # 0) 续期优先（长期稳定主路径）：wr_rt 有效即可刷新会话，无需浏览器/扫码。
    #    放在浏览器链之前：renewal 成功且正文验证通过即结束，避免无谓的 profile 启动。
    cur_cookie = (data.get("cookie") or "").strip()
    if cur_cookie:
        renewed = _renew_session(cur_cookie)
        if renewed:
            if _verify_cookie(renewed, DEFAULT_LIC_PATH):
                _save_cookie(renewed, lic_path=DEFAULT_LIC_PATH)
                if verbose:
                    print("[refresh] renewal 续期成功，会话已刷新并验证通过")
                return True
            if verbose:
                print("[refresh] renewal 已刷新会话，但正文验证未通过，继续完整刷新链")
        elif verbose:
            print("[refresh] renewal 不可用（wr_rt 缺失或服务端拒绝），继续完整刷新链")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("[refresh] 未安装 playwright，请执行: pip install playwright && playwright install chromium")
        return False

    profile_dir = DEFAULT_PROFILE_DIR
    os.makedirs(profile_dir, exist_ok=True)

    def _launch(p, headless: bool, force_bundled: bool = False):
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        proxy_url = os.environ.get("WEREAD_BROWSER_PROXY", "").strip()
        proxy = {"server": proxy_url} if proxy_url else None
        if force_bundled:
            return p.chromium.launch_persistent_context(profile_dir, headless=headless, args=args, proxy=proxy)
        if not browser_path:
            return p.chromium.launch_persistent_context(
                profile_dir, headless=headless, channel="chrome", args=args, proxy=proxy)
        return p.chromium.launch_persistent_context(
            profile_dir, headless=headless, executable_path=browser_path, args=args, proxy=proxy,
        )

    def _try(headless: bool, wait_login: bool = False, timeout_s: int = 300,
             force_bundled: bool = False, seed_cookie: str = "") -> str:
        """打开页面并提取 Cookie；wait_login=True 时若未登录，保持窗口等待用户扫码。

        seed_cookie: wx.lic 中已有的 Cookie，作为登录态种子注入上下文。
        解决宿主(macOS Chrome)/容器(Linux Chromium) profile cookie 加密不互通的问题：
        容器无头刷新无法解密宿主写入的 profile cookie，故改以 wx.lic 的 Cookie 为可信源。
        """
        with sync_playwright() as p:
            context = _launch(p, headless=headless, force_bundled=force_bundled)
            page = context.new_page()
            # 扫码模式下绝不注入种子 Cookie：过期 Cookie 会让页面停在“已登录但失效”的
            # 状态，既出不来二维码，也会让下面的等待逻辑误判为“已拿到 Cookie”而直接退出。
            if wait_login:
                seed_cookie = ""
                try:
                    context.clear_cookies()
                except Exception:
                    pass
            # 注入种子 Cookie（来源：wx.lic，先去重避免重复键）。即便 profile 跨平台
            # 无法复用也能复用登录态；若种子本身已过期，则必须扫码重建有效 session。
            seed_cookie = _dedupe_cookie(seed_cookie)
            if seed_cookie:
                try:
                    cookies = []
                    for item in seed_cookie.split(";"):
                        item = item.strip()
                        if "=" in item:
                            name, _, value = item.partition("=")
                            name = name.strip()
                            value = value.strip()
                            if name and value:
                                cookies.append({
                                    "name": name,
                                    "value": value,
                                    "url": "https://weread.qq.com",
                                })
                    if cookies:
                        context.add_cookies(cookies)
                        if verbose:
                            print(f"[refresh] 已注入 wx.lic 种子 Cookie（{len(cookies)} 项）")
                except Exception as e:
                    if verbose:
                        print(f"[refresh] 注入种子 Cookie 失败（不影响）: {e}")
            try:
                cookie = _extract_cookie_from_page(page, context, url)
                cookie = _dedupe_cookie(cookie)
                if wait_login:
                    # 扫码模式：只有“验证真实有效”的 Cookie 才算数；否则保持可见窗口，
                    # 轮询等待用户扫码登录（登录态持久化到 profile_dir）。
                    if cookie and _verify_cookie(cookie):
                        return cookie
                    # 确保窗口停在可扫码的登录入口（reader 页未登录时可能是报错页）
                    try:
                        page.goto("https://weread.qq.com/", wait_until="domcontentloaded",
                                  timeout=30000)
                    except Exception:
                        pass
                    if verbose:
                        print("[refresh] 请在弹出的 Chrome 窗口中用微信扫码登录微信读书…")
                    deadline = time.time() + timeout_s
                    while time.time() < deadline:
                        try:
                            cookies = context.cookies("https://weread.qq.com")
                            ck = _dedupe_cookie(
                                "; ".join(f"{c['name']}={c['value']}" for c in cookies))
                        except Exception:
                            ck = ""
                        if "wr_vid=" in ck and _verify_cookie(ck):
                            cookie = ck
                            break
                        time.sleep(3)
                    else:
                        cookie = ""
                return cookie
            finally:
                context.close()

    # 1) 先使用持久 profile，避免过期种子覆盖仍有效的登录态。
    seed_cookie = (data.get("cookie") or "").strip()
    cookie = _try(headless=True, force_bundled=force_bundled)
    # 必须实打实验证 Cookie 真能拉到数据，避免把过期 Cookie 误判为有效（假阳性）
    if cookie and "wr_vid=" in cookie and _verify_cookie(cookie):
        vid = extract_vid(cookie)
        _save_cookie(cookie, name=data.get("name", ""))
        if verbose:
            print(f"[refresh] Cookie 已自动更新 (vid={vid})")
        return True

    # profile 无效时再尝试 wx.lic 种子，支持不同平台无法解密 profile 的场景。
    if seed_cookie:
        if verbose:
            print("[refresh] 持久 profile 刷新失败，尝试 wx.lic 种子 Cookie")
        cookie = _try(headless=True, force_bundled=force_bundled, seed_cookie=seed_cookie)
        if cookie and "wr_vid=" in cookie and _verify_cookie(cookie):
            vid = extract_vid(cookie)
            _save_cookie(cookie, name=data.get("name", ""))
            if verbose:
                print(f"[refresh] Cookie 已自动更新（种子回退, vid={vid}）")
            return True

    # 2) 未拿到有效 Cookie（或拿到但验证失败＝过期）
    if headless_only:
        if verbose:
            print("[refresh] Cookie 未通过正文访问验证。无头模式不弹窗，"
                  "请在本机运行 'python scripts/refresh_weread_cookie.py' 扫码登录后刷新")
        return False

    # 3) 弹可见窗口，提示扫码登录，等待登录后更新
    if verbose:
        print("[refresh] 未获取到有效 Cookie，已打开浏览器窗口，请扫码登录微信读书…")
        print("[refresh] 等待登录（最长 10 分钟），登录成功后自动保存 Cookie…")
    cookie = _try(headless=False, wait_login=True, timeout_s=600,
                 force_bundled=force_bundled, seed_cookie=seed_cookie)
    if cookie and "wr_vid=" in cookie and _verify_cookie(cookie):
        vid = extract_vid(cookie)
        _save_cookie(cookie, name=data.get("name", ""))
        if verbose:
            print(f"[refresh] 扫码登录后 Cookie 已更新 (vid={vid})")
        return True

    if verbose:
        print("[refresh] 等待扫码超时或 Cookie 仍无效，请检查微信读书登录状态")
    return False


def request_host_refresh(timeout_s: int = 180) -> dict:
    """容器内调用宿主机刷新代理（**不在容器内启动浏览器**）。

    浏览器刷新动作由宿主机代理完成（macOS 钥匙串加密的 profile 容器内解不开）。
    容器内只负责：在同步文章前，发现/怀疑 Cookie 过期时，请宿主机代理去刷新，
    代理把最新明文 Cookie 写回数据卷 wx.lic，容器随后读取它同步文章。

    可通过环境变量 ``WEREAD_REFRESH_AGENT_URL`` 配置代理地址
    （默认 http://host.docker.internal:9876/refresh）。

    返回: ``{"triggered": bool, "ok": bool, "needs_scan": bool, "message": str}``
      - triggered=False 表示未配置代理（手动模式，跳过自动刷新，不报错）；
      - ok=False 且 needs_scan=True 表示登录态过期需扫码，调用方应中止任务并提示用户；
      - ok=False 且 needs_scan=False 表示代理调用本身失败（网络/代理未启动等）。
    """
    import urllib.request

    agent_url = (os.environ.get("WEREAD_REFRESH_AGENT_URL") or "").strip()
    if not agent_url:
        # 未配置代理：保持向后兼容，不强制刷新（手动模式）
        return {
            "triggered": False,
            "ok": True,
            "needs_scan": False,
            "message": "未配置 WEREAD_REFRESH_AGENT_URL，跳过自动刷新（手动模式）",
        }
    if not agent_url.endswith("/refresh"):
        agent_url = agent_url.rstrip("/") + "/refresh"
    try:
        req = urllib.request.Request(
            agent_url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "triggered": True,
            "ok": bool(payload.get("ok")),
            "needs_scan": bool(payload.get("needs_scan")),
            "message": payload.get("message", ""),
        }
    except Exception as e:
        return {
            "triggered": True,
            "ok": False,
            "needs_scan": False,
            "message": f"调用宿主机刷新代理失败: {e}",
        }


if __name__ == "__main__":
    import sys

    ok = refresh_weread_cookie(verbose=True)
    sys.exit(0 if ok else 1)
