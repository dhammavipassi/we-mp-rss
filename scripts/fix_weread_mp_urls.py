# -*- coding: utf-8 -*-
"""一次性修复：把已入库公众号文章 URL 中的 WeRead ``~`` 规范化为 ``_``。

WeRead 返回的 originalId/reviewId token 可能含 ``~``，但微信公开短链
``https://mp.weixin.qq.com/s/<token>`` 在该位置要求 ``_``。历史数据需要
单独回写一次，后续新文章由 ``weread_mp.py`` 在生成链接时自动规范化。

用法：python scripts/fix_weread_mp_urls.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import requests

from core.db import DB
from core.models.article import Article

PREFIX = "https://mp.weixin.qq.com/s/"


def main():
    session = DB.get_session()
    try:
        rows = (
            session.query(Article)
            .filter(Article.mp_id.like("MP_WXS_%"), Article.url != "")
            .all()
        )
    finally:
        pass

    fixed = 0
    for art in rows:
        url = (art.url or "").strip()
        if not url.startswith(PREFIX):
            continue
        token = url[len(PREFIX):]
        if "~" not in token:
            continue
        fixed_url = f"{PREFIX}{token.replace('~', '_')}"
        print(f"[修复] {art.id}\n  {url}\n  -> {fixed_url}")
        art.url = fixed_url
        session.add(art)
        fixed += 1

    if fixed:
        session.commit()
    session.close()
    print(f"\n修复 {fixed} 条")


if __name__ == "__main__":
    main()
