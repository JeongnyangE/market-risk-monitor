"""
오늘의 시장 이슈 — Google 뉴스 RSS(한국어)에서 증시 헤드라인을 모은다.

  python notion_kit/news.py --out outputs/KR/dashboard/news.csv

헤드라인(제목)·언론사·시각·원문 링크만 저장하고 기사 본문은 가져오지 않는다.
네트워크가 막혀 있으면 빈 목록으로 조용히 넘어간다(MRM_OFFLINE=1 이면 시도하지 않음).
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pandas as pd

KST = dt.timezone(dt.timedelta(hours=9))
TOPICS = [
    ("국내 증시", "코스피 OR 코스닥 증시"),
    ("해외 증시", "뉴욕증시 OR 미국 증시"),
    ("반도체", "반도체 주가"),
    ("환율·금리", "환율 OR 국채 금리"),
]
RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def parse_rss(xml_bytes: bytes, topic: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        pub = it.findtext("pubDate")
        if not title or not link.startswith("http"):
            continue
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].rstrip()
        try:
            t = email.utils.parsedate_to_datetime(pub).astimezone(KST) if pub else None
        except (TypeError, ValueError):
            t = None
        out.append({"topic": topic, "title": title, "source": src, "link": link,
                    "published": t.strftime("%Y-%m-%d %H:%M") if t else ""})
    return out


def _key(title: str) -> str:
    return re.sub(r"[\W_]+", "", title)[:40]


def fetch(per_topic: int = 8, max_age_h: int = 36, timeout: int = 15) -> list[dict]:
    if os.environ.get("MRM_OFFLINE") == "1":
        return []
    rows, seen = [], set()
    now = dt.datetime.now(KST)
    for topic, q in TOPICS:
        url = RSS.format(q=urllib.parse.quote(f"{q} when:1d"))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-risk-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                items = parse_rss(r.read(), topic)
        except Exception as e:
            print(f"[news] {topic} 실패 ({type(e).__name__}) — 건너뜀")
            continue
        n = 0
        for it in items:
            k = _key(it["title"])
            if k in seen:
                continue
            if it["published"]:
                age = now - dt.datetime.strptime(it["published"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
                if age > dt.timedelta(hours=max_age_h):
                    continue
            seen.add(k)
            rows.append(it)
            n += 1
            if n >= per_topic:
                break
    rows.sort(key=lambda r: r["published"], reverse=True)
    return rows


def update(dash_dir: str) -> int:
    rows = fetch()
    path = os.path.join(dash_dir, "news.csv")
    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)
    elif os.path.exists(path):
        print("[news] 새 헤드라인을 받지 못해 이전 파일을 그대로 둡니다")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash-dir", default="outputs/KR/dashboard")
    a = ap.parse_args()
    print(f"[news] 헤드라인 {update(a.dash_dir)}건")


if __name__ == "__main__":
    main()
