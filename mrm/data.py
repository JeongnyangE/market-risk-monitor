"""
데이터 계층 — 소스별 로더 (모두 같은 형식으로 반환)

반환 규약
  market   : pd.Series   시장 지수 일간 수익률 (index=거래일)
  panel    : dict        {'ret','mcap','amount','close'} → 거래일 x 종목 wide DataFrame
  macro    : pd.DataFrame 거래일 기준 정렬된 글로벌 변수 (VIX, 환율, 유가 …), 1영업일 지연 적용

소스
  1) KRX 전종목 일별 (FinanceData/marcap, GitHub)          ← 이 작업환경에서 검증
  2) 글로벌 보조 (datasets/finance-vix, exchange-rates, oil-prices, gold-prices, GitHub)
  3) 미국 시장 (yfinance + FRED)                           ← 인터넷 되는 로컬에서 사용
"""
from __future__ import annotations

import glob
import os
import subprocess

import numpy as np
import pandas as pd

GITHUB_SOURCES = {
    "marcap": "https://github.com/FinanceData/marcap.git",
    "finance-vix": "https://github.com/datasets/finance-vix.git",
    "exchange-rates": "https://github.com/datasets/exchange-rates.git",
    "oil-prices": "https://github.com/datasets/oil-prices.git",
    "gold-prices": "https://github.com/datasets/gold-prices.git",
}


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def sync_github(data_root: str, names: list[str] | None = None) -> None:
    """GitHub 데이터 저장소를 clone(없으면) 또는 pull(있으면)."""
    os.makedirs(data_root, exist_ok=True)
    for name in names or GITHUB_SOURCES:
        url = GITHUB_SOURCES[name]
        dest = os.path.join(data_root, name)
        if os.path.isdir(os.path.join(dest, ".git")):
            # 얕은 복제본은 pull 시 '분기' 오류가 나므로 최신 커밋을 받아 강제로 맞춘다
            subprocess.run(["git", "-C", dest, "fetch", "-q", "--depth", "1", "origin", "HEAD"], check=True)
            subprocess.run(["git", "-C", dest, "reset", "-q", "--hard", "FETCH_HEAD"], check=True)
        else:
            subprocess.run(["git", "clone", "-q", "--depth", "1", url, dest], check=True)


def read_parquet(path: str, columns: list[str] | None = None) -> pd.DataFrame:
    try:
        return pd.read_parquet(path, columns=columns)
    except ImportError:
        from .parquet_lite import read_parquet as _rp
        return _rp(path, columns=columns)


# ---------------------------------------------------------------------------
# 1) KRX (marcap)
# ---------------------------------------------------------------------------
KRX_COLS = ["Code", "Name", "Date", "Market", "Close", "ChangesRatio", "Amount", "Marcap", "Volume"]


def load_krx_panel(marcap_dir: str, market: str = "KOSPI", start: str = "1995-01-01",
                   common_only: bool = True) -> dict:
    """
    marcap 연도별 parquet → wide 패널.
      common_only=True : 보통주만(코드 끝자리 0) → 실제 KOSPI 구성과 일치
    수익률은 KRX 공식 등락률(ChangesRatio, 권리락·액면분할 보정 기준가 대비)을 사용.
    """
    files = sorted(glob.glob(os.path.join(marcap_dir, "data", "marcap-*.parquet")))
    frames = []
    for f in files:
        yr = int(os.path.basename(f)[7:11])
        if yr < int(start[:4]):
            continue
        df = read_parquet(f, columns=KRX_COLS)
        df = df[df["Market"].astype(str) == market]
        code = df["Code"].astype(str)
        if common_only:
            df = df[code.str.endswith("0").values]
        frames.append(pd.DataFrame({
            "Date": pd.to_datetime(df["Date"]).values,
            "Code": df["Code"].astype(str).values,
            "Name": df["Name"].astype(str).values,
            "ret": df["ChangesRatio"].astype(float).values / 100.0,
            "mcap": df["Marcap"].astype(float).values,
            "amount": df["Amount"].astype(float).values,
            "close": df["Close"].astype(float).values,
            "volume": df["Volume"].astype(float).values,
        }))
    long = pd.concat(frames, ignore_index=True)
    long = long[long["Date"] >= pd.Timestamp(start)]
    long = long.drop_duplicates(["Date", "Code"], keep="last")
    # 거래정지(거래량 0) 일의 수익률은 결측 처리
    long.loc[long["volume"] <= 0, "ret"] = np.nan
    # 극단치 정제: 기준가 재설정일 등 비정상 등락률
    long["ret"] = long["ret"].clip(-0.6, 1.5)
    names = long.sort_values("Date").groupby("Code")["Name"].last()
    panel = {}
    for col in ["ret", "mcap", "amount", "close"]:
        panel[col] = long.pivot(index="Date", columns="Code", values=col).sort_index()
    panel["names"] = names
    return panel


def cap_weighted_index(panel: dict) -> pd.Series:
    """전일 시가총액 가중 시장 수익률 (KOSPI 근사)."""
    w = panel["mcap"].shift(1)
    r = panel["ret"]
    valid = r.notna() & w.notna()
    num = (r.where(valid, 0.0) * w.where(valid, 0.0)).sum(axis=1)
    den = w.where(valid, 0.0).sum(axis=1)
    out = (num / den).replace([np.inf, -np.inf], np.nan)
    return out.iloc[1:].fillna(0.0).rename("mkt")


# ---------------------------------------------------------------------------
# 2) 글로벌 보조 데이터 (datasets/*)
# ---------------------------------------------------------------------------
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def load_vix_cboe(timeout: int = 20) -> pd.Series | None:
    """Cboe 공식 VIX 일별 종가 CSV. 네트워크가 막혀 있거나 MRM_OFFLINE=1 이면 None."""
    if os.environ.get("MRM_OFFLINE") == "1":
        return None
    import io
    import urllib.request
    try:
        req = urllib.request.Request(CBOE_VIX_URL, headers={"User-Agent": "market-risk-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            df = pd.read_csv(io.BytesIO(r.read()))
        df["DATE"] = pd.to_datetime(df["DATE"], format="%m/%d/%Y")
        return df.set_index("DATE")["CLOSE"].astype(float)
    except Exception as e:  # 실패해도 GitHub 저장소 데이터로 계속 진행
        print(f"[VIX] Cboe 다운로드 실패 → 저장소 데이터 사용 ({type(e).__name__})")
        return None


def load_global_aux(data_root: str, manual_csv: str | None = None) -> pd.DataFrame:
    """
    VIX, 달러/원, 엔/달러, 달러 바스켓, 브렌트유, 금(월간) → 일별 DataFrame (원시, 지연 미적용).
    manual_csv: (Date, Series, Value, Source) — 원천 데이터 마지막 관측일 '이후' 값만 보강(덮어쓰기 없음).
    """
    out = {}
    vix_parts = []
    p = os.path.join(data_root, "finance-vix", "data", "vix-daily.csv")
    if os.path.exists(p):
        vix_parts.append(pd.read_csv(p, parse_dates=["DATE"]).set_index("DATE")["CLOSE"])
    cboe = load_vix_cboe()
    if cboe is not None:
        vix_parts.append(cboe)          # Cboe 원본이 있으면 같은 날짜는 Cboe 값 우선
    if vix_parts:
        v = pd.concat(vix_parts)
        out["VIX"] = v.groupby(level=0).last().sort_index()
    p = os.path.join(data_root, "exchange-rates", "data", "daily.csv")
    if os.path.exists(p):
        fx = pd.read_csv(p, parse_dates=["Date"]).pivot_table(index="Date", columns="Country",
                                                               values="Exchange rate")
        out["USDKRW"] = fx.get("South Korea")
        out["USDJPY"] = fx.get("Japan")
        # 달러 바스켓 근사(DXY 구성통화 + 위안): 통화당 USD 가치의 기하평균 (모두 '외화/USD' 표기)
        basket = {"Euro": .576, "Japan": .136, "United Kingdom": .119, "Canada": .091,
                  "Sweden": .042, "Switzerland": .036}
        cols = [c for c in basket if c in fx]
        wts = np.array([basket[c] for c in cols]); wts = wts / wts.sum()
        logs = np.log(fx[cols])
        out["USD_BASKET"] = np.exp((logs * wts).sum(axis=1, min_count=len(cols)))
    p = os.path.join(data_root, "oil-prices", "data", "brent-daily.csv")
    if os.path.exists(p):
        out["BRENT"] = pd.read_csv(p, parse_dates=["Date"]).set_index("Date")["Price"]
    p = os.path.join(data_root, "gold-prices", "data", "monthly.csv")
    if os.path.exists(p):
        g = pd.read_csv(p)
        g["Date"] = pd.to_datetime(g["Date"]) + pd.offsets.MonthEnd(0)
        out["GOLD_M"] = g.set_index("Date")["Price"]
    df = pd.DataFrame({k: v for k, v in out.items() if v is not None}).sort_index()
    df.attrs["manual"] = []
    if manual_csv and os.path.exists(manual_csv):
        man = pd.read_csv(manual_csv, parse_dates=["Date"])
        for _, row in man.iterrows():
            col = row["Series"]
            last = df[col].dropna().index.max() if col in df else pd.Timestamp.min
            if row["Date"] > last:
                df.loc[row["Date"], col] = float(row["Value"])
                df.attrs["manual"].append(dict(date=str(row["Date"].date()), series=col,
                                               value=float(row["Value"]), source=row["Source"]))
        df = df.sort_index()
    return df


def align_macro(macro_raw: pd.DataFrame, calendar: pd.DatetimeIndex, lag_days: int = 1) -> pd.DataFrame:
    """
    거래일 캘린더에 정렬 + 전방채움 + lag_days 지연.
    미국 장 마감/뉴욕 정오 환율은 한국 장 시작 전 확인 가능 → 동일 일자 사용 시 미래참조 위험이 있어 1영업일 지연.
    """
    full = macro_raw.reindex(macro_raw.index.union(calendar)).sort_index().ffill(limit=10)
    out = full.reindex(calendar).shift(lag_days)
    # 원천 데이터의 마지막 관측일 이후는 stale 표시를 위해 기록
    out.attrs["last_obs"] = {c: str(macro_raw[c].dropna().index.max().date()) for c in macro_raw}
    return out


# ---------------------------------------------------------------------------
# 3) 미국 시장 어댑터 (yfinance + FRED) — 인터넷 가능한 로컬 환경용
# ---------------------------------------------------------------------------
US_TICKERS = {
    "index": "^GSPC",
    "sectors": ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
                "SMH", "IWM", "QQQ", "HYG", "LQD", "TLT", "GLD", "UUP"],
    "vol": ["^VIX", "^VIX3M", "^VVIX", "^MOVE", "^SKEW"],
}
FRED_SERIES = {
    "HY_OAS": "BAMLH0A0HYM2", "CCC_OAS": "BAMLH0A3HYC", "IG_OAS": "BAMLC0A0CM",
    "DGS10": "DGS10", "DGS2": "DGS2", "DGS3MO": "DGS3MO", "T10Y3M": "T10Y3M",
    "STLFSI": "STLFSI4", "NFCI": "NFCI", "USD_BROAD": "DTWEXBGS", "BRENT": "DCOILBRENTEU",
    "BREAKEVEN10": "T10YIE", "SOFR": "SOFR",
}


def load_us_yfinance(start: str = "1998-01-01") -> dict:
    """yfinance로 S&P500, 섹터 ETF(교차단면), 변동성 지수 다운로드."""
    import yfinance as yf  # pip install yfinance
    tick = [US_TICKERS["index"]] + US_TICKERS["sectors"] + US_TICKERS["vol"]
    px = yf.download(tick, start=start, auto_adjust=True, progress=False)["Close"]
    idx = px[US_TICKERS["index"]].dropna()
    secs = [t for t in ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB"] if t in px]
    ret_x = px[secs].pct_change(fill_method=None)
    return {
        "market": idx.pct_change().dropna().rename("mkt"),
        "panel": {"ret": ret_x, "close": px[secs]},
        "close_index": idx,
        "vol": px[[t for t in US_TICKERS["vol"] if t in px]],
        "etf": px[[t for t in ["SMH", "IWM", "QQQ", "HYG", "LQD", "TLT", "GLD", "UUP"] if t in px]],
    }


def load_fred(series: dict | None = None, api_key: str | None = None, start: str = "1990-01-01") -> pd.DataFrame:
    """FRED 시계열. API 키 없으면 fredgraph CSV 엔드포인트 사용."""
    series = series or FRED_SERIES
    out = {}
    for name, sid in series.items():
        try:
            if api_key:
                from fredapi import Fred  # pip install fredapi
                out[name] = Fred(api_key=api_key).get_series(sid, observation_start=start)
            else:
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
                s = pd.read_csv(url, index_col=0, parse_dates=True, na_values=".").iloc[:, 0]
                out[name] = s[s.index >= start]
        except Exception as e:  # 일부 실패해도 나머지 진행
            print(f"[FRED] {sid} 실패: {e}")
    return pd.DataFrame(out).sort_index()
