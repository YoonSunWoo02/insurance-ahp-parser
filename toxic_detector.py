"""독소조항(계약자에게 불리한 조항) 탐지 모듈.

2단계로 동작한다.
    1단계 - 키워드 기반 후보 선별 (비용 없음):
        면책/지급제외·제한조건·감액/삭감·기간제한·고지/통지 의무 관련
        키워드가 포함된 조항만 후보로 추린다. 노이즈 제목은 제외.
    2단계 - GPT 정밀 판단:
        후보 조항만 GPT에 넘겨 실제 독소조항 여부를 판단한다.
        독소조항이 없는 조항은 결과에서 제외한다.

GPT 모델/클라이언트는 parser/gpt_classifier.py의 설정을 재사용한다.

메인 함수: detect_toxic_clauses(articles)
    - extract_articles() 반환값(list[Article])을 그대로 받음
    - 독소조항이 1개 이상 발견된 조항만 리스트로 반환
"""

import json
import sys

from parser.pdf_extractor import Article
from parser.gpt_classifier import (
    MODEL,
    REQUEST_TIMEOUT,
    SYSTEM_PROMPT,
    _get_client,
)
from validator.verifier import _normalize as _norm_text, _contains as _in_source


# ---------------------------------------------------------------------------
# 1단계: 키워드 사전
# ---------------------------------------------------------------------------
# 카테고리별 독소조항 후보 키워드. 하나라도 포함되면 후보로 선별한다.
TOXIC_KEYWORDS: dict[str, list[str]] = {
    "면책/지급제외": [
        "지급하지 않", "보상하지 않", "면책", "제외한다",
        "해당하지 않", "적용하지 않", "보험금을 드리지 않", "보장하지 않",
    ],
    "제한조건": [
        "다만,", "단,", "단서", "경우에 한하여", "경우에만",
        "한도 내에서", "초과하는 경우", "미만인 경우",
    ],
    "감액/삭감": [
        "감액", "삭감", "차감", "공제", "본인부담", "자기부담",
    ],
    "기간제한": [
        "면책기간", "대기기간", "감액기간", "이내 발생", "경과 후",
        "이후부터", "계약일로부터", "부활일로부터",
    ],
    "고지/통지의무": [
        "알릴 의무", "고지의무", "통지의무", "위반한 경우",
        "사실과 다른 경우", "직업 변경", "직무 변경",
    ],
}

# 독소조항 탐지에서 제외할 노이즈 조항 제목
NOISE_TITLE_KEYWORDS = [
    "용어의 정의", "예금자보호", "준용규정", "관할법원",
    "분쟁조정", "개인신용정보", "주소변경", "계약의 성립",
]

# 본문이 이 길이 미만이면 목차(TOC) stub 등으로 보고 후보에서 제외한다.
# (약관 PDF는 목차 항목도 "제N조(제목)" 형태라 헤더로 잡히지만 본문이 거의 없음)
MIN_BODY_LENGTH = 50

# source_quote가 이 길이 미만이면 근거가 빈약한 것으로 보고 신뢰 불가 처리한다.
MIN_QUOTE_LENGTH = 15

# 조 번호가 이 값을 넘으면 상품 조항이 아니라 '인용된 법령 조문'이 헤더로 잘못 분리된
# 파싱 아티팩트로 본다. 소비자 보험약관 본문 조 번호는 (특약별로 재시작하므로) 사실상
# 제99조를 넘지 않는다. 3자리 조 번호(예: 상법 제657조, 제663조)는 약관이 본문에 인용한
# 법령 조문이 "제657조(보험사고발생의 통지의무)"처럼 조항 헤더로 오인식된 경우다.
MAX_PLAUSIBLE_ARTICLE_NO = 99


def _is_noise_title(title: str) -> bool:
    """독소조항과 무관한 노이즈 제목인지."""
    return any(kw in title for kw in NOISE_TITLE_KEYWORDS)


def matched_keywords(text: str) -> list[str]:
    """텍스트에 포함된 독소조항 키워드 목록을 반환한다."""
    found: list[str] = []
    for keywords in TOXIC_KEYWORDS.values():
        for kw in keywords:
            if kw in text:
                found.append(kw)
    return found


def is_candidate(article: Article) -> bool:
    """조항이 독소조항 후보인지 (키워드 포함 & 노이즈 제목·목차 stub·비정상 조번호 아님)."""
    if article.number > MAX_PLAUSIBLE_ARTICLE_NO:
        # 인용된 법령 조문(상법 제657조 등)이 조항 헤더로 잘못 분리된 파싱 아티팩트 → 제외
        return False
    if _is_noise_title(article.title):
        return False
    if len(article.body) < MIN_BODY_LENGTH:
        return False
    return bool(matched_keywords(article.full_text()))


# ---------------------------------------------------------------------------
# 2단계: GPT 정밀 판단
# ---------------------------------------------------------------------------
USER_TEMPLATE = """다음 보험 약관 조항에서 계약자(가입자)에게 불리하게 작용할 수 있는
독소조항을 찾아라. 면책/지급제외, 제한조건, 감액/삭감, 기간제한,
고지·통지 의무 위반의 효과 등 계약자에게 불리한 내용만 추출한다.

불리한 내용이 없으면 빈 배열로 답한다.
원문에 없는 내용은 절대 지어내지 말고, source_quote는 원문을 그대로 인용한다.

매우 중요 — 예외(단서) 조건도 함께 찾아라:
"보상하지 않는 사항"류 조항은 "...한 경우. 다만, ~~인 경우에는 보상합니다"처럼
원칙적으로 불리한 내용(면책) 뒤에 "다만," "단," 등으로 이어지는 예외 문구가 붙어
그 불리함을 일부 되돌리는 경우가 많다. 이런 예외 문구가 있으면 절대 무시하거나
source_quote에만 섞어 넣지 말고, exception과 exception_quote 필드에 별도로 채워라.
  - exception: 예외 조건이 원칙(독소조항)을 어떻게 바꾸는지 한 줄 요약
    (예: "심신상실 등으로 자유로운 의사결정이 불가능했음이 증명되면 보상함")
  - exception_quote: 그 예외 문구의 원문 그대로 인용 (반드시 조항 원문에 실제로 있는 문장)

예외 문구를 찾을 때 다음 두 가지 함정에 빠지지 마라:
  1) 예외는 마침표(.) 뒤에 "다만,"으로 새 문장이 시작하는 형태만 있는 게 아니다.
     "비급여 주사료[다만, 항암제, 항생제(항진균제 포함), 희귀의약품은 보상합니다]"
     처럼 명사구 바로 뒤에 대괄호"[...]"나 괄호"(...)"로 붙어있는 형태도 있다.
     대괄호/괄호 안에 "다만,"/"단," 이 있으면 그 괄호 안 내용 전체를 exception으로
     분리해서 추출하고, source_quote에는 괄호를 뺀 본체 조항만 남겨라.
  2) 예외 = "계약자에게 유리한 혜택(보상해줌)"이라고만 생각하지 마라. 원칙의 효과를
     조금이라도 바꾸는 단서라면 계약자에게 불리하거나 중립적인 내용(예: "나이가
     이미 찼으면 계약을 무효로 하지 않고 유효로 본다" 같은 행정적 처리 변경)도
     전부 예외로 인정한다. "보상함/지급함"이라는 표현이 없다고 예외를 건너뛰지 마라.
  3) exception_quote는 그 예외 문장이 끝나는 지점(마침표 또는 다음 항목 번호가
     나오기 전)까지 전부 인용해라. 문장을 중간에서 스스로 끊지 마라 — 특히 문장이
     "~하며 회사는" 처럼 애매하게 끝나 보여도, 원문에 이어지는 내용이 있으면 그
     이어지는 부분까지 포함해서 완전한 문장으로 인용해야 한다.

예외 문구가 전혀 없으면 exception과 exception_quote는 둘 다 null로 둔다.
지어내지 마라 — 원문에 "다만/단/except" 류의 예외 문구가 없는데 있는 것처럼 만들면 안 된다.

[조항 제목]
{title}

[조항 원문]
{body}

[출력 JSON 스키마]
{{
  "toxic_clauses": [
    {{
      "clause_summary": "독소조항 핵심 요약",
      "reason": "계약자에게 불리한 이유",
      "severity": "높음 | 중간 | 낮음",
      "source_quote": "근거가 된 원문 문장 (원문 그대로)",
      "exception": "예외(단서) 조건 요약. 없으면 null",
      "exception_quote": "예외 조건 원문 그대로 인용. 없으면 null"
    }}
  ]
}}

반드시 위 스키마의 JSON만 출력하라."""


def detect_in_article(article: Article, client=None) -> list[dict]:
    """조항 1개를 GPT로 분석해 독소조항 목록을 반환한다.

    독소조항이 없으면 빈 리스트. API/파싱 오류 시에도 빈 리스트를 반환하고
    경고를 stderr로 남긴다(해당 조항만 건너뜀).
    """
    client = client or _get_client()
    user_msg = USER_TEMPLATE.format(
        title=article.title or "(제목 없음)",
        body=article.full_text(),
    )

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
        return data.get("toxic_clauses", []) or []
    except Exception as e:  # API 오류/JSON 파싱 오류 모두 포착
        print(f"      → 건너뜀 (오류: {e})", file=sys.stderr)
        return []


def _clear_hallucinated_exception(clause: dict, src_norm: str) -> bool:
    """exception_quote가 원문에 없으면(환각) exception 필드를 null로 지운다.

    exception은 독소조항 본체와 달리 있으면 좋은 부가 정보이므로, 근거가
    빈약하다고 해서 독소조항 전체(clause)를 버리지 않고 exception 필드만
    무효화한다. exception이 애초에 없으면(둘 다 null) 손대지 않는다.

    반환: exception을 지웠으면 True.
    """
    quote = (clause.get("exception_quote") or "").strip()
    if not quote:
        # exception만 채우고 exception_quote를 비워둔 경우도 근거 불명이므로 함께 정리
        if clause.get("exception"):
            clause["exception"] = None
            clause["exception_quote"] = None
            return True
        return False
    if len(quote) < MIN_QUOTE_LENGTH or not _in_source(src_norm, quote):
        clause["exception"] = None
        clause["exception_quote"] = None
        return True
    return False


def _filter_clauses(clauses: list[dict], source_text: str) -> tuple[list[dict], int, int]:
    """GPT가 낸 독소조항을 후처리로 검증한다.

    독소조항 본체 제외 기준:
        1) source_quote가 MIN_QUOTE_LENGTH(15자) 미만 → 근거 빈약, 신뢰 불가
        2) source_quote가 원문에 실제로 없음 → 환각/오탐 (verifier 대조 로직 재사용)

    exception/exception_quote는 같은 기준으로 검증하되, 실패해도 독소조항
    본체는 살리고 exception 필드만 null로 지운다(_clear_hallucinated_exception).

    반환: (남은_clauses, 짧은인용_제외수, 원문불일치_제외수)
    """
    src_norm = _norm_text(source_text)
    kept: list[dict] = []
    excluded_short = 0
    excluded_not_in_source = 0
    for c in clauses:
        quote = (c.get("source_quote") or "").strip()
        if len(quote) < MIN_QUOTE_LENGTH:
            excluded_short += 1
            continue
        if not _in_source(src_norm, quote):
            excluded_not_in_source += 1
            continue
        c.setdefault("exception", None)
        c.setdefault("exception_quote", None)
        _clear_hallucinated_exception(c, src_norm)
        kept.append(c)
    return kept, excluded_short, excluded_not_in_source


def _dedup_clauses(results: list[dict]) -> tuple[list[dict], int]:
    """여러 조항에 걸쳐 동일 원문(source_quote)을 인용한 중복 독소조항을 제거한다.

    같은 근거 문장이 여러 조항에서 반복 추출되면 DB에 같은 독소조항이 여러 행으로
    쌓인다. 정규화한 source_quote가 같으면 같은 독소조항으로 보고 **처음 것만** 남긴다.
    조항 순서·구조는 유지하고, 비게 된 조항은 결과에서 뺀다.

    반환: (중복 제거된 results, 제거 건수).
    """
    seen: set[str] = set()
    deduped: list[dict] = []
    removed = 0
    for art in results:
        kept = []
        for c in art.get("toxic_clauses", []):
            key = _norm_text(c.get("source_quote") or "")
            if key and key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(c)
        if kept:
            deduped.append({**art, "toxic_clauses": kept})
    return deduped, removed


def detect_toxic_clauses(articles: list[Article]) -> tuple[list[dict], dict]:
    """전체 조항 중 후보만 GPT로 판단해 독소조항이 있는 조항만 반환한다.

    GPT 결과는 후처리 필터(짧은 인용 제외 + 원문 대조)로 검증한다.

    반환: (results, filter_stats)
        results: [
          {
            "article_number": N,
            "article_title": "...",
            "toxic_clauses": [ {clause_summary, reason, severity, source_quote}, ... ]
          }, ...
        ]
        filter_stats: {"excluded_short_quote": x, "excluded_not_in_source": y}
    """
    candidates = [a for a in articles if is_candidate(a)]
    print(
        f"전체 {len(articles)}개 조항 중 독소조항 후보 {len(candidates)}개 GPT 판단",
        file=sys.stderr,
    )

    client = _get_client()
    total = len(candidates)
    results: list[dict] = []
    excluded_short_total = 0
    excluded_not_in_source_total = 0
    for idx, art in enumerate(candidates, start=1):
        print(
            f"[{idx}/{total}] 제{art.number}조 '{art.title}' 독소조항 판단 중...",
            file=sys.stderr,
        )
        raw = detect_in_article(art, client=client)
        if not raw:
            continue
        kept, ex_short, ex_src = _filter_clauses(raw, art.full_text())
        excluded_short_total += ex_short
        excluded_not_in_source_total += ex_src
        if kept:
            results.append(
                {
                    "article_number": art.number,
                    "article_title": art.title,
                    "toxic_clauses": kept,
                }
            )

    # 여러 조항에 걸친 동일 원문 중복 독소조항 제거 (구조적 정리)
    results, deduped_total = _dedup_clauses(results)

    filter_stats = {
        "excluded_short_quote": excluded_short_total,
        "excluded_not_in_source": excluded_not_in_source_total,
        "deduped": deduped_total,
    }
    print(
        f"      → 독소조항 발견 조항 {len(results)}개 "
        f"(필터 제외: 짧은인용 {excluded_short_total}건, "
        f"원문불일치 {excluded_not_in_source_total}건, 중복 {deduped_total}건)",
        file=sys.stderr,
    )
    return results, filter_stats


def summarize(toxic_results: list[dict], filter_stats: dict | None = None) -> dict:
    """탐지 결과 요약을 만든다 (총 독소조항 수, 영향 조항 수, 필터 제외 수)."""
    total = sum(len(r["toxic_clauses"]) for r in toxic_results)
    with_exception = sum(
        1
        for r in toxic_results
        for c in r["toxic_clauses"]
        if c.get("exception")
    )
    summary = {
        "total_toxic_clauses": total,
        "affected_articles": len(toxic_results),
        "with_exception": with_exception,
    }
    if filter_stats:
        summary["excluded_short_quote"] = filter_stats.get("excluded_short_quote", 0)
        summary["excluded_not_in_source"] = filter_stats.get(
            "excluded_not_in_source", 0
        )
        summary["deduped"] = filter_stats.get("deduped", 0)
        summary["total_excluded"] = (
            summary["excluded_short_quote"]
            + summary["excluded_not_in_source"]
            + summary["deduped"]
        )
    return summary


if __name__ == "__main__":
    from parser.pdf_extractor import extract_articles

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if len(sys.argv) < 2:
        print("사용법: python toxic_detector.py <pdf_경로>")
        sys.exit(1)

    arts = extract_articles(sys.argv[1])
    toxic, stats = detect_toxic_clauses(arts)
    print(
        json.dumps(
            {"toxic_summary": summarize(toxic, stats), "toxic_clauses": toxic},
            ensure_ascii=False,
            indent=2,
        )
    )
