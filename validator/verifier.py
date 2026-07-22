"""원문 대조 검증 + 신뢰도 점수 모듈.

GPT가 추출한 보장 정보를, 조항 원문과 대조해
환각(hallucination) 여부를 점검하고 0~100 신뢰도 점수를 부여한다.

점수 구성 (총 100점):
  - 보장명 근거 존재     : 25점
  - 금액 원문 일치       : 30점
  - 지급조건 근거 존재   : 20점
  - source_quote 원문 포함: 25점
"""

import re

from parser.pdf_extractor import Article


def _normalize(text: str) -> str:
    """공백/특수문자 제거한 정규화 문자열 (느슨한 포함 비교용)."""
    return re.sub(r"\s+", "", text or "")


def _contains(haystack_norm: str, needle: str) -> bool:
    """정규화 기준으로 needle이 원문에 포함되는지."""
    n = _normalize(needle)
    return bool(n) and n in haystack_norm


# 원 단위 금액 표현을 찾는 범용 패턴 ("5,000만원", "5천만원", "1억원", "50,000,000원" 등).
_WON_AMOUNT_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:억|천만|만)?\s*원")


def parse_won_amount(amount_str) -> int | None:
    """금액 문자열을 원 단위 정수로 정규화한다.

    "5,000만원"과 "5천만원"은 표기만 다를 뿐 값은 둘 다 50,000,000원으로 같다.
    이 함수로 그 둘을 같은 정수로 정규화해 비교할 수 있게 한다.
    비율(%)이나 단순 숫자처럼 원 단위로 환산할 수 없는 값은 None을 반환한다.
    (upload_to_supabase.py의 DB 적재용 parse_amount와 같은 로직을 공유한다.)
    """
    if not amount_str or str(amount_str).strip().lower() == "null":
        return None
    s = str(amount_str).replace(",", "").replace(" ", "")

    match = re.search(r"(\d+(?:\.\d+)?)억원", s)
    if match:
        return int(float(match.group(1)) * 100_000_000)

    match = re.search(r"(\d+(?:\.\d+)?)천만원", s)
    if match:
        return int(float(match.group(1)) * 10_000_000)

    match = re.search(r"(\d+(?:\.\d+)?)만원", s)
    if match:
        return int(float(match.group(1)) * 10_000)

    match = re.search(r"(\d+)원", s)
    if match:
        return int(match.group(1))

    return None


def _amount_equivalent(gpt_amount, source_text: str) -> bool:
    """GPT가 낸 금액과 원 단위 값이 같은 금액 표현이 원문에 있는지 확인한다.

    "5,000만원"(GPT)과 "5천만원"(원문)처럼 표기가 달라도 같은 금액(50,000,000원)이면
    일치로 인정한다. GPT 금액이 원 단위로 환산되지 않는 값(비율 등)이면 항상 False.
    """
    target = parse_won_amount(gpt_amount)
    if target is None:
        return False
    return any(
        parse_won_amount(m.group(0)) == target
        for m in _WON_AMOUNT_PATTERN.finditer(source_text)
    )


# source_quote 어절 겹침 인정 임계값 (이 비율 이상 어절이 원문에 있으면 인정)
QUOTE_OVERLAP_THRESHOLD = 0.7

# GPT enum 필드별 허용값. 이 목록에 정확히 속하지 않는 값(예: GPT가 선택지 문자열
# "주계약 | 특약 | 불명"을 통째로 반환한 경우)은 "불명"으로 강제 치환하고 감점한다.
ALLOWED_ENUM_VALUES: dict[str, set[str]] = {
    "contract_type": {"주계약", "특약", "불명"},
    "coverage_type": {"입원", "통원", "수술", "불명"},
    "benefit_type": {"급여", "비급여", "급여+비급여", "불명"},
}

# 이 필드는 "입원 | 통원"처럼 여러 값을 조합해 반환하는 것이 정당하다.
# 단, GPT가 선택지 문자열 전체("입원 | 통원 | 수술 | 불명")를 반환한 버그와는
# 구분해야 한다 → 조합에 "불명"이 섞여 있으면(불명은 단독 전용) 버그로 보고 무효 처리.
PIPE_COMBO_FIELDS = {"coverage_type"}

# enum 필드 1개가 유효하지 않을 때마다 신뢰도에서 깎는 점수
ENUM_INVALID_PENALTY = 10


def _is_valid_enum(field: str, val, allowed: set[str]) -> bool:
    """enum 필드 값이 유효한지. coverage_type은 허용값 '|' 조합도 인정한다."""
    if not isinstance(val, str):
        return False
    v = val.strip()
    if v in allowed:
        return True
    # 조합 허용 필드: "입원 | 통원"처럼 모든 조각이 허용값(불명 제외)이면 인정.
    # 조합에 "불명"이 있으면 선택지 문자열 통째 반환(버그)이므로 인정하지 않는다.
    if field in PIPE_COMBO_FIELDS and "|" in v:
        parts = [p.strip() for p in v.split("|")]
        real_values = allowed - {"불명"}
        return len(parts) >= 2 and all(p in real_values for p in parts)
    return False


def _validate_enum_fields(coverage: dict) -> tuple[dict, dict, int]:
    """GPT enum 필드(contract_type/coverage_type/benefit_type)의 유효성을 검사한다.

    허용값에 정확히 속하지 않으면 해당 필드를 "불명"으로 치환한다.
    (coverage_type만 "입원 | 통원" 같은 허용값 조합을 예외로 인정한다.)
    반환: (치환된 필드 dict, {필드명+"_valid": bool} 검증결과, 총 감점).
    """
    corrected: dict[str, str] = {}
    checks: dict[str, bool] = {}
    penalty = 0
    for field, allowed in ALLOWED_ENUM_VALUES.items():
        valid = _is_valid_enum(field, coverage.get(field), allowed)
        checks[f"{field}_valid"] = valid
        if not valid:
            corrected[field] = "불명"
            penalty += ENUM_INVALID_PENALTY
    return corrected, checks, penalty


def _quote_in_source(haystack_norm: str, quote: str) -> bool:
    """source_quote가 원문에 있는지 느슨하게 판정한다.

    GPT가 원문을 글자 그대로 인용하지 않고 살짝 바꿔 쓰는 경우가 많아,
    '정확히 포함'뿐 아니라 '어절 70% 이상이 원문에 존재'하면 인정한다.
    """
    if _contains(haystack_norm, quote):
        return True
    tokens = [t for t in re.split(r"\s+", (quote or "").strip()) if t]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if _normalize(t) in haystack_norm)
    return hits / len(tokens) >= QUOTE_OVERLAP_THRESHOLD


def verify_coverage(coverage: dict, source_text: str) -> dict:
    """보장 1건을 원문과 대조해 점수와 세부 근거를 반환한다."""
    src_norm = _normalize(source_text)
    score = 0
    checks: dict[str, bool] = {}

    # 1) 보장명 근거 (25)
    name = coverage.get("coverage_name") or ""
    # 보장명 전체 혹은 핵심 키워드(끝 2~4글자)라도 포함되면 인정
    name_hit = _contains(src_norm, name) or any(
        _contains(src_norm, tok) for tok in _name_tokens(name)
    )
    checks["coverage_name"] = name_hit
    if name_hit:
        score += 25

    # 2) 금액 원문 일치 (30)
    amount = coverage.get("amount")
    if amount is None:
        # 금액이 원래 없는 보장(예: 사망 보장 일부)은 감점 대신 부분 인정
        checks["amount"] = None
        score += 15
    else:
        # 글자 그대로 일치("5천만원")하지 않아도 표기만 다를 뿐 값이 같으면
        # ("5,000만원" vs "5천만원") 원 단위로 환산해 비교하면 일치로 인정한다.
        amount_hit = _contains(src_norm, str(amount)) or _amount_equivalent(
            amount, source_text
        )
        checks["amount"] = amount_hit
        if amount_hit:
            score += 30

    # 3) 지급조건 근거 (20)
    condition = coverage.get("payment_condition") or ""
    cond_hit = any(
        _contains(src_norm, tok) for tok in _condition_tokens(condition)
    )
    checks["payment_condition"] = cond_hit
    if cond_hit:
        score += 20

    # 4) source_quote 원문 포함 (25) — 정확 일치 또는 어절 70% 겹침 인정
    quote = coverage.get("source_quote") or ""
    quote_hit = _quote_in_source(src_norm, quote)
    checks["source_quote"] = quote_hit
    if quote_hit:
        score += 25

    # 5) GPT enum 필드 유효성 검사 — 허용값 외 값은 "불명"으로 치환하고 필드당 감점
    corrected, enum_checks, penalty = _validate_enum_fields(coverage)
    checks.update(enum_checks)
    score -= penalty

    score = max(0, min(score, 100))
    result = {
        "coverage_name": name,
        "confidence": score,
        "level": _level(score),
        "checks": checks,
    }
    # 유효하지 않았던 필드만 "불명"으로 덮어쓴다 (유효 필드는 원본값 유지)
    result.update(corrected)
    return result


def _name_tokens(name: str) -> list[str]:
    """보장명에서 비교용 토큰(끝 3~4글자)을 만든다.

    끝 2글자 토큰은 '보험'/'장해'처럼 흔한 어미가 우연히 원문에 있어도
    일치로 잡혀 과대평가되므로 제외한다. 보장명 전체 일치는
    verify_coverage에서 별도로 검사하므로, 여기서는 끝 3글자 이상만 본다.
    """
    name = name.strip()
    tokens = []
    if len(name) >= 3:
        tokens.append(name[-3:])
    if len(name) >= 4:
        tokens.append(name[-4:])
    return tokens


def _condition_tokens(condition: str) -> list[str]:
    """지급조건 문장에서 의미 있는 명사구 후보를 잘라낸다."""
    if not condition:
        return []
    # 너무 짧은 토막은 의미 없으므로 4글자 이상 어절만 사용
    words = re.split(r"[\s,./()]+", condition)
    return [w for w in words if len(w) >= 4]


def _level(score: int) -> str:
    """점수를 신뢰 등급 라벨로."""
    if score >= 80:
        return "높음"
    if score >= 50:
        return "보통"
    return "낮음"


def verify_result(gpt_result: dict, article: Article) -> dict:
    """GPT 분석 결과(조항 1개)를 원문과 대조 검증한다.

    각 보장에 confidence를 부여하고, 조항 평균 신뢰도를 함께 반환한다.
    """
    source_text = article.full_text()
    verified = []
    for cov in gpt_result.get("coverages", []):
        v = verify_coverage(cov, source_text)
        # 원본 추출값 + 검증결과 병합
        merged = dict(cov)
        merged.update(v)
        verified.append(merged)

    avg = (
        round(sum(v["confidence"] for v in verified) / len(verified))
        if verified
        else 0
    )
    return {
        "number": gpt_result.get("number"),
        "title": gpt_result.get("title"),
        "avg_confidence": avg,
        "coverages": verified,
        "error": gpt_result.get("error"),
    }


def verify_all(gpt_results: list[dict]) -> list[dict]:
    """전체 GPT 결과를 검증한다.

    각 결과에 동봉된 "_article"(원본 조항)과 1:1로 대조한다.
    조항 번호로 매칭하지 않으므로 같은 번호가 중복돼도 안전하다.
    """
    out = []
    for res in gpt_results:
        art = res.get("_article")
        if art is None:
            continue
        out.append(verify_result(res, art))
    return out
