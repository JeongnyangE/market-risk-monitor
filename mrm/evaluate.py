"""
검증 — 학습(1998–2011) / 홀드아웃(2012–현재) 분리

  1) 신호별 예측력(AUC): 향후 63일 내 -10% 이상 낙폭, 향후 21일 변동성 상위 20%
  2) 레짐별 사후 성과: 낙폭 확률, 평균 최대낙폭, 변동성, 수익률
  3) 대형 하락 사건 연구: 고점 전후 신호 수준, 선행 경보 일수
  4) 리스크 오버레이(노출 조절) 예시 백테스트
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


def make_labels(tg: pd.DataFrame, dd_thr: float = -0.10, vol_q: float = 0.8) -> pd.DataFrame:
    y_dd = (tg["fwd_maxdd"] <= dd_thr).astype(float).where(tg["fwd_maxdd"].notna())
    thr = tg["fwd_vol"].shift(22).expanding(504).quantile(vol_q)   # 확정된 과거 분포만 사용
    y_vol = (tg["fwd_vol"] >= thr).astype(float).where(tg["fwd_vol"].notna() & thr.notna())
    return pd.DataFrame({"y_dd": y_dd, "y_vol": y_vol})


def _auc(s: pd.Series, y: pd.Series, a=None, b=None) -> float:
    d = pd.concat([s, y], axis=1).dropna()
    d = d.loc[a:b]
    if len(d) < 250 or d.iloc[:, 1].nunique() < 2:
        return np.nan
    return roc_auc_score(d.iloc[:, 1], d.iloc[:, 0])


def auc_table(frame: pd.DataFrame, labels: pd.DataFrame, train=("1998", "2011"), test=("2012", None)) -> pd.DataFrame:
    rows = []
    for c in frame.columns:
        rows.append({
            "signal": c,
            "train_dd": _auc(frame[c], labels["y_dd"], *train),
            "test_dd": _auc(frame[c], labels["y_dd"], *test),
            "train_vol": _auc(frame[c], labels["y_vol"], *train),
            "test_vol": _auc(frame[c], labels["y_vol"], *test),
        })
    return pd.DataFrame(rows).set_index("signal")


def calibrate_bands(score: pd.Series, train=("1998", "2011"), qs=(0.5, 0.75, 0.9)) -> list[float]:
    s = score.loc[train[0]:train[1]].dropna()
    return [float(np.round(s.quantile(q), 1)) for q in qs]


def assign_regime(score: pd.Series, cuts: list[float], names=("안정", "주의", "경계", "위험")) -> pd.Series:
    bins = [-np.inf] + list(cuts) + [np.inf]
    return pd.cut(score, bins=bins, labels=list(names), right=False)


def regime_stats(regime: pd.Series, tg: pd.DataFrame, a=None, b=None) -> pd.DataFrame:
    d = pd.concat([regime.rename("regime"), tg], axis=1).loc[a:b].dropna()
    g = d.groupby("regime", observed=True)
    out = pd.DataFrame({
        "days_share": g.size() / len(d),
        "P(DD≥10%,63d)": g["fwd_maxdd"].apply(lambda x: (x <= -0.10).mean()),
        "P(DD≥20%,63d)": g["fwd_maxdd"].apply(lambda x: (x <= -0.20).mean()),
        "avg_fwd_maxdd": g["fwd_maxdd"].mean(),
        "avg_fwd_vol21": g["fwd_vol"].mean(),
        "avg_fwd_ret63": g["fwd_ret"].mean(),
    })
    return out


def drawdown_episodes(level: pd.Series, threshold: float = 0.20, recovery: float = 0.10) -> pd.DataFrame:
    """고점→저점 하락률 ≥ threshold 인 사건 추출 (저점 이후 recovery 반등 시 사건 종료)."""
    eps = []
    peak_i = 0
    trough_i = 0
    in_dd = False
    vals = level.to_numpy()
    idx = level.index
    for i in range(1, len(vals)):
        if not in_dd:
            if vals[i] > vals[peak_i]:
                peak_i = i
            elif vals[i] / vals[peak_i] - 1 <= -threshold:
                in_dd = True
                trough_i = i
        else:
            if vals[i] < vals[trough_i]:
                trough_i = i
            if vals[i] / vals[trough_i] - 1 >= recovery and vals[i] > vals[trough_i]:
                # 사건 확정 후 다음 고점 탐색 재시작
                eps.append((idx[peak_i], idx[trough_i], vals[trough_i] / vals[peak_i] - 1))
                in_dd = False
                peak_i = i
        if in_dd and i == len(vals) - 1:
            eps.append((idx[peak_i], idx[trough_i], vals[trough_i] / vals[peak_i] - 1))
    return pd.DataFrame(eps, columns=["peak", "trough", "depth"])


def event_study(episodes: pd.DataFrame, agg: pd.DataFrame, regime: pd.Series, prob: pd.Series | None = None,
                lookback: int = 126) -> pd.DataFrame:
    rows = []
    idx = agg.index
    for _, e in episodes.iterrows():
        pi = idx.get_indexer([e["peak"]], method="nearest")[0]
        ti = idx.get_indexer([e["trough"]], method="nearest")[0]
        pre = regime.iloc[max(0, pi - lookback):pi + 1]
        warn = pre.isin(["경계", "위험"])
        first_warn = pre.index[warn.to_numpy()][0] if warn.any() else None
        # 고점 이후: 최초 '경계 이상' 도달까지 걸린 거래일, 그 시점까지 이미 진행된 낙폭
        post = regime.iloc[pi:ti + 1]
        hit = post.isin(["경계", "위험"]).to_numpy()
        lag = int(np.argmax(hit)) if hit.any() else None
        rows.append({
            "peak": e["peak"].date(), "trough": e["trough"].date(), "depth": round(e["depth"], 3),
            "F_at_peak": round(agg["FRAGILITY"].iloc[pi], 1) if not np.isnan(agg["FRAGILITY"].iloc[pi]) else None,
            "S_at_peak": round(agg["STRESS"].iloc[pi], 1) if not np.isnan(agg["STRESS"].iloc[pi]) else None,
            "C_at_peak": round(agg["COMPOSITE"].iloc[pi], 1) if not np.isnan(agg["COMPOSITE"].iloc[pi]) else None,
            "max_C_6m_before": round(agg["COMPOSITE"].iloc[max(0, pi - lookback):pi + 1].max(), 1),
            "warn_days_6m_before": int(warn.sum()),
            "days_peak_to_warn": lag,
            "prob_at_peak": round(float(prob.iloc[pi]), 3) if prob is not None and not np.isnan(prob.iloc[pi]) else None,
        })
    return pd.DataFrame(rows)


def weight_backtest(mkt: pd.Series, weight: pd.Series, cost_bp: float = 10.0, start: str = "2000") -> pd.DataFrame:
    """임의의 비중 시계열(t일 정보로 산출) → t+1일부터 적용."""
    regime_like = weight.shift(1)
    d = pd.concat([mkt.rename("mkt"), regime_like.rename("w")], axis=1).loc[start:].dropna()
    return _perf(d, cost_bp)


def _perf(d: pd.DataFrame, cost_bp: float) -> pd.DataFrame:
    turn = d["w"].diff().abs().fillna(0)
    d = d.assign(strat=d["w"] * d["mkt"] - turn * cost_bp / 1e4)
    out = {}
    for c in ["mkt", "strat"]:
        r = d[c]
        lv = (1 + r).cumprod()
        yrs = len(r) / 252
        cagr = lv.iloc[-1] ** (1 / yrs) - 1
        mdd = (lv / lv.cummax() - 1).min()
        out[c] = {"CAGR": cagr, "Vol": r.std() * np.sqrt(252), "Sharpe(rf=0)": r.mean() / r.std() * np.sqrt(252),
                  "MaxDD": mdd, "Calmar": cagr / abs(mdd), "AvgExposure": d["w"].mean() if c == "strat" else 1.0}
    res = pd.DataFrame(out).T
    res.attrs["curve"] = (1 + d[["mkt", "strat"]]).cumprod()
    return res


def overlay_backtest(mkt: pd.Series, regime: pd.Series, exposure_map: dict, cost_bp: float = 10.0,
                     start: str = "2000") -> pd.DataFrame:
    """레짐 → 다음날 노출 비중. 비교용 예시이며 투자 권유가 아님."""
    expo = regime.astype(object).map(exposure_map).astype(float).shift(1)
    d = pd.concat([mkt.rename("mkt"), expo.rename("w")], axis=1).loc[start:].dropna()
    return _perf(d, cost_bp)
