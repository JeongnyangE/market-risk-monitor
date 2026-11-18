#!/usr/bin/env python3
"""
대시보드 웹페이지 생성 — Notion /embed 로 넣을 단일 HTML

  python notion_kit/build_site.py --in outputs/KR --out site

결과
  site/index.html        데이터·d3가 모두 들어 있는 단일 파일 (인터넷 없이도 열림)
  site/charts/*.png      Notion 페이지용 정적 차트
  site/latest.json       현재 판독 요약(JSON)
  site/.nojekyll         GitHub Pages가 파일을 그대로 서비스하도록

주소 뒤에 붙일 수 있는 옵션
  ?theme=dark | light    Notion 다크 모드에 맞추기
  #now | #history | #validation | #method   처음 열 탭
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TABLES = ["kpi", "categories", "signals", "alerts", "history", "regime_stats", "composite_auc", "events",
          "overlay", "calibration", "freshness", "top_holdings", "transitions"]
SOURCES = "KRX 전종목(FinanceData/marcap), Cboe VIX, FRB H.10 환율, EIA 브렌트유"


def _clean(v, digits: int = 6):
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return float(f"{v:.{digits}g}")
    if hasattr(v, "item"):            # numpy 스칼라
        return _clean(v.item(), digits)
    return v


def table_json(df: pd.DataFrame, digits: int = 6) -> dict:
    df = df.astype(object).where(df.notna(), None)
    return {"columns": [str(c) for c in df.columns],
            "rows": [[_clean(v, digits) for v in row] for row in df.itertuples(index=False, name=None)]}


def build(in_dir: str, out_dir: str, template: str | None = None, d3_path: str | None = None,
          charts: bool = True) -> dict:
    dash_dir = os.path.join(in_dir, "dashboard")
    template = template or os.path.join(HERE, "site_template.html")
    d3_path = d3_path or os.path.join(HERE, "vendor", "d3.min.js")
    os.makedirs(out_dir, exist_ok=True)
    tables = {}
    for t in TABLES:
        p = os.path.join(dash_dir, f"{t}.csv")
        if not os.path.exists(p):
            print(f"[site] {t}.csv 없음 — 해당 카드는 비어 보입니다")
            continue
        df = pd.read_csv(p, dtype={"code": str}) if t == "top_holdings" else pd.read_csv(p)
        tables[t] = table_json(df, digits=5 if t == "history" else 6)
    kpi = pd.read_csv(os.path.join(dash_dir, "kpi.csv")).iloc[0].to_dict()
    now = dt.datetime.now(dt.timezone.utc)
    meta = {"asof": str(kpi["asof"]), "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"), "sources": SOURCES,
            "market": str(kpi.get("market", ""))}
    payload = json.dumps({"meta": meta, "tables": tables}, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")           # <script> 안에서 태그가 닫히지 않도록
    d3 = open(d3_path, encoding="utf-8").read().replace("</script", "<\\/script")
    html = open(template, encoding="utf-8").read()
    assert "/*__D3__*/" in html and "__MRM_DATA__" in html, "템플릿 자리표시자 누락"
    html = html.replace("/*__D3__*/", d3, 1).replace("__MRM_DATA__", payload, 1)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    latest = {k: _clean(v) for k, v in kpi.items()}
    latest["generated_at"] = meta["generated_at"]
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    out = {"index": os.path.join(out_dir, "index.html"), "bytes": len(html.encode("utf-8"))}
    if charts:
        from charts import render_all
        out["charts"] = render_all(dash_dir, os.path.join(out_dir, "charts"))
    return out


def main():
    ap = argparse.ArgumentParser(description="KOSPI 리스크 대시보드 웹페이지 생성")
    ap.add_argument("--in", dest="in_dir", default="outputs/KR", help="run_monitor.py 결과 폴더")
    ap.add_argument("--out", default="site")
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args()
    res = build(a.in_dir, a.out, charts=not a.no_charts)
    print(f"[site] {res['index']} ({res['bytes'] / 1e6:.2f} MB)")
    for k, v in res.get("charts", {}).items():
        print(f"[site] chart {k}: {v}")


if __name__ == "__main__":
    main()
