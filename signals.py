"""
신호 라이브러리 — 모든 함수는 '그날 종가까지의 정보'만 사용 (미래참조 없음)

방향 규약: 각 신호는 '값이 클수록 위험'이 되도록 부호를 맞춘 뒤 정규화 단계로 넘긴다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

ANN = 252


# ---------------------------------------------------------------------------
# 시장 수준(시계열) 신호
# ---------------------------------------------------------------------------
def ewma_vol(r: pd.Series, lam: float = 0.94) -> pd.Series:
    """RiskMetrics EWMA 변동성 (연율)."""
    return np.sqrt(ANN * (r ** 2).ewm(alpha=1 - lam, adjust=False).mean())


def vol_ratio(r: pd.Series, short: int = 10, long: int = 126) -> pd.Series:
    """단기/장기 실현변동성 비율 — 변동성 '충격'(가속) 감지."""
    return r.rolling(short).std() / r.rolling(long).std()


def downside_vol(r: pd.Series, win: int = 63) -> pd.Series:
    return np.sqrt(ANN * (r.clip(upper=0) ** 2).rolling(win).mean())


def drawdown(level: pd.Series, win: int = 252) -> pd.Series:
    """직전 win일 고점 대비 낙폭 (음수)."""
    return level / level.rolling(win, min_periods=20).max() - 1


def cvar(r: pd.Series, win: int = 126, q: float = 0.05) -> pd.Series:
    """역사적 CVaR(Expected Shortfall), 손실을 양수로."""
    x = r.to_numpy()
    out = np.full(len(x), np.nan)
    if len(x) >= win:
        w = sliding_window_view(x, win)
        k = max(1, int(np.floor(q * win)))
        part = np.partition(w, k - 1, axis=1)[:, :k]
        out[win - 1:] = -np.nanmean(part, axis=1)
    return pd.Series(out, index=r.index)


def trend_gap(level: pd.Series, win: int = 200) -> pd.Series:
    return level / level.rolling(win).mean() - 1


def momentum(level: pd.Series, lookback: int = 252, skip: int = 21) -> pd.Series:
    return level.shift(skip) / level.shift(lookback) - 1


# ---------------------------------------------------------------------------
# 교차단면(구조) 신호
# ---------------------------------------------------------------------------
def top_n_membership(mcap: pd.DataFrame, n: int) -> pd.DataFrame:
    """매월 말 시가총액 상위 n 종목(전월말 기준, 다음 달 동안 고정) — 생존편향 없음."""
    me = mcap.resample("ME").last()
    ranks = me.rank(axis=1, ascending=False)
    member_me = ranks <= n
    member = member_me.shift(1).reindex(mcap.index, method="ffill")
    return member.fillna(False).astype(bool)


def _window_matrix(R: np.ndarray, cols: np.ndarray, t_idx: int, win: int, min_frac: float = 0.9):
    block = R[t_idx - win:t_idx][:, cols]
    ok = (~np.isnan(block)).mean(axis=0) >= min_frac
    return np.nan_to_num(block[:, ok]), cols[ok]


def absorption_ratio(ret: pd.DataFrame, member: pd.DataFrame, win: int = 252, frac_eig: float = 0.2,
                     halflife: int = 126, step: int = 1) -> pd.DataFrame:
    """
    Kritzman, Li, Page & Rigobon (2011) 흡수비율:
      상위 (자산수 x frac_eig)개 고유벡터가 설명하는 총분산 비중. 높을수록 시장이 '한 몸'처럼 움직임 → 충격 전이 취약.
    지수가중 공분산(반감기 halflife) 사용. 함께 평균 상관계수도 반환.
    """
    idx = ret.index
    R = ret.to_numpy(dtype=float)
    M = member.reindex(columns=ret.columns, fill_value=False).to_numpy(dtype=bool)
    ar = np.full(len(idx), np.nan)
    ac = np.full(len(idx), np.nan)
    wts = 0.5 ** (np.arange(win)[::-1] / halflife)
    wts = wts / wts.sum()
    for t in range(win, len(idx) + 1, step):
        cols = np.flatnonzero(M[t - 1])
        A, _ = _window_matrix(R, cols, t, win)
        n = A.shape[1]
        if n < 10:
            continue
        A = A - (wts[:, None] * A).sum(axis=0)
        cov = (A * wts[:, None]).T @ A
        ev = np.linalg.eigvalsh(cov)[::-1]
        k = max(1, int(round(n * frac_eig)))
        ar[t - 1] = ev[:k].sum() / ev.sum()
        sd = np.sqrt(np.diag(cov))
        sd[sd == 0] = np.nan
        corr = cov / np.outer(sd, sd)
        ac[t - 1] = (np.nansum(corr) - n) / (n * (n - 1))
    out = pd.DataFrame({"absorption_ratio": ar, "avg_corr": ac}, index=idx)
    if step > 1:
        out = out.ffill()
    return out


def delta_ar(ar: pd.Series, short: int = 15, long: int = 252) -> pd.Series:
    """표준화 흡수비율 변화 (Kritzman: 15일 평균 - 1년 평균) / 1년 표준편차."""
    return (ar.rolling(short).mean() - ar.rolling(long).mean()) / ar.rolling(long).std()


def turbulence(ret: pd.DataFrame, member: pd.DataFrame, win: int = 252, shrink: float = 0.2) -> pd.Series:
    """
    Kritzman & Li (2010) 금융 난기류 지수: 오늘 수익률 벡터의 마할라노비스 거리 / 자산수.
    과거 win일 공분산(당일 제외)에 대각 수축(shrink) 적용해 역행렬 안정화. 정상 상태 기대값 ≈ 1.
    """
    idx = ret.index
    R = ret.to_numpy(dtype=float)
    M = member.reindex(columns=ret.columns, fill_value=False).to_numpy(dtype=bool)
    out = np.full(len(idx), np.nan)
    for t in range(win, len(idx)):
        cols = np.flatnonzero(M[t])
        A, kept = _window_matrix(R, cols, t, win)
        y = R[t, kept]
        okc = ~np.isnan(y)
        if okc.sum() < 10:
            continue
        A = A[:, okc]
        y = y[okc]
        mu = A.mean(axis=0)
        S = np.cov(A, rowvar=False)
        S = (1 - shrink) * S + shrink * np.diag(np.diag(S))
        d = y - mu
        try:
            out[t] = d @ np.linalg.solve(S, d) / len(d)
        except np.linalg.LinAlgError:
            continue
    return pd.Series(out, index=idx)


def adjusted_level(ret: pd.DataFrame) -> pd.DataFrame:
    """종목별 수익률 누적 → 액면분할/권리락 보정 가격지수."""
    return (1 + ret.fillna(0.0)).cumprod().where(ret.notna().cumsum() > 0)


def breadth(ret: pd.DataFrame, mcap: pd.DataFrame, ma: int = 200, hl_win: int = 252,
            min_mcap_rank: int = 500) -> pd.DataFrame:
    """
    시장 폭 지표 (시총 상위 min_mcap_rank 종목, 전월말 기준):
      pct_above_ma   : 200일 이평 위 종목 비율
      nl_minus_nh    : 52주 신저가 비율 - 신고가 비율
      adv_ratio_21   : 상승종목/(상승+하락) 21일 평균
      dispersion_21  : 교차단면 수익률 표준편차 21일 평균
    """
    member = top_n_membership(mcap, min_mcap_rank)
    lvl = adjusted_level(ret)
    has_hist = ret.notna().rolling(ma).sum() >= ma * 0.9
    above = (lvl > lvl.rolling(ma, min_periods=int(ma * 0.9)).mean()) & has_hist & member
    elig = has_hist & member
    pct_above = above.sum(axis=1) / elig.sum(axis=1).replace(0, np.nan)
    hi = lvl.rolling(hl_win, min_periods=200).max()
    lo = lvl.rolling(hl_win, min_periods=200).min()
    nh = ((lvl >= hi) & elig).sum(axis=1)
    nl = ((lvl <= lo) & elig).sum(axis=1)
    nlnh = (nl - nh) / elig.sum(axis=1).replace(0, np.nan)
    r_m = ret.where(member)
    adv = (r_m > 0).sum(axis=1)
    dec = (r_m < 0).sum(axis=1)
    adv_ratio = (adv / (adv + dec).replace(0, np.nan)).rolling(21).mean()
    disp = r_m.std(axis=1).rolling(21).mean()
    return pd.DataFrame({"pct_above_ma": pct_above, "nl_minus_nh": nlnh,
                         "adv_ratio_21": adv_ratio, "dispersion_21": disp})


def concentration(mcap: pd.DataFrame, k: int = 5) -> pd.DataFrame:
    """상위 k 종목 시총 비중 + HHI 기반 유효 종목수."""
    m = np.nan_to_num(mcap.to_numpy(dtype=float))
    tot = m.sum(axis=1)
    topk = -np.partition(-m, k - 1, axis=1)[:, :k].sum(axis=1)
    hhi = ((m / tot[:, None]) ** 2).sum(axis=1)
    return pd.DataFrame({"topk_share": topk / tot, "eff_n": 1.0 / hhi}, index=mcap.index)


def liquidity(ret: pd.DataFrame, amount: pd.DataFrame, mcap: pd.DataFrame, n: int = 200) -> pd.DataFrame:
    """
    Amihud(2002) 비유동성: |r| / 거래대금(10억원), 시총상위 n 종목 중앙값, 21일 평균.
    turnover: 시장 거래대금/시가총액 (회전율).
    """
    member = top_n_membership(mcap, n)
    amt = amount.where(amount > 0) / 1e9
    illiq = (ret.abs() / amt).where(member)
    amihud = illiq.median(axis=1).rolling(21).mean()
    turnover = amount.sum(axis=1) / mcap.shift(1).sum(axis=1)
    return pd.DataFrame({"amihud_21": amihud, "turnover": turnover})


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------
def expanding_pct(x: pd.Series, min_periods: int = 504) -> pd.Series:
    """확장창 백분위(0~100): t시점 값이 과거 전체 분포에서 차지하는 위치 — 미래참조 없음."""
    return x.expanding(min_periods=min_periods).rank(pct=True) * 100
