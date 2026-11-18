#!/usr/bin/env python3
"""
KOSPI 리스크 모니터 → Notion 동기화 (표준 라이브러리만 사용)

환경변수
  NOTION_TOKEN     Notion 내부 통합(Internal integration)의 시크릿
  NOTION_PAGE_ID   결과를 넣을 '부모 페이지'의 주소(URL) 또는 ID — 그 페이지에 통합이 연결돼 있어야 함
  SITE_URL         (선택) GitHub Pages 대시보드 주소 → 요약 페이지에 임베드 블록 추가

실행 예
  python notion_kit/notion_sync.py --in outputs/KR --charts site/charts
  python notion_kit/notion_sync.py --backfill 250     # 처음 한 번: 과거 250거래일을 로그에 기록
  python notion_kit/notion_sync.py --dry-run          # 토큰 없이, 보낼 요청만 파일로 확인

부모 페이지 아래에 만드는 것 (없으면 새로 만들고, 있으면 찾아서 갱신)
  📉 KOSPI 리스크 — 현재 판독    매 실행마다 내용을 통째로 교체하는 요약 페이지
  📈 KOSPI 리스크 로그            날짜별 1행 데이터베이스 (같은 날짜는 덮어씀)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pandas as pd

API_BASE = os.environ.get("NOTION_API_BASE", "https://api.notion.com/v1")
NOTION_VERSION = "2022-06-28"          # 단일 데이터 소스 데이터베이스에서 계속 지원되는 안정 버전
PAGE_TITLE = "KOSPI 리스크 — 현재 판독"
DB_TITLE = "KOSPI 리스크 로그"

REG_STYLE = {"안정": ("🟢", "green_background"), "주의": ("🟡", "yellow_background"),
             "경계": ("🟠", "orange_background"), "위험": ("🔴", "red_background")}
QUAD_DESC = {"위기(Crisis)": "두 축이 모두 높은", "충격(Shock)": "스트레스는 높지만 구조적 취약성은 상대적으로 낮은",
             "취약·안일(Fragile)": "겉보기 스트레스는 낮지만 구조적 취약성이 쌓인", "안정(Calm)": "두 축이 모두 낮은"}


# ===========================================================================
# HTTP 클라이언트
# ===========================================================================
class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class Notion:
    def __init__(self, token: str, base: str = API_BASE, min_interval: float = 0.34, max_retries: int = 6):
        self.token, self.base = token, base.rstrip("/")
        self.min_interval, self.max_retries = min_interval, max_retries
        self._last = 0.0
        self.calls = 0

    def _req(self, method: str, path: str, body=None, raw: bytes | None = None, headers: dict | None = None,
             query: dict | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
        hdr = {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}
        if raw is None and body is not None:
            hdr["Content-Type"] = "application/json"
        hdr.update(headers or {})
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.time() - self._last)      # 평균 초당 3회 제한
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers=hdr)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    txt = r.read().decode("utf-8")
                    return json.loads(txt) if txt else {}
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")
                try:
                    err = json.loads(txt)
                except ValueError:
                    err = {"code": "http_error", "message": txt[:300]}
                retryable = e.code == 429 or e.code >= 500 or err.get("code") == "conflict_error"
                if retryable and attempt < self.max_retries:
                    ra = e.headers.get("Retry-After")
                    delay = float(ra) if ra else min(30.0, 1.5 * 2 ** attempt)
                    print(f"[notion] {e.code} {err.get('code')} → {delay:.1f}초 후 재시도")
                    time.sleep(delay)
                    continue
                raise NotionError(e.code, err.get("code", "?"), err.get("message", ""))
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(min(30.0, 1.5 * 2 ** attempt))
                    continue
                raise NotionError(0, "network_error", str(e.reason))
        raise NotionError(0, "retry_exhausted", path)

    def get(self, path, query=None):
        return self._req("GET", path, query=query)

    def post(self, path, body):
        return self._req("POST", path, body=body)

    def patch(self, path, body):
        return self._req("PATCH", path, body=body)

    def delete(self, path):
        return self._req("DELETE", path)

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            r = self.get(f"/blocks/{block_id}/children", {"page_size": 100, "start_cursor": cursor})
            out += r.get("results", [])
            if not r.get("has_more"):
                return out
            cursor = r.get("next_cursor")

    def query_db(self, db_id: str, body: dict) -> list[dict]:
        out, cursor = [], None
        while True:
            b = dict(body, page_size=100)
            if cursor:
                b["start_cursor"] = cursor
            r = self.post(f"/databases/{db_id}/query", b)
            out += r.get("results", [])
            if not r.get("has_more"):
                return out
            cursor = r.get("next_cursor")

    def upload_file(self, path: str, content_type: str = "image/png") -> str:
        """Notion 파일 업로드(단일 파트, 20MB 이하) → file_upload id (1시간 안에 블록에 붙여야 함)."""
        name = os.path.basename(path)
        fu = self.post("/file_uploads", {"filename": name, "content_type": content_type})
        boundary = "----mrm" + uuid.uuid4().hex
        with open(path, "rb") as f:
            payload = f.read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n").encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode()
        r = self._req("POST", f"/file_uploads/{fu['id']}/send", raw=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if r.get("status") != "uploaded":
            raise NotionError(0, "upload_failed", f"{name}: status={r.get('status')}")
        return fu["id"]


class DryNotion(Notion):
    """--dry-run: 네트워크 없이 요청을 기록만 한다."""

    def __init__(self):
        super().__init__("dry-run")
        self.log = []
        self.n = 0

    def _req(self, method, path, body=None, raw=None, headers=None, query=None):
        self.calls += 1
        self.log.append({"method": method, "path": path, "body": body if raw is None else f"<{len(raw)} bytes>"})
        self.n += 1
        fake = f"00000000-0000-0000-0000-{self.n:012d}"
        if path.endswith("/children") and method == "GET":
            return {"results": [], "has_more": False}
        if path.endswith("/query"):
            return {"results": [], "has_more": False}
        if path.startswith("/databases/") and method == "GET":
            return {"id": path.split("/")[2], "properties": {k: {"type": next(iter(v))} for k, v in db_schema().items()}}
        if path.endswith("/send"):
            return {"status": "uploaded"}
        return {"id": fake, "url": f"https://www.notion.so/{fake.replace('-', '')}", "status": "pending"}


# ===========================================================================
# 블록·속성 도우미
# ===========================================================================
def _num(v, nd: int = 4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


def rt(text: str, bold: bool = False, color: str = "default", link: str | None = None, code: bool = False) -> dict:
    t = {"content": str(text)[:2000]}
    if link:
        t["link"] = {"url": link}
    return {"type": "text", "text": t, "annotations": {"bold": bold, "italic": False, "strikethrough": False,
                                                     "underline": False, "code": code, "color": color}}


def _blk(kind: str, **payload) -> dict:
    return {"object": "block", "type": kind, kind: payload}


def para(*parts, color="default"):
    return _blk("paragraph", rich_text=[p if isinstance(p, dict) else rt(p) for p in parts], color=color)


def h2(text):
    return _blk("heading_2", rich_text=[rt(text)])


def h3(text):
    return _blk("heading_3", rich_text=[rt(text)])


def bullet(*parts):
    return _blk("bulleted_list_item", rich_text=[p if isinstance(p, dict) else rt(p) for p in parts])


def callout(parts: list, emoji: str, color: str):
    return _blk("callout", rich_text=parts, icon={"type": "emoji", "emoji": emoji}, color=color)


def table(rows: list[list], header: bool = True) -> dict:
    width = len(rows[0])
    kids = []
    for r in rows:
        cells = [[c] if isinstance(c, dict) else ([rt(c)] if str(c) != "" else []) for c in r]
        kids.append(_blk("table_row", cells=cells))
    return _blk("table", table_width=width, has_column_header=header, has_row_header=False, children=kids)


def image(src: dict, caption: str = ""):
    return _blk("image", **src, caption=[rt(caption)] if caption else [])


def embed(url: str):
    return _blk("embed", url=url)


def divider():
    return _blk("divider")


def bar(score: float, n: int = 10) -> str:
    k = int(round(max(0.0, min(100.0, score)) / (100 / n)))
    return "█" * k + "░" * (n - k)


# ===========================================================================
# 데이터베이스
# ===========================================================================
def db_schema() -> dict:
    pct = {"number": {"format": "percent"}}
    num = {"number": {"format": "number"}}
    return {
        "기준일": {"title": {}},
        "날짜": {"date": {}},
        "레짐": {"select": {"options": [{"name": "안정", "color": "green"}, {"name": "주의", "color": "yellow"},
                                        {"name": "경계", "color": "orange"}, {"name": "위험", "color": "red"}]}},
        "사분면": {"select": {"options": [{"name": "안정(Calm)", "color": "green"},
                                         {"name": "취약·안일(Fragile)", "color": "yellow"},
                                         {"name": "충격(Shock)", "color": "orange"},
                                         {"name": "위기(Crisis)", "color": "red"}]}},
        "종합점수": num,
        "스트레스": num,
        "취약성": num,
        "낙폭확률(보정)": pct,
        "낙폭확률(모형)": pct,
        "예상변동성": pct,
        "VaR95(1일)": pct,
        "KOSPI(재구성)": {"number": {"format": "number_with_commas"}},
        "1일 수익률": pct,
        "고점대비": pct,
        "경보 수": num,
        "경보": {"rich_text": {}},
    }


def build_rows(data: dict, days: int) -> list[dict]:
    """history.csv 최근 days 거래일 → 행 목록. 마지막 날은 kpi·경보로 보강."""
    h = data["hist"].copy()
    k = data["kpi"]
    h["ret1d"] = h["kospi"].pct_change()
    s_thr, f_thr = float(k["quad_stress_thr"]), float(k["quad_fragility_thr"])

    def quad(s, f):
        if pd.isna(s) or pd.isna(f):
            return None
        if s >= s_thr and f >= f_thr:
            return "위기(Crisis)"
        if s >= s_thr:
            return "충격(Shock)"
        if f >= f_thr:
            return "취약·안일(Fragile)"
        return "안정(Calm)"

    rows = []
    for _, r in h.tail(days).iterrows():
        rows.append({"date": str(r["date"])[:10], "regime": r["regime"] if r["regime"] in REG_STYLE else None,
                     "quadrant": quad(r["stress"], r["fragility"]), "composite": _num(r["composite"], 2),
                     "stress": _num(r["stress"], 2), "fragility": _num(r["fragility"], 2), "prob_model": _num(r["prob"]),
                     "prob_cal": None, "vol": _num(r["vol_fc"]), "var95": None, "kospi": _num(r["kospi"], 2),
                     "ret1d": _num(r["ret1d"]), "dd": _num(r["drawdown"]), "n_alerts": None, "alerts": None})
    if rows and rows[-1]["date"] == str(k["asof"]):
        al = data["alerts"]
        main = al[al["level"].isin(["high", "medium"])] if len(al) else al
        rows[-1].update({"prob_cal": _num(k["prob_calibrated"]), "var95": _num(k["var95_1d"]),
                         "quadrant": k["quadrant"], "n_alerts": int(len(main)),
                         "alerts": " / ".join(main["code"].astype(str)) if len(main) else "—"})
    return rows


def row_properties(r: dict) -> dict:
    def n(v):
        return {"number": v}
    p = {
        "기준일": {"title": [rt(r["date"])]},
        "날짜": {"date": {"start": r["date"]}},
        "레짐": {"select": {"name": r["regime"]} if r["regime"] else None},
        "사분면": {"select": {"name": r["quadrant"]} if r["quadrant"] else None},
        "종합점수": n(r["composite"]), "스트레스": n(r["stress"]), "취약성": n(r["fragility"]),
        "낙폭확률(모형)": n(r["prob_model"]), "예상변동성": n(r["vol"]), "KOSPI(재구성)": n(r["kospi"]),
        "1일 수익률": n(r["ret1d"]), "고점대비": n(r["dd"]),
    }
    if r["prob_cal"] is not None:          # 마지막 날에만 있는 값 — 과거 행의 기존 값을 지우지 않도록 있을 때만 보냄
        p["낙폭확률(보정)"] = n(r["prob_cal"])
        p["VaR95(1일)"] = n(r["var95"])
        p["경보 수"] = n(r["n_alerts"])
        p["경보"] = {"rich_text": [rt(r["alerts"])]}
    return p


def ensure_schema(nt: Notion, db_id: str) -> None:
    db = nt.get(f"/databases/{db_id}")
    have = db.get("properties", {})
    want = db_schema()
    missing = {k: v for k, v in want.items() if k not in have and "title" not in v}
    for k, v in want.items():
        if k in have and have[k].get("type") != next(iter(v)):
            print(f"[notion] 경고: '{k}' 속성 유형이 {have[k].get('type')} 입니다 (예상 {next(iter(v))})")
    if missing:
        nt.patch(f"/databases/{db_id}", {"properties": missing})
        print(f"[notion] 로그 데이터베이스에 속성 {len(missing)}개 추가: {', '.join(missing)}")


def upsert_rows(nt: Notion, db_id: str, rows: list[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    since = min(r["date"] for r in rows)
    existing = {}
    for pg in nt.query_db(db_id, {"filter": {"property": "날짜", "date": {"on_or_after": since}}}):
        d = (pg.get("properties", {}).get("날짜", {}) or {}).get("date") or {}
        if d.get("start"):
            existing.setdefault(d["start"][:10], pg["id"])
    created = updated = 0
    for r in rows:
        props = row_properties(r)
        if r["date"] in existing:
            nt.patch(f"/pages/{existing[r['date']]}", {"properties": props})
            updated += 1
        else:
            nt.post("/pages", {"parent": {"database_id": db_id}, "properties": props})
            created += 1
    return created, updated


# ===========================================================================
# 요약 페이지
# ===========================================================================
def build_blocks(data: dict, images: dict, site_url: str | None, generated_at: str) -> list[dict]:
    k = data["kpi"]
    reg = str(k["regime"])
    emoji, color = REG_STYLE.get(reg, ("⚪", "gray_background"))
    comp, st, fr = float(k["composite"]), float(k["stress"]), float(k["fragility"])
    chg = comp - float(k["composite_5d_ago"])
    quad = str(k["quadrant"])
    blocks = [
        callout([
            rt(f"{reg} 레짐 · 종합 리스크 점수 {comp:.1f}점", bold=True),
            rt(f"  (5거래일 전 대비 {chg:+.1f}점)\n"),
            rt(f"스트레스 {st:.1f} · 취약성 {fr:.1f} → {quad}: {QUAD_DESC.get(quad, '')} 국면\n"),
            rt(f"63거래일 안에 −10% 이상 낙폭이 나올 확률 {float(k['prob_calibrated']):.0%}"
               f" (평상시 {float(k['base_rate_holdout']):.0%}) · 예상 변동성 연 {float(k['vol_forecast']):.0%}"
               f" · 1일 VaR95 {float(k['var95_1d']):.1%}"),
        ], emoji, color),
        para(rt(f"데이터 기준일 {k['asof']} · 업데이트 {generated_at} · 자동 생성 페이지라 직접 고친 내용은 다음 실행 때 사라집니다.",
                color="gray")),
    ]
    if images.get("overview"):
        blocks.append(image(images["overview"], "최근 18개월 KOSPI(재구성, 로그)와 위험 점수 · 배경색 = 레짐"))

    blocks += [h2("핵심 수치"), table([
        ["지표", "현재", "참고"],
        ["종합 리스크 점수", f"{comp:.1f}", f"경계값 주의 {float(k['cut_watch']):.1f} · 경계 {float(k['cut_alert']):.1f} · 위험 {float(k['cut_danger']):.1f}"],
        ["스트레스 축", f"{st:.1f}", f"5거래일 전 {float(k['stress_5d_ago']):.1f}"],
        ["취약성 축", f"{fr:.1f}", f"5거래일 전 {float(k['fragility_5d_ago']):.1f}"],
        ["63일 내 −10% 낙폭 확률(보정)", f"{float(k['prob_calibrated']):.0%}",
         f"모형 원값 {float(k['prob_model']):.0%} · 평상시 {float(k['base_rate_holdout']):.0%}"],
        ["예상 변동성(향후 21일, 연율)", f"{float(k['vol_forecast']):.0%}", f"21일 VaR95 {float(k['var95_21d']):.1%}"],
        ["1일 VaR 95% / 99%", f"{float(k['var95_1d']):.1%} / {float(k['var99_1d']):.1%}", f"ES95 {float(k['es95_1d']):.1%}"],
        ["KOSPI(재구성)", f"{float(k['index_level']):,.0f}",
         f"1일 {float(k['index_ret_1d']):+.1%} · 1년 고점 대비 {float(k['drawdown_1y']):.1%}"],
    ])]

    cats = data["cats"]
    if len(cats):
        rows = [["카테고리", "축", "점수", "5일 변화", ""]]
        for _, c in cats.iterrows():
            s = float(c["score"]) if pd.notna(c["score"]) else float("nan")
            rows.append([c["label"], c["axis"], "—" if math.isnan(s) else f"{s:.1f}",
                         "—" if pd.isna(c["change_5d"]) else f"{float(c['change_5d']):+.1f}",
                         "" if math.isnan(s) else bar(s)])
        blocks += [h2("카테고리 점수"), para(rt("0–100, 높을수록 위험. 각 신호의 확장창 백분위 평균.", color="gray")), table(rows)]

    al = data["alerts"]
    blocks.append(h2("활성 경보"))
    main = al[al["level"].isin(["high", "medium"])] if len(al) else al
    if len(main):
        for _, a in main.iterrows():
            icon = "🔴" if a["level"] == "high" else "🟠"
            v = a["value"]
            vs = "" if pd.isna(v) else (f" ({float(v):.3f})" if abs(float(v)) < 1 else f" ({float(v):.1f})")
            blocks.append(bullet(rt(f"{icon} {a['level_kr']} ", bold=True), rt(f"{a['message']}{vs}")))
    else:
        blocks.append(para("조기경보 규칙 중 켜진 것이 없습니다."))
    info = al[al["level"] == "info"] if len(al) else al
    if len(info):
        names = ", ".join(re.sub(r" 역사적 상위.*$", "", str(m)) for m in info["message"])
        blocks.append(para(rt("역사적 상위 5% 극단 신호: ", bold=True), rt(names)))

    br, mv = data.get("breadth", pd.DataFrame()), data.get("movers", pd.DataFrame())
    if len(br) or len(mv):
        blocks.append(h2("코스피 시장 폭과 급등락"))
        if len(br):
            b = br.iloc[0]
            blocks.append(para(rt(f"{b['date']} 종가 기준  ", color="gray"), rt(f"▲ 상승 {int(b['up'])}", color="green"),
                               rt("   "), rt(f"▼ 하락 {int(b['down'])}", color="red"),
                               rt(f"   ■ 보합 {int(b['flat'])}", color="gray")))
        if len(mv):
            rows = [["구분", "종목", "종가", "등락률"]]
            for side, lab in (("gainer", "급등"), ("loser", "급락")):
                for _, r in mv[mv["side"] == side].iterrows():
                    rows.append([lab, str(r["name"]), f"{float(r['close']):,.0f}", f"{float(r['ret_1d']):+.2%}"])
            blocks += [table(rows), para(rt("급등락은 시가총액 상위 200 종목 기준입니다.", color="gray"))]

    nw = data.get("news", pd.DataFrame())
    if len(nw):
        blocks.append(h2("오늘의 시장 이슈"))
        for _, n in nw.head(10).iterrows():
            link = str(n["link"]) if str(n.get("link", "")).startswith("http") else None
            when = str(n.get("published", ""))[11:16]
            blocks.append(bullet(rt(f"[{n['topic']}] ", color="gray"), rt(str(n["title"]), link=link),
                                 rt(f"  {n.get('source', '') if pd.notna(n.get('source')) else ''} {when}".rstrip(), color="gray")))
        blocks.append(para(rt("Google 뉴스 헤드라인입니다. 제목을 누르면 원문으로 이동합니다.", color="gray")))

    sig = data["signals"]
    if len(sig):
        s2 = sig[(sig["use"] == "종합점수") & sig["pct"].notna()].sort_values("pct", ascending=False).head(8)
        rows = [["신호", "카테고리", "백분위", "5일 변화", "검증 AUC(낙폭)"]]
        for _, r in s2.iterrows():
            rows.append([r["label"], r["category_label"], f"{float(r['pct']):.1f}",
                         "—" if pd.isna(r["change_5d"]) else f"{float(r['change_5d']):+.1f}",
                         "—" if pd.isna(r["test_dd"]) else f"{float(r['test_dd']):.2f}"])
        blocks += [h2("주목할 신호 (백분위 상위 8)"), table(rows)]

    tr = data["trans"]
    if len(tr):
        blocks.append(h2("최근 레짐 전환"))
        for _, r in tr.tail(6).iloc[::-1].iterrows():
            blocks.append(bullet(rt(f"{r['date']}  "), rt(f"{REG_STYLE.get(r['regime'], ('⚪',))[0]} {r['regime']}", bold=True)))

    if site_url:
        blocks += [h2("인터랙티브 대시보드"), embed(site_url),
                   para(rt("새 탭에서 열기", link=site_url), rt("  ·  이력·검증 탭과 툴팁은 임베드 안에서도 작동합니다.", color="gray"))]

    fresh = data["fresh"]
    fr_txt = ", ".join(f"{r['series']} {r['last_obs']}" for _, r in fresh.iterrows()
                       if "수동" not in str(r["series"])) if len(fresh) else ""
    blocks += [divider(), h3("읽는 법"),
               bullet("점수는 각 신호가 '그날까지의 과거 전체'에서 차지하는 백분위의 평균입니다(0–100, 높을수록 위험)."),
               bullet("레짐 경계값은 1998–2011년 학습구간 종합점수의 50·75·90 백분위로 정했고, 2012년 이후는 검증용으로만 썼습니다."),
               bullet("변동성·꼬리위험의 '크기'를 재는 도구입니다. 수익률 방향은 예측하지 못합니다."),
               bullet(rt("데이터 마지막 관측일: ", bold=True), rt(fr_txt or "—"))]
    return blocks


def replace_children(nt: Notion, page_id: str, blocks: list[dict]) -> None:
    """새 블록을 먼저 붙인 뒤 옛 블록을 지운다(갱신 중에도 페이지가 비지 않도록)."""
    old = [b["id"] for b in nt.children(page_id)]
    for i in range(0, len(blocks), 100):
        nt.patch(f"/blocks/{page_id}/children", {"children": blocks[i:i + 100]})
    for bid in old:
        try:
            nt.delete(f"/blocks/{bid}")
        except NotionError as e:
            if e.status != 404:
                raise


# ===========================================================================
# 구조 찾기/만들기
# ===========================================================================
def parse_id(s: str) -> str:
    s = s.strip()
    u = urllib.parse.urlparse(s)
    q = urllib.parse.parse_qs(u.query)
    cand = q.get("p", [None])[0] or (u.path.rstrip("/").split("/")[-1] if u.scheme else s)
    m = re.search(r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})$", cand)
    if not m:
        raise SystemExit(f"NOTION_PAGE_ID에서 페이지 ID(32자리)를 찾지 못했습니다: {s!r}")
    h = m.group(1).replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def ensure_structure(nt: Notion, parent_id: str) -> tuple[str, str, str | None]:
    page_id = os.environ.get("NOTION_DASHBOARD_PAGE_ID")
    db_id = os.environ.get("NOTION_DATABASE_ID")
    page_url = None
    if not (page_id and db_id):
        for b in nt.children(parent_id):
            if b.get("type") == "child_page" and b["child_page"].get("title") == PAGE_TITLE and not page_id:
                page_id = b["id"]
            if b.get("type") == "child_database" and b["child_database"].get("title") == DB_TITLE and not db_id:
                db_id = b["id"]
    if not page_id:
        pg = nt.post("/pages", {"parent": {"page_id": parent_id}, "icon": {"type": "emoji", "emoji": "📉"},
                                "properties": {"title": {"title": [rt(PAGE_TITLE)]}}})
        page_id, page_url = pg["id"], pg.get("url")
        print(f"[notion] 요약 페이지 생성: {PAGE_TITLE}")
    if not db_id:
        db = nt.post("/databases", {"parent": {"type": "page_id", "page_id": parent_id}, "is_inline": True,
                                    "icon": {"type": "emoji", "emoji": "📈"},
                                    "title": [rt(DB_TITLE)], "properties": db_schema()})
        db_id = db["id"]
        print(f"[notion] 로그 데이터베이스 생성: {DB_TITLE}")
    return page_id, db_id, page_url


def prepare_images(nt: Notion, charts_dir: str | None, mode: str, site_url: str | None, asof: str) -> dict:
    out = {}
    if mode == "none" or not charts_dir:
        return out
    path = os.path.join(charts_dir, "overview.png")
    if not os.path.exists(path):
        print(f"[notion] 차트 파일 없음({path}) — 이미지 없이 진행")
        return out
    if mode in ("auto", "upload"):
        try:
            out["overview"] = {"type": "file_upload", "file_upload": {"id": nt.upload_file(path)}}
            return out
        except NotionError as e:
            print(f"[notion] 이미지 업로드 실패({e}) — " + ("사이트 주소로 대체" if site_url else "이미지 생략"))
            if mode == "upload" and not site_url:
                return out
    if site_url:
        base = site_url.split("#")[0].split("?")[0].rstrip("/")
        out["overview"] = {"type": "external", "external": {"url": f"{base}/charts/overview.png?v={asof}"}}
    return out


def load_inputs(in_dir: str) -> dict:
    d = os.path.join(in_dir, "dashboard")

    def rd(name):
        p = os.path.join(d, name)
        return pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()
    kpi = rd("kpi.csv")
    if not len(kpi):
        raise SystemExit(f"{d}/kpi.csv 가 없습니다. 먼저 run_monitor.py 를 실행하세요.")
    return {"kpi": kpi.iloc[0].to_dict(), "cats": rd("categories.csv"), "alerts": rd("alerts.csv"),
            "signals": rd("signals.csv"), "hist": rd("history.csv"), "trans": rd("transitions.csv"),
            "fresh": rd("freshness.csv"), "movers": rd("movers.csv"), "breadth": rd("breadth.csv"),
            "news": rd("news.csv")}


HINTS = {
    401: "토큰이 틀렸거나 폐기됐습니다. Notion 통합 설정에서 시크릿을 다시 복사해 NOTION_TOKEN에 넣으세요.",
    403: "통합에 권한이 없습니다. 통합 설정의 '기능'에서 콘텐츠 읽기·업데이트·삽입을 모두 켜세요.",
    404: "부모 페이지를 찾지 못했습니다. 그 페이지 오른쪽 위 ⋯ → 연결(Connections)에서 통합을 추가했는지, NOTION_PAGE_ID가 맞는지 확인하세요.",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KOSPI 리스크 모니터 → Notion 동기화")
    ap.add_argument("--in", dest="in_dir", default="outputs/KR")
    ap.add_argument("--charts", default="site/charts", help="build_site.py 가 만든 차트 폴더")
    ap.add_argument("--page", default=os.environ.get("NOTION_PAGE_ID"), help="부모 페이지 URL 또는 ID")
    ap.add_argument("--site-url", default=os.environ.get("SITE_URL") or None)
    ap.add_argument("--images", choices=["auto", "upload", "url", "none"], default="auto")
    ap.add_argument("--days", type=int, default=5, help="매 실행 시 다시 쓰는 최근 거래일 수")
    ap.add_argument("--backfill", type=int, default=0, help="과거 N거래일을 로그에 기록(처음 한 번)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dry-out", default="notion_dry_run.json")
    a = ap.parse_args(argv)

    token = os.environ.get("NOTION_TOKEN")
    if not a.dry_run and (not token or not a.page):
        print("NOTION_TOKEN 과 NOTION_PAGE_ID 를 설정하세요 (README의 'Notion 연결' 참고). --dry-run 으로 미리보기는 가능합니다.")
        return 2
    nt = DryNotion() if a.dry_run else Notion(token)
    data = load_inputs(a.in_dir)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        parent = parse_id(a.page) if a.page else "00000000-0000-0000-0000-000000000000"
        page_id, db_id, page_url = ensure_structure(nt, parent)
        ensure_schema(nt, db_id)
        rows = build_rows(data, max(a.days, a.backfill))
        c, u = upsert_rows(nt, db_id, rows)
        print(f"[notion] 로그: 새 행 {c}개, 갱신 {u}개 ({rows[0]['date']} ~ {rows[-1]['date']})")
        imgs = prepare_images(nt, a.charts, a.images, a.site_url, str(data["kpi"]["asof"]))
        blocks = build_blocks(data, imgs, a.site_url, generated)
        replace_children(nt, page_id, blocks)
        print(f"[notion] 요약 페이지 갱신: 블록 {len(blocks)}개 · API 호출 {nt.calls}회"
              + (f" · {page_url}" if page_url else ""))
    except NotionError as e:
        print(f"[notion] 오류 {e}")
        if e.status in HINTS:
            print("  → " + HINTS[e.status])
        return 1
    if a.dry_run:
        with open(a.dry_out, "w", encoding="utf-8") as f:
            json.dump(nt.log, f, ensure_ascii=False, indent=1)
        print(f"[dry-run] 요청 {len(nt.log)}건을 {a.dry_out} 에 저장했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
