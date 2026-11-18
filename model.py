"""
리스크 모델 — 원시 신호 → 백분위 점수 → 카테고리 → 2축(스트레스·취약성) → 종합점수·레짐·경보

구조
  STRESS (지금 시장이 아픈가: 빠른 신호)      FRAGILITY (충격이 오면 크게 번질 구조인가: 느린 신호)
   volatility / tail / systemic / breadth      structure / heat / macro_pressure
   trend / liquidity / global
  종합점수 = w_s * STRESS + (1-w_s) * FRAGILITY
  2축 사분면 레짐: 안정(Calm) / 취약·안일(Fragile) / 충격(Shock) / 위기(Crisis)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import signals as S


@dataclass(frozen=True)
class Sig:
    name: str
    category: str
    axis: str        # 'stress' | 'fragility'
    sign: int        # +1: 값↑ = 위험↑,  -1: 값↓ = 위험↑
    label: str       # 한글 설명
    in_composite: bool = True   # False = 모니터링·경보 전용 (학습구간 검증 탈락)


SPEC: list[Sig] = [
    # ---------------- STRESS ----------------
    Sig("vol_ewma", "volatility", "stress", +1, "EWMA 실현변동성(λ=0.94)"),
    Sig("vol_ratio", "volatility", "stress", +1, "변동성 가속(10일/126일)"),
    Sig("downside_vol", "volatility", "stress", +1, "하방 변동성(63일)"),
    Sig("drawdown", "tail", "stress", -1, "1년 고점 대비 낙폭"),
    Sig("cvar", "tail", "stress", +1, "CVaR 95%(126일)"),
    Sig("turbulence_21", "systemic", "stress", +1, "금융 난기류(마할라노비스, 21일 평균)"),
    Sig("delta_ar", "systemic", "stress", +1, "흡수비율 급변(ΔAR)"),
    Sig("pct_above_ma", "breadth", "stress", -1, "200일선 위 종목 비율"),
    Sig("nlnh_21", "breadth", "stress", +1, "52주 신저가-신고가 비율(21일)"),
    Sig("adv_ratio_21", "breadth", "stress", -1, "상승종목 비율(21일)"),
    Sig("trend_gap", "trend", "stress", -1, "지수/200일선 괴리"),
    Sig("mom_12_1", "trend", "stress", -1, "12-1개월 모멘텀"),
    Sig("amihud_rel", "liquidity", "stress", +1, "Amihud 비유동성(1년 중앙값 대비)"),
    Sig("dispersion_21", "liquidity", "stress", +1, "종목간 수익률 분산(21일)"),
    Sig("vix", "global", "stress", +1, "VIX 수준"),
    Sig("vix_chg_10", "global", "stress", +1, "VIX 10일 변화"),
    Sig("krw_chg_21", "global", "stress", +1, "원화 약세(USD/KRW 21일)", in_composite=False),
    Sig("jpy_strength_21", "global", "stress", +1, "엔화 강세(캐리 청산, 21일)"),
    Sig("usd_chg_63", "global", "stress", +1, "달러 강세(63일)"),
    # US 모듈 전용(FRED/yfinance 데이터가 있을 때만 사용)
    Sig("hy_oas", "credit", "stress", +1, "하이일드 스프레드(OAS)"),
    Sig("hy_oas_chg_63", "credit", "stress", +1, "하이일드 스프레드 63일 변화"),
    Sig("stlfsi", "credit", "stress", +1, "세인트루이스 연준 금융스트레스지수"),
    Sig("vix_term", "global", "stress", +1, "VIX/VIX3M (기간구조 역전)"),
    # ---------------- FRAGILITY ----------------
    Sig("absorption_ratio", "structure", "fragility", +1, "흡수비율(상위20% 고유벡터)"),
    Sig("topk_share", "structure", "fragility", +1, "상위5종목 시총 집중도"),
    Sig("overextension", "heat", "fragility", +1, "200일선 상방 과열", in_composite=False),
    Sig("turnover_heat", "heat", "fragility", +1, "회전율 과열(3개월/3년)"),
    Sig("oil_chg_63", "macro_pressure", "fragility", +1, "유가 충격(브렌트 63일)"),
    Sig("oil_gap_756", "macro_pressure", "fragility", +1, "유가 수준(3년 평균 대비)"),
    Sig("krw_gap_252", "macro_pressure", "fragility", +1, "원화 약세 누적(1년 평균 대비)"),
    Sig("curve_10y3m", "macro_pressure", "fragility", -1, "장단기 금리차(10Y-3M) 역전"),
    Sig("rate_shock_63", "macro_pressure", "fragility", +1, "미 10년물 금리 63일 상승폭"),
    Sig("ccc_minus_hy", "structure", "fragility", +1, "CCC-HY 스프레드 격차(저신용 스트레스)"),
]

CATEGORY_LABEL = {
    "volatility": "변동성", "tail": "꼬리위험·낙폭", "systemic": "시스템 스트레스", "breadth": "시장 폭",
    "trend": "추세", "liquidity": "유동성", "global": "글로벌 매크로",
    "structure": "구조적 집중", "heat": "과열", "macro_pressure": "매크로 압력", "credit": "신용",
}

REGIME_BANDS = [(0, 40, "안정"), (40, 55, "주의"), (55, 70, "경계"), (70, 101, "위험")]


# ---------------------------------------------------------------------------
def build_raw_features(market: pd.Series, xs: pd.DataFrame, macro: pd.DataFrame | None) -> pd.DataFrame:
    r = market
    lvl = (1 + r).cumprod()
    f = pd.DataFrame(index=r.index)
    f["vol_ewma"] = S.ewma_vol(r)
    f["vol_ratio"] = S.vol_ratio(r)
    f["downside_vol"] = S.downside_vol(r)
    f["drawdown"] = S.drawdown(lvl)
    f["cvar"] = S.cvar(r)
    f["trend_gap"] = S.trend_gap(lvl)
    f["mom_12_1"] = S.momentum(lvl)
    f["overextension"] = f["trend_gap"]
    if xs is not None:
        x = xs.reindex(r.index)
        has = x.columns
        if "turbulence" in has:
            f["turbulence_21"] = x["turbulence"].rolling(21, min_periods=15).mean()
        if "absorption_ratio" in has:
            f["delta_ar"] = S.delta_ar(x["absorption_ratio"])
            f["absorption_ratio"] = x["absorption_ratio"]
        for c in ("avg_corr", "pct_above_ma", "adv_ratio_21", "dispersion_21", "topk_share"):
            if c in has:
                f[c] = x[c]
        if "nl_minus_nh" in has:
            f["nlnh_21"] = x["nl_minus_nh"].rolling(21).mean()
        if "amihud_21" in has:
            f["amihud_rel"] = np.log(x["amihud_21"] / x["amihud_21"].rolling(252, min_periods=126).median())
        if "turnover" in has:
            to = x["turnover"].replace([np.inf, -np.inf], np.nan)
            f["turnover_heat"] = np.log(to.rolling(63).mean() / to.rolling(756, min_periods=252).mean())
    if macro is not None and len(macro):
        m = macro.reindex(r.index)
        if "VIX" in m:
            f["vix"] = m["VIX"]
            f["vix_chg_10"] = np.log(m["VIX"] / m["VIX"].shift(10))
        if "USDKRW" in m:
            f["krw_chg_21"] = np.log(m["USDKRW"] / m["USDKRW"].shift(21))
            f["krw_gap_252"] = m["USDKRW"] / m["USDKRW"].rolling(252, min_periods=126).mean() - 1
        if "USDJPY" in m:
            f["jpy_strength_21"] = -np.log(m["USDJPY"] / m["USDJPY"].shift(21))
        if "USD_BASKET" in m:
            f["usd_chg_63"] = np.log(m["USD_BASKET"] / m["USD_BASKET"].shift(63))
        if "HY_OAS" in m:
            f["hy_oas"] = m["HY_OAS"]
            f["hy_oas_chg_63"] = m["HY_OAS"] - m["HY_OAS"].shift(63)
        if "CCC_OAS" in m and "HY_OAS" in m:
            f["ccc_minus_hy"] = m["CCC_OAS"] - m["HY_OAS"]
        if "STLFSI" in m:
            f["stlfsi"] = m["STLFSI"]
        if "T10Y3M" in m:
            f["curve_10y3m"] = m["T10Y3M"]
        if "DGS10" in m:
            f["rate_shock_63"] = m["DGS10"] - m["DGS10"].shift(63)
        if "VIX" in m and "VIX3M" in m:
            f["vix_term"] = m["VIX"] / m["VIX3M"]
        if "BRENT" in m:
            f["oil_chg_63"] = np.log(m["BRENT"] / m["BRENT"].shift(63))
            f["oil_gap_756"] = m["BRENT"] / m["BRENT"].rolling(756, min_periods=252).mean() - 1
    return f.replace([np.inf, -np.inf], np.nan)


def score_features(raw: pd.DataFrame, spec: list[Sig] = SPEC, min_periods: int = 504) -> pd.DataFrame:
    """방향을 맞춘 확장창 백분위 점수(0~100, 높을수록 위험)."""
    out = {}
    for s in spec:
        if s.name not in raw:
            continue
        out[s.name] = S.expanding_pct(s.sign * raw[s.name], min_periods=min_periods)
    return pd.DataFrame(out, index=raw.index)


def aggregate(scores: pd.DataFrame, spec: list[Sig] = SPEC, w_stress: float = 0.7,
              min_signals_frac: float = 0.5) -> pd.DataFrame:
    """카테고리 = 소속 신호 평균, 축 = 카테고리 평균(동일가중), 종합 = 축 가중평균."""
    cats = {}
    by_cat: dict[str, list[str]] = {}
    for s in spec:
        if s.name in scores and s.in_composite:
            by_cat.setdefault(s.category, []).append(s.name)
    for c, names in by_cat.items():
        sub = scores[names]
        need = max(1, int(np.ceil(len(names) * min_signals_frac)))
        cats[c] = sub.mean(axis=1).where(sub.notna().sum(axis=1) >= need)
    cats = pd.DataFrame(cats)
    axis_of = {s.category: s.axis for s in spec}
    st = [c for c in cats if axis_of[c] == "stress"]
    fr = [c for c in cats if axis_of[c] == "fragility"]
    out = cats.copy()
    out["STRESS"] = cats[st].mean(axis=1).where(cats[st].notna().sum(axis=1) >= max(1, len(st) // 2))
    out["FRAGILITY"] = cats[fr].mean(axis=1).where(cats[fr].notna().sum(axis=1) >= 1)
    out["COMPOSITE"] = w_stress * out["STRESS"] + (1 - w_stress) * out["FRAGILITY"]
    return out


def regime_label(score: float) -> str:
    if score is None or np.isnan(score):
        return "N/A"
    for lo, hi, lab in REGIME_BANDS:
        if lo <= score < hi:
            return lab
    return "N/A"


def quadrant(stress: pd.Series, fragility: pd.Series, s_thr: float = 55, f_thr: float = 55) -> pd.Series:
    q = np.select(
        [(stress >= s_thr) & (fragility >= f_thr), (stress >= s_thr) & (fragility < f_thr),
         (stress < s_thr) & (fragility >= f_thr)],
        ["위기(Crisis)", "충격(Shock)", "취약·안일(Fragile)"], default="안정(Calm)")
    return pd.Series(q, index=stress.index).where(stress.notna() & fragility.notna())


# ---------------------------------------------------------------------------
# 경보 규칙 (문헌 기반 조기경보 + 신호 극단값)
# ---------------------------------------------------------------------------
def alerts(raw: pd.DataFrame, scores: pd.DataFrame, agg: pd.DataFrame, asof=None,
           spec: list[Sig] = SPEC) -> list[dict]:
    t = asof or agg["COMPOSITE"].dropna().index[-1]
    i = raw.index.get_loc(t)
    out = []

    def add(level, code, msg, value=None):
        out.append({"level": level, "code": code, "message": msg, "value": value})

    row = raw.iloc[i]
    lvl_dd = row.get("drawdown", np.nan)
    if row.get("delta_ar", np.nan) > 1.0:
        add("high", "AR_SHIFT", "흡수비율 급등(ΔAR>1σ): 종목들이 한 방향으로 묶이는 중 — Kritzman 조기경보", round(float(row["delta_ar"]), 2))
    turb_hot = (raw["turbulence_21"].iloc[max(0, i - 4):i + 1] > raw["turbulence_21"].iloc[:i + 1].quantile(0.9)).sum()
    if turb_hot >= 3 and lvl_dd > -0.05:
        add("high", "HIDDEN_TURBULENCE", "지수는 고점 근처인데 난기류 지수 상위 10% — 표면 아래 스트레스", int(turb_hot))
    if row.get("pct_above_ma", 1) < 0.4 and lvl_dd > -0.05:
        add("high", "BREADTH_DIVERGENCE", "지수 고점권(-5% 이내)인데 200일선 위 종목 40% 미만 — 소수 대형주 주도 랠리",
            round(float(row["pct_above_ma"]), 3))
    if row.get("vix_chg_10", 0) > np.log(1.4):
        add("high", "VIX_SPIKE", "VIX 10일간 +40% 이상 급등", round(float(np.exp(row["vix_chg_10"]) - 1), 3))
    if row.get("krw_chg_21", 0) > 0.03:
        add("medium", "KRW_SHOCK", "원화 21일간 3% 이상 약세 — 외국인 자금 이탈 위험", round(float(row["krw_chg_21"]), 4))
    if row.get("jpy_strength_21", 0) > 0.04:
        add("medium", "YEN_CARRY", "엔화 21일간 4% 이상 강세 — 엔캐리 청산 위험(2024.8 유형)", round(float(row["jpy_strength_21"]), 4))
    if "topk_share" in raw and row.get("topk_share", 0) >= raw["topk_share"].iloc[:i + 1].quantile(0.98):
        add("medium", "CONCENTRATION", "상위5종목 시총 집중도 역사적 최상단(98%ile 이상)", round(float(row["topk_share"]), 3))
    if row.get("oil_chg_63", 0) > np.log(1.3):
        add("medium", "OIL_SHOCK", "브렌트유 3개월 +30% 이상 — 인플레이션·금리 경로 위험", round(float(np.exp(row["oil_chg_63"]) - 1), 3))
    # 카테고리 급변
    cats = [c for c in CATEGORY_LABEL if c in agg]
    for c in cats:
        if i >= 5:
            d = agg[c].iloc[i] - agg[c].iloc[i - 5]
            if d >= 20:
                add("medium", "FAST_DETERIORATION", f"{CATEGORY_LABEL[c]} 점수 5일간 +{d:.0f}p 급등", round(float(d), 1))
    # 개별 신호 극단(95%ile 이상)
    label = {s.name: s.label for s in spec}
    srow = scores.iloc[i]
    for n, v in srow.items():
        if v >= 95:
            add("info", "EXTREME_SIGNAL", f"{label.get(n, n)} 역사적 상위 {100 - v:.1f}%", round(float(v), 1))
    return out


# ---------------------------------------------------------------------------
# 목표 변수 & 통계 모형
# ---------------------------------------------------------------------------
def forward_targets(market: pd.Series, h_dd: int = 63, h_vol: int = 21) -> pd.DataFrame:
    lvl = (1 + market).cumprod().to_numpy()
    n = len(lvl)
    fmin = np.full(n, np.nan)
    from numpy.lib.stride_tricks import sliding_window_view
    if n > h_dd:
        w = sliding_window_view(lvl[1:], h_dd)          # t+1 … t+h
        fmin[: n - h_dd] = w.min(axis=1)[: n - h_dd] / lvl[: n - h_dd] - 1
    r = market
    fvol = r[::-1].rolling(h_vol).std()[::-1].shift(-1) * np.sqrt(252)
    fret = pd.Series(lvl, index=market.index).shift(-h_dd) / lvl - 1
    return pd.DataFrame({"fwd_maxdd": fmin, "fwd_vol": fvol.to_numpy(), "fwd_ret": fret.to_numpy()},
                        index=market.index)


def walk_forward_probability(X: pd.DataFrame, y: pd.Series, start_year: int = 2003,
                             embargo: int = 63, C: float = 0.5) -> pd.Series:
    """
    연 단위 확장창 재학습 로지스틱 회귀 → 향후 63일 내 10% 이상 낙폭 확률.
    학습 표본은 예측연도 시작일 - embargo 이전까지만(목표 구간 중첩 누수 차단).
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    Xf = X.copy()
    prob = pd.Series(np.nan, index=X.index)
    years = sorted(set(X.index.year))
    for yr in years:
        if yr < start_year:
            continue
        test = (X.index.year == yr)
        cut = X.index[test][0]
        cut_i = X.index.get_loc(cut) - embargo
        if cut_i <= 500:
            continue
        tr_idx = X.index[:cut_i]
        tr = Xf.loc[tr_idx].join(y.rename("y")).dropna()
        if tr["y"].nunique() < 2:
            continue
        model = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=1000))
        model.fit(tr.drop(columns="y"), tr["y"].astype(int))
        te = Xf.loc[test].dropna()
        if len(te):
            prob.loc[te.index] = model.predict_proba(te)[:, 1]
    # 전체 확정 표본으로 학습한 모형의 계수(해석용 — 예측에는 사용하지 않음)
    tr = Xf.join(y.rename("y")).dropna()
    full = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=1000)).fit(
        tr.drop(columns="y"), tr["y"].astype(int))
    prob.attrs["coef"] = dict(zip(X.columns, full[-1].coef_[0].round(3)))
    return prob


def har_vol_forecast(market: pd.Series, horizon: int = 21, start_year: int = 2003, embargo: int = 22) -> pd.Series:
    """
    HAR-RV(Corsi 2009) 형태의 변동성 예측: log(향후 21일 실현변동성) ~ log RV(5) + log RV(21) + log RV(63).
    연 단위 확장창 재추정, 예측치는 연율 변동성.
    """
    from sklearn.linear_model import LinearRegression
    r = market
    X = pd.DataFrame({f"rv{w}": np.log(r.rolling(w).std() * np.sqrt(252)) for w in (5, 21, 63)})
    y = np.log(r[::-1].rolling(horizon).std()[::-1].shift(-1) * np.sqrt(252))
    out = pd.Series(np.nan, index=r.index)
    resid_sd = pd.Series(np.nan, index=r.index)
    for yr in sorted(set(r.index.year)):
        if yr < start_year:
            continue
        te = r.index.year == yr
        ci = r.index.get_loc(r.index[te][0]) - embargo
        tr = pd.concat([X, y.rename("y")], axis=1).iloc[:ci].dropna()
        if len(tr) < 500:
            continue
        m = LinearRegression().fit(tr[X.columns], tr["y"])
        xt = X.loc[te].dropna()
        out.loc[xt.index] = m.predict(xt)
        resid_sd.loc[xt.index] = float(np.std(tr["y"] - m.predict(tr[X.columns])))
    # 로그정규 평균 보정
    return np.exp(out + 0.5 * resid_sd ** 2)


def calibrate_now(prob: pd.Series, y: pd.Series, p_now: float) -> float:
    """결과가 확정된 과거 OOS 예측(prob, y)으로 등위회귀 보정 → 현재 확률의 실현빈도 추정."""
    from sklearn.isotonic import IsotonicRegression
    d = pd.concat([prob, y], axis=1).dropna()
    if len(d) < 500 or np.isnan(p_now):
        return float("nan")
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(d.iloc[:, 0], d.iloc[:, 1])
    return float(iso.predict([p_now])[0])
