"""독소조항 파이프라인 구조 보강 테스트 (pytest 불필요).

    python test_toxic_structural.py

- 비정상 조번호(상법 제657조 등 인용문이 헤더로 오인식된 것) 후보 제외
- 동일 원문(source_quote) 중복 독소조항 dedup
"""

import sys

from parser.pdf_extractor import Article, _strip_appendix
from toxic_detector import is_candidate, _dedup_clauses, MAX_PLAUSIBLE_ARTICLE_NO

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def _art(number: int, title: str, body: str) -> Article:
    return Article(number=number, title=title, body=body,
                   raw_header=f"제{number}조({title})")


# 독소 키워드 포함 + 본문 50자 이상 (후보 조건 충족용)
TOXIC_BODY = ("회사는 다음의 경우 보험금을 지급하지 않으며, 계약자가 알릴 의무를 "
             "위반한 경우 계약을 해지할 수 있습니다. 이는 계약자에게 불리한 조건입니다.")


def test_article_number_guard():
    print("[구조1] 비정상 조번호(인용 법령) 후보 제외")
    # 정상 범위 조번호는 후보 유지
    ok = _art(50, "보상하지 않는 사항", TOXIC_BODY)
    check(is_candidate(ok) is True, "제50조(정상) → 후보 유지")
    check(is_candidate(_art(MAX_PLAUSIBLE_ARTICLE_NO, "면책", TOXIC_BODY)) is True,
          f"제{MAX_PLAUSIBLE_ARTICLE_NO}조(경계) → 후보 유지")
    # 3자리 조번호(상법 657 등 인용문) 제외
    bad = _art(657, "보험사고발생의 통지의무", TOXIC_BODY)
    check(is_candidate(bad) is False, "제657조(상법 인용) → 후보 제외")
    check(is_candidate(_art(663, "보험자의 면책사유", TOXIC_BODY)) is False,
          "제663조(상법 인용) → 후보 제외")


def test_dedup_clauses():
    print("[구조2] 동일 원문 중복 독소조항 제거")
    q = "회사는 정당한 사유 없이 지급을 지연한 경우 이자를 지급하지 않습니다."
    results = [
        {"article_number": 4, "article_title": "보상하지 않는 사항",
         "toxic_clauses": [
             {"clause_summary": "지급 지연 이자 미지급", "source_quote": q},
             {"clause_summary": "고의 면책", "source_quote": "고의로 자신을 해친 경우 보상하지 않습니다."},
         ]},
        {"article_number": 50, "article_title": "예금보험에 의한 지급보장",
         "toxic_clauses": [
             # article 4와 동일 원문(공백만 다름) → 제거 대상
             {"clause_summary": "지급 지연", "source_quote": "회사는 정당한 사유 없이  지급을 지연한 경우 이자를 지급하지 않습니다."},
         ]},
    ]
    deduped, removed = _dedup_clauses(results)
    total = sum(len(a["toxic_clauses"]) for a in deduped)
    check(removed == 1, f"동일 원문 1건 제거 (실제 {removed})")
    check(total == 2, f"3건 → 2건 (실제 {total})")
    # article 50은 유일 조항이 제거되어 빈 조항이 되므로 결과에서 빠짐
    check(all(a["article_number"] != 50 for a in deduped), "비게 된 조항(50)은 결과에서 제외")
    # 완전히 다른 원문은 유지
    d2, r2 = _dedup_clauses([
        {"article_number": 1, "article_title": "A", "toxic_clauses": [{"source_quote": "가나다"}]},
        {"article_number": 2, "article_title": "B", "toxic_clauses": [{"source_quote": "라마바"}]},
    ])
    check(r2 == 0, "서로 다른 원문은 제거 안 함")
    # 멱등성
    _, r3 = _dedup_clauses(deduped)
    check(r3 == 0, "dedup 멱등(2회차 0건)")


def test_strip_appendix():
    print("[구조3] 흡수된 별표/부록 제거 (줄머리 커팅 vs 인라인 참조 보존)")
    # 줄머리 부록 마커 → 그 앞까지만 남기고 부록 표는 제거
    body = ("이 계약의 예금보험에 의한 지급보장은 예금자보호법에 따릅니다.\n"
            "【별표1】 용어의 정의\n용어 정의 계약 보험계약...\n"
            "【별표3】 질병입원형에서 보상하지 않는 질병\n1. 정신 및 행동장애(F04∼F99)")
    out = _strip_appendix(body)
    check("예금자보호법에 따릅니다" in out, "부록 앞 본문은 유지")
    check("【별표1】" not in out and "정신 및 행동장애" not in out,
          "줄머리 별표부터 뒤(부록 표)는 제거")

    # 인라인 참조([별표N] 참조, 문장 중간)는 자르지 않음 → 정당한 면책 목록 보존
    inline = ("보상하지 않는 질병은 다음과 같다.[“보상하지 않는 질병”(【별표3】 참조)]\n"
              "1. 정신 및 행동장애(F04∼F99)\n"
              "「요양급여의 기준에 관한 규칙」 제9조([별표2] 비급여대상)에 따른 질환")
    out2 = _strip_appendix(inline)
    check(out2 == inline, "괄호 속 인라인 [별표N] 참조는 커팅 안 함(면책 목록 보존)")

    # 마커 없으면 원본 그대로
    plain = "회사는 계약자에게 해지환급금을 지급합니다."
    check(_strip_appendix(plain) == plain, "부록 마커 없으면 원본 유지")


def main() -> int:
    for t in (test_article_number_guard, test_dedup_clauses, test_strip_appendix):
        t()
    print()
    if _failures:
        print(f"❌ 실패 {len(_failures)}건")
        return 1
    print("✅ 전체 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
