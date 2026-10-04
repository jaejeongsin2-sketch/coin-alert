#!/usr/bin/env python3
"""코인 투자 타이밍 알리미.

데이터 소스
  - 업비트   : 원화 시세, RSI, 이동평균, 급등락, 국내 거래량
  - 바이낸스 : 글로벌 현물 거래량, 시장가 매수 비율(taker buy ratio)  ※ data-api.binance.vision
  - OKX      : 선물 펀딩비, 미결제약정(OI)  ※ 바이낸스 선물은 미국 서버에서 차단되어 대체
  - 환율 API : 김치 프리미엄 계산용 USD/KRW

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
BINANCE = "https://data-api.binance.vision/api/v3"
OKX = "https://www.okx.com/api/v5"
FX_URL = "https://open.er-api.com/v6/latest/USD"

session = requests.Session()
session.headers.update({"Accept": "application/json", "User-Agent": "coin-alert/1.1"})


# ──────────────────────────── 데이터 조회 ────────────────────────────
def http_get(url: str, **params):
    """GET + JSON (429 대응 재시도 포함)."""
    resp = None
    for attempt in range(3):
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 429:
            time.sleep(1 + attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def upbit(path: str, **params):
    return http_get(f"{UPBIT}{path}", **params)


def okx(path: str, **params) -> list:
    body = http_get(f"{OKX}{path}", **params)
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX 오류 {body.get('code')}: {body.get('msg')}")
    time.sleep(0.25)  # OKX 공개 API 속도 제한 여유
    return body["data"]


def fetch_usdkrw() -> tuple[float | None, str]:
    """USD/KRW 환율. 실패 시 업비트 USDT 가격으로 대체."""
    try:
        return float(http_get(FX_URL)["rates"]["KRW"]), "환율"
    except Exception as e:  # noqa: BLE001
        print(f"환율 조회 실패({e}) → 업비트 USDT 가격으로 대체", file=sys.stderr)
    try:
        return float(upbit("/ticker", markets="KRW-USDT")[0]["trade_price"]), "USDT"
    except Exception as e:  # noqa: BLE001
        print(f"USDT 가격 조회 실패: {e}", file=sys.stderr)
    return None, ""


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


def analyze(coin: str, cfg: dict, usdkrw: float | None) -> dict:
    """코인 하나의 지표 스냅샷을 계산한다. 업비트 실패 시 예외, 나머지 소스는 실패해도 None."""
    market = f"KRW-{coin}"

    # ── 업비트: 1시간봉 200개 (최신순 → 과거순). 마지막 원소는 진행 중인 봉.
    hourly = list(reversed(upbit("/candles/minutes/60", market=market, count=200)))
    closes = [c["trade_price"] for c in hourly]
    volumes = [c["candle_acc_trade_volume"] for c in hourly]
    price = closes[-1]
    closed = closes[:-1]
    closed_vol = volumes[:-1]

    snap: dict = {"coin": coin, "price": price, "closes": closes}
    snap["rsi"] = calc_rsi(closes, cfg["rsi"]["period"])

    window = int(cfg["price_change"]["window_minutes"])
    n = max(1, window // 5)
    m5 = upbit("/candles/minutes/5", market=market, count=n + 1)
    past = m5[-1]["trade_price"] if len(m5) > n else m5[-1]["opening_price"]
    snap["change_window"] = (price - past) / past * 100

    s, l = cfg["ma_cross"]["short"], cfg["ma_cross"]["long"]
    ma_s, ma_l = sma(closed, s), sma(closed, l)
    pma_s, pma_l = sma(closed[:-1], s), sma(closed[:-1], l)
    snap["ma_s"], snap["ma_l"], snap["cross"] = ma_s, ma_l, None
    if None not in (ma_s, ma_l, pma_s, pma_l):
        if pma_s <= pma_l and ma_s > ma_l:
            snap["cross"] = "golden"
        elif pma_s >= pma_l and ma_s < ma_l:
            snap["cross"] = "dead"

    lb = cfg["volume_spike"]["lookback"]
    snap["vol_ratio"] = None
    if len(closed_vol) > lb:
        avg = sum(closed_vol[-lb - 1:-1]) / lb
        snap["vol_ratio"] = closed_vol[-1] / avg if avg > 0 else 0.0

    # ── 바이낸스 현물: 글로벌 거래량 + 시장가 매수 비율
    snap.update(bn_price=None, bn_vol_ratio=None, bn_buy_ratio=None, bn_candle_pct=None)
    bcfg = cfg.get("binance_volume", {})
    need_bn = bcfg.get("enabled") or cfg.get("kimchi_premium", {}).get("enabled")
    if need_bn:
        try:
            blb = int(bcfg.get("lookback", 20))
            kl = http_get(f"{BINANCE}/klines", symbol=f"{coin}USDT", interval="1h", limit=blb + 2)
            # [openTime, open, high, low, close, vol, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, _]
            snap["bn_price"] = float(kl[-1][4])
            done = kl[:-1]
            last = done[-1]
            qv, tbq = float(last[7]), float(last[10])
            hist = [float(k[7]) for k in done[-blb - 1:-1]]
            avg = sum(hist) / len(hist) if hist else 0
            snap["bn_vol_ratio"] = qv / avg if avg > 0 else None
            snap["bn_buy_ratio"] = tbq / qv if qv > 0 else None
            o, c = float(last[1]), float(last[4])
            snap["bn_candle_pct"] = (c - o) / o * 100 if o else None
        except Exception as e:  # noqa: BLE001
            print(f"[{coin}] 바이낸스 조회 실패: {e}", file=sys.stderr)

    # ── 김치 프리미엄
    snap["kimp"] = None
    if usdkrw and snap["bn_price"]:
        snap["kimp"] = (price / (snap["bn_price"] * usdkrw) - 1) * 100

    # ── OKX 선물: 펀딩비, 미결제약정
    snap.update(funding=None, oi_change=None, oi_price_change=None)
    inst = f"{coin}-USDT-SWAP"
    if cfg.get("funding_rate", {}).get("enabled"):
        try:
            snap["funding"] = float(okx("/public/funding-rate", instId=inst)[0]["fundingRate"]) * 100
        except Exception as e:  # noqa: BLE001
            print(f"[{coin}] OKX 펀딩비 조회 실패: {e}", file=sys.stderr)
    ocfg = cfg.get("open_interest", {})
    if ocfg.get("enabled"):
        try:
            w = int(ocfg.get("window_hours", 1))
            rows = okx("/rubik/stat/contracts/open-interest-history", instId=inst, period="1H", limit=w + 1)
            # [ts, oi(계약수), oiCcy(코인수량), oiUsd] 최신순
            now_oi, past_oi = float(rows[0][2]), float(rows[w][2])
            if past_oi > 0:
                snap["oi_change"] = (now_oi - past_oi) / past_oi * 100
            if len(closes) > w:
                snap["oi_price_change"] = (closes[-1] - closes[-1 - w]) / closes[-1 - w] * 100
        except Exception as e:  # noqa: BLE001
            print(f"[{coin}] OKX 미결제약정 조회 실패: {e}", file=sys.stderr)

    del snap["closes"]
    return snap


# ──────────────────────────── 신호 판정 ────────────────────────────
def won(p: float) -> str:
    if p >= 100:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:,.4f}"


def band(value: float | None, low: float, high: float, *, inclusive: bool = False) -> str:
    if value is None:
        return "mid"
    if (value <= low) if inclusive else (value < low):
        return "low"
    if (value >= high) if inclusive else (value > high):
        return "high"
    return "mid"


def zones(snap: dict, cfg: dict) -> dict:
    """구간(과매도/과열 등) 상태. 구간에 '진입'할 때만 알림을 보내기 위해 사용."""
    k = cfg.get("kimchi_premium", {})
    f = cfg.get("funding_rate", {})
    return {
        "rsi": band(snap["rsi"], cfg["rsi"]["oversold"], cfg["rsi"]["overbought"]),
        "kimp": band(snap.get("kimp"), k.get("low_pct", -1.0), k.get("high_pct", 5.0), inclusive=True),
        "funding": band(snap.get("funding"), f.get("low_pct", -0.03), f.get("high_pct", 0.05), inclusive=True),
    }


def flow_text(buy_ratio: float | None, cfg: dict) -> str:
    if buy_ratio is None:
        return ""
    b = cfg.get("binance_volume", {})
    if buy_ratio >= b.get("buy_strong", 0.55):
        return f"💚 매수세 우위 (시장가 매수 {buy_ratio * 100:.0f}%)"
    if buy_ratio <= b.get("sell_strong", 0.45):
        return f"❤️ 매도세 우위 (시장가 매수 {buy_ratio * 100:.0f}%)"
    return f"⚖️ 매수·매도 비슷 (시장가 매수 {buy_ratio * 100:.0f}%)"


def oi_text(oi: float, pchg: float | None) -> str:
    if pchg is None:
        return "신규 포지션 유입" if oi > 0 else "포지션 청산"
    if oi > 0 and pchg >= 0:
        return "신규 롱 유입 → 상승 추세 강화"
    if oi > 0:
        return "신규 숏 유입 → 하락 압력 증가"
    if pchg >= 0:
        return "숏 청산 → 숏 스퀴즈성 상승"
    return "롱 청산 → 투매성 하락"


def detect(snap: dict, cfg: dict, prev: dict) -> list[tuple[str, str]]:
    """(신호키, 메시지) 목록을 반환한다. prev: 직전 구간 상태."""
    c, p = snap["coin"], won(snap["price"])
    z = zones(snap, cfg)
    out: list[tuple[str, str]] = []

    def entered(name: str, zone: str) -> bool:
        return z[name] == zone and prev.get(name, "mid") != zone

    # RSI
    if cfg["rsi"]["enabled"] and snap["rsi"] is not None:
        if entered("rsi", "low"):
            out.append(("rsi_low", f"🟢 <b>{c}</b> RSI 과매도 진입\nRSI {snap['rsi']:.1f} · 현재가 {p}원\n→ 많이 떨어졌어요. 매수 검토 구간"))
        elif entered("rsi", "high"):
            out.append(("rsi_high", f"🔴 <b>{c}</b> RSI 과매수 진입\nRSI {snap['rsi']:.1f} · 현재가 {p}원\n→ 많이 올랐어요. 매도 검토 구간"))

    # 급등락
    pc = cfg["price_change"]
    if pc["enabled"]:
        chg = snap["change_window"]
        if chg >= pc["threshold_pct"]:
            out.append(("surge_up", f"🚀 <b>{c}</b> 급등 +{chg:.2f}% ({pc['window_minutes']}분)\n현재가 {p}원"))
        elif chg <= -pc["threshold_pct"]:
            out.append(("surge_down", f"⚠️ <b>{c}</b> 급락 {chg:.2f}% ({pc['window_minutes']}분)\n현재가 {p}원"))

    # 이동평균 크로스
    mc = cfg["ma_cross"]
    if mc["enabled"] and snap["cross"] == "golden":
        out.append(("golden", f"✨ <b>{c}</b> 골든크로스 (1시간봉 MA{mc['short']} ↗ MA{mc['long']})\n현재가 {p}원\n→ 상승 추세 전환 가능성"))
    elif mc["enabled"] and snap["cross"] == "dead":
        out.append(("dead", f"💀 <b>{c}</b> 데드크로스 (1시간봉 MA{mc['short']} ↘ MA{mc['long']})\n현재가 {p}원\n→ 하락 추세 전환 가능성"))

    # 업비트(국내) 거래량 급증
    vs = cfg["volume_spike"]
    if vs["enabled"] and snap["vol_ratio"] and snap["vol_ratio"] >= vs["multiplier"]:
        out.append(("volume", f"🇰🇷 <b>{c}</b> 업비트 거래량 급증 ×{snap['vol_ratio']:.1f}\n(직전 1시간 vs {vs['lookback']}시간 평균) · 현재가 {p}원"))

    # 바이낸스(글로벌) 거래량 급증 + 매수/매도 방향
    bv = cfg.get("binance_volume", {})
    if bv.get("enabled") and snap["bn_vol_ratio"] and snap["bn_vol_ratio"] >= bv.get("multiplier", 3.0):
        candle = f" · 1시간 캔들 {snap['bn_candle_pct']:+.2f}%" if snap["bn_candle_pct"] is not None else ""
        out.append(("bn_volume", f"🐋 <b>{c}</b> 바이낸스 거래량 급증 ×{snap['bn_vol_ratio']:.1f}{candle}\n"
                                 f"{flow_text(snap['bn_buy_ratio'], cfg)}\n현재가 {p}원"))

    # 김치 프리미엄
    if cfg.get("kimchi_premium", {}).get("enabled") and snap.get("kimp") is not None:
        k = snap["kimp"]
        if entered("kimp", "high"):
            out.append(("kimp_high", f"🌶️ <b>{c}</b> 김치 프리미엄 과열 {k:+.2f}%\n→ 국내 매수 과열. 고점 주의"))
        elif entered("kimp", "low"):
            out.append(("kimp_low", f"🧊 <b>{c}</b> 역프리미엄 {k:+.2f}%\n→ 국내 투자심리 위축. 저점 신호일 수 있음"))

    # 펀딩비
    if cfg.get("funding_rate", {}).get("enabled") and snap.get("funding") is not None:
        f = snap["funding"]
        if entered("funding", "high"):
            out.append(("funding_high", f"🔥 <b>{c}</b> 펀딩비 과열 {f:.3f}% (OKX)\n→ 롱 포지션 과밀. 롱 청산(급락) 주의"))
        elif entered("funding", "low"):
            out.append(("funding_low", f"🥶 <b>{c}</b> 펀딩비 마이너스 {f:.3f}% (OKX)\n→ 숏 포지션 과밀. 숏 스퀴즈(급등) 가능성"))

    # 미결제약정 급변
    oc = cfg.get("open_interest", {})
    if oc.get("enabled") and snap.get("oi_change") is not None and abs(snap["oi_change"]) >= oc.get("threshold_pct", 3.0):
        oi, pchg = snap["oi_change"], snap.get("oi_price_change")
        ptxt = f"가격 {pchg:+.2f}% · " if pchg is not None else ""
        key = "oi_up" if oi > 0 else "oi_down"
        out.append((key, f"📊 <b>{c}</b> 미결제약정 {oi:+.2f}% ({oc.get('window_hours', 1)}시간, OKX)\n{ptxt}{oi_text(oi, pchg)}"))

    return out


def summary_text(snaps: list[dict], now: datetime, cfg: dict, fx: tuple[float | None, str]) -> str:
    days = "월화수목금토일"
    lines = [f"📰 <b>코인 요약</b> · {now:%m/%d} ({days[now.weekday()]}) {now:%H:%M}", ""]
    for s in snaps:
        z = zones(s, cfg)
        mark = {"low": "🟢과매도", "high": "🔴과매수"}.get(z["rsi"], "")
        trend = ("📈" if s["ma_s"] > s["ma_l"] else "📉") if s["ma_s"] and s["ma_l"] else "-"
        rsi_txt = f"{s['rsi']:.0f}" if s["rsi"] is not None else "-"
        row1 = f"<b>{s['coin']}</b> {won(s['price'])}원 ({s.get('change_24h', 0):+.2f}%)"
        row2 = [f"RSI {rsi_txt}{(' ' + mark) if mark else ''}", f"추세 {trend}"]
        if s.get("kimp") is not None:
            row2.append(f"김프 {s['kimp']:+.1f}%")
        row3 = []
        if s.get("bn_buy_ratio") is not None:
            row3.append(f"매수비율 {s['bn_buy_ratio'] * 100:.0f}%")
        if s.get("funding") is not None:
            row3.append(f"펀딩 {s['funding']:.3f}%")
        if s.get("oi_change") is not None:
            row3.append(f"OI {s['oi_change']:+.1f}%")
        lines.append(row1)
        lines.append("   " + " · ".join(row2))
        if row3:
            lines.append("   " + " · ".join(row3))
    if fx[0]:
        lines += ["", f"💱 USD/KRW {fx[0]:,.1f} ({fx[1]} 기준)"]
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


def fmt(v: float | None, spec: str, suffix: str = "") -> str:
    return "-" if v is None else f"{v:{spec}}{suffix}"


def main() -> int:
    test = "--test" in sys.argv
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    coins = [c.upper() for c in cfg["coins"]]
    now = datetime.now(KST)

    state = load_state()
    original = json.dumps(state, sort_keys=True)
    state.setdefault("last_alert", {})
    state.setdefault("zones", {})
    # 구버전 상태 마이그레이션
    for coin, z in state.pop("rsi_zone", {}).items():
        state["zones"].setdefault(coin, {})["rsi"] = z

    tickers = {}
    try:
        data = upbit("/ticker", markets=",".join(f"KRW-{c}" for c in coins))
        tickers = {t["market"].split("-", 1)[1]: t for t in data}
    except Exception as e:  # noqa: BLE001
        print(f"현재가(ticker) 조회 실패: {e}", file=sys.stderr)

    fx = fetch_usdkrw() if cfg.get("kimchi_premium", {}).get("enabled") else (None, "")

    snaps, alerts, errors = [], [], 0
    cooldown = timedelta(minutes=cfg.get("cooldown_minutes", 180))

    for coin in coins:
        try:
            snap = analyze(coin, cfg, fx[0])
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[{coin}] 조회/계산 실패: {e}", file=sys.stderr)
            continue
        snap["change_24h"] = tickers.get(coin, {}).get("signed_change_rate", 0.0) * 100
        snaps.append(snap)

        print(f"[{coin}] 가격 {won(snap['price'])} | RSI {fmt(snap['rsi'], '.1f')} | "
              f"{cfg['price_change']['window_minutes']}분 {snap['change_window']:+.2f}% | "
              f"업비트량 ×{fmt(snap['vol_ratio'], '.2f')} | 크로스 {snap['cross']}")
        print(f"       바이낸스량 ×{fmt(snap['bn_vol_ratio'], '.2f')} | 매수비율 {fmt(snap['bn_buy_ratio'], '.1%')} | "
              f"김프 {fmt(snap['kimp'], '+.2f', '%')} | 펀딩 {fmt(snap['funding'], '.4f', '%')} | "
              f"OI {fmt(snap['oi_change'], '+.2f', '%')}")

        prev = state["zones"].get(coin, {})
        for key, text in detect(snap, cfg, prev):
            k = f"{coin}:{key}"
            last = state["last_alert"].get(k)
            if test or not last or now - datetime.fromisoformat(last) >= cooldown:
                alerts.append(text)
                if not test:
                    state["last_alert"][k] = now.isoformat(timespec="seconds")
            else:
                print(f"  └ {key} 신호 있음 (쿨다운 중이라 생략)")
        if not test:
            state["zones"][coin] = zones(snap, cfg)
        time.sleep(0.15)

    if not snaps:
        print("모든 코인 조회 실패", file=sys.stderr)
        return 1

    header = f"🔔 <b>코인 알림</b> · {now:%m/%d %H:%M} KST"
    if test:
        send("✅ <b>코인 알리미 테스트</b>\n연결 성공! 아래는 현재 상태예요.")
        send(summary_text(snaps, now, cfg, fx))
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
        send(summary_text(snaps, now, cfg, fx))
        state["last_summary"] = today

    if json.dumps(state, sort_keys=True) != original:
        save_state(state)
        print("state.json 갱신")

    return 0 if errors < len(coins) else 1


if __name__ == "__main__":
    sys.exit(main())
