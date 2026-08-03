"""가입제안서(가입설계서) PDF에서 보험료·납입기간 정보를 추출하는 모듈.

"약관"은 "제N조(제목)" 형식의 조항으로 쪼개 파싱하지만, 가입제안서는 조항 구조가
아니라 "담보명 | 가입금액 | 보험료 | 납입기간" 같은 표 형식이라 완전히 다른 방식이
필요하다. 표지~앞부분 몇 페이지에 담보별 보험료 표가 나오고, 뒤쪽은 유의사항/약관
요약 등 표와 무관한 내용이 대부분이라 "보험료"/"가입금액"/"납입기간" 키워드가
있는 페이지만 모아 GPT에 넘긴다.

메인 함수: extract_premium_info(pdf_path)
"""

import json
import os
import sys

import pdfplumber
from openai import OpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 가입제안서는 표가 많아 페이지당 텍스트가 크고, 응답도 담보 수만큼 길어질 수 있어
# 약관 조항 분석(30초)보다 넉넉하게 잡는다.
REQUEST_TIMEOUT = 60

# 담보별 보험료 표는 보통 표지~앞부분 10여 페이지 안에 나온다. 뒤쪽(유의사항,
# 약관 요약, 해약환급금 예시표 등)까지 다 넘기면 토큰만 늘고 표 인식은 오히려
# 방해받으므로, 키워드가 있는 페이지만 골라 앞에서부터 최대 이 개수만큼 쓴다.
MAX_RELEVANT_PAGES = 12

_PAGE_SIGNAL_KEYWORDS = ("보험료", "가입금액", "납입기간", "담보")


def is_proposal_pdf(pdf_path: str, check_pages: int = 3) -> bool:
    """첫 몇 페이지에 "가입제안서"/"가입설계서" 문구가 있으면 가입제안서 문서로 본다."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:check_pages]:
            text = page.extract_text() or ""
            if "가입제안서" in text or "가입설계서" in text:
                return True
    return False


def _extract_premium_text(pdf_path: str) -> str:
    """보험료 표가 있을 가능성이 높은 페이지의 텍스트만 모아 반환한다."""
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if any(kw in text for kw in _PAGE_SIGNAL_KEYWORDS):
                pages.append(text)
            if len(pages) >= MAX_RELEVANT_PAGES:
                break
    return "\n\n".join(pages)


SYSTEM_PROMPT = """너는 한국 보험 가입제안서(가입설계서) 분석 전문가다.
표 형식으로 나열된 담보별 보험료와 계약 전체의 납입 조건을 정확히 추출해
JSON으로만 응답한다. 원문에 없는 내용은 절대 지어내지 말고, 숫자는 원문 표기를
그대로 보존한다(예: "31,550원", "1,000만원", "20년납")."""

USER_TEMPLATE = """다음은 보험 가입제안서에서 보험료/가입금액/납입기간이 나온
페이지들의 원문이다. 아래 두 가지를 추출하라.

1) contract_summary — 계약 전체 수준의 납입 정보:
   - total_premium: 전체 보험료 합계 (예: "보장보험료 합계 129,507원",
     "납입보험료 85,286원", "1회 보험료 129,507원(매월)" 등에서 찾는다.
     여러 표기가 있으면 "총납입보험료"나 "보장보험료 합계"처럼 전체를
     대표하는 값을 우선한다)
   - payment_period: 납입기간 (예: "20년납", "30년납". 담보마다 다르면
     가장 많이 나오는(기본계약 기준) 값을 대표값으로 쓴다)
   - maturity: 보험기간/만기 (예: "100세만기", "20년만기")
   - payment_cycle: 납입주기 (예: "매월", "월납". 안 보이면 null)
   - insurer: 보험회사명 (안 보이면 null)
   - product_name: 상품명 (안 보이면 null)

2) coverages — 담보(보장)별 보험료 내역. 표의 각 행을 하나씩:
   - coverage_name: 담보명 (원문 그대로, 예: "[건강]암 진단비(유사암 제외)")
   - subscribed_amount: 가입금액 (예: "5,000만원". 안 보이면 null)
   - premium: 그 담보의 보험료 (예: "31,550원". 반드시 숫자만, 서술문장 금지.
     안 보이면 null)
   - payment_period: 그 담보의 납입기간 (기본계약 전체와 다를 수 있음, 예:
     "30년납". 안 보이면 null)

표에 없는 담보를 만들어내지 마라. 같은 담보가 여러 줄에 걸쳐 나오면(예: 하나의
가입금액 아래 여러 세부 진단명이 나열된 경우) 각 세부 항목을 개별 coverage로
분리해라.

[원문]
{proposal_text}

[출력 JSON 스키마]
{{
  "contract_summary": {{
    "insurer": "...",
    "product_name": "...",
    "total_premium": "... 또는 null",
    "payment_period": "... 또는 null",
    "maturity": "... 또는 null",
    "payment_cycle": "... 또는 null"
  }},
  "coverages": [
    {{
      "coverage_name": "...",
      "subscribed_amount": "... 또는 null",
      "premium": "... 또는 null",
      "payment_period": "... 또는 null"
    }}
  ]
}}

반드시 위 스키마의 JSON만 출력하라."""


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY 환경변수가 설정되어 있지 않습니다. "
            ".env 파일 또는 환경변수로 설정하세요."
        )
    return OpenAI(api_key=api_key)


def extract_premium_info(pdf_path: str, client: OpenAI | None = None) -> dict:
    """가입제안서 PDF에서 보험료/납입기간 정보를 추출한다.

    반환: {"contract_summary": {...}, "coverages": [...], "error": str | None}
    """
    client = client or _get_client()
    text = _extract_premium_text(pdf_path)
    if not text.strip():
        return {
            "contract_summary": {},
            "coverages": [],
            "error": "보험료/가입금액/납입기간 관련 페이지를 찾지 못함",
        }

    user_msg = USER_TEMPLATE.format(proposal_text=text)
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            timeout=REQUEST_TIMEOUT,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        return {
            "contract_summary": data.get("contract_summary", {}) or {},
            "coverages": data.get("coverages", []) or [],
            "error": None,
        }
    except Exception as e:  # API 오류/JSON 파싱 오류 모두 포착
        print(f"      → 보험료 추출 실패 (오류: {e})", file=sys.stderr)
        return {"contract_summary": {}, "coverages": [], "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python -m parser.proposal_extractor <가입제안서_pdf_경로>")
        sys.exit(1)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    path = sys.argv[1]
    print(f"가입제안서 여부: {is_proposal_pdf(path)}", file=sys.stderr)
    result = extract_premium_info(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
