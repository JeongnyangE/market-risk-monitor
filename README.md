# KOSPI 리스크 모니터 — Notion 연동판

KRX 전 종목 데이터로 시장 위험(스트레스·취약성)을 매일 계산하고, 결과를 **Notion**에 자동으로 기록합니다.
Notion 안에서는 코드가 실행되지 않기 때문에, 계산은 **GitHub Actions**(무료 클라우드 실행)가 맡고 Notion에는 결과만 써 넣습니다.

```
GitHub Actions (평일 아침 자동 실행)
 ├─ 데이터 받기: KRX 전종목(marcap), VIX(Cboe), 환율(FRB), 브렌트유(EIA)
 ├─ 모델 계산: run_monitor.py
 ├─ 웹 대시보드 만들기 → GitHub Pages (선택)
 └─ Notion API로 기록
      부모 페이지
       ├─ 📉 KOSPI 리스크 — 현재 판독   레짐·핵심 수치·차트·경보·임베드 대시보드 (매번 교체)
       └─ 📈 KOSPI 리스크 로그           날짜별 1행 데이터베이스 (필터·정렬·보기 추가 가능)
```

준비물: GitHub 계정, Notion 계정(그 워크스페이스에서 통합을 만들 수 있는 권한 — 개인 워크스페이스면 됨). 설정은 20분쯤 걸립니다.

---

## 1단계 — GitHub 저장소 만들기

1. GitHub에서 **New repository** → 이름 예: `market-risk-monitor` → **Public** 선택(무료로 웹 대시보드를 쓰려면 공개 저장소여야 함) → Create.
2. 이 폴더의 파일을 모두 올립니다.
   - **가장 쉬운 방법**: 저장소 화면 **Add file → Upload files** 에 폴더 안 파일·폴더를 끌어다 놓고 Commit.
   - ⚠️ `.github` 폴더는 이름이 점으로 시작해서 Finder/탐색기에서 숨겨질 수 있습니다(맥: `Cmd+Shift+.` 로 보이기).
     안 올라갔다면 **Add file → Create new file** 에서 파일 이름에 `.github/workflows/risk-monitor.yml` 을 입력하고 같은 이름 파일 내용을 붙여 넣으세요.
   - 터미널을 쓴다면:
     ```bash
     git init && git add . && git commit -m "risk monitor"
     git branch -M main
     git remote add origin https://github.com/<내아이디>/market-risk-monitor.git
     git push -u origin main
     ```

> 토큰은 코드에 들어가지 않고 GitHub의 '시크릿'에만 저장되므로 저장소를 공개해도 안전합니다.

## 2단계 — Notion 통합(봇) 만들기

1. https://www.notion.so/profile/integrations 접속 → **새 통합(New integration)**.
2. 이름 예: `리스크 모니터`, 워크스페이스 선택, 유형 **내부(Internal)** → 저장.
3. **기능(Capabilities)** 에서 *콘텐츠 읽기 · 콘텐츠 업데이트 · 콘텐츠 삽입* 을 모두 켭니다.
4. **내부 통합 시크릿(Internal Integration Secret)** 을 복사해 둡니다 (`ntn_…` 으로 시작). 이게 `NOTION_TOKEN` 입니다.

## 3단계 — 결과를 넣을 Notion 페이지 준비

1. Notion에 빈 페이지를 하나 만듭니다. 예: `📊 시장 리스크`.
2. 페이지 오른쪽 위 **⋯ → 연결(Connections) → 2단계에서 만든 통합 검색 → 추가**.
   (이걸 빼먹으면 404 오류가 납니다 — 통합은 연결된 페이지만 볼 수 있습니다.)
3. **⋯ → 링크 복사**. 이 주소가 `NOTION_PAGE_ID` 입니다 (주소 그대로 넣어도 됩니다).

## 4단계 — GitHub에 시크릿 등록

저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서 두 개를 만듭니다.

| Name | Secret |
|---|---|
| `NOTION_TOKEN` | 2단계의 `ntn_…` 시크릿 |
| `NOTION_PAGE_ID` | 3단계에서 복사한 페이지 링크 |

## 5단계 — (선택) 웹 대시보드 켜기

**Settings → Pages → Build and deployment → Source: GitHub Actions** 로 바꿉니다.
켜면 `https://<내아이디>.github.io/market-risk-monitor/` 에 인터랙티브 대시보드(현재 판독·이력·검증·방법론 탭, 툴팁)가 올라가고,
Notion 요약 페이지 안에도 자동으로 임베드됩니다. 켜지 않아도 Notion 기록은 정상 작동합니다(차트는 이미지로 업로드).

## 6단계 — 첫 실행

1. 저장소 **Actions** 탭 → 왼쪽 **KOSPI 리스크 모니터** → **Run workflow**.
2. `backfill` 칸에 **250** 입력(과거 1년치를 로그에 기록) → Run. 5~10분 걸립니다.
3. 끝나면 Notion 부모 페이지 아래에 요약 페이지와 로그 데이터베이스가 생깁니다.

이후에는 **평일 한국시간 아침 7:30**(화~토)에 자동으로 전 거래일 데이터를 반영합니다.
시간을 바꾸려면 `.github/workflows/risk-monitor.yml` 의 `cron` 을 고치세요(UTC 기준).

## 7단계 — Notion에서 쓰기

- **요약 페이지**: 매 실행마다 내용이 통째로 바뀝니다. 메모는 이 페이지 말고 부모 페이지에 쓰세요.
- **로그 데이터베이스**: 표 보기에서 `레짐 = 위험` 필터, 날짜 정렬, 보드·캘린더 보기 등을 자유롭게 추가해도 됩니다(속성 이름만 바꾸지 마세요 — 지워진 속성은 다음 실행 때 다시 만들어집니다).
- **대시보드를 다른 페이지에 넣기**: `/embed` 입력 → Pages 주소 붙여넣기 → 모서리를 끌어 높이 조절(1200px 정도 권장).
  - Notion 다크 모드: 주소 끝에 `?theme=dark`
  - 특정 탭만: `…/#history`, `…/#validation` (예: `https://…github.io/market-risk-monitor/?theme=dark#history`)

---

## 내 PC에서 직접 돌리기 (선택)

```bash
pip install -r requirements.txt
python run_monitor.py --market KR --data-root data_root --sync      # 데이터 받기 + 계산 (첫 실행 1GB 내려받음)
python notion_kit/build_site.py --in outputs/KR --out site          # site/index.html — 더블클릭으로 열림
python notion_kit/notion_sync.py --dry-run                           # Notion에 보낼 내용만 미리보기 (토큰 불필요)

# 실제 기록 (macOS/Linux)
export NOTION_TOKEN="ntn_..."
export NOTION_PAGE_ID="https://www.notion.so/..."
python notion_kit/notion_sync.py --in outputs/KR --charts site/charts
# Windows PowerShell:  $env:NOTION_TOKEN="ntn_..."; $env:NOTION_PAGE_ID="..."
```

토큰 없이 동기화 코드를 검증하려면 가짜 Notion 서버 테스트를 돌리세요: `python -m unittest tests/test_notion_sync.py -v`

## 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `401 unauthorized` | 토큰 오타·폐기. 통합 페이지에서 시크릿을 다시 복사해 `NOTION_TOKEN` 갱신 |
| `404 object_not_found` | 부모 페이지에 통합을 **연결**하지 않았거나 페이지 주소가 다름 (3단계) |
| `403 restricted_resource` | 통합 기능에서 읽기·업데이트·삽입 권한을 켜지 않음 (2단계 3번) |
| Notion에 임베드가 안 생김 | Pages를 켜지 않은 상태(5단계). 켠 뒤 다시 실행 |
| 자동 실행이 멈춤 | 공개 저장소는 60일간 커밋이 없으면 GitHub이 예약 실행을 끕니다. Actions 탭에서 다시 켜기 |
| VIX가 오래된 값 | Cboe 사이트 접속 실패 시 GitHub 데이터로 대체됩니다. 로그의 `[VIX]` 줄 확인 |

## 폴더 구조

```
run_monitor.py               실행기: 데이터 → 신호 → 점수 → 레짐·확률·VaR → outputs/KR/
mrm/                         모델 (data, signals, model, evaluate, pipeline, export)
config/manual_observations.csv  원천 데이터가 늦을 때 수동 보강값(출처 포함)
notion_kit/
  build_site.py              단일 HTML 대시보드 + 차트 PNG 생성 (site/)
  site_template.html         대시보드 화면 (d3 내장, 인터넷 없이도 열림)
  charts.py                  Notion용 정적 차트
  notion_sync.py             Notion API 동기화 (표준 라이브러리만 사용)
  mock_notion_server.py      테스트용 가짜 Notion API
  vendor/d3.min.js           d3 v7.9.0 (ISC 라이선스, d3-LICENSE.txt)
tests/test_notion_sync.py    동기화 전 과정 테스트
.github/workflows/risk-monitor.yml   자동 실행 설정
```

## 알아둘 점

- 웹 대시보드 주소는 **누구나 볼 수 있습니다**(공개 시세 기반 계산 결과뿐이라 개인정보는 없음).
- Notion API 버전은 `2022-06-28` 로 고정했습니다. Notion은 이 버전을 단일 데이터 소스 데이터베이스에서 계속 지원한다고 밝히고 있습니다. 로그 데이터베이스에 데이터 소스를 하나 더 추가하면 이 버전으로는 쓸 수 없게 되니 추가하지 마세요.
- 이 모델은 변동성·꼬리위험의 **크기**를 재는 도구이며 수익률 방향을 예측하지 않습니다. 투자 판단은 본인 책임입니다.
