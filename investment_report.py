import anthropic
import yfinance as yf
import smtplib
import schedule
import time
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 설정 (환경변수 우선) ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS",      "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL",    "")

if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

US_TICKERS = ["NVDA", "GOOGL", "VOO", "FCX", "MSFT", "PLTR", "RGTI", "JOBY", "BEAM", "PWFL"]
KR_TICKERS = [
    "068760.KS",  # 셀트리온제약 (포트폴리오)
    "035720.KS",  # 카카오 (포트폴리오)
    "035420.KS",  # 네이버 (포트폴리오)
    "005930.KS",  # 삼성전자
    "000660.KS",  # SK하이닉스
    "012450.KS",  # 한화에어로스페이스
    "068270.KS",  # 셀트리온
    "042700.KS",  # 한미반도체
]


# ── 환율 조회 ─────────────────────────────────────────────────────────────────
def get_exchange_rate():
    print("  환율(KRW/USD) 조회 중...", flush=True)
    try:
        krw = yf.Ticker("KRW=X")
        rate = krw.fast_info.last_price
        if rate and rate > 0:
            print(f"  환율: {rate:.1f}원/달러", flush=True)
            return round(rate, 1)
    except Exception as e:
        print(f"  환율 조회 실패: {e}", flush=True)
    print("  환율 폴백 적용: 1,380원/달러", flush=True)
    return 1380.0


# ── 기술적 지표 계산 ──────────────────────────────────────────────────────────
def _calc_rsi(close, period=14):
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 1)


def _calc_macd(close):
    if len(close) < 26:
        return None, None
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return round(macd.iloc[-1], 4), round(signal.iloc[-1], 4)


def _calc_ma(close, period):
    if len(close) >= period:
        return round(close.rolling(period).mean().iloc[-1], 2)
    return None


def _calc_volume_ratio(volume):
    if len(volume) < 20:
        return None
    avg5 = volume.iloc[-5:].mean()
    avg20 = volume.iloc[-20:].mean()
    if avg20 == 0:
        return None
    return round(avg5 / avg20, 2)


# ── 현재가 수집 ───────────────────────────────────────────────────────────────
def _get_current_price(stock, info, hist, ticker):
    is_kr = ticker.endswith(".KS") or ticker.endswith(".KQ")

    try:
        price = stock.fast_info.last_price
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: fast_info", flush=True)
            return price
    except Exception:
        pass

    for key in ("currentPrice", "regularMarketPrice"):
        price = info.get(key)
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: info[{key}]", flush=True)
            return price

    if is_kr:
        price = info.get("regularMarketPreviousClose")
        if price and price > 0:
            print(f"    [{ticker}] 현재가 출처: regularMarketPreviousClose (KR 폴백)", flush=True)
            return price

    if not hist.empty:
        print(f"    [{ticker}] 현재가 출처: hist 폴백", flush=True)
        return hist["Close"].iloc[-1]

    return None


# ── 주식 데이터 수집 ──────────────────────────────────────────────────────────
def get_stock_data(tickers):
    results = {}
    for ticker in tickers:
        print(f"    [{ticker}] 데이터 수집 중...", flush=True)
        try:
            stock = yf.Ticker(ticker)

            print(f"    [{ticker}] info 요청 중...", flush=True)
            info = stock.info

            print(f"    [{ticker}] 히스토리 요청 중...", flush=True)
            hist = stock.history(period="1y", auto_adjust=True)

            current_price = _get_current_price(stock, info, hist, ticker)
            if current_price is None:
                print(f"    [{ticker}] 현재가 수집 실패, 건너뜀", flush=True)
                continue

            # NaN 제거 + float 변환 → rolling/ewm 계산 안정화
            if not hist.empty:
                close  = hist["Close"].astype(float).dropna()
                volume = hist["Volume"].astype(float).fillna(0)
            else:
                close = volume = None

            print(f"    [{ticker}] 데이터 포인트: {len(close) if close is not None else 0}일", flush=True)

            high_52w = close.max() if close is not None else info.get("fiftyTwoWeekHigh", "N/A")
            low_52w  = close.min() if close is not None else info.get("fiftyTwoWeekLow",  "N/A")

            def period_return(days):
                if close is not None and len(close) >= days:
                    return (current_price / close.iloc[-days] - 1) * 100
                return None

            # 기술적 지표 계산
            ma5   = _calc_ma(close, 5)   if close is not None else None
            ma20  = _calc_ma(close, 20)  if close is not None else None
            ma60  = _calc_ma(close, 60)  if close is not None else None
            ma120 = _calc_ma(close, 120) if close is not None else None

            aligned = (
                ma5 is not None and ma20 is not None and
                ma60 is not None and ma120 is not None and
                ma5 > ma20 > ma60 > ma120
            )
            golden_cross = (ma5 is not None and ma20 is not None and ma5 > ma20)

            rsi = _calc_rsi(close) if close is not None else None
            macd_val, macd_signal = _calc_macd(close) if close is not None else (None, None)
            macd_golden = (
                macd_val is not None and macd_signal is not None and
                macd_val > macd_signal
            )

            vol_ratio = _calc_volume_ratio(volume) if volume is not None else None

            results[ticker] = {
                "현재가": round(current_price, 2),
                "52주_고": round(high_52w, 2) if isinstance(high_52w, (int, float)) else high_52w,
                "52주_저": round(low_52w,  2) if isinstance(low_52w,  (int, float)) else low_52w,
                "1개월_수익률": round(period_return(21),  2) if period_return(21)  is not None else "N/A",
                "3개월_수익률": round(period_return(63),  2) if period_return(63)  is not None else "N/A",
                "6개월_수익률": round(period_return(126), 2) if period_return(126) is not None else "N/A",
                "1년_수익률":   round(period_return(252), 2) if period_return(252) is not None else "N/A",
                "PER": info.get("trailingPE", "N/A"),
                "PBR": info.get("priceToBook", "N/A"),
                "시가총액": info.get("marketCap", "N/A"),
                "매출성장률": info.get("revenueGrowth", "N/A"),
                "영업이익률": info.get("operatingMargins", "N/A"),
                "ROE": info.get("returnOnEquity", "N/A"),
                # 기술적 지표
                "MA5": ma5,
                "MA20": ma20,
                "MA60": ma60,
                "MA120": ma120,
                "정배열": aligned,
                "골든크로스(MA5>MA20)": golden_cross,
                "RSI14": rsi,
                "MACD": macd_val,
                "MACD_시그널": macd_signal,
                "MACD_골든크로스": macd_golden,
                "거래량비율(5일/20일)": vol_ratio,
            }
            print(f"    [{ticker}] 완료 — 현재가: {round(current_price, 2)} / RSI: {rsi} / 정배열: {aligned}", flush=True)
        except Exception as e:
            print(f"    [{ticker}] 실패: {e}", flush=True)

    return results


# ── 보고서 생성 ───────────────────────────────────────────────────────────────
def generate_report(us_data, kr_data, exchange_rate):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%Y년 %m월 %d일")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    static_system = """너는 500만원에서 시작해 자산 10억 이상을 달성한 전문 퀀트 트레이더이자 포트폴리오 매니저다.
목표는 단 하나 — 사용자가 최대한 많은 돈을 버는 것.
감정 없이 냉정하게, 데이터와 확률 기반으로 분석하라.
단, 손실 = 손절이 아님을 명심하라. 근거가 살아있으면 홀딩, 근거가 무너지면 손절.

[내 포트폴리오 현황]
※ 카테고리 구분
- 장기 적립식 (매일 자동 매수, 절대 단기 매도 금지): GOOGL, FCX, VOO
- 500만원 1억 프로젝트 종목: 셀트리온제약, 카카오, 네이버, BEAM, NVDA, PLTR, PWFL
- 500만원 1억 프로젝트 시드: 현금 500만원 (별도 분리 운용)

국내:
- 셀트리온제약: 평단 68,287원 / 398주 / 현재가 51,900원
- 카카오: 평단 36,151원 / 104주 / 현재가 42,000원
- 네이버: 평단 214,000원 / 50주 / 현재가 203,000원

미국:
- 빔 테라퓨틱스(BEAM): 평단 $32.49 / 100주 / 현재가 $28.64
- 엔비디아(NVDA): 평단 $178.84 / 10주 / 현재가 $215.00
- 팔란티어(PLTR): 평단 $142.36 / 11.64주 / 현재가 $136.88
- 파워플리트(PWFL): 평단 $3.68 / 1200주 / 현재가 $3.41
- 알파벳A(GOOGL): 평단 $325.10 / 2.77주 / 현재가 $382.97 (매일 $25 적립 중)
- 프리포트맥모란(FCX): 평단 $61.53 / 12.84주 / 현재가 $61.99 (매일 $10 적립 중)
- VOO: 평단 $621.68 / 3.39주 / 현재가 $685.55 (매일 $20 적립 중)

보유 현금: 13,585,062원 + $3,566
500만원 1억 프로젝트 전용 시드: 500만원 (위 현금에서 별도 분리 운용)

아래 형식으로 보고서를 작성하라. 반드시 HTML로 작성하라.

<h2>📊 오늘의 시장 총평</h2>
미국/국내 시장 전체 분위기, 주요 이슈, 섹터 흐름 3~5줄.
오늘 주목해야 할 핵심 변수 1가지를 굵게 강조하라.

<h2>💼 내 포트폴리오 진단</h2>
각 보유 종목을 표로 작성:
- 종목명 / 평단 / 현재가 / 수익률(%) / 평가손익 / RSI / MA정배열 여부
- 판단: 홀딩 / 추가매수 / 비중축소 / 손절 중 하나
- 판단 기준: 반드시 "근거가 살아있는가"로 판단하라.
  * 손실 중이어도 성장 스토리, 기술적 지지, 실적 모멘텀이 유효하면 홀딩 or 추가매수
  * 손절은 오직 "투자 근거 자체가 무너진 경우"에만 권고
  * SK하이닉스가 -50%여도 HBM 스토리가 살아있으면 홀딩이 정답이었던 것처럼
- 손절 트리거 (가격 기준이 아닌 근거 기반): "이 조건이 깨지면 손절"
- 목표가: 단기(3개월) / 중기(1년) / 장기(3년) 구분
전체 포트폴리오 총 평가금액 (실시간 환율 적용), 총 손익, 현금 포함 총자산.

PWFL(파워플리트)의 경우:
- "AI 텔레매틱스 스토리"를 근거로 홀딩을 권고하려면
  반드시 수집된 실제 데이터(매출성장률, 영업이익률, RSI, MA배열)로
  근거를 뒷받침해야 한다.
- 실제 데이터상 모멘텀이 없으면 기회비용을 명시하고
  더 나은 종목으로의 교체를 적극 검토하라.
- 감정적 홀딩(손실이 확정되기 싫어서 버티는 것)과
  근거 기반 홀딩을 명확히 구분하라.

<h2>🔍 기술적 분석 기반 추천 종목 (단기 스윙)</h2>
수집된 실제 데이터 기반으로 기술적으로 매력적인 종목 3개.
각 종목마다 반드시 수집된 실제 수치를 사용해서 작성하라:
- 이동평균선 배열 (MA5/MA20/MA60/MA120 실제 값 명시, 정배열 여부)
- 거래량 비율 (실제 계산된 값: 최근5일/20일평균)
- RSI 실제 값 및 해석 (70 이상=과매수, 30 이하=과매도)
- MACD 골든크로스/데드크로스 여부 (실제 계산값)
- 주요 지지/저항 구간
- 왜 지금인가 (위 데이터 종합 결론)
- 매수가 / 1차 목표가 / 2차 목표가 / 손절 트리거 (근거 기반)
- 예상 수익률 및 기간

<h2>📰 뉴스+기업분석 기반 추천 종목 (중장기 성장주 발굴)</h2>
반드시 아직 시장에 덜 알려진 소형~중형주를 발굴하라.
엔비디아, SK하이닉스처럼 초기 저평가됐지만 폭발적 성장한 유형이 목표.
AI/반도체/바이오/에너지전환/방산/양자컴퓨팅/UAM 등 메가트렌드 수혜주 우선.
삼성전자, 애플, 구글, MS 같은 대형주는 이 섹션에 넣지 마라.
각 종목마다:
- 왜 지금 저평가인가 (구체적 수치: 시총, PSR, PER 등)
- 핵심 성장 스토리 (기술적 우위, 시장 독점, 규제 수혜 등)
- 트리거: 언제 주가가 움직이는가 (구체적 이벤트)
- 목표가 (6개월 / 1년 / 3년)
- 손절 트리거 (근거 기반)
- 리스크 요인

<h2>🚀 500만원 → 1억 만들기 프로젝트</h2>
목표: 시드 500만원으로 1억 달성 (수익률 1,900%).
전략: 6개월 단위 수익률 100% 달성 → 복리로 1억.
1단계(~6개월): 500만원 → 1,000만원
2단계(~12개월): 1,000만원 → 2,000만원
3단계(~18개월): 2,000만원 → 4,000만원
4단계(~24개월): 4,000만원 → 1억

운용 원칙:
- 시드 500만원은 다른 포트폴리오와 완전 분리 운용
- 확신도에 따라 비중 유동 배분
  → 멀티배거 가능 종목: 시드의 50~80%까지 집중 가능
  → 확신 낮은 종목: 10~20% 소액 분산
- 손절은 "근거 붕괴 시"에만. 단순 하락은 손절 이유가 아님
- 6개월 내 2배가 목표. 이를 위한 리스크는 감수
- 멀티배거 후보 발견 시 집중 투자 전략
- 시드 전액 손실만 방어

전문 퀀트 트레이더 관점에서:
- 현재 단계 진단 및 이번 달 전략
- 지금 당장 500만원으로 공략할 종목 3개
  (6개월~1년 내 2~10배 가능 종목 발굴, 수집 데이터 종목 우선)
  각 종목: 진입가 / 6개월 목표가 / 1년 목표가 / 손절 트리거(근거 기반) / 투입 비중 / 핵심 근거
- 기존 프로젝트 종목 진단 (셀트리온제약/카카오/네이버/BEAM/NVDA/PLTR/PWFL):
  * 손실 중이어도 근거가 살아있으면 홀딩 또는 추가매수 권고 가능
  * 근거가 무너진 종목만 손절 or 갈아타기 권고
  * 각 종목별 "이 근거가 무너지면 손절" 조건 명시
- 리스크 시나리오: 시드 반토막 시 대응 전략

<h2>⭐ 종합 추천 TOP 3</h2>
기술적 + 펀더멘털 모두 좋은 종목 3개.
멀티배거 가능 종목은 반드시 🔥 표시.
단기/중기/장기 구분, 목표가 3단계, 손절 트리거 명시.

<h2>⚡ 단타 추천 (오늘~1주일)</h2>
수집된 실제 기술적 데이터 기반 단기 종목 2~3개.
진입가 / 목표가 / 손절 트리거 / 예상 수익률 명시.

<h2>🚫 지금 피해야 할 것들</h2>
위험 종목, 과열 섹터, 주의 이슈. 이유 포함.

<h2>💡 오늘의 액션 플랜</h2>
지금 당장 해야 할 행동 3~5가지. 구체적 가격과 수량 포함.

<h2>📅 이번 주 핵심 이벤트</h2>
주가에 영향을 줄 이벤트. 날짜 / 이벤트 / 예상 영향 포함.

종목 추천 시 목표주가 설정 원칙:
- 획일적 -10%손절/+20%익절 금지
- 단기 스윙: 기술적 저항선 기반 목표가
- 중장기 성장주: 6개월/1년/3년 목표가 각각 제시
- 🔥 멀티배거 후보: 매수 후 보유 전략, 중간 익절 구간, 최종 목표가 상세 제시
- 손절 트리거는 반드시 "근거 붕괴 조건"으로 명시 (단순 % 하락 금지)
- 구조적 성장 종목은 손절가 타이트하게, 목표가는 과감하게

냉정하고 솔직하게 작성하라. 손실 중인 종목도 근거가 있으면 당당히 홀딩을 권고하라.
멀티배거 가능성이 있으면 근거와 함께 과감히 추천하라.
수집된 실제 데이터를 최대한 활용하고, 추정치를 사용할 경우 반드시 "(추정)"으로 표시하라.

뉴스, 기업 이벤트, 계약 내용 등 실시간 확인이 불가한 정보를 사용할 경우
반드시 해당 내용 뒤에 "(※ AI 학습 데이터 기반, 최신 뉴스 직접 확인 필요)"
라고 표시하라. 수집된 실제 데이터(주가, RSI, MA, 거래량 등)에는 이 표시를
붙이지 않는다."""

    print("  [Claude API] 스트리밍 요청 시작...", flush=True)
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": static_system,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=[{
            "role": "user",
            "content": (
                f"오늘 날짜: {today}\n"
                f"데이터 기준: {generated_at} (한국 주식은 전일 종가 기준)\n"
                f"실시간 환율: {exchange_rate}원/달러\n\n"
                f"[수집된 시장 데이터]\n"
                f"미국 주식: {us_data}\n\n"
                f"국내 주식: {kr_data}"
            )
        }]
    ) as stream:
        for event in stream:
            event_type = type(event).__name__
            if event_type == "RawContentBlockStartEvent":
                block_type = getattr(getattr(event, "content_block", None), "type", "?")
                print(f"  [Claude API] 블록 시작: {block_type}", flush=True)
            elif event_type == "RawMessageDeltaEvent":
                usage = getattr(event, "usage", None)
                if usage:
                    print(f"  [Claude API] 출력 토큰: {usage.output_tokens}", flush=True)
        final = stream.get_final_message()

    print("  [Claude API] 응답 완료", flush=True)
    for block in final.content:
        if block.type == "text":
            return block.text

    return ""


# ── 이메일 발송 ───────────────────────────────────────────────────────────────
def send_email(report_content):
    today = datetime.now().strftime("%Y년 %m월 %d일")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 투자 분석 보고서 — {today}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    html_body = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; color: #333; }}
            h1, h2, h3 {{ color: #1a1a2e; }}
            .container {{ max-width: 860px; margin: 0 auto; padding: 20px; }}
            .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e);
                       color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
            .data-time {{ font-size: 12px; color: #ccc; margin-top: 6px; }}
            .footer {{ color: #888; font-size: 12px; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📈 일일 투자 분석 보고서</h1>
                <p>{today}</p>
                <p class="data-time">데이터 기준: {generated_at} (한국 주식은 전일 종가 기준)</p>
            </div>
            {report_content}
            <div class="footer">
                <p>본 보고서는 AI 분석 시스템이 자동 생성한 참고 자료입니다. 투자 결정은 본인의 판단과 책임 하에 이루어져야 합니다.</p>
            </div>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_body, "html"))

    print("  [이메일] smtp.gmail.com:465 연결 중...", flush=True)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        print("  [이메일] 로그인 중...", flush=True)
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        print("  [이메일] 발송 중...", flush=True)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())

    print(f"  [이메일] 발송 완료 → {RECIPIENT_EMAIL}", flush=True)


# ── 메인 실행 ─────────────────────────────────────────────────────────────────
def run_daily_report():
    print(f"\n{'='*50}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 보고서 생성 시작", flush=True)
    print(f"{'='*50}", flush=True)

    print(f"\n[0/4] 환율 조회", flush=True)
    exchange_rate = get_exchange_rate()

    print(f"\n[1/4] 미국 주식 데이터 수집 ({len(US_TICKERS)}개 종목)", flush=True)
    us_data = get_stock_data(US_TICKERS)
    print(f"  → 수집 완료: {list(us_data.keys())}", flush=True)

    print(f"\n[2/4] 국내 주식 데이터 수집 ({len(KR_TICKERS)}개 종목)", flush=True)
    kr_data = get_stock_data(KR_TICKERS)
    print(f"  → 수집 완료: {list(kr_data.keys())}", flush=True)

    print(f"\n[3/4] AI 분석 보고서 생성 (Claude Opus 4.7)", flush=True)
    report = generate_report(us_data, kr_data, exchange_rate)
    print(f"  → 보고서 생성 완료 ({len(report)}자)", flush=True)

    print(f"\n[4/4] 이메일 발송", flush=True)
    send_email(report)

    print(f"\n{'='*50}", flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 전체 완료", flush=True)
    print(f"{'='*50}\n", flush=True)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        # GitHub Actions: 한 번만 실행 후 종료
        run_daily_report()
    else:
        # 로컬: 즉시 1회 실행 후 매일 07:00 자동 실행
        run_daily_report()
        schedule.every().day.at("07:00").do(run_daily_report)
        print("스케줄러 시작 — 매일 07:00 자동 실행")
        while True:
            schedule.run_pending()
            time.sleep(60)
