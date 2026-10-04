#!/usr/bin/env python3
"""코인 투자 타이밍 알리미.

업비트 공개 API로 시세를 조회해 기술적 지표(RSI, 이동평균 크로스, 급등락,
거래량 급증)를 계산하고, 조건이 충족되면 텔레그램으로 알림을 보낸다.

사용법:
    python alert.py          # 일반 실행 (쿨다운/상태 반영)
    python alert.py --test   # 테스트: 현재 스냅샷 + 신호를 즉시 전송 (상태 저장 안 함)

환경 변수:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (없으면 콘솔에만 출력하는 DRY RUN)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"
KST = timezone(timedelta(hours=9))
UPBIT = "https://api.upbit.com/v1"

session = requests.Session()
session.headers.update({"Accept": "application/json", "User-Agent": "coin-alert/1.0"})


# ──────────────────────────── 데이터 조회 ────────────────────────────
def upbit(path: str, **params):
    """업비트 API GET (429 대응 재시도 포함)."""
    resp = None
    for attempt in range(3):
        resp = session.get(f"{UPBIT}{path}", params=params, timeout=10)
        if resp.status_code == 429:
            time.sleep(1 + attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


# ──────────────────────────── 지표 계산 ────────────────────────────
def calc_rsi(closes: list[float], period: int) -> float | None:
    """Wilder 방식 RSI."""
    if len(closes) < period + 1:
        return None
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    avg_g, avg_l = gain / period, loss / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def analyze(coin: str, cfg: dict) -> dict:
    """코인 하나의 지표 스냅샷을 계산한다."""
    market = f"KRW-{coin}"

    # 1시간봉 200개 (업비트는 최신순 → 과거순으로 뒤집기). 마지막 원소는 진행 중인 봉.
    hourly = list(reversed(upbit("/candles/minutes/60", market=market, count=200)))
    closes = [c["trade_price"] for c in hourly]
    volumes = [c["candle_acc_trade_volume"] for c in hourly]
    price = closes[-1]
    closed = closes[:-1]          # 완성된 봉만 (크로스 판정용)
    closed_vol = volumes[:-1]

    snap: dict = {"coin": coin, "price": price}

    # RSI (현재가 포함)
    snap["rsi"] = calc_rsi(closes, cfg["rsi"]["period"])

    # N분 변동률 (5분봉 기준)
    window = int(cfg["price_change"]["window_minutes"])
    n = max(1, window // 5)
    m5 = upbit("/candles/minutes/5", market=market, count=n + 1)
    past = m5[-1]["trade_price"] if len(m5) > n else m5[-1]["opening_price"]
    snap["change_window"] = (price - past) / past * 100

    # 이동평균 크로스 (완성봉 기준, 직전 봉 대비 교차 여부)
    s, l = cfg["ma_cross"]["short"], cfg["ma_cross"]["long"]
    ma_s, ma_l = sma(closed, s), sma(closed, l)
    pma_s, pma_l = sma(closed[:-1], s), sma(closed[:-1], l)
    snap["ma_s"], snap["ma_l"] = ma_s, ma_l
    snap["cross"] = None
    if None not in (ma_s, ma_l, pma_s, pma_l):
        if pma_s <= pma_l and ma_s > ma_l:
            snap["cross"] = "golden"
        elif pma_s >= pma_l and ma_s < ma_l:
            snap["cross"] = "dead"

    # 거래량 급증 (직전 완성봉 vs 그 이전 N개 평균)
    lb = cfg["volume_spike"]["lookback"]
    if len(closed_vol) > lb:
        avg = sum(closed_vol[-lb - 1:-1]) / lb
        snap["vol_ratio"] = closed_vol[-1] / avg if avg > 0 else 0.0
    else:
        snap["vol_ratio"] = None

    return snap


# ──────────────────────────── 신호 판정 ────────────────────────────
def won(p: float) -> str:
    if p >= 100:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:,.4f}"


def rsi_zone(rsi: float | None, cfg: dict) -> str:
    if rsi is None:
        return "mid"
    if rsi < cfg["rsi"]["oversold"]:
        return "low"
    if rsi > cfg["rsi"]["overbought"]:
        return "high"
    return "mid"


def detect(snap: dict, cfg: dict, prev_zone: str) -> list[tuple[str, str]]:
    """(신호키, 메시지) 목록을 반환한다."""
    c, p = snap["coin"], won(snap["price"])
    out: list[tuple[str, str]] = []

    if cfg["rsi"]["enabled"] and snap["rsi"] is not None:
        zone = rsi_zone(snap["rsi"], cfg)
        if zone != prev_zone and zone == "low":
            out.append(("rsi_low", f"🟢 <b>{c}</b> RSI 과매도 진입\nRSI {snap['rsi']:.1f} · 현재가 {p}원\n→ 많이 떨어졌어요. 매수 검토 구간"))
        elif zone != prev_zone and zone == "high":
            out.append(("rsi_high", f"🔴 <b>{c}</b> RSI 과매수 진입\nRSI {snap['rsi']:.1f} · 현재가 {p}원\n→ 많이 올랐어요. 매도 검토 구간"))

    pc = cfg["price_change"]
    if pc["enabled"]:
        chg = snap["change_window"]
        if chg >= pc["threshold_pct"]:
            out.append(("surge_up", f"🚀 <b>{c}</b> 급등 +{chg:.2f}% ({pc['window_minutes']}분)\n현재가 {p}원"))
        elif chg <= -pc["threshold_pct"]:
            out.append(("surge_down", f"⚠️ <b>{c}</b> 급락 {chg:.2f}% ({pc['window_minutes']}분)\n현재가 {p}원"))

    mc = cfg["ma_cross"]
    if mc["enabled"] and snap["cross"]:
        if snap["cross"] == "golden":
            out.append(("golden", f"✨ <b>{c}</b> 골든크로스 (1시간봉 MA{mc['short']} ↗ MA{mc['long']})\n현재가 {p}원\n→ 상승 추세 전환 가능성"))
        else:
            out.append(("dead", f"💀 <b>{c}</b> 데드크로스 (1시간봉 MA{mc['short']} ↘ MA{mc['long']})\n현재가 {p}원\n→ 하락 추세 전환 가능성"))

    vs = cfg["volume_spike"]
    if vs["enabled"] and snap["vol_ratio"] and snap["vol_ratio"] >= vs["multiplier"]:
        out.append(("volume", f"🐋 <b>{c}</b> 거래량 급증 ×{snap['vol_ratio']:.1f}\n(직전 1시간 vs {vs['lookback']}시간 평균) · 현재가 {p}원"))

    return out


def summary_text(snaps: list[dict], now: datetime, cfg: dict) -> str:
    days = "월화수목금토일"
    lines = [f"📰 <b>코인 요약</b> · {now:%m/%d} ({days[now.weekday()]}) {now:%H:%M}", ""]
    for s in snaps:
        rsi = s["rsi"]
        zone = rsi_zone(rsi, cfg)
        mark = {"low": "🟢과매도", "high": "🔴과매수"}.get(zone, "")
        trend = ""
        if s["ma_s"] and s["ma_l"]:
            trend = "📈" if s["ma_s"] > s["ma_l"] else "📉"
        rsi_txt = f"{rsi:.0f}" if rsi is not None else "-"
        lines.append(
            f"<b>{s['coin']}</b> {won(s['price'])}원 ({s.get('change_24h', 0):+.2f}%)\n"
            f"   RSI {rsi_txt} {mark} · 추세 {trend}"
        )
    lines += ["", "<i>※ 투자 참고용이며 투자 조언이 아닙니다.</i>"]
    return "\n".join(lines)


# ──────────────────────────── 알림 / 상태 ────────────────────────────
def send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("──── [DRY RUN] 텔레그램 미설정 · 전송될 메시지 ────")
        print(text)
        print("─" * 50)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")
    print("텔레그램 전송 완료")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    test = "--test" in sys.argv
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    coins = [c.upper() for c in cfg["coins"]]
    now = datetime.now(KST)

    state = load_state()
    original = json.dumps(state, sort_keys=True)
    state.setdefault("last_alert", {})
    state.setdefault("rsi_zone", {})

    tickers = {}
    try:
        data = upbit("/ticker", markets=",".join(f"KRW-{c}" for c in coins))
        tickers = {t["market"].split("-", 1)[1]: t for t in data}
    except Exception as e:  # noqa: BLE001
        print(f"현재가(ticker) 조회 실패: {e}", file=sys.stderr)

    snaps, alerts, errors = [], [], 0
    cooldown = timedelta(minutes=cfg.get("cooldown_minutes", 180))

    for coin in coins:
        try:
            snap = analyze(coin, cfg)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[{coin}] 조회/계산 실패: {e}", file=sys.stderr)
            continue
        snap["change_24h"] = tickers.get(coin, {}).get("signed_change_rate", 0.0) * 100
        snaps.append(snap)

        rsi_txt = f"{snap['rsi']:.1f}" if snap["rsi"] is not None else "-"
        vol_txt = f"{snap['vol_ratio']:.2f}" if snap["vol_ratio"] is not None else "-"
        print(f"[{coin}] 가격 {won(snap['price'])} | RSI {rsi_txt} | "
              f"{cfg['price_change']['window_minutes']}분 {snap['change_window']:+.2f}% | "
              f"거래량배수 {vol_txt} | 크로스 {snap['cross']}")

        prev_zone = state["rsi_zone"].get(coin, "mid")
        for key, text in detect(snap, cfg, prev_zone):
            k = f"{coin}:{key}"
            last = state["last_alert"].get(k)
            if test or not last or now - datetime.fromisoformat(last) >= cooldown:
                alerts.append(text)
                if not test:
                    state["last_alert"][k] = now.isoformat(timespec="seconds")
            else:
                print(f"  └ {key} 신호 있음 (쿨다운 중이라 생략)")
        if not test:
            state["rsi_zone"][coin] = rsi_zone(snap["rsi"], cfg)
        time.sleep(0.15)

    if not snaps:
        print("모든 코인 조회 실패", file=sys.stderr)
        return 1

    header = f"🔔 <b>코인 알림</b> · {now:%m/%d %H:%M} KST"
    if test:
        send("✅ <b>코인 알리미 테스트</b>\n연결 성공! 아래는 현재 상태예요.")
        send(summary_text(snaps, now, cfg))
        if alerts:
            send(header + " (테스트)\n\n" + "\n\n".join(alerts))
        return 0

    if alerts:
        send(header + "\n\n" + "\n\n".join(alerts))
    else:
        print("발생한 신호 없음")

    ds = cfg.get("daily_summary", {})
    today = now.strftime("%Y-%m-%d")
    if ds.get("enabled") and now.hour >= ds.get("hour_kst", 9) and state.get("last_summary") != today:
        send(summary_text(snaps, now, cfg))
        state["last_summary"] = today

    if json.dumps(state, sort_keys=True) != original:
        save_state(state)
        print("state.json 갱신")

    return 0 if errors < len(coins) else 1


if __name__ == "__main__":
    sys.exit(main())
