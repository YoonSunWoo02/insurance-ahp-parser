"""정규식 기반 1차 분류 모듈.

GPT 호출 전에, 비용 없이 빠르게 조항을 분류한다.
- 보장 키워드(진단비/수술비/입원비/골절/사망 등) 감지
- 금액 패턴(1,000만원 / 3천만원 / 10,000,000원 등) 감지
"""

import re

from .pdf_extractor import Article


# 보장 카테고리별 키워드 사전
# (기존 상해 보장 키워드 유지 + 실손보험 보장/질병 키워드 추가)
COVERAGE_KEYWORDS: dict[str, list[str]] = {
    # --- 기존 상해 보장 ---
    "진단비": ["진단비", "진단급여금", "진단보험금", "진단확정"],
    "수술비": ["수술비", "수술급여금", "수술보험금", "수술자금"],
    "입원비": ["입원비", "입원급여금", "입원일당", "입원보험금"],
    "골절": ["골절", "깁스", "분쇄골절"],
    "사망": ["사망보험금", "사망급여금", "사망 시", "사망하였을"],
    "통원": ["통원", "통원비", "외래", "통원의료비"],
    "암": ["암보장", "암진단", "유사암", "고액암", "항암"],
    # --- 실손보험 보장 키워드 ---
    "요양병원": ["요양병원"],
    "본인부담금": ["본인부담금", "본인부담"],
    "급여구분": ["급여", "비급여"],
    "처치치료": ["처치", "치료"],
    # --- 질병 키워드 ---
    "뇌혈관": ["뇌혈관", "뇌출혈", "뇌경색"],
    "심장": ["심장", "심근경색", "허혈"],
    # --- 보장/지급 일반 표현 (후보 회수율 향상용) ---
    "보상": ["보상한다", "보상합니다", "보상하는", "보상하지 않"],
    "보장": ["보장종목", "보장내용", "보장금액", "보장한도"],
    "지급": ["지급한다", "지급합니다", "지급하는", "지급하지 않", "지급사유"],
    "실손": ["실손", "실손의료비", "의료비"],
    "한도": ["보험가입금액", "가입금액 한도", "연간 한도"],
}

# 금액 표현 패턴들
AMOUNT_PATTERNS = [
    # 1,000만원 / 3천만원 / 5백만원
    re.compile(r"\d{1,3}(?:,\d{3})*\s*(?:만|천만|억)?\s*원"),
    re.compile(r"\d+\s*(?:천만|백만|만|억)\s*원"),
    # 10,000,000원
    re.compile(r"\d{1,3}(?:,\d{3})+\s*원"),
    # 가입금액의 100% / 50% 지급
    re.compile(r"\d{1,3}\s*%"),
]

# 지급 조건 신호 단어 (지급조건 존재 가능성 판단용)
CONDITION_SIGNALS = [
    "경우", "때", "한하여", "한정", "이내", "이상", "이하",
    "최초", "1회", "지급하지", "면책", "감액",
]

# 보장과 무관한 노이즈 조항 제목 (후보에서 제외)
NOISE_TITLE_KEYWORDS = [
    # 기존
    "용어의 정의", "예금보험", "준용규정",
    # 실손보험 약관에서 걸러야 할 계약/절차성 조항
    "보험료의 납입", "계약의 해지", "보험계약의 성립",
    "개인정보", "분쟁조정", "소멸시효",
    # 절차·관리성 조항 (보장 없음 → 노이즈 유발)
    "배당금", "약관의 해석", "약관 해석", "연대책임", "손해배상",
    "해약환급금", "예금자보호", "관할법원", "계약의 부활",
    "보험료의 환급", "보험나이",
    # 면책(보장 제외) 조항 — GPT가 '보장하지 않는 항목'을 보장으로 뒤집어 읽음
    "보상하지 않는 사항", "보험금을 지급하지 않는 사유", "특별면책조건",
    "알릴 의무 위반의 효과",
    # 약관 뒤에 첨부된 법령 인용 조문 (상품 보장이 아님)
    "요양급여", "급여비용의 부담", "급여비용의 청구",
    "의료급여의 제한", "의료급여의 변경", "의료급여의 중지",
    "의료급여의 내용",  # 국민건강보험법상 요양급여 종류를 나열 → 보장 오탐 (참편한실손 1901 제7조)
    "수급권자의 인정 절차", "난민에 대한 특례",
    "유산 또는 사산", "복수출생",
    "응급환자", "응급증상",  # 응급의료법상 판정 기준 나열 → 보장 오탐 (참편한실손 1901 제2조)
    "상급종합병원 지정", "신의료기술평가위원회", "의료기관의 시설기준",
    "보건진료소", "운동기능장해의 측정", "과로한 때 등의 운전 금지",
]

# 유효 금액으로 인정하는 큰 단위 (이 단위가 없는 소액 원화는 이자 계산식 등으로 간주)
LARGE_UNITS = ["천만", "백만", "억", "만"]

# 법령 인용/정의 패턴 — 조항이 상품 보장이 아니라 법률 조문을 나열/정의하는지 판단용.
#   「국민건강보험법」, 「응급의료에 관한 법률」 …  (낫표로 감싼 법령명)
#   법 제41조 / 법률 제2조 / 시행령 제5조 …        (법령 + 제N조 인용)
LAW_CITATION_PATTERN = re.compile(
    r"「[^」]*(?:법|법률)[^」]*」"
    r"|(?:법|법률|시행령|시행규칙)\s*제\s*\d+\s*조"
)


def is_noise_title(title: str) -> bool:
    """보장과 무관한 노이즈 제목(용어 정의/예금보험/준용규정 등)인지."""
    return any(kw in title for kw in NOISE_TITLE_KEYWORDS)


def is_law_citation(text: str, amounts: list[str] | None = None) -> bool:
    """법령을 인용·정의하는 조항인지 판단한다.

    「○○법」 인용이나 "법 제N조" 형태가 있으면서 **금액 표현이 전혀 없는** 조항은
    상품 보장이 아니라 법률 조문(예: 국민건강보험법상 요양급여 종류, 응급의료법상
    응급증상 판정 기준)을 나열/정의하는 조항으로 본다.

    실제 보장 조항도 근거 법령을 언급할 수 있으므로, 금액이 하나라도 있으면
    보장으로 간주해 제외하지 않는다(회수율 훼손 최소화). amounts를 넘기면
    재계산을 생략한다.
    """
    if not LAW_CITATION_PATTERN.search(text):
        return False
    if amounts is None:
        amounts = detect_amounts(text)
    return not amounts


def is_noise_amount(amount: str) -> bool:
    """노이즈 금액인지 판단한다.

    - "000만원"처럼 선행 숫자가 모두 0 → 파싱 오류로 제외
    - 큰 단위(만/백만/천만/억)가 없고 값이 1,000원 미만 → 이자 계산식 소액으로 제외
    """
    val = amount.replace(",", "").replace(" ", "")

    # %는 비율이므로 노이즈 판정 대상이 아님
    if val.endswith("%"):
        return False

    # 선행 숫자 추출
    m = re.match(r"(\d+)", val)
    if not m:
        return False
    digits = m.group(1)

    # "000만원" 등 선행 숫자가 전부 0 → 파싱 오류
    if int(digits) == 0:
        return True

    # 큰 단위가 없는 소액(100원/110원 등 100원 단위 이하) → 이자 계산식
    if not any(unit in val for unit in LARGE_UNITS):
        if int(digits) < 1000:
            return True

    return False


def detect_coverages(text: str) -> list[str]:
    """텍스트에서 매칭되는 보장 카테고리 목록을 반환한다."""
    found = []
    for category, keywords in COVERAGE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            found.append(category)
    return found


def detect_amounts(text: str) -> list[str]:
    """텍스트에서 금액/비율 표현을 모두 추출한다 (중복 제거, 등장 순서 유지)."""
    results: list[str] = []
    seen = set()
    for pattern in AMOUNT_PATTERNS:
        for m in pattern.finditer(text):
            val = m.group(0).strip()
            if val in seen or is_noise_amount(val):
                continue
            seen.add(val)
            results.append(val)
    return results


def has_condition_signal(text: str) -> bool:
    """지급조건 관련 신호 단어가 있는지 여부."""
    return any(sig in text for sig in CONDITION_SIGNALS)


def classify_article(article: Article) -> dict:
    """조항 1개를 규칙 기반으로 분류한 결과 dict를 반환한다."""
    text = article.full_text()
    coverages = detect_coverages(text)
    amounts = detect_amounts(text)
    return {
        "number": article.number,
        "title": article.title,
        "coverages": coverages,
        "amounts": amounts,
        "has_condition": has_condition_signal(text),
        # GPT로 정밀 분석할 가치가 있는지 (완화된 조건):
        # 보장 키워드 OR 금액 중 하나만 있어도 후보 (노이즈 제목은 제외).
        # 회수율(recall)을 높이고, 정밀도는 노이즈 제목 필터와
        # 후처리 단계의 신뢰도 임계값(postprocess.MIN_CONFIDENCE)이 보강한다.
        # 단, 금액 없이 법령만 인용/정의하는 조항은 상품 보장이 아니므로 제외한다.
        "is_candidate": (
            (bool(coverages) or bool(amounts))
            and not is_noise_title(article.title)
            and not is_law_citation(text, amounts)
        ),
    }


def _iter_candidates(articles: list[Article]):
    """후보 조항을 (Article, 분류dict)로, 중복 제거하며 순서대로 내보낸다.

    - is_candidate=True 인 것만
    - 같은 (제목 + 보장조합)은 첫 번째만 (순서 무관 중복 제거)

    filter_candidates(dict 결과)와 select_candidate_articles(Article 결과)가
    동일한 선별/중복제거 로직을 공유하도록 하는 내부 헬퍼.
    """
    seen_keys: set[tuple] = set()
    for art in articles:
        c = classify_article(art)
        if not c["is_candidate"]:
            continue
        key = (c["title"], tuple(sorted(c["coverages"])))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        yield art, c


def filter_candidates(articles: list[Article]) -> list[dict]:
    """보장+금액이 모두 감지된 '후보 조항'의 분류 dict 목록을 반환한다.

    노이즈 제목/금액은 제외되며, 같은 (제목 + 보장조합) 중복은 하나만 남긴다.
    """
    return [c for _, c in _iter_candidates(articles)]


def select_candidate_articles(articles: list[Article]) -> list[Article]:
    """후보로 선별된 Article 객체 목록을 그대로 반환한다.

    filter_candidates와 1:1 대응한다(같은 순서·같은 개수). 조항 번호가
    중복될 수 있으므로, 번호로 다시 매칭하지 말고 이 목록을 직접 사용해야
    GPT 호출 대상이 정확히 후보 수만큼으로 제한된다.
    """
    return [art for art, _ in _iter_candidates(articles)]


if __name__ == "__main__":
    import sys

    from .pdf_extractor import extract_articles

    if len(sys.argv) < 2:
        print("사용법: python -m parser.rule_extractor <pdf_경로>")
        sys.exit(1)

    arts = extract_articles(sys.argv[1])
    cands = filter_candidates(arts)
    print(f"전체 {len(arts)}조 중 후보 {len(cands)}조")
    for c in cands:
        print(f"  제{c['number']}조 {c['title']} | 보장={c['coverages']} | 금액={c['amounts'][:3]}")
