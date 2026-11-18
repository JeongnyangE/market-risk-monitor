"""
Notion 페이지에 넣을 정적 차트(PNG) — matplotlib

  overview.png    최근 18개월 KOSPI(로그, 배경 = 레짐) + 종합·스트레스·취약성 점수
  categories.png  카테고리별 현재 점수(막대)와 5일 전 위치(눈금)
"""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

KOREAN_FONTS = ["Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "Malgun Gothic", "AppleGothic",
                "Apple SD Gothic Neo", "Noto Sans CJK JP"]
REGIME_COLOR = {"안정": "#2c7a4b", "주의": "#8e8c85", "경계": "#d08a1e", "위험": "#c03a26"}
FG, MUTED, GRID = "#1f1e1c", "#6b6963", "#e8e6e0"
C_COMP, C_STRESS, C_FRAG = "#2f6bd6", "#7a4cc0", "#d4772a"


def _setup_font() -> str | None:
    names = {f.name for f in font_manager.fontManager.ttflist}
    for n in KOREAN_FONTS:
        if n in names:
            plt.rcParams["font.family"] = n
            plt.rcParams["axes.unicode_minus"] = False
            return n
    print("[charts] 한글 글꼴을 찾지 못했습니다 — 글자가 깨질 수 있습니다 (fonts-noto-cjk 설치 권장)")
    return None


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#b7b5ad")
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def overview(hist: pd.DataFrame, kpi: dict, path: str, months: int = 18) -> str:
    _setup_font()
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"])
    h = h.set_index("date")
    h = h[h.index >= h.index.max() - pd.DateOffset(months=months)]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6.2), dpi=150, sharex=True,
                                 gridspec_kw={"height_ratios": [1.15, 1], "hspace": 0.12})
    fig.patch.set_facecolor("white")
    # 레짐 배경
    reg = h["regime"].astype(str)
    blocks = (reg != reg.shift()).cumsum()
    for _, seg in h.groupby(blocks):
        r = str(seg["regime"].iloc[0])
        x0 = seg.index[0]
        nxt = h.index[h.index > seg.index[-1]]
        x1 = nxt[0] if len(nxt) else seg.index[-1]
        for ax in (a1, a2):
            ax.axvspan(x0, x1, color=REGIME_COLOR.get(r, "#cccccc"), alpha=0.12, lw=0)
    a1.plot(h.index, h["kospi"], color=FG, lw=1.6)
    a1.set_yscale("log")
    lo, hi = float(h["kospi"].min()), float(h["kospi"].max())
    a1.set_ylim(lo * 0.94, hi * 1.06)
    cands = [500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 12000, 15000, 20000]
    ticks = [c for c in cands if lo * 0.94 <= c <= hi * 1.06]
    if len(ticks) > 6:
        ticks = ticks[::2]
    a1.yaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
    a1.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    a1.set_title("KOSPI (재구성, 로그)  ·  배경 = 레짐", loc="left", fontsize=11, color=FG)
    last = h.iloc[-1]
    a1.annotate(f"{last['kospi']:,.0f}", (h.index[-1], last["kospi"]), xytext=(4, 0),
                textcoords="offset points", fontsize=9, color=FG, va="center")
    ends = []
    for col, c, lab, lw in (("composite", C_COMP, "종합", 2.0), ("stress", C_STRESS, "스트레스", 1.2),
                            ("fragility", C_FRAG, "취약성", 1.2)):
        a2.plot(h.index, h[col], color=c, lw=lw, label=lab)
        ends.append([float(h[col].iloc[-1]), f"{lab} {h[col].iloc[-1]:.1f}", c])
    ends.sort(key=lambda e: e[0])                       # 끝 라벨 겹침 방지(최소 6점 간격)
    for k in range(1, len(ends)):
        ends[k][0] = max(ends[k][0], ends[k - 1][0] + 6)
    for yy, txt, c in ends:
        a2.text(1.01, yy, txt, transform=a2.get_yaxis_transform(), fontsize=8.5, color=c, va="center", ha="left")
    for key, lab, c in (("cut_watch", "주의", MUTED), ("cut_alert", "경계", "#d08a1e"), ("cut_danger", "위험", "#c03a26")):
        v = float(kpi[key])
        a2.axhline(v, color=c, lw=0.9, ls=(0, (4, 4)))
        a2.text(0.006, v, f" {lab} {v:.0f} ", transform=a2.get_yaxis_transform(), fontsize=7.5, color=c,
                va="center", ha="left", bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=0.6))
    a2.set_ylim(0, 100)
    a2.set_title("위험 점수 (0–100, 높을수록 위험)", loc="left", fontsize=11, color=FG)
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
    for ax in (a1, a2):
        _style(ax)
    handles = [matplotlib.patches.Patch(color=REGIME_COLOR[k], alpha=0.35, label=k) for k in REGIME_COLOR]
    a1.legend(handles=handles, loc="upper left", ncol=4, frameon=False, fontsize=8.5)
    fig.subplots_adjust(left=0.08, right=0.88, top=0.94, bottom=0.07)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def categories(cats: pd.DataFrame, path: str) -> str:
    _setup_font()
    c = cats.copy()
    c["order"] = np.where(c["axis"] == "스트레스", 0, 1)
    c = c.sort_values(["order"]).reset_index(drop=True)[::-1]
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(c) + 0.9), dpi=150)
    fig.patch.set_facecolor("white")
    y = np.arange(len(c))
    colors = [("#c03a26" if s >= 80 else (C_COMP if a == "스트레스" else C_FRAG)) for s, a in zip(c["score"], c["axis"])]
    ax.barh(y, [100] * len(c), color=GRID, height=0.55)
    ax.barh(y, c["score"], color=colors, height=0.55)
    ax.scatter(c["score_5d_ago"], y, marker="|", s=260, color=FG, linewidths=2, zorder=3, label="5일 전")
    ax.set_yticks(y, [f"{lab}  ({a})" for lab, a in zip(c["label"], c["axis"])], fontsize=9.5)
    for yi, s in zip(y, c["score"]):
        ax.text(101.5, yi, f"{s:.1f}", va="center", fontsize=9, color=FG)
    ax.set_xlim(0, 108)
    ax.set_title("카테고리별 위험 점수  ·  눈금 = 5일 전", loc="left", fontsize=11, color=FG)
    _style(ax)
    ax.grid(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def render_all(dash_dir: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    hist = pd.read_csv(os.path.join(dash_dir, "history.csv"))
    kpi = pd.read_csv(os.path.join(dash_dir, "kpi.csv")).iloc[0].to_dict()
    cats = pd.read_csv(os.path.join(dash_dir, "categories.csv"))
    return {
        "overview": overview(hist, kpi, os.path.join(out_dir, "overview.png")),
        "categories": categories(cats, os.path.join(out_dir, "categories.png")),
    }
