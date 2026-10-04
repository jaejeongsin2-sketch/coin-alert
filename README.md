# 🪙 코인 투자 타이밍 알리미

GitHub Actions가 **5분마다** 업비트 시세를 확인하고, 아래 조건이 충족되면 **텔레그램으로 알림**을 보냅니다. 서버 비용 0원.

| 신호 | 기본 조건 |
|---|---|
| 🟢 RSI 과매도 / 🔴 과매수 | 1시간봉 RSI(14) 30 미만 / 70 초과 구간 진입 |
| 🚀 급등 / ⚠️ 급락 | 최근 60분 ±3% 이상 |
| ✨ 골든크로스 / 💀 데드크로스 | 1시간봉 MA20 ↔ MA60 교차 |
| 🐋 거래량 급증 | 직전 1시간 거래량이 20시간 평균의 3배 이상 |
| 📰 일일 요약 | 매일 오전 9시(KST) |

같은 코인의 같은 신호는 3시간에 한 번만 보냅니다.

## 설정 바꾸기
[`config.yaml`](config.yaml)을 GitHub 웹에서 ✏️ 수정 → **Commit changes** 하면 끝. 다음 실행부터 반영됩니다.

## 테스트 / 수동 실행
**Actions** 탭 → `coin-alert` → **Run workflow** → `test` 체크 → 실행
→ 텔레그램으로 현재 상태가 바로 옵니다.

## 텔레그램 연결 (Secrets)
**Settings → Secrets and variables → Actions** 에 아래 두 값을 등록합니다.
- `TELEGRAM_BOT_TOKEN` — @BotFather 에게 받은 봇 토큰
- `TELEGRAM_CHAT_ID` — 알림 받을 채팅 ID

Secrets가 없으면 메시지를 Actions 로그에만 출력합니다(DRY RUN).

## 일시 정지
**Actions** 탭 → `coin-alert` → 우측 `···` → **Disable workflow**

## 파일 구성
- `alert.py` — 시세 조회 · 지표 계산 · 알림 전송
- `config.yaml` — 감시 코인 및 조건
- `state.json` — 중복 알림 방지 기록 (자동 갱신)
- `.github/workflows/alert.yml` — 5분 주기 실행

> ⚠️ 투자 참고용 도구이며 투자 조언이 아닙니다. 모든 투자 판단과 책임은 본인에게 있습니다.
