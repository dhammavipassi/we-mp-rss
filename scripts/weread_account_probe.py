#!/usr/bin/env python3
"""逐个验证 wx.lic 里的微信读书账号能否真的拿到公众号正文。

为什么需要它：`-2012` 在微信读书侧同时代表「登录态失效」和「额度/风控受限」，
而 `/api/mp/cover`（公共信息）在正文被限时照样返回成功。只看一个错误码分不出
是哪个号出了问题、问题出在登录还是额度。这里对每个账号分别打一次 cover 和
一次 content，把两个接口的结果并排列出来——加第二个账号之后，用它一眼就能
看出新号是不是真的能用。

用法（在容器里跑，读的是容器挂载的 wx.lic）：

    docker exec -i we-mp-rss-shadow /app/env_x86_64/bin/python - <<'PY'
    ...（本文件内容）
    PY

或者拷进容器后用 python 执行。只打印 vid 前缀和错误码，不打印 Cookie。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

LIC_PATH = "/app/data/wx.lic"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def load_accounts(path: str) -> list[dict]:
    """从 wx.lic 里取出 accounts 列表；单账号旧格式也认。"""
    raw = open(path, encoding="utf-8").read()
    accounts: list[dict] = []
    in_accounts = False
    cur: dict = {}
    for line in raw.splitlines():
        if re.match(r"^  accounts:\s*$", line):
            in_accounts = True
            continue
        if in_accounts:
            if re.match(r"^  [A-Za-z_]", line) and not re.match(r"^\s{4,}", line):
                break
            m = re.match(r"^\s*-\s*([A-Za-z_]+):\s*(.*)$", line)
            if m:
                if cur:
                    accounts.append(cur)
                cur = {}
                in_accounts = True
            m = re.match(r"^\s*(?:-\s*)?([A-Za-z_]+):\s*(.*)$", line)
            if m:
                cur[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    if cur:
        accounts.append(cur)
    if not accounts:
        m = re.search(r"^\s*cookie:\s*(\S.*)$", raw, re.M)
        if m:
            accounts = [{"cookie": m.group(1).strip()}]
    return [a for a in accounts if a.get("cookie")]


def probe(cookie: str, book_id: str) -> tuple[object, object]:
    def get(url: str, params: dict) -> dict:
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"Cookie": cookie, "User-Agent": UA,
                     "Accept": "application/json",
                     "Referer": "https://weread.qq.com/"},
        )
        return json.load(urllib.request.urlopen(req, timeout=25))

    cover = get("https://weread.qq.com/api/mp/cover", {"bookId": book_id})
    rid = str(cover.get("reviewId") or "").strip()
    cover_code = cover.get("errCode", cover.get("errcode"))
    if not rid:
        return cover_code, "no_review_id"
    content = get("https://weread.qq.com/web/mp/content", {"reviewId": rid})
    return cover_code, content.get("errCode", content.get("errcode"))


def main() -> int:
    accounts = load_accounts(LIC_PATH)
    if not accounts:
        print("wx.lic 里没有可用账号")
        return 1
    book_id = ""
    m = re.search(r"MP_WXS_\d+", open(LIC_PATH, encoding="utf-8").read())
    if m:
        book_id = m.group(0)
    print(f"账号数：{len(accounts)}   探针公众号：{book_id or '(未配置)'}")
    ok = 0
    for i, acc in enumerate(accounts, 1):
        vid = str(acc.get("vid") or "")
        name = str(acc.get("name") or "")
        label = f"#{i} vid={vid[:6]}… name={name or '-'}"
        try:
            cover_code, content_code = probe(acc["cookie"], book_id)
        except Exception as exc:  # 网络问题不该中断整轮检查
            print(f"  {label}  探测失败：{type(exc).__name__} {str(exc)[:60]}")
            continue
        usable = content_code in (None, 0)
        ok += 1 if usable else 0
        print(f"  {label}  cover={cover_code}  content={content_code}"
              f"  → {'可用' if usable else '正文被拒'}")
    print(f"可用账号：{ok}/{len(accounts)}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
