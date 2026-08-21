# JY Fund Signal

미국 주식 매수 신호 스캐너. JY Fund PRD v2.1 기반 7팩터 스코어링.

**Live**: https://thehfk.github.io/jyfund-signal/

## 아키텍처

```
data/universe.json ──→ GitHub Actions cron (30분마다)
                          │
                          ├─ Yahoo Finance 200일 OHLCV × 종목
                          ├─ 7 factors 계산 → JY Score
                          ├─ State 결정 (STRONG/ACTIONABLE/READY/WATCH/PASS)
                          └─→ data/data.json → git commit → GitHub Pages

브라우저 (index.html)
   ├─ data.json fetch (전체 universe 시그널)
   ├─ localStorage jyfund_groups 읽기
   └─ 선택된 그룹 종목만 필터링해서 표시
```

## Universe 관리

`data/universe.json`에 티커 추가/제거 → commit → 다음 cron부터 반영.

```json
{ "tickers": ["NVDA", "MSFT", ...] }
```

## 그룹 (localStorage, 브라우저 로컬)

`⚙ 그룹 관리`에서 그룹 생성/편집. 브라우저마다 다르며 서버에 저장되지 않음.

기본 그룹: `Default` = `[NVDA, GOOGL, MSFT]`.

## 팩터 스코어 (총 100점, 실질 상한 ~85)

| 팩터 | 배점 | 로직 |
|---|---|---|
| Long-term Trend | 20 | MA20>MA50>MA200 정렬 + MA20 기울기 |
| Pullback Quality | 15 | 20d 고점 대비 2-8% 조정이면서 MA20 위 sweet spot |
| Volume | 15 | 오늘 volume / 20일 평균 (RVOL) + 캔들 색 |
| Reversal Pattern | 15 | Hammer / Bullish Engulfing / Higher Low |
| Relative Strength | 10 | 20d 수익률 vs SPY |
| Market Environment | 10 | VIX + SPY vs MA50/MA200 |
| R:R | 15 | Trigger→Target vs Trigger→Invalidation |

State thresholds: STRONG ≥78, ACTIONABLE ≥65, READY ≥52, WATCH ≥38, else PASS.

## Trigger 가격

| 이름 | 정의 |
|---|---|
| EARLY | MA20 (되돌림 진입) |
| CONFIRM | 20d 종가 신고점 (돌파 확인) |
| TREND | CONFIRM × 1.10 (추세 확장 타깃) |
| Invalidation | min(MA50, MA20 × 0.95) |

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
python scripts/refresh_data.py
python -m http.server 8000
# → http://localhost:8000
```

## 저장소 구조

```
jyfund-signal/
├── index.html              # 프론트엔드 (SPA)
├── data/
│   ├── universe.json       # 스캔 대상 티커 목록
│   └── data.json           # workflow가 자동 생성
├── scripts/
│   ├── refresh_data.py     # 스코어링 파이프라인
│   └── requirements.txt
└── .github/workflows/
    └── refresh-data.yml    # 30분 cron
```

## Phase 2 (예정)

- Event Risk Overlay (earnings calendar)
- Slack 웹훅 알림 (상태 변화 시)
- Support/Resistance 자동 감지 개선
- Universe 웹 UI 관리
