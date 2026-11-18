"""
엔드투엔드 파이프라인: 데이터 → 신호 → 점수 → 레짐/확률 → 검증 → 스냅샷(JSON)
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import data as D
from . import evaluate as E
from . import model as M
from . import signals as S

TRAIN = ("1998", "2011")
TEST = ("2012", None)


# ---------------------------------------------------------------------------
# 시장별 입력 준비
# ---------------------------------------------------------------------------
def prepare_kr(data_root: str, manual_csv: str | None, cache_dir: str, refresh: bool = False):
    os.makedirs(cache_dir, exist_ok=True)
    pk = os.path.join(cache_dir, "krx_panel.pkl")
    xk = os.path.join(cache_dir, "krx_xs.pkl")
    if refresh or not os.path.exists(pk):
        panel = D.load_krx_panel(os.path.join(data_root, "marcap"), "KOSPI", "1995-01-01")
        pd.to_pickle(panel, pk)
    else:
        panel = pd.read_pickle(pk)
    mkt = D.cap_weighted_index(panel)
    if refresh or not os.path.exists(xk) or pd.read_pickle(xk).index.max() < mkt.index.max():
        ret = panel["ret"].iloc[1:]
        mcap = panel["mcap"].iloc[1:]
        m100 = S.top_n_membership(mcap, 100)
        m50 = S.top_n_membership(mcap, 50)
        xs = pd.concat([
            S.absorption_ratio(ret, m100),
            S.turbulence(ret, m50).rename("turbulence"),
            S.breadth(ret, mcap),
            S.concentration(mcap),
            S.liquidity(ret, panel["amount"].iloc[1:], mcap),
        ], axis=1)
        pd.to_pickle(xs, xk)
    else:
        xs = pd.read_pickle(xk)
    macro_raw = D.load_global_aux(data_root, manual_csv)
    macro = D.align_macro(macro_raw, mkt.index)
    # 상위 종목 정보(스냅샷용)
    last = panel["mcap"].iloc[-1].dropna().sort_values(ascending=False)
    top = [{"code": c, "name": str(panel["names"].get(c, c)), "weight": float(v / last.sum()),
            "ret_1d": float(panel["ret"].iloc[-1].get(c, np.nan))} for c, v in last.head(10).items()]
    meta = {"market": "KOSPI (시총가중 재구성, 보통주)", "top_holdings": top,
            "macro_last_obs": {c: str(macro_raw[c].dropna().index.max().date()) for c in macro_raw},
            "manual_obs": macro_raw.attrs.get("manual", [])}
    return mkt, xs, macro, meta


def prepare_us(fred_api_key: str | None = None, start: str = "1998-01-01"):
    """미국 모듈(인터넷 필요). 교차단면 = 9개 SPDR 섹터 ETF."""
    us = D.load_us_yfinance(start)
    fred = D.load_fred(api_key=fred_api_key, start=start)
    mkt = us["market"]
    ret = us["panel"]["ret"].reindex(mkt.index)
    member = pd.DataFrame(True, index=ret.index, columns=ret.columns)
    lvl = us["panel"]["close"].reindex(mkt.index)
    ma = lvl.rolling(200).mean()
    xs = pd.concat([
        S.absorption_ratio(ret, member, frac_eig=0.2),
        S.turbulence(ret, member).rename("turbulence"),
        pd.DataFrame({
            "pct_above_ma": (lvl > ma).sum(axis=1) / lvl.notna().sum(axis=1),
            "nl_minus_nh": ((lvl <= lvl.rolling(252).min()).sum(axis=1)
                            - (lvl >= lvl.rolling(252).max()).sum(axis=1)) / lvl.notna().sum(axis=1),
            "adv_ratio_21": ((ret > 0).sum(axis=1) / ret.notna().sum(axis=1)).rolling(21).mean(),
            "dispersion_21": ret.std(axis=1).rolling(21).mean(),
        }),
    ], axis=1)
    vol = us["vol"].rename(columns={"^VIX": "VIX", "^VIX3M": "VIX3M", "^VVIX": "VVIX", "^MOVE": "MOVE"})
    macro_raw = pd.concat([vol, fred], axis=1).sort_index()
    if "USD_BROAD" in macro_raw:
        macro_raw["USD_BASKET"] = macro_raw["USD_BROAD"]
    macro = D.align_macro(macro_raw, mkt.index, lag_days=0)   # 같은 미국 장 마감 기준
    meta = {"market": "S&P 500", "top_holdings": [],
            "macro_last_obs": {c: str(macro_raw[c].dropna().index.max().date()) for c in macro_raw},
            "manual_obs": []}
    return mkt, xs, macro, meta


# ---------------------------------------------------------------------------
def run(mkt: pd.Series, xs: pd.DataFrame, macro: pd.DataFrame, meta: dict, out_dir: str,
        w_grid=(0.5, 0.6, 0.7, 0.8)) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    raw = M.build_raw_features(mkt, xs, macro)
    scores = M.score_features(raw)
    tg = M.forward_targets(mkt)
    labels = E.make_labels(tg)

    # 1) 신호 검증표 (학습/홀드아웃)
    sig_auc = E.auc_table(scores, labels, TRAIN, TEST)
    label = {s.name: s.label for s in M.SPEC}
    cat = {s.name: s.category for s in M.SPEC}
    incl = {s.name: s.in_composite for s in M.SPEC}
    sig_auc.insert(0, "label", [label.get(i, i) for i in sig_auc.index])
    sig_auc.insert(1, "category", [cat.get(i, "") for i in sig_auc.index])
    sig_auc.insert(2, "in_composite", [incl.get(i, True) for i in sig_auc.index])
    sig_auc.to_csv(os.path.join(out_dir, "signal_auc.csv"))

    # 2) 스트레스 가중치: 학습구간 AUC로만 선택
    best_w, best_auc, w_table = None, -1, []
    for w in w_grid:
        a = M.aggregate(scores, w_stress=w)
        auc_tr = E._auc(a["COMPOSITE"], labels["y_dd"], *TRAIN)
        auc_te = E._auc(a["COMPOSITE"], labels["y_dd"], *TEST)
        w_table.append({"w_stress": w, "train_auc_dd": auc_tr, "test_auc_dd": auc_te})
        if auc_tr > best_auc:
            best_w, best_auc = w, auc_tr
    agg = M.aggregate(scores, w_stress=best_w)

    # 3) 레짐 경계값: 학습구간 종합점수 분포의 50/75/90 백분위
    cuts = E.calibrate_bands(agg["COMPOSITE"], TRAIN)
    regime = E.assign_regime(agg["COMPOSITE"], cuts)
    quad = M.quadrant(agg["STRESS"], agg["FRAGILITY"],
                      s_thr=float(agg["STRESS"].loc[TRAIN[0]:TRAIN[1]].quantile(0.75)),
                      f_thr=float(agg["FRAGILITY"].loc[TRAIN[0]:TRAIN[1]].quantile(0.75)))

    # 4) 워크포워드 로지스틱: 카테고리 점수 → P(63일 내 -10%)
    cat_cols = [c for c in M.CATEGORY_LABEL if c in agg and agg[c].loc["2003":].notna().mean() > 0.5]
    prob = M.walk_forward_probability(agg[cat_cols], labels["y_dd"])

    # 5) 종합 검증
    comp_auc = E.auc_table(pd.concat([agg[["STRESS", "FRAGILITY", "COMPOSITE"] + cat_cols],
                                      prob.rename("WF_PROB")], axis=1), labels, TRAIN, TEST)
    comp_auc.to_csv(os.path.join(out_dir, "composite_auc.csv"))
    rs_all = E.regime_stats(regime, tg, "1998", None)
    rs_test = E.regime_stats(regime, tg, *TEST)
    rs_test.to_csv(os.path.join(out_dir, "regime_stats_holdout.csv"))
    lvl = (1 + mkt).cumprod()
    eps = E.drawdown_episodes(lvl.loc["1997":], 0.20)
    ev = E.event_study(eps, agg, regime, prob)
    ev.to_csv(os.path.join(out_dir, "event_study.csv"), index=False)
    expo = {"안정": 1.0, "주의": 1.0, "경계": 0.6, "위험": 0.3}
    bt_all = E.overlay_backtest(mkt, regime, expo, start="2000")
    bt_test = E.overlay_backtest(mkt, regime, expo, start="2012")
    # 변동성 목표(20%) 오버레이: HAR 예측 변동성 기반
    volf = M.har_vol_forecast(mkt)
    vt_w = (0.20 / volf).clip(0, 1.0)
    vt_all = E.weight_backtest(mkt, vt_w, start="2003")
    vt_test = E.weight_backtest(mkt, vt_w, start="2012")

    # 6) 현재 스냅샷
    t = agg["COMPOSITE"].dropna().index[-1]
    i = agg.index.get_loc(t)
    p_now = float(prob.iloc[i]) if not pd.isna(prob.iloc[i]) else float("nan")
    p_cal = M.calibrate_now(prob.iloc[: max(0, i - 63)], labels["y_dd"], p_now)
    vol_fc = float(volf.iloc[i]) if not pd.isna(volf.iloc[i]) else float("nan")
    r_hist = mkt.iloc[max(0, i - 1260):i + 1]
    z = (r_hist / r_hist.rolling(21).std()).dropna()      # 변동성 표준화 잔차(필터드 히스토리컬)
    q01, q05 = float(z.quantile(0.01)), float(z.quantile(0.05))
    es05 = float(z[z <= q05].mean())
    d_sd = vol_fc / np.sqrt(252)
    risk_now = {
        "vol_forecast_21d_ann": vol_fc,
        "VaR95_1d": -q05 * d_sd, "VaR99_1d": -q01 * d_sd, "ES95_1d": -es05 * d_sd,
        "VaR95_21d_normal": 1.645 * vol_fc * np.sqrt(21 / 252),
    }
    # 최근 레짐 전환 이력
    reg_s = regime.astype(str)
    chg = reg_s[reg_s != reg_s.shift(1)].loc[str(t.year - 1):]
    transitions = [{"date": str(d.date()), "regime": v} for d, v in chg.items()]
    al = M.alerts(raw, scores, agg, asof=t)
    sig_rows = []
    for s in M.SPEC:
        if s.name not in scores:
            continue
        v = scores[s.name].iloc[i]
        sig_rows.append({"name": s.name, "label": s.label, "category": s.category, "axis": s.axis,
                         "in_composite": s.in_composite,
                         "raw": None if pd.isna(raw[s.name].iloc[i]) else float(raw[s.name].iloc[i]),
                         "pct": None if pd.isna(v) else float(v),
                         "pct_5d_ago": None if pd.isna(scores[s.name].iloc[i - 5]) else float(scores[s.name].iloc[i - 5])})
    hist = pd.DataFrame({
        "index_level": lvl, "COMPOSITE": agg["COMPOSITE"], "STRESS": agg["STRESS"],
        "FRAGILITY": agg["FRAGILITY"], "WF_PROB": prob, "VOL_FC": volf, "regime": regime.astype(str),
        "drawdown": raw["drawdown"],
    }).loc["1998":]
    hist.to_csv(os.path.join(out_dir, "risk_history.csv"))
    pd.concat([raw, scores.add_prefix("pct_"), agg], axis=1).to_csv(os.path.join(out_dir, "signals_full.csv"))

    def _f(x):
        return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 4)

    snapshot = {
        "asof": str(t.date()),
        "market": meta["market"],
        "composite": _f(agg["COMPOSITE"].iloc[i]),
        "stress": _f(agg["STRESS"].iloc[i]),
        "fragility": _f(agg["FRAGILITY"].iloc[i]),
        "regime": str(regime.iloc[i]),
        "quadrant": str(quad.iloc[i]),
        "prob_dd10_63d": _f(prob.iloc[i]),
        "prob_dd10_63d_calibrated": _f(p_cal),
        "risk_now": {k: _f(v) for k, v in risk_now.items()},
        "transitions": transitions,
        "composite_5d_ago": _f(agg["COMPOSITE"].iloc[i - 5]),
        "composite_21d_ago": _f(agg["COMPOSITE"].iloc[i - 21]),
        "regime_cuts": cuts,
        "w_stress": best_w,
        "categories": {c: {"label": M.CATEGORY_LABEL[c], "score": _f(agg[c].iloc[i]),
                           "score_5d_ago": _f(agg[c].iloc[i - 5]),
                           "axis": next(s.axis for s in M.SPEC if s.category == c)} for c in cat_cols},
        "signals": sig_rows,
        "alerts": al,
        "drawdown": _f(raw["drawdown"].iloc[i]),
        "vol_ewma": _f(raw["vol_ewma"].iloc[i]),
        "meta": meta,
        "validation": {
            "w_table": w_table,
            "composite_auc": comp_auc.round(3).reset_index().to_dict(orient="records"),
            "regime_stats_all": rs_all.round(4).reset_index().to_dict(orient="records"),
            "regime_stats_holdout": rs_test.round(4).reset_index().to_dict(orient="records"),
            "event_study": ev.astype(object).where(ev.notna(), None).to_dict(orient="records"),
            "overlay_all": bt_all.round(4).reset_index().to_dict(orient="records"),
            "overlay_holdout": bt_test.round(4).reset_index().to_dict(orient="records"),
            "voltarget_all": vt_all.round(4).reset_index().to_dict(orient="records"),
            "voltarget_holdout": vt_test.round(4).reset_index().to_dict(orient="records"),
            "wf_coef": prob.attrs.get("coef", {}),
            "base_rate_dd10": _f(labels["y_dd"].loc["1998":].mean()),
        },
    }
    with open(os.path.join(out_dir, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1, default=str)
    return {"snapshot": snapshot, "agg": agg, "scores": scores, "raw": raw, "prob": prob, "regime": regime,
            "quad": quad, "sig_auc": sig_auc, "comp_auc": comp_auc, "events": ev, "bt_all": bt_all,
            "bt_test": bt_test, "vt_all": vt_all, "vt_test": vt_test, "volf": volf, "rs_all": rs_all, "rs_test": rs_test, "w_table": w_table, "mkt": mkt}
