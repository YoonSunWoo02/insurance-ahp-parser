"""보장 추출 결과 후처리 모듈.

verifier까지 끝난 results 배열을 받아 DB/JSON에 내보내기 전 정제한다.
main.py(JSON 저장 전)와 upload_to_supabase.py(DB 적재 전)가 공유한다.

네 단계로 구성되며 **순서가 중요하다**:
    1) clear_low_confidence_amounts — 원문 대조에 실패한 금액을 null로 지운다.
    2) sanitize_amounts             — 금액이 아닌 문장/한도 문구를 null로 지운다.
    3) fill_missing_amounts         — 같은 coverage_name 그룹의 신뢰 가능한 금액으로 null을 채운다.
    4) dedup_coverages              — 완전히 동일한 보장이 여러 조항에 반복된 것을 1건으로 줄인다.

1)·2) → 3) 순서를 지켜야 환각/비금액 문자열이 3)에서 donor가 되어 같은 그룹으로
전파되는 것을 막을 수 있다. 3) → 4) 순서를 지켜야 amount가 채워진 뒤 중복 판정이
이뤄진다(채우기 전이면 amount=None 항목과 값 있는 항목이 서로 달라 중복이 안 잡힌다).
"""

import re
import sys

# 이 점수 미만이면 verifier가 원문 대조에 실패한 것으로 보고 금액을 신뢰하지 않는다.
# (보장명 자체는 실재할 수 있으므로 항목은 남기고 amount만 지운다.)
MIN_CONFIDENCE = 50


def _iter_coverages(results: list[dict]):
    """results 안의 모든 coverage dict를 순회한다."""
    for article in results:
        for cov in article.get("coverages", []):
            yield cov


def _is_blank_amount(amount) -> bool:
    """amount가 비어있는(없는) 값인지."""
    return not amount or str(amount).strip().lower() == "null"


# 금액/비율 신호: 숫자 + 화폐단위(억/천만/백만/만/원) 또는 백분율.
# 이 신호가 전혀 없는 값(예: "연간 보험가입금액의 한도 내에서 보상합니다",
# "년간 방문 180회 한도")은 금액이 아니라 한도 서술 문장이므로 amount로 인정하지 않는다.
_MONEY_SIGNAL = re.compile(r"\d[\d,]*\s*(?:억|천만|백만|만|원)|\d+\s*%")


def sanitize_amounts(results: list[dict]) -> int:
    """금액 표현이 아닌 amount 값을 None으로 지운다(비금액 문장 정리).

    실손 보장 조항은 금액을 별도 '가입금액 한도' 조항에 두고 본문에는
    "가입금액 한도 내에서 보상"이라고만 쓰는 경우가 많다. GPT는 원문에 충실해
    이 문장을 amount에 담지만, 이는 숫자가 아니므로 정리한다. 숫자 화폐 표현이
    하나라도 있으면(예: "1일 평균금액 10만원") 유지한다.

    지운 건수를 반환한다.
    """
    cleared = 0
    for cov in _iter_coverages(results):
        amt = cov.get("amount")
        if not _is_blank_amount(amt) and not _MONEY_SIGNAL.search(str(amt)):
            cov["amount"] = None
            cleared += 1
    return cleared


def _score(cov: dict) -> float:
    """confidence를 숫자로. 없거나 숫자가 아니면 0으로 본다."""
    conf = cov.get("confidence")
    return conf if isinstance(conf, (int, float)) else 0


def clear_low_confidence_amounts(
    results: list[dict], min_confidence: int = MIN_CONFIDENCE
) -> int:
    """신뢰도가 임계값 미만인 보장의 amount를 None으로 지운다(항목은 유지).

    verifier가 원문에서 근거를 찾지 못한 금액은 환각일 수 있다. 보장명은
    실재할 수 있으므로 항목째 버리지 않고 금액만 무효화한다. 지워진 금액은
    이후 fill_missing_amounts가 같은 이름의 신뢰 가능한 값으로 채울 수 있다.

    지운 건수를 반환한다.
    """
    cleared = 0
    for cov in _iter_coverages(results):
        if _score(cov) < min_confidence and not _is_blank_amount(cov.get("amount")):
            cov["amount"] = None
            cleared += 1
    return cleared


def fill_missing_amounts(results: list[dict]) -> int:
    """같은 coverage_name 그룹 내 amount가 있는 항목의 값으로 null 항목을 채운다.

    예) 제3조 '상해급여' amount=null, 제6조 '상해급여' amount='5천만원'
        → 제3조 '상해급여' amount='5천만원'

    clear_low_confidence_amounts가 먼저 실행된 뒤라면, 남아있는 amount는 모두
    신뢰도 임계값을 통과한 값이므로 donor로 안전하다.

    채운 건수를 반환한다.
    """
    groups: dict[str, list[dict]] = {}
    for cov in _iter_coverages(results):
        name = cov.get("coverage_name")
        if name is None:
            continue
        groups.setdefault(name, []).append(cov)

    filled = 0
    for covs in groups.values():
        donor = next(
            (c["amount"] for c in covs if not _is_blank_amount(c.get("amount"))),
            None,
        )
        if donor is None:
            continue
        for c in covs:
            if _is_blank_amount(c.get("amount")):
                c["amount"] = donor
                filled += 1
    return filled


def _dedup_key(cov: dict) -> tuple:
    """중복 판정 키. 이 셋이 모두 같으면 같은 보장으로 본다.

    금액이 다르면 다른 보장으로 취급한다(같은 이름이라도 급여/비급여로 한도가
    갈리는 경우). 주계약/특약도 구분한다.

    payment_condition은 **키에 넣지 않는다.** GPT가 조항마다 원문을 다르게 인용해
    같은 보장이라도 문구가 매번 달라지기 때문이다. 키에 포함하면 중복이 하나도
    잡히지 않는다(실측: 26건 → 26건).
    """
    return (
        cov.get("coverage_name"),
        cov.get("amount"),
        cov.get("contract_type"),
    )


def dedup_coverages(results: list[dict]) -> int:
    """여러 조항에 반복 등장하는 동일 보장을 1건만 남긴다(제거 건수 반환).

    같은 PDF의 여러 조항에 같은 보장이 중복 서술되는 경우가 많아, 그대로 두면
    product_coverage 테이블에 같은 보장이 여러 행으로 적재된다.

    fill_missing_amounts **뒤에** 실행해야 한다. 금액을 채우기 전이면 amount=None인
    항목과 amount가 있는 항목의 키가 달라져 중복으로 잡히지 않는다.

    같은 키의 항목이 여럿이면 **confidence가 가장 높은 것**을 남긴다. 동점이면
    먼저 등장한 것을 남긴다. 남는 항목의 조항 내 위치는 그 항목이 원래 있던
    조항이다(승자가 속한 조항에 그대로 유지).
    """
    # 1) 키별 승자(최고 신뢰도) 선정 — 동점이면 먼저 등장한 것
    winners: dict[tuple, dict] = {}
    for cov in _iter_coverages(results):
        key = _dedup_key(cov)
        best = winners.get(key)
        if best is None or _score(cov) > _score(best):
            winners[key] = cov

    # 2) 승자가 아닌 항목 제거 (id로 대조 — 같은 dict 객체인지)
    winner_ids = {id(c) for c in winners.values()}
    removed = 0
    for article in results:
        kept = []
        for cov in article.get("coverages", []):
            if id(cov) in winner_ids:
                kept.append(cov)
            else:
                removed += 1
        article["coverages"] = kept
    return removed


def count_coverages(results: list[dict]) -> int:
    """results 안의 보장 총 건수."""
    return sum(len(a.get("coverages", [])) for a in results)


def postprocess(
    results: list[dict],
    min_confidence: int = MIN_CONFIDENCE,
    verbose: bool = True,
) -> dict[str, int]:
    """정제 4단계를 순서대로 적용하고 통계를 반환한다. results는 in-place로 수정된다.

    여러 번 실행해도 결과가 같다(멱등). JSON 저장 전과 DB 적재 전 모두에서
    안전하게 호출할 수 있다.

    로그는 stderr로 나간다 (main.py --stdout의 JSON 출력을 오염시키지 않도록).
    """
    before = count_coverages(results)

    cleared = clear_low_confidence_amounts(results, min_confidence)
    sanitized = sanitize_amounts(results)
    filled = fill_missing_amounts(results)
    removed = dedup_coverages(results)

    after = count_coverages(results)

    if verbose:
        print(f"[후처리] 신뢰도 {min_confidence} 미만 금액 무효화: {cleared}건", file=sys.stderr)
        print(f"[후처리] 비금액 문자열 정리: {sanitized}건", file=sys.stderr)
        print(f"[후처리] amount 후처리: {filled}건 채움", file=sys.stderr)
        print(f"[보장] 중복 제거: {before}건 → {after}건", file=sys.stderr)

    return {"cleared": cleared, "sanitized": sanitized, "filled": filled,
            "removed": removed, "before": before, "after": after}
