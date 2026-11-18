#!/usr/bin/env python3
"""
시장 리스크 모니터 실행기

예)
  # 한국(KOSPI): GitHub 데이터 동기화 후 실행
  python run_monitor.py --market KR --data-root ./data_root --sync

  # 미국(S&P 500): yfinance + FRED (인터넷 필요, pip install yfinance)
  python run_monitor.py --market US --fred-key YOUR_KEY

출력(out 폴더)
  snapshot.json          현재 판독: 종합점수·레짐·확률·VaR·경보·신호별 백분위·검증 요약
  risk_history.csv       일별 종합/스트레스/취약성/확률/레짐 이력
  signals_full.csv       원시 신호 + 백분위 + 카테고리 점수 전체
  signal_auc.csv         신호별 학습/홀드아웃 AUC
  composite_auc.csv      카테고리·종합·확률모형 AUC
  regime_stats_holdout.csv, event_study.csv
"""
import argparse
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mrm import data as D          # noqa: E402
from mrm import pipeline as P      # noqa: E402
from mrm.export import export_dashboard  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Quant Market Risk Monitor")
    ap.add_argument("--market", choices=["KR", "US"], default="KR")
    ap.add_argument("--data-root", default="./data_root", help="GitHub 데이터 저장 폴더 (KR)")
    ap.add_argument("--sync", action="store_true", help="GitHub 데이터 clone/pull")
    ap.add_argument("--refresh", action="store_true", help="패널·교차단면 캐시 재계산")
    ap.add_argument("--manual", default=os.path.join(os.path.dirname(__file__), "config", "manual_observations.csv"))
    ap.add_argument("--fred-key", default=os.environ.get("FRED_API_KEY"))
    ap.add_argument("--out", default="./outputs")
    ap.add_argument("--anchor", default="2026-10-15:5554.22",
                    help="재구성 지수를 실제 지수에 맞출 기준 '날짜:종가' (KR 전용, 표시용). 'none'이면 미적용")
    a = ap.parse_args()

    if a.market == "KR":
        if a.sync:
            D.sync_github(a.data_root)
        mkt, xs, macro, meta = P.prepare_kr(a.data_root, a.manual, os.path.join(a.data_root, "cache"), a.refresh)
    else:
        mkt, xs, macro, meta = P.prepare_us(a.fred_key)
    out_dir = os.path.join(a.out, a.market)
    res = P.run(mkt, xs, macro, meta, out_dir)
    anchor = None
    if a.market == "KR" and a.anchor and a.anchor.lower() != "none":
        d, v = a.anchor.split(":")
        anchor = (d, float(v))
    export_dashboard(res, out_dir, anchor=anchor)     # → out/<market>/dashboard/*.csv (웹·노션용)
    s = res["snapshot"]
    print(json.dumps({k: s[k] for k in ["asof", "market", "composite", "stress", "fragility", "regime", "quadrant",
                                        "prob_dd10_63d", "prob_dd10_63d_calibrated", "risk_now"]},
                     ensure_ascii=False, indent=1))
    for al in s["alerts"]:
        if al["level"] != "info":
            print(f"[{al['level'].upper()}] {al['message']} ({al['value']})")
    print(f"\n→ 결과: {os.path.abspath(out_dir)}")


if __name__ == "__main__":
    main()
