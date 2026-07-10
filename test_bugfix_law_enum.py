"""버그 재현 테스트 — 법조문 인용 조항 오탐 + GPT enum 필드 유효성.

실제 사례(무배당 참 편한 실손의료보험 1901)에서 발견된 두 버그를 pytest 없이
표준 라이브러리만으로 재현/검증한다. 실제 PDF 없이 Article 객체를 직접 만들어
pdf_extractor를 우회한다.

    python test_bugfix_law_enum.py

기존 postprocess 로직이 깨지지 않았는지도 함께 확인한다.
"""

import sys

from parser.pdf_extractor import Article
from parser.rule_extractor import (
    is_law_citation,
    select_candidate_articles,
    classify_article,
)
from validator.verifier import verify_coverage, verify_result
from postprocess import postprocess


_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        _failures.append(msg)


# ── 테스트용 조항 (실제 PDF 우회) ────────────────────────────────

# 참편한실손 1901 제7조 유형: 국민건강보험법상 요양급여 '종류'를 나열 (금액 없음)
LAW_DEF_BODY = """① 「국민건강보험법」 제41조에 따른 요양급여의 내용은 다음 각 호와 같다.
1. 진찰ㆍ검사
2. 약제(藥劑)ㆍ치료재료의 지급
3. 처치ㆍ수술 및 그 밖의 치료
4. 예방ㆍ재활
5. 입원
6. 간호
7. 이송(移送)
② 요양급여의 절차와 방법은 같은 법 시행규칙 제5조에서 정하는 바에 따른다."""

# 참편한실손 1901 제2조 유형: 응급의료법상 응급증상 판정 기준 나열 (금액 없음)
EMERGENCY_BODY = """「응급의료에 관한 법률」 제2조 및 같은 법 시행규칙에 따라 응급환자란 다음
각 호의 증상을 나타내는 사람을 말한다.
1. 신경학적 응급증상: 급성 의식장애, 급성 신경학적 이상
2. 심혈관계 응급증상: 급성 심근경색, 심폐소생술이 필요한 상태
3. 중독 및 대사 장애: 심한 탈수, 급성 신부전"""

# 실제 보장 조항(대조군): 법령을 언급하지만 '금액'이 있으므로 보장으로 인정되어야 함
REAL_COVERAGE_BODY = """피보험자가 상해로 「국민건강보험법」에 따른 요양급여 중 본인이 부담한
금액을 입원과 통원을 합산하여 5,000만원 이내에서 보상합니다."""


def make_article(number: int, title: str, body: str) -> Article:
    return Article(number=number, title=title, body=body,
                   raw_header=f"제{number}조({title})")


# ── 작업 1: 법조문 인용/정의 조항 필터링 ─────────────────────────

def test_law_citation_heuristic():
    print("[작업1] 법령 인용 휴리스틱 (is_law_citation)")
    # 제목이 노이즈 목록에 없어도 본문 휴리스틱만으로 걸러지는지 확인
    check(is_law_citation(LAW_DEF_BODY),
          "국민건강보험법 요양급여 종류 나열(금액X) → 법령 인용으로 판정")
    check(is_law_citation(EMERGENCY_BODY),
          "응급의료법 응급증상 기준 나열(금액X) → 법령 인용으로 판정")
    check(not is_law_citation(REAL_COVERAGE_BODY),
          "법령 언급 + 금액 5,000만원 있음 → 보장으로 인정(회수율 보호)")


def test_law_articles_excluded_from_candidates():
    print("[작업1] 법령 정의 조항이 후보에서 제외되는지")
    # 제목을 일부러 노이즈 목록에 없는 것으로 두어 '본문 휴리스틱'만 검증
    law1 = make_article(7, "보장 관련 부속 규정", LAW_DEF_BODY)
    law2 = make_article(2, "정의 관련 조항", EMERGENCY_BODY)
    real = make_article(3, "보장종목별 보상내용", REAL_COVERAGE_BODY)

    check(classify_article(law1)["is_candidate"] is False,
          "요양급여 종류 나열 조항 → is_candidate=False")
    check(classify_article(real)["is_candidate"] is True,
          "실제 보장 조항(금액 있음) → is_candidate=True")

    selected = select_candidate_articles([law1, law2, real])
    titles = [a.title for a in selected]
    check(law1.title not in titles, "법령 정의 조항1 후보 제외됨")
    check(law2.title not in titles, "법령 정의 조항2 후보 제외됨")
    check(real.title in titles, "실제 보장 조항은 후보에 남음")


# ── 작업 2: GPT enum 필드 유효성 검사 ────────────────────────────

def test_enum_invalid_value_coerced():
    print("[작업2] 잘못된 enum 값 → '불명' 치환 + 감점")
    src = REAL_COVERAGE_BODY
    # GPT가 선택지 문자열을 통째로 반환한 버그 재현
    bad = {
        "coverage_name": "상해급여",
        "amount": "5,000만원",
        "payment_condition": "입원과 통원을 합산하여 보상",
        "contract_type": "주계약 | 특약 | 불명",       # 버그 값
        "coverage_type": "입원 | 통원 | 수술 | 불명",   # 버그 값
        "benefit_type": "급여 | 비급여 | 급여+비급여 | 불명",  # 버그 값
        "source_quote": "입원과 통원을 합산하여 5,000만원 이내에서 보상",
    }
    good = dict(bad)
    good["contract_type"] = "주계약"
    good["coverage_type"] = "입원"
    good["benefit_type"] = "급여"

    rbad = verify_coverage(bad, src)
    rgood = verify_coverage(good, src)

    check(rbad["contract_type"] == "불명", "잘못된 contract_type → '불명' 치환")
    check(rbad["coverage_type"] == "불명", "잘못된 coverage_type → '불명' 치환")
    check(rbad["benefit_type"] == "불명", "잘못된 benefit_type → '불명' 치환")
    check(rbad["checks"]["contract_type_valid"] is False,
          "checks.contract_type_valid = False 기록")
    check(rbad["checks"]["coverage_type_valid"] is False,
          "checks.coverage_type_valid = False 기록")
    check(rbad["checks"]["benefit_type_valid"] is False,
          "checks.benefit_type_valid = False 기록")
    # 세 필드 무효 → 30점 감점
    check(rgood["confidence"] - rbad["confidence"] == 30,
          f"3개 필드 무효 시 30점 감점 (good {rgood['confidence']} vs bad {rbad['confidence']})")


def test_enum_valid_value_untouched():
    print("[작업2] 올바른 enum 값 → 유지, 감점 없음, checks=True")
    art = make_article(3, "보장종목별 보상내용", REAL_COVERAGE_BODY)
    good = {
        "coverage_name": "상해급여",
        "amount": "5,000만원",
        "payment_condition": "입원과 통원을 합산하여 보상",
        "contract_type": "특약",
        "coverage_type": "수술",
        "benefit_type": "급여+비급여",
        "source_quote": "입원과 통원을 합산하여 5,000만원 이내에서 보상",
    }
    # 유효 필드는 verify_coverage 반환에 넣지 않고 원본을 유지하므로 verify_result로 검증
    r = verify_result({"number": 3, "title": "보장종목별 보상내용", "coverages": [good]}, art)
    cov = r["coverages"][0]
    check(cov["contract_type"] == "특약", "유효 contract_type 원본 유지")
    check(cov["benefit_type"] == "급여+비급여", "유효 benefit_type(조합값) 원본 유지")
    check(cov["checks"]["contract_type_valid"] is True, "checks.contract_type_valid = True")
    check(cov["checks"]["benefit_type_valid"] is True, "checks.benefit_type_valid = True")
    # 감점 없음: 동일 보장에서 contract_type만 잘못된 값으로 바꾸면 정확히 10점 낮아야 함
    invalid_one = dict(good, contract_type="주계약 | 특약 | 불명")
    r_invalid = verify_coverage(invalid_one, REAL_COVERAGE_BODY)
    r_valid = verify_coverage(good, REAL_COVERAGE_BODY)
    check(r_valid["confidence"] - r_invalid["confidence"] == 10,
          f"유효 enum은 감점 0, 무효 1건은 -10 (valid {r_valid['confidence']} vs invalid {r_invalid['confidence']})")


def test_coverage_type_combo_allowed():
    print("[작업2-완화] coverage_type 조합값 인정 vs 선택지 문자열 버그 구분")
    src = REAL_COVERAGE_BODY
    base = {
        "coverage_name": "상해급여",
        "amount": "5,000만원",
        "payment_condition": "입원과 통원을 합산하여 보상",
        "contract_type": "주계약",
        "benefit_type": "급여",
        "source_quote": "입원과 통원을 합산하여 5,000만원 이내에서 보상",
    }
    # 정당한 조합값 → 유지, 감점 없음
    combo = verify_coverage(dict(base, coverage_type="입원 | 통원"), src)
    check(combo["checks"]["coverage_type_valid"] is True,
          "coverage_type='입원 | 통원' → 유효(조합값 인정)")
    check("coverage_type" not in combo,
          "유효 조합값은 '불명'으로 치환되지 않음(원본 유지)")
    # 선택지 문자열 전체(불명 포함) → 버그로 간주해 여전히 무효
    literal = verify_coverage(dict(base, coverage_type="입원 | 통원 | 수술 | 불명"), src)
    check(literal["checks"]["coverage_type_valid"] is False,
          "coverage_type='입원 | 통원 | 수술 | 불명'(선택지 통째) → 무효")
    check(literal["coverage_type"] == "불명",
          "선택지 문자열 버그는 여전히 '불명'으로 치환")
    # 조합에 허용 안 되는 값이 섞이면 무효
    bad = verify_coverage(dict(base, coverage_type="입원 | 응급"), src)
    check(bad["checks"]["coverage_type_valid"] is False,
          "coverage_type='입원 | 응급'(허용 외 조각) → 무효")


def test_enum_merged_into_result():
    print("[작업2] verify_result 병합 시 치환값이 최종 보장에 반영되는지")
    art = make_article(3, "보장종목별 보상내용", REAL_COVERAGE_BODY)
    gpt_result = {
        "number": 3,
        "title": "보장종목별 보상내용",
        "coverages": [{
            "coverage_name": "상해급여",
            "amount": "5,000만원",
            "payment_condition": "입원과 통원을 합산하여 보상",
            "contract_type": "주계약 | 특약 | 불명",
            "coverage_type": "입원",
            "benefit_type": "급여",
            "source_quote": "입원과 통원을 합산하여 5,000만원 이내에서 보상",
        }],
    }
    out = verify_result(gpt_result, art)
    cov = out["coverages"][0]
    check(cov["contract_type"] == "불명", "잘못된 contract_type이 최종 결과에 '불명'으로 반영")
    check(cov["coverage_type"] == "입원", "유효 coverage_type은 그대로 유지")


# ── 회귀: 기존 postprocess 로직이 깨지지 않았는지 ────────────────

def test_postprocess_regression():
    print("[회귀] postprocess 저신뢰 무효화 + 채우기 + dedup + 멱등")
    res = [{"coverages": [
        {"coverage_name": "상해급여", "amount": "5천만원", "contract_type": "주계약",
         "payment_condition": "A문구", "confidence": 85},
        {"coverage_name": "상해급여", "amount": "5천만원", "contract_type": "주계약",
         "payment_condition": "B문구", "confidence": 100},
        {"coverage_name": "환각", "amount": "1,000만원", "contract_type": "주계약",
         "confidence": 0},
    ]}]
    s1 = postprocess(res, verbose=False)
    covs = res[0]["coverages"]
    check(s1["removed"] == 1, "상해급여 중복 1건 제거")
    check(any(c["coverage_name"] == "상해급여" and c["confidence"] == 100 for c in covs),
          "중복 중 최고 신뢰도(100) 유지")
    hall = [c for c in covs if c["coverage_name"] == "환각"][0]
    check(hall["amount"] is None, "신뢰도 0 환각 금액 무효화")
    s2 = postprocess(res, verbose=False)
    check((s2["cleared"], s2["filled"], s2["removed"]) == (0, 0, 0), "멱등성(2회차 변경 0)")


def main() -> int:
    for t in (
        test_law_citation_heuristic,
        test_law_articles_excluded_from_candidates,
        test_enum_invalid_value_coerced,
        test_enum_valid_value_untouched,
        test_coverage_type_combo_allowed,
        test_enum_merged_into_result,
        test_postprocess_regression,
    ):
        t()
    print()
    if _failures:
        print(f"❌ 실패 {len(_failures)}건:")
        for f in _failures:
            print(f"   - {f}")
        return 1
    print("✅ 전체 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
