"""
대시보드용 데이터 파일 내보내기 (CSV/JSON) — pipeline.run() 결과를 받아 out_dir/dashboard/ 에 저장
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import evaluate as E
from . import model as M


def export_dashboard(res: dict, out_dir: str, anchor: tuple[str, float] | None = None) -> dict:
    """anchor=(날짜, 실제 지수값): 재구성 지수를 실제 지수 수준에 맞춤(표시용)."""
    d = os.path.join(out_dir, "dashboard")
    os.makedirs(d, exist_ok=True)
    s = res["snapshot"]
    agg, prob, regime, raw, mkt, volf = res["agg"], res["prob"], res["regime"], res["raw"], res["mkt"], res["volf"]
    lvl = (1 + mkt).cumprod()
    if anchor:
        lvl = lvl * anchor[1] / lvl.loc[anchor[0]]
    tg = M.forward_targets(mkt)
    labels = E.make_labels(tg)
    i = agg.index.get_loc(pd.Timestamp(s["asof"]))
    files = {}

    def save(name, df):
        p = os.path.join(d, name)
        df.to_csv(p, index=False, float_format="%.6g")
        files[name] = p

    # 1) 현재 판독 KPI (1행)
    rn = s["risk_now"]
    kpi = pd.DataFrame([{
        "asof": s["asof"], "market": s["market"], "index_level": float(lvl.iloc[i]),
        "index_ret_1d": float(mkt.iloc[i]), "drawdown_1y": s["drawdown"],
        "composite": s["composite"], "composite_5d_ago": s["composite_5d_ago"],
        "composite_21d_ago": s["composite_21d_ago"], "stress": s["stress"], "fragility": s["fragility"],
        "stress_5d_ago": float(agg["STRESS"].iloc[i - 5]), "fragility_5d_ago": float(agg["FRAGILITY"].iloc[i - 5]),
        "regime": s["regime"], "quadrant": s["quadrant"],
        "cut_watch": s["regime_cuts"][0], "cut_alert": s["regime_cuts"][1], "cut_danger": s["regime_cuts"][2],
        "prob_model": s["prob_dd10_63d"], "prob_calibrated": s["prob_dd10_63d_calibrated"],
        "base_rate_all": s["validation"]["base_rate_dd10"],
        "base_rate_holdout": float(labels["y_dd"].loc["2012":].mean()),
        "vol_forecast": rn["vol_forecast_21d_ann"], "var95_1d": rn["VaR95_1d"], "var99_1d": rn["VaR99_1d"],
        "es95_1d": rn["ES95_1d"], "var95_21d": rn["VaR95_21d_normal"], "w_stress": s["w_stress"],
        "quad_stress_thr": float(agg["STRESS"].loc["1998":"2011"].quantile(0.75)),
        "quad_fragility_thr": float(agg["FRAGILITY"].loc["1998":"2011"].quantile(0.75)),
    }])
    save("kpi.csv", kpi)

    # 2) 카테고리
    cats = pd.DataFrame([{"category": k, "label": v["label"], "axis": "스트레스" if v["axis"] == "stress" else "취약성",
                          "score": v["score"], "score_5d_ago": v["score_5d_ago"],
                          "change_5d": (v["score"] - v["score_5d_ago"]) if v["score"] is not None and v["score_5d_ago"] is not None else None}
                         for k, v in s["categories"].items()])
    save("categories.csv", cats)

    # 3) 신호 + 검증 AUC
    sig = pd.DataFrame(s["signals"])
    auc = res["sig_auc"][["train_dd", "test_dd", "train_vol", "test_vol"]].reset_index().rename(columns={"signal": "name"})
    sig = sig.merge(auc, on="name", how="left")
    sig["axis"] = sig["axis"].map({"stress": "스트레스", "fragility": "취약성"})
    sig["category_label"] = sig["category"].map(M.CATEGORY_LABEL)
    sig["change_5d"] = sig["pct"] - sig["pct_5d_ago"]
    sig["use"] = np.where(sig["in_composite"], "종합점수", "모니터링 전용")
    save("signals.csv", sig[["name", "label", "category_label", "axis", "use", "raw", "pct", "change_5d",
                             "train_dd", "test_dd", "train_vol", "test_vol"]])

    # 4) 경보
    al = pd.DataFrame(s["alerts"]) if s["alerts"] else pd.DataFrame(columns=["level", "code", "message", "value"])
    al["level_kr"] = al["level"].map({"high": "높음", "medium": "중간", "info": "참고"})
    al["rank"] = al["level"].map({"high": 0, "medium": 1, "info": 2})
    al = al.sort_values("rank").reset_index(drop=True)
    al["id"] = [f"a{k}" for k in range(len(al))]
    save("alerts.csv", al[["id", "level", "level_kr", "code", "message", "value"]])

    # 5) 일별 이력
    hist = pd.DataFrame({
        "date": agg.index.strftime("%Y-%m-%d"), "kospi": lvl.reindex(agg.index).to_numpy(),
        "composite": agg["COMPOSITE"].to_numpy(), "stress": agg["STRESS"].to_numpy(),
        "fragility": agg["FRAGILITY"].to_numpy(), "prob": prob.to_numpy(), "vol_fc": volf.reindex(agg.index).to_numpy(),
        "drawdown": raw["drawdown"].to_numpy(), "regime": regime.astype(str).to_numpy(),
    })
    hist = hist[hist["date"] >= "1998-01-01"].dropna(subset=["composite"])
    save("history.csv", hist)

    # 6) 레짐별 사후 성과
    rs = []
    for nm, df in [("전체 1998–", res["rs_all"]), ("홀드아웃 2012–", res["rs_test"])]:
        x = df.reset_index().rename(columns={"P(DD≥10%,63d)": "p_dd10", "P(DD≥20%,63d)": "p_dd20"})
        x.insert(0, "sample", nm)
        rs.append(x)
    rs = pd.concat(rs)
    rs["key"] = rs["sample"] + "|" + rs["regime"].astype(str)
    save("regime_stats.csv", rs)

    # 7) 종합·카테고리 AUC
    ca = res["comp_auc"].reset_index()
    ca["label"] = ca["signal"].map({**M.CATEGORY_LABEL, "STRESS": "스트레스 축", "FRAGILITY": "취약성 축",
                                    "COMPOSITE": "종합점수", "WF_PROB": "확률모형(워크포워드)"})
    save("composite_auc.csv", ca)

    # 8) 사건 연구
    ev = res["events"].copy()
    ev["key"] = ev["peak"].astype(str)
    save("events.csv", ev)

    # 9) 오버레이 비교
    ov = []
    for nm, df, smp in [("레짐 노출조절", res["bt_all"], "2000–"), ("레짐 노출조절", res["bt_test"], "2012–"),
                        ("변동성 목표 20%", res["vt_all"], "2003–"), ("변동성 목표 20%", res["vt_test"], "2012–")]:
        x = df.reset_index().rename(columns={"index": "series"})
        x.attrs = {}
        x["strategy"] = np.where(x["series"] == "mkt", "매수후보유", nm)
        x["sample"] = smp
        x["key"] = nm + "|" + smp + "|" + x["series"]
        ov.append(x)
    ov = pd.concat(ov)[["key", "strategy", "sample", "CAGR", "Vol", "Sharpe(rf=0)", "MaxDD", "Calmar", "AvgExposure"]]
    ov.columns = ["key", "strategy", "sample", "cagr", "vol", "sharpe", "maxdd", "calmar", "avg_exposure"]
    save("overlay.csv", ov)

    # 10) 확률 보정표 (홀드아웃)
    dd = pd.concat([prob.rename("p"), labels["y_dd"]], axis=1).dropna().loc["2012":]
    dd["bin"] = pd.cut(dd["p"], [0, .1, .2, .3, .4, .5, .6, .7, 1.0])
    cal = dd.groupby("bin", observed=True).agg(n=("p", "size"), predicted=("p", "mean"), realized=("y_dd", "mean")).reset_index()
    cal["bin"] = cal["bin"].astype(str)
    save("calibration.csv", cal)

    # 11) 데이터 신선도 + 수동 보강
    fr = [{"series": k, "last_obs": v, "note": ""} for k, v in s["meta"]["macro_last_obs"].items()]
    fr.insert(0, {"series": "KRX 전종목(marcap)", "last_obs": s["asof"], "note": ""})
    for m in s["meta"]["manual_obs"]:
        fr.append({"series": f"{m['series']} (수동 보강)", "last_obs": m["date"], "note": f"{m['value']} — {m['source']}"})
    save("freshness.csv", pd.DataFrame(fr))

    # 12) 상위 종목 / 레짐 전환
    th = pd.DataFrame(s["meta"]["top_holdings"])
    save("top_holdings.csv", th)
    tr = pd.DataFrame(s["transitions"][-14:])
    save("transitions.csv", tr)

    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({k: os.path.basename(v) for k, v in files.items()}, f, ensure_ascii=False, indent=1)
    return files
