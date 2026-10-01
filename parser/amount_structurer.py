"""보장 금액(amount)을 단일 문자열에서 종류별 구조로 변환하는 모듈.

기존 파이프라인은 GPT가 뽑은 금액을 그대로 문자열(amount)로 저장했다. 그런데
실손 약관의 "금액"은 실제로는 성격이 다른 여러 개념이 뒤섞여 있다:
  - 회당 지급 한도 (per_time)   예) "통원 1회당 20만원"
  - 연간/총 한도 (annual_limit)  예) "입원과 통원 합산 5천만원"
  - 공제금액 (deductible, 지급액이 아니라 보상액에서 "빼는" 돈)
                                 예) "통원항목별 공제금액 5만원"
  - 방문·일수 횟수 한도 (visit_limit, 금액이 아님)
                                 예) "연간 방문 180회 한도"
같은 amount 필드에 이 넷이 섞이면 가성비 계산(회당 지급액 대비 공제금액 등)이
뒤집힐 수 있다. verify_all()+postprocess()로 문자열 amount 신뢰도 검증/중복
제거가 끝난 뒤, 마지막 단계로 이 모듈이 문자열을 구조로 바꾼다.

판단 기준은 실제 파싱된 실손 약관들의 통원 담보 62건 amount/payment_condition/
source_quote 표기 패턴을 직접 조사해 정했다. 공제금액 판정은 coverage_name 또는
본문에 "공제금액"이라는 단어가 명시된 경우로 좁혔다 — "본인부담금"/"뺀 금액" 같은
표현은 실손 조항 거의 전부에 나와서(정상 지급 한도 설명에도 쓰임) 이걸 기준으로
삼으면 정상 지급액까지 공제금액으로 오분류된다. 애매하면(예: 조항 간 참조로 실제
숫자가 다른 조항에만 있는 경우) 억지로 채우지 않고 null로 둔다.
"""

import re

from validator.verifier import parse_won_amount

# 공제금액 — 실사례 전부 coverage_name 자체에 "공제금액"이 박혀 있다.
# (예: "상해통원의료비 공제금액", "통원당 공제금액")
_DEDUCTIBLE_NAME_PATTERN = re.compile(r"공제금액")

# 본문에 "공제금액"이라는 단어 없이 "3만원 공제(후)"처럼 금액 바로 뒤에 "공제"만
# 붙는 경우도 있다 (예: "통원당 3만원 공제 후 보상비율 50%"). 이 금액 토큰과
# amount 값이 같으면 공제금액으로 인정한다.
_DEDUCTIBLE_SUFFIX_PATTERN = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:억|천만|만)?\s*원\s*(?:을|를)?\s*공제"
)

# "1회당"/"회당"/"회(건)당" — 회당 지급 한도를 나타내는 표현.
_PER_TIME_PATTERN = re.compile(r"1?\s*회\s*\(?\s*건?\s*\)?\s*당")

# 다른 담보와 한도를 같이 쓴다는 표현. is_shared_limit 판정에 쓴다.
_SHARED_PATTERN = re.compile(r"합산|통합")

# 연간/총 한도를 나타내는 보조 신호. per_time 신호가 없을 때만 쓴다.
# "보험가입금액"/"가입금액"은 "이 금액이 곧 보장 전체의 한도"라는 뜻이라 안전한 신호다
# (예: "다만, 보험가입금액은 5천만원으로 합니다.").
_ANNUAL_PATTERN = re.compile(r"연간|한도|이내|가입금액")

# 방문·일수 횟수 한도 — "180회 한도", "방문 90회를 한도로", "100일을 한도로" 등.
_VISIT_COUNT_PATTERN = re.compile(r"(\d+)\s*회\s*(?:를|을)?\s*한도")
_VISIT_DAY_PATTERN = re.compile(r"(\d+)\s*일\s*(?:을|를)?\s*한도")

# 공제금액 여부 판정용: "공제금액" 주변(약 30자 이내)에서 원 단위 금액 표현을 찾는다.
_WON_TOKEN_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:억|천만|만)?\s*원")


def _has_deductible_signal(coverage_name: str, text: str, amount_value: int) -> bool:
    """coverage_name 또는 본문이 이 금액을 '공제금액'이라고 명시하는지."""
    if _DEDUCTIBLE_NAME_PATTERN.search(coverage_name or ""):
        return True
    for m in _DEDUCTIBLE_NAME_PATTERN.finditer(text or ""):
        window = text[max(0, m.start() - 10): m.end() + 30]
        for num_m in _WON_TOKEN_PATTERN.finditer(window):
            if parse_won_amount(num_m.group(0)) == amount_value:
                return True
    for m in _DEDUCTIBLE_SUFFIX_PATTERN.finditer(text or ""):
        if parse_won_amount(m.group(0)) == amount_value:
            return True
    return False


def _visit_limit(text: str) -> dict | None:
    m = _VISIT_COUNT_PATTERN.search(text or "")
    if m:
        return {"value": int(m.group(1)), "unit": "회"}
    m = _VISIT_DAY_PATTERN.search(text or "")
    if m:
        return {"value": int(m.group(1)), "unit": "일"}
    return None


def structure_amount(cov: dict) -> dict:
    """coverage dict 하나를 받아 구조화된 amount 객체를 반환한다 (cov는 변경 안 함)."""
    raw = cov.get("amount")
    name = cov.get("coverage_name") or ""
    text = " ".join(
        str(cov.get(k) or "") for k in ("payment_condition", "source_quote")
    )

    is_shared = bool(_SHARED_PATTERN.search(text))
    visit_limit = _visit_limit(text)

    amount_value = parse_won_amount(raw) if raw else None

    per_time = None
    annual_limit = None
    deductible = None

    if amount_value is not None:
        if _has_deductible_signal(name, text, amount_value):
            deductible = amount_value
        elif _PER_TIME_PATTERN.search(text):
            per_time = amount_value
        elif _ANNUAL_PATTERN.search(text):
            annual_limit = amount_value
        # 위 세 신호가 다 없으면 애매한 케이스 — 억지로 채우지 않고 raw만 남긴다.

    return {
        "per_time": per_time,
        "annual_limit": annual_limit,
        "deductible": deductible,
        "visit_limit": visit_limit,
        "is_shared_limit": is_shared,
        "raw": raw,
    }


def structure_all_amounts(results: list[dict]) -> int:
    """results 안의 모든 coverage의 amount를 구조화된 dict로 교체한다.

    postprocess()로 문자열 amount 정제(신뢰도 필터링/중복 제거)가 다 끝난 뒤
    마지막 단계로 호출해야 한다 — 구조화 후에는 postprocess.dedup_coverages의
    dict 키 비교가 깨진다(dict는 unhashable이라 그대로 넣으면 TypeError).
    처리한 coverage 건수를 반환한다.
    """
    count = 0
    for article in results:
        for cov in article.get("coverages", []):
            cov["amount"] = structure_amount(cov)
            count += 1
    return count


# ---------------------------------------------------------------------------
# 가입화면·가입제안서처럼 "담보명 | 가입금액" 표로 나오는 금액의 구조화.
# 가입금액은 보통 총한도(annual_limit)지만, 일당·통원비·내원처럼 1일/1회당 지급액인
# 담보는 per_time이 맞다(예: "암입원일당 2만원" = 입원 하루에 2만원). 담보명에 적힌
# "연간30회한도", "1일이상180일한도" 같은 횟수·일수 한도는 visit_limit으로 옮긴다.
# ---------------------------------------------------------------------------

_LISTED_PER_TIME_NAME = re.compile(r"일당|통원비|내원|입원비\s*\(|1회당|회당")
_LISTED_VISIT_COUNT = re.compile(r"연간\s*(\d+)\s*회\s*한도")
_LISTED_VISIT_DAYS = [re.compile(r"(\d+)\s*일\s*한도"), re.compile(r"\(\s*1일\s*-\s*(\d+)\s*일\s*\)")]


def structure_listed_amount(coverage_name: str | None, raw, value: int | None) -> dict:
    """표에 적힌 가입금액(raw, 원 단위 value)을 담보명 기준으로 구조화한다."""
    name = coverage_name or ""
    visit_limit = None
    m = _LISTED_VISIT_COUNT.search(name)
    if m:
        visit_limit = {"value": int(m.group(1)), "unit": "회"}
    else:
        for pat in _LISTED_VISIT_DAYS:
            m = pat.search(name)
            if m:
                visit_limit = {"value": int(m.group(1)), "unit": "일"}
                break
    per_time = value if _LISTED_PER_TIME_NAME.search(name) else None
    return {
        "per_time": per_time,
        "annual_limit": None if per_time is not None else value,
        "deductible": None,
        "visit_limit": visit_limit,
        "is_shared_limit": False,
        "raw": raw,
    }
