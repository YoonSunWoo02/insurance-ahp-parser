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
import re
import sys

import pdfplumber
from openai import OpenAI

from parser.amount_structurer import structure_listed_amount

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
    """첫 몇 페이지에 "가입제안서"/"가입설계서" 문구, 또는 "상품설명서(제안용)"류
    표지 문구가 있으면 담보별 보험료 표 형식 문서로 본다.

    "상품설명서(제안용)"은 가입제안서와 이름은 다르지만 [계약사항] 표에 동일하게
    구분/보험가입금액/보험기간/납입기간/보험료 컬럼이 나오므로 같은 경로로 처리한다.
    """
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:check_pages]:
            text = page.extract_text() or ""
            if "가입제안서" in text or "가입설계서" in text:
                return True
            if "상품설명서" in text and "제안용" in text:
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
   - total_premium: 1회(보통 매월) 납입하는 보험료 합계 (예: "합계보험료
     171,492원", "납입보험료 85,286원", "1회 보험료 129,507원(매월)").
     "총납입보험료"처럼 납입기간 전체를 누적한 금액(예: 18,597,840원)은
     절대 쓰지 마라 — 담보별 보험료를 모두 더한 값과 비슷해야 맞는 값이다.
   - payment_period: 납입기간 (예: "20년납", "30년납". 담보마다 다르면
     가장 많이 나오는(기본계약 기준) 값을 대표값으로 쓴다)
   - maturity: 보험기간/만기 (예: "100세만기", "20년만기")
   - payment_cycle: 납입주기 (예: "매월", "월납". 안 보이면 null)
   - insurer: 보험회사명 (설계사 소속 대리점·GA 이름이 아니라 상품을 판매하는
     보험회사. 안 보이면 null)
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


# ---------------------------------------------------------------------------
# 가입화면(_screen.json)과 같은 팀 공통 양식으로 변환
# ---------------------------------------------------------------------------

_KRW_UNITS = {"억": 10**8, "천만": 10**7, "백만": 10**6, "십만": 10**5, "만": 10**4, "천": 1000, "백": 100, "십": 10}
_KRW_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(억|천만|백만|십만|만|천|백|십)?")

# 제안서에 약칭으로만 찍혀 모호한 보험사명 → 정식 명칭.
_INSURER_ALIASES = {"NH": "NH농협손해보험"}


def parse_krw(raw) -> int | None:
    """"2만2천원", "1억원", "5,000만원", "1,700"(원 생략) 같은 금액을 원 단위 정수로.
    전체 문자열이 금액 표기로만 이뤄져 있지 않으면 None."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace(" ", "").strip()
    if s.endswith("원"):
        s = s[:-1]
    if not s:
        return None
    pos, total = 0, 0.0
    for m in _KRW_TOKEN.finditer(s):
        if m.start() != pos:
            return None
        total += float(m.group(1)) * _KRW_UNITS.get(m.group(2) or "", 1)
        pos = m.end()
    return int(round(total)) if pos == len(s) else None


# 보험사마다 표기가 다르다: "나이 31세"(삼성화재), "나이:31세"(흥국), "(31세, 여)"(삼성),
# "(여자,31세)"/"피보험자:31세여"(농협). "보험나이 15세미만 피보험자의 경우…",
# "[보험나이 0세]" 같은 약관 안내 문구를 잡지 않도록 "보험나이"와 "N세 미만/이상"은 뺀다.
_AGE_PATTERNS = [
    re.compile(r"(?<!보험)나이\s*:?\s*(\d{1,3})\s*세(?!\s*(?:미만|이상|이하|초과|\]))"),
    re.compile(r"\(\s*(\d{1,3})\s*세\s*,\s*(?:여|남)"),
    re.compile(r"\((?:여자|남자)\s*,\s*(\d{1,3})\s*세\)"),
    re.compile(r"피보험자\s*:\s*(\d{1,3})\s*세\s*(?:여|남)"),
]


def find_insured_age(pdf_path: str, check_pages: int = 20) -> int | None:
    """제안서에서 피보험자 나이 표기를 찾는다(표기가 없으면 None — 주민번호로 계산하지 않는다)."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:check_pages])
    for pat in _AGE_PATTERNS:
        m = pat.search(text)
        if m:
            return int(m.group(1))
    return None


# 성별: 글자 표기("성별:여자", "(여자,31세)", "(31세, 여)", "31세여") 우선, 없으면
# 주민번호 뒷자리 첫 숫자(1·3 남, 2·4 여). "40세 남성" 같은 예시 문구는 뒤에 한글이
# 이어지므로 걸리지 않는다.
_GENDER_PATTERNS = [
    re.compile(r"성별\s*:?\s*(여|남)"),
    re.compile(r"\((여|남)자\s*,"),
    re.compile(r"\d{1,3}\s*세\s*,?\s*(여|남)(?![가-힣])"),
]
_RRN_PATTERN = re.compile(r"\d{6}\s*-\s*([1-4])")

# 직업: 보험사별 표기 — "직업코드 회사 사무직종사자 상해급수"(삼성화재),
# "직업 회사 사무직 종사자 급수"(농협), 직업명 컬럼 표(DB).
_JOB_PATTERNS = [
    re.compile(r"직업코드\s+(.+?)\s+상해급수"),
    re.compile(r"직업\s+(.+?)\s+급수"),
    re.compile(r"직업명[^\n]*\n[^\n]*?\d{5}\s+(.+?)\s+(?:자가용|영업용|비운전|운전안함|\d)"),
]


def _proposal_text(pdf_path: str, check_pages: int = 20) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages[:check_pages])


def find_insured_gender(pdf_path: str) -> str | None:
    """제안서에 적힌 피보험자 성별("남"/"여"). 표기가 전혀 없으면 None."""
    text = _proposal_text(pdf_path)
    for pat in _GENDER_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    m = _RRN_PATTERN.search(text)
    if m:
        return "남" if m.group(1) in "13" else "여"
    return None


def find_insured_job(pdf_path: str) -> str | None:
    """제안서에 적힌 피보험자 직업명. 표기가 없으면 None."""
    text = _proposal_text(pdf_path)
    for pat in _JOB_PATTERNS:
        m = pat.search(text)
        if m:
            job = re.sub(r"\s+", " ", m.group(1)).strip()
            return re.sub(r"(\S)종사자$", r"\1 종사자", job)
    return None


def _normalize_cycle(cycle):
    if not cycle:
        return None
    c = str(cycle)
    if "월" in c:
        return "월납"
    if "연" in c or "년" in c:
        return "연납"
    return c


def _coverage_category(name):
    m = re.match(r"^\(([^)]+)\)", name or "")
    return m.group(1) if m else None


def to_unified_format(
    raw: dict,
    source: str,
    age: int | None,
    gender: str | None = None,
    job: str | None = None,
) -> dict:
    """extract_premium_info 결과를 가입화면과 같은 공통 양식으로 바꾼다.

    팀 규칙: 문서에 성별·직업 정보가 있으면 그 값, 없으면 "상관없음".
    가입금액은 가입화면과 같은 규칙(총한도는 annual_limit, 일당·통원비 등은 per_time)으로 넣는다.
    """
    s = raw.get("contract_summary") or {}
    insurer = s.get("insurer")
    total_raw = s.get("total_premium")
    summary = {
        "insurer": _INSURER_ALIASES.get(insurer, insurer),
        "product_name": s.get("product_name"),
        "product_case": s.get("product_name"),
        "job": job or "상관없음",
        "gender": gender or "상관없음",
        "age": age,
        "age_raw": f"{age}세" if age is not None else None,
        "payment_cycle": _normalize_cycle(s.get("payment_cycle")),
        "medical_aid": None,
        "total_premium": parse_krw(total_raw),
        "total_premium_raw": total_raw,
        "renewal": None,
        "maturity": s.get("maturity"),
        "payment_period": s.get("payment_period"),
    }
    coverages = []
    for c in raw.get("coverages") or []:
        amount_raw = c.get("subscribed_amount")
        coverages.append({
            "coverage_name": c.get("coverage_name"),
            "subscribed_amount": structure_listed_amount(
                c.get("coverage_name"), amount_raw, parse_krw(amount_raw)
            ),
            "premium": parse_krw(c.get("premium")),
            "is_enrolled": True,
            "coverage_category": _coverage_category(c.get("coverage_name")),
            "payment_period": c.get("payment_period"),
        })
    return {
        "source": source,
        "document_type": "가입제안서",
        "contract_summary": summary,
        "coverages": coverages,
        "error": raw.get("error"),
    }


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
