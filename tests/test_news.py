"""news.parse_rss 파싱 검증 (네트워크 불필요)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notion_kit"))
import news  # noqa: E402

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>코스피, 외국인 매도에 5100선 후퇴 - 연합뉴스</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>Tue, 17 Nov 2026 07:12:00 GMT</pubDate><source url="https://www.yna.co.kr">연합뉴스</source></item>
<item><title>반도체주 반등 &amp; 환율 안정 - 한국경제</title><link>https://news.google.com/rss/articles/def</link>
<pubDate>Tue, 17 Nov 2026 05:00:00 GMT</pubDate><source url="https://www.hankyung.com">한국경제</source></item>
<item><title>링크 없는 항목</title><link></link></item>
</channel></rss>"""


class NewsTest(unittest.TestCase):
    def test_parse(self):
        rows = news.parse_rss(SAMPLE.encode("utf-8"), "국내 증시")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "코스피, 외국인 매도에 5100선 후퇴")
        self.assertEqual(rows[0]["source"], "연합뉴스")
        self.assertEqual(rows[0]["published"], "2026-11-17 16:12")      # KST 변환
        self.assertEqual(rows[1]["title"], "반도체주 반등 & 환율 안정")

    def test_offline(self):
        os.environ["MRM_OFFLINE"] = "1"
        try:
            self.assertEqual(news.fetch(), [])
        finally:
            os.environ.pop("MRM_OFFLINE")


if __name__ == "__main__":
    unittest.main()
