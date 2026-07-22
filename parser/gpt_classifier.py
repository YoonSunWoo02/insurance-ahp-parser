"""GPT 기반 정밀 분류 모듈.

규칙 기반으로 걸러진 후보 조항을, GPT API로 보내
보장명/보장금액/지급조건/주계약·특약구분을 구조화된 JSON으로 추출한다.
"""

import json
import os
import sys

from openai import OpenAI

from .pdf_extractor import Article
from .rule_extractor import select_candidate_articles


MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# GPT 호출 1건당 타임아웃(초). 초과 시 해당 조항은 건너뛴다.
REQUEST_TIMEOUT = 30

SYSTEM_PROMPT = """너는 한국 실손의료비보험 약관 분석 전문가다.
주어진 약관 조항 원문에서 보장 정보를 정확히 추출해 JSON으로만 응답한다.
원문에 없는 내용은 절대 지어내지 말고, 없으면 null 또는 빈 배열로 둔다.
금액은 원문 표기를 그대로 보존한다 (예: "1,000만원").
실손보험 특성상 입원/통원/수술 구분, 급여/비급여 구분,
본인부담금 비율을 원문 근거에 따라 정확히 분류한다."""

# GPT가 따라야 할 출력 스키마 안내
USER_TEMPLATE = """다음 실손의료비보험 약관 조항에서 보장 정보를 추출하라.

중요: 조항에 여러 보장이 표·목록·항목(예: 상해입원/상해통원/질병입원/질병비급여,
급여/비급여 구분, 각종 의료비 항목 등)으로 나열되어 있으면 요약하지 말고
각 항목을 빠짐없이 개별 객체로 추출하라. 보장 항목이 N개면 coverages도 N개여야 한다.
표가 있으면 표의 각 행(보장종목)을 하나의 개별 보장으로 본다.
입원/통원, 급여/비급여처럼 구분이 나뉘면 각 구분을 별도 보장으로 나눈다.

source_quote는 반드시 원문에서 연속된 구절을 '글자 그대로' 복사하라.
요약·바꿔쓰기·문장 합치기 금지. 공백·문장부호·숫자 표기도 원문 그대로 둔다.
(검증 단계에서 원문 대조에 실패하면 신뢰 불가로 처리되므로 정확한 인용이 중요하다.)

매우 중요 — 법령 인용/정의 조항 제외:
이 조항이 특정 법령(예: 「국민건강보험법」, 「의료급여법」, 「응급의료에 관한 법률」)의
내용·종류·판정 기준·정의를 인용하거나 나열하는 조항이라면, 그 나열 항목은 이 보험상품이
실제로 지급하는 보장이 아니다. 이런 경우 coverages를 반드시 빈 배열([])로 반환하라.
예: "요양급여의 종류(진찰·검사, 약제 지급, 처치·수술, 예방·재활, 입원, 간호, 이송)",
"응급증상·신경학적 응급증상의 판정 기준" 등은 법령이 정의하는 항목일 뿐이므로
개별 보장으로 추출하지 마라. 이 보험상품이 직접 보장·지급하는 항목만 추출한다.

amount 규칙(엄격):
amount에는 **순수 금액 표기만** 넣는다 (예: "5,000만원", "3천만원", "10만원", "70%").
"연간 보험가입금액의 한도 내에서 보상", "가입금액 한도" 같은 서술 문장이나 방문 횟수
("180회") 등은 금액이 아니다. 조항 본문에 구체적 금액 숫자가 없으면(가입금액 한도를
다른 조항에서 정하는 경우 등) amount는 반드시 null로 둔다. 문장을 amount에 넣지 마라.

매우 중요 — "공제금액"과 "보장한도"를 절대 혼동하지 마라:
실손보험 약관은 "<표1> 공제금액 및 보장한도"처럼 **공제금액**과 **보장한도**를
같은 표/문단에 나란히 적는 경우가 매우 많다. 이 둘은 완전히 다른 값이다.
  - amount(보장금액/한도)에는 오직 "보장한도"/"보상한도" 열·문구에 적힌 값만 넣는다.
    (예: "연간 200만원 이내에서 보상", "1회당 20만원 이내에서" → amount = "200만원", "20만원")
  - "공제금액" 열·문구에 적힌 값(예: "1회당 5만원과 보장대상의료비의 50%중 큰 금액")은
    보장금액이 아니라 자기부담(공제) 기준이다. 이 값, 특히 그 안의 %는 amount에 넣지 말고
    self_payment_ratio 또는 payment_condition에 넣어라.
  - 표에서 같은 문장 안에 "1회당 5만원과 ... 50%중 큰 금액"처럼 %가 등장해도, 바로 뒤에
    "...200만원 이내에서 보상"처럼 별도의 한도 금액이 있으면 amount는 반드시 그 한도
    금액("200만원")이어야 한다. %만 있고 원 단위 한도 금액이 전혀 없는 경우에만 %를
    amount로 써도 된다.
  - 판단이 애매하면 "표"의 열 제목(구분/공제금액/보장한도)이 어느 열인지를 기준으로
    판단하고, 공제금액 열의 숫자를 amount로 옮기지 마라.

매우 중요 — 본체 한도와 "다만/단, ~에 한함" 부속 한도를 혼동하지 마라:
한 보장 항목에 금액이 두 개 이상 같이 나오는 경우, 그중 "(단, ○○에 한함)"이나
"다만 ○○의 경우"처럼 **특정 하위 항목에만 적용되는 부속·예외 한도**와, 그 보장
전체에 적용되는 **본체 한도**를 구분해야 한다.
  예) "실손의료비(상해입원형) (단, 상급병실료 차액에 한함) 1일 평균금액 10만원 한도"
      뒤에 별도로 "1,000만원"처럼 그 보장의 연간 전체 한도가 명시돼 있다면,
      amount는 본체 한도인 "1,000만원"이어야 한다. "1일 평균금액 10만원"은
      상급병실료 차액이라는 하위 항목에만 걸리는 부속 조건이므로 payment_condition에
      넣어라. 본체 한도가 안 보이고 부속 한도만 있으면 그때는 부속 한도를 amount로
      써도 된다 — 하지만 반드시 먼저 본체 한도가 조항/표 안에 있는지 확인하라.

contract_type 판단 기준:
급여 담보(상해급여/질병급여 등)는 통상 '주계약(기본계약)', 비급여 담보(상해비급여/
질병비급여/3대비급여 등)는 통상 '특약'이다. 조항·표에 '특약'/'주계약(기본계약)' 표시가
있으면 그 표시를 최우선으로 따르고, 표시가 없으면 위 급여/비급여 기준으로 추정한다.
판단 근거가 전혀 없으면 '불명'으로 둔다.

[조항]
{article_text}

[출력 JSON 스키마]
{{
  "coverages": [
    {{
      "coverage_name": "보장명 (예: 상해입원의료비)",
      "amount": "보장한도(보상한도) 금액만. 공제금액/자기부담 관련 값은 절대 넣지 말 것. 서술문장·한도문구 금지, 숫자 금액 없으면 null",
      "payment_condition": "지급조건 요약 (원문 근거)",
      "contract_type": "주계약 | 특약 | 불명",
      "coverage_type": "입원 | 통원 | 수술 | 불명",
      "benefit_type": "급여 | 비급여 | 급여+비급여 | 불명",
      "self_payment_ratio": "본인부담금 비율 원문 표기 (예: 20%, 없음, 불명)",
      "source_quote": "근거가 된 원문 문장 일부 (검증용, 원문 그대로)"
    }}
  ]
}}

반드시 위 스키마의 JSON만 출력하라."""


def _get_client() -> OpenAI:
    """OPENAI_API_KEY 환경변수로 클라이언트를 생성한다."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY 환경변수가 설정되어 있지 않습니다. "
            ".env 파일 또는 환경변수로 설정하세요."
        )
    return OpenAI(api_key=api_key)


def classify_with_gpt(article: Article, client: OpenAI | None = None) -> dict:
    """조항 1개를 GPT로 분석해 보장 정보 dict를 반환한다.

    반환 형태:
        {"number": N, "title": "...", "coverages": [...], "_article": Article}
    실패 시 coverages는 빈 배열, "error" 키에 사유가 담긴다.
    "_article"은 검증 단계에서 원문을 번호 매칭 없이 1:1로 대조하기 위한
    원본 조항 참조이며, 최종 JSON 출력에는 포함되지 않는다.
    """
    client = client or _get_client()
    user_msg = USER_TEMPLATE.format(article_text=article.full_text())

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
        coverages = data.get("coverages", [])
    except Exception as e:  # API 오류/JSON 파싱 오류 모두 포착
        return {
            "number": article.number,
            "title": article.title,
            "coverages": [],
            "error": str(e),
            "_article": article,
        }

    return {
        "number": article.number,
        "title": article.title,
        "coverages": coverages,
        "_article": article,
    }


def classify_candidates(articles: list[Article]) -> list[dict]:
    """전체 조항 중 규칙 기반 후보(is_candidate=True)만 골라 GPT 분석한다.

    rule_extractor.select_candidate_articles로 선별·중복제거된 Article만
    GPT에 보낸다. 조항 번호로 다시 매칭하지 않으므로, 같은 번호가 여러 번
    등장해도 GPT 호출은 정확히 후보 조항 수만큼만 발생한다.
    """
    targets = select_candidate_articles(articles)
    print(
        f"전체 {len(articles)}개 조항 중 후보 {len(targets)}개만 GPT 호출",
        file=sys.stderr,
    )
    return classify_articles(targets)


# ── GPT 1차 판단: 보장 관련 조항 빠른 선별 ──────────────────────
FILTER_SYSTEM_PROMPT = (
    "너는 한국 실손의료비보험 약관 분석가다. 조항이 '실제 보장 항목'을 "
    "담고 있는지 엄격하게 판단해 JSON으로만 답한다. 절차·관리성 조항은 보장이 아니다."
)

FILTER_USER_TEMPLATE = """다음 약관 조항이 '보험금 지급 대상이 되는 보장 내용'과 조금이라도 관련되는지 판단하라.
보장 회수율을 위해, 보장과 관련될 가능성이 있으면 너그럽게 true로 본다.

[true] 보장 항목·지급사유·의료비(입원/통원/수술/진단/처치 등)·보장금액·한도를
  다루거나, 보상하는/보상하지 않는 사항을 규정하는 조항이면 true.
  (애매하면 true)

[false] 아래 '순수 절차·관리성' 조항만 false:
  - 계약의 성립·해지·부활·변경, 보험료 납입/연체, 해약환급금
  - 배당금 지급, 약관의 해석, 연대책임·손해배상, 분쟁조정·관할법원
  - 용어의 정의, 개인정보, 예금자보호 등 보장과 무관한 일반 규정

[조항 제목] {title}
[조항 본문(일부)] {body}

아래 형식의 JSON만 출력:
{{"is_coverage": true 또는 false, "reason": "한 줄 이유"}}"""


def judge_is_coverage(article: Article, client: OpenAI | None = None) -> dict:
    """조항 1개가 보장 관련인지 GPT로 빠르게 판단한다.

    반환: {"is_coverage": bool, "reason": str, "_article": Article}
    오류 시 보수적으로 is_coverage=True 처리(상세 추출 단계에서 다시 걸러짐).
    """
    client = client or _get_client()
    user_msg = FILTER_USER_TEMPLATE.format(
        title=article.title or "(제목 없음)",
        # 큰 보장 표는 금액/항목이 본문 중반에 나오므로 충분히 넓게 본다
        body=article.full_text()[:3000],
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=50,
            response_format={"type": "json_object"},
            timeout=REQUEST_TIMEOUT,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        is_cov = bool(data.get("is_coverage", False))
        reason = str(data.get("reason", ""))
    except Exception as e:
        # 판단 실패 시 누락 방지를 위해 통과시킨다
        is_cov, reason = True, f"판단 실패(통과 처리): {e}"
    return {"is_coverage": is_cov, "reason": reason, "_article": article}


def filter_by_gpt(articles: list[Article]) -> list[Article]:
    """후보 조항을 GPT 1차 판단으로 보장 관련만 선별해 Article 리스트로 반환한다.

    temperature=0, max_tokens=50으로 빠르게 yes/no만 받는다.
    """
    client = _get_client()
    total = len(articles)
    kept: list[Article] = []
    print(f"[GPT 1차 판단] 후보 {total}개 조항 보장 여부 판별", file=sys.stderr)
    for idx, art in enumerate(articles, start=1):
        j = judge_is_coverage(art, client=client)
        mark = "O" if j["is_coverage"] else "X"
        print(
            f"  [{idx}/{total}] {mark} 제{art.number}조 '{art.title}' — {j['reason']}",
            file=sys.stderr,
        )
        if j["is_coverage"]:
            kept.append(art)
    print(f"      → 보장 관련 {len(kept)}개 조항 선별", file=sys.stderr)
    return kept


def classify_articles(articles: list[Article]) -> list[dict]:
    """주어진 조항들을 순차적으로 GPT 분석한다 (필터링 없음).

    호출부에서 후보만 넘겨야 한다. 전체 조항을 후보 선별 없이
    GPT로 보내려면 이 함수를, 후보만 자동 선별하려면
    classify_candidates()를 사용한다.

    조항마다 진행상황을 출력하며, 타임아웃 등 오류가 난 조항은
    건너뛰고(빈 결과 + error) 계속 진행한다.
    """
    client = _get_client()
    total = len(articles)
    results = []
    for idx, art in enumerate(articles, start=1):
        print(
            f"[{idx}/{total}] 제{art.number}조 '{art.title}' 처리 중...",
            file=sys.stderr,
        )
        result = classify_with_gpt(art, client=client)
        if result.get("error"):
            print(
                f"      → 건너뜀 (오류: {result['error']})",
                file=sys.stderr,
            )
        results.append(result)
    return results


if __name__ == "__main__":
    from .pdf_extractor import extract_articles

    if len(sys.argv) < 2:
        print("사용법: python -m parser.gpt_classifier <pdf_경로>")
        sys.exit(1)

    arts = extract_articles(sys.argv[1])
    for result in classify_candidates(arts):
        print(json.dumps(result, ensure_ascii=False, indent=2))
