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


# source_quote 어절 겹침 인정 임계값 (이 비율 이상 어절이 원문에 있으면 인정)
QUOTE_OVERLAP_THRESHOLD = 0.7


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
        amount_hit = _contains(src_norm, str(amount))
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

    score = min(score, 100)
    return {
        "coverage_name": name,
        "confidence": score,
        "level": _level(score),
        "checks": checks,
    }


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
