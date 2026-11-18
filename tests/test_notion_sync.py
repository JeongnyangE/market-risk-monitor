"""
가짜 Notion 서버로 동기화 전 과정을 검증한다 — 실제 토큰 없이 실행 가능.

  python -m unittest tests/test_notion_sync.py -v
  (먼저 run_monitor.py 와 notion_kit/build_site.py 를 한 번 실행해 outputs/, site/ 가 있어야 함)
"""
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "notion_kit"))

import mock_notion_server as mock  # noqa: E402


class NotionSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.st = mock.start()
        os.environ["NOTION_API_BASE"] = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        os.environ["NOTION_TOKEN"] = mock.TOKEN
        for k in ("NOTION_DASHBOARD_PAGE_ID", "NOTION_DATABASE_ID"):
            os.environ.pop(k, None)
        import notion_sync
        cls.ns = importlib.reload(notion_sync)                  # API_BASE를 다시 읽도록
        cls.ns.Notion.__init__.__defaults__ = (cls.ns.API_BASE, 0.0, 6)   # 테스트에서는 속도 제한 대기 생략
        cls.parent = cls.st.add_root_page()
        cls.page_url = f"https://www.notion.so/myws/Risk-Cafe-{cls.parent.replace('-', '')}?pvs=4"
        cls.args = ["--in", os.path.join(ROOT, "outputs", "KR"), "--charts", os.path.join(ROOT, "site", "charts"),
                    "--page", cls.page_url, "--site-url", "https://example.github.io/market_risk_monitor/"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _live_children(self, cid):
        return [b for b in self.st.container(cid)["children"] if not self.st.blocks[b]["archived"]]

    def test_end_to_end(self):
        st, ns = self.st, self.ns
        # 1) 처음 실행: 구조 생성 + 30거래일 백필
        self.assertEqual(ns.main(self.args + ["--backfill", "30"]), 0)
        kids = self._live_children(self.parent)
        types = sorted(st.blocks[k]["type"] for k in kids)
        self.assertEqual(types, ["child_database", "child_page"])
        db_id = next(k for k in kids if st.blocks[k]["type"] == "child_database")
        page_id = next(k for k in kids if st.blocks[k]["type"] == "child_page")
        self.assertEqual(len(st.dbs[db_id]["rows"]), 30)
        first_blocks = self._live_children(page_id)
        self.assertGreater(len(first_blocks), 15)
        btypes = [st.blocks[b]["type"] for b in first_blocks]
        self.assertEqual(btypes[0], "callout")
        self.assertIn("image", btypes)
        self.assertIn("embed", btypes)
        self.assertTrue(st.throttled_once, "429 재시도 경로가 실행되지 않음")
        # 마지막 행에는 보정 확률·경보가 들어가 있어야 함
        last = max(st.dbs[db_id]["rows"], key=lambda r: st.pages[r]["properties"]["날짜"]["date"]["start"])
        self.assertIsNotNone(st.pages[last]["properties"]["낙폭확률(보정)"]["number"])
        # 2) 두 번째 실행(일일): 중복 행·중복 페이지가 생기면 안 됨, 옛 블록은 모두 보관 처리
        self.assertEqual(ns.main(self.args), 0)
        kids2 = self._live_children(self.parent)
        self.assertEqual(len(kids2), 2)
        self.assertEqual(len(st.dbs[db_id]["rows"]), 30)
        second = self._live_children(page_id)
        self.assertEqual(len(second), len(first_blocks))
        self.assertTrue(all(st.blocks[b]["archived"] for b in first_blocks))
        # 3) 사용자가 로그 DB의 속성 하나를 지운 경우 → 자동 복구
        st.dbs[db_id]["properties"].pop("경보")
        self.assertEqual(ns.main(self.args), 0)
        self.assertIn("경보", st.dbs[db_id]["properties"])
        # 4) 잘못된 토큰 → 종료코드 1과 안내
        os.environ["NOTION_TOKEN"] = "wrong"
        try:
            self.assertEqual(ns.main(self.args), 1)
        finally:
            os.environ["NOTION_TOKEN"] = mock.TOKEN

    def test_news_and_movers_blocks(self):
        import pandas as pd
        data = self.ns.load_inputs(os.path.join(ROOT, "outputs", "KR"))
        data["news"] = pd.DataFrame([{"topic": "국내 증시", "title": "테스트 헤드라인 " + "가" * 30,
                                      "source": "테스트", "link": "https://example.com/a?x=" + "b" * 400,
                                      "published": "2026-11-17 16:12"}])
        blocks = self.ns.build_blocks(data, {}, None, "2026-11-18 00:00 UTC")
        heads = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] == "heading_2"]
        self.assertIn("오늘의 시장 이슈", heads)
        self.assertIn("코스피 시장 폭과 급등락", heads)
        for i, b in enumerate(blocks):            # 가짜 서버의 API 제약 검사기로 전부 검증
            mock.validate_block(b, self.st, 1, f"blocks[{i}]")

    def test_parse_id(self):
        h = "1a2b3c4d5e6f40718293a4b5c6d7e8f9"
        want = "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
        for s in (h, want, f"https://www.notion.so/ws/Cafe-Deadbeef-{h}?pvs=4",
                  f"https://www.notion.so/{h}", f"https://www.notion.so/ws/Board-abc?p={h}&pm=s"):
            self.assertEqual(self.ns.parse_id(s), want, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
