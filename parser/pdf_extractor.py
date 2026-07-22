"""PDF 텍스트/조항 추출 모듈.

pdfplumber로 보험 약관 PDF에서 텍스트를 추출하고, 조항(article) 단위로 분리한다.
보험사마다 조항 헤더 표기가 달라("제N조(제목)" 또는 "N.(제목)") 두 형식을 모두
인식하며, 여러 페이지에 반복되는 머리말/꼬리말(목차 바로가기, 워터마크, 쪽번호,
2단 레이아웃 세로탭 잔재)도 문서 전체를 스캔해 자동으로 걸러낸다.
"""

import re
from dataclasses import dataclass, field

import pdfplumber


# 제목 괄호 캡처: "(효력회복)"처럼 괄호가 1단 중첩된 제목도 온전히 잡는다.
#   예) "제26조(...계약의 부활(효력회복))" → title = "...계약의 부활(효력회복)"
# 단순 "[^)]*"는 첫 ")"에서 멈춰 중첩 제목을 잘라먹고 남은 ")"가 본문에 새어 들어간다.
#
# 줄바꿈은 절대 건너뛰지 않는다([^()\n]). 조 제목은 항상 한 줄 안에 있으므로,
# 줄바꿈을 허용하면 2단 목차(다른 컬럼의 "제N조(...)" 조각)가 중간에 끼어들어와
# 하나의 거대한 가짜 제목으로 합쳐지는 사고가 난다. 실측(삼성화재/참편한 약관)에서
# "강제집행 등으로 인하여 해지된 계약의\n제8조 (보험금 지급사유 발생의 통지) 29\n
# 특별부활(효력회복)"처럼 서로 다른 목차 항목 두 개가 한 제목으로 뒤섞이는 사고를
# 실제로 확인했다 — 중첩 괄호 허용 폭이 넓어지면서 생긴 부작용이었다.
_TITLE_GROUP = r"\(((?:[^()\n]|\([^()\n]*\))*)\)"

# "제 12 조(보험금의 지급)" 같은 조항 헤더 패턴.
# 두 가지 조건을 모두 만족할 때만 조항 헤더로 인정한다.
#   1) 줄 시작(앞 공백 허용)에 위치  → 본문 속 상호참조 제외
#   2) 괄호 제목 "(...)"이 뒤따름     → 제목 없는 단순 언급 제외
# 이렇게 좁혀야 "제3조의 규정에 따라..." 같은 참조나
# 표/목차 속 숫자가 새 조항으로 잘못 분리되지 않는다.
ARTICLE_PATTERN = re.compile(
    rf"^[ \t]*제\s*(\d+)\s*조(?:\s*의\s*\d+)?\s*{_TITLE_GROUP}",
    re.MULTILINE,
)

# 일부 보험사(예: 무배당 프로미라이프 간편실손의료비보험)는 조항 헤더에
# "제N조" 대신 "N. (제목)"처럼 번호+마침표+괄호 제목만 쓴다.
# ARTICLE_PATTERN이 매칭을 거의 못 찾을 때(문서 전체가 이 형식일 때) 대신 사용한다.
# 조건은 ARTICLE_PATTERN과 동일(줄 시작 + 괄호 제목 즉시 뒤따름)하게 좁혀
# 본문 중간의 번호 매긴 목록 항목("1. 피보험자가 고의로...")이 오매칭되지 않게 한다.
ALT_ARTICLE_PATTERN = re.compile(
    rf"^[ \t]*(\d{{1,3}})\.\s*{_TITLE_GROUP}",
    re.MULTILINE,
)

# 부록(별표/부록/붙임/별지) 섹션 시작 표시.
# 줄머리에 여는 낫표/대괄호 + '별표'류가 오면 그 지점부터는 조항 본문이 아니라
# 뒤에 붙은 부록 표(용어정의표·질병분류표·적립이율표 등)로 본다.
#   예) "【별표1】 용어의 정의", "[별표3] 설명사항", "[붙임2] ..."
# 본문 속 인라인 참조("(【별표3】 참조)", "제1항([별표2] 비급여대상)")는 줄머리가
# 아니라 괄호/문장 중간이므로 매칭되지 않는다 → 정당한 면책 목록 등은 보존된다.
APPENDIX_PATTERN = re.compile(
    r"^[ \t]*[\[【]\s*(?:별\s*표|부록|붙\s*임|별지)",
    re.MULTILINE,
)


def _strip_appendix(body: str) -> str:
    """조항 본문 뒤에 흡수된 별표/부록 표를 잘라낸다.

    약관 본문의 마지막 조항(예: '예금보험에 의한 지급보장')은 바로 뒤에 오는 별표
    (제N조 헤더가 없는 부록 표)를 통째로 본문에 흡수해, 제목과 무관한 내용(질병분류표
    등)이 섞인다. 줄머리 [별표N]/【별표N】/부록/붙임/별지를 부록 시작으로 보고 그 앞까지만
    남긴다. 부록 마커가 없으면 원본을 그대로 반환한다.
    """
    m = APPENDIX_PATTERN.search(body)
    return body[: m.start()].rstrip() if m else body


@dataclass
class Article:
    """약관 조항 1개 단위."""

    number: int          # 조 번호 (제N조의 N)
    title: str           # 조 제목 (괄호 안 내용)
    body: str            # 조항 본문 전체 텍스트
    raw_header: str = ""  # 매칭된 헤더 원문 (예: "제12조(보험금의 지급)")

    def full_text(self) -> str:
        """헤더 + 본문 전체 원문 (검증 대조용)."""
        return f"{self.raw_header}\n{self.body}".strip()


# 페이지 하단 쪽번호 표기 ("- 61 -" 류). 어느 보험사 PDF에나 나오는 보편적 패턴이라
# 특정 문구 하드코딩 없이 정규식만으로 안전하게 제거할 수 있다.
_PAGE_NUMBER_LINE = re.compile(r"^[ \t]*-\s*\d{1,4}\s*-[ \t]*$")

# 2단 레이아웃 약관 PDF는 페이지 옆면의 세로 탭 글자("약관", "요약서" 등)가
# pdfplumber에 의해 한 글자씩 한 줄로 쪼개져 본문 중간에 끼어드는 경우가 많다.
# ("약\n관\n...요\n약\n서\n" 처럼) 한글 단어는 정상적으로 한 글자짜리 줄을
# 이루지 않으므로, 줄 전체가 한글 한 글자뿐이면 세로 탭 잔재로 보고 제거한다.
_LONE_HANGUL_CHAR_LINE = re.compile(r"^[ \t]*[가-힣][ \t]*$")


def _extract_pages(pdf_path: str) -> list[str]:
    """PDF의 페이지별 원문 텍스트 목록을 반환한다."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def _strip_running_boilerplate(pages: list[str]) -> list[str]:
    """여러 페이지에 반복되는 머리말/꼬리말(목차 바로가기, 상품명 워터마크 등)을 제거한다.

    특정 보험사 문구를 하드코딩하지 않고, 페이지의 상당수에서 글자 그대로
    반복되는 줄을 "반복 머리말/꼬리말"로 판단해 제거하는 범용 방식이다.
    페이지 수가 적은 문서(4페이지 미만)는 반복 여부를 신뢰하기 어려우므로 건너뛴다.
    """
    if len(pages) < 4:
        return pages

    from collections import Counter

    page_lines = [p.split("\n") for p in pages]
    counts: Counter = Counter()
    for lines in page_lines:
        for line in {l.strip() for l in lines if l.strip()}:
            # 조항 헤더처럼 보이는 줄은 절대 보일러플레이트로 취급하지 않는다.
            # 문서에 리더(rider)가 많으면 "제4조(준용규정)"처럼 여러 특약이 똑같은
            # 조항을 반복해서 쓸 수 있는데, 이는 우연한 반복이 아니라 각기 다른 진짜
            # 조항 헤더이므로 지워지면 조항 분리 자체가 깨진다.
            if ARTICLE_PATTERN.match(line) or ALT_ARTICLE_PATTERN.match(line):
                continue
            counts[line] += 1

    # 페이지의 1/4 이상에 똑같이 등장하는 줄만 반복 요소로 간주한다(최소 6페이지).
    # ("☞ 목차로 돌아가기" 같은 러닝헤더는 표지/목차/부록 페이지엔 없어 100%가 아니라
    # 40% 안팎으로만 등장하기도 한다.) 여러 단어로 된 문장이 우연히 여러 페이지에
    # 글자 그대로 반복될 확률은 극히 낮으므로, 이 임계값에서도 오삭제 위험은 낮다.
    threshold = max(6, int(len(pages) * 0.25))
    boilerplate = {line for line, c in counts.items() if c >= threshold}
    if not boilerplate:
        return pages

    return [
        "\n".join(l for l in lines if l.strip() not in boilerplate)
        for lines in page_lines
    ]


def extract_text(pdf_path: str) -> str:
    """PDF 전체 페이지의 텍스트를 하나의 문자열로 추출한다.

    페이지 간 반복되는 머리말/꼬리말(목차 바로가기 링크, 상품명 워터마크 등)을
    제거한 뒤 페이지를 합친다.
    """
    pages = _strip_running_boilerplate(_extract_pages(pdf_path))
    return "\n".join(pages)


def clean_text(text: str) -> str:
    """추출 텍스트 정리: 페이지번호/세로탭 잔재 제거 및 과도한 공백 정돈."""
    lines = text.split("\n")
    lines = [
        l for l in lines
        if not _PAGE_NUMBER_LINE.match(l) and not _LONE_HANGUL_CHAR_LINE.match(l)
    ]
    text = "\n".join(lines)
    # 줄 끝 공백 제거
    text = re.sub(r"[ \t]+\n", "\n", text)
    # 3줄 이상 연속 빈 줄 → 2줄
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _select_header_matches(text: str) -> list[re.Match]:
    """ARTICLE_PATTERN과 ALT_ARTICLE_PATTERN 매칭 결과 중 실제 조항 헤더로 쓸 것을 고른다.

    세 가지 경우를 구분한다:
      1) alt가 primary보다 많다
         → 프로미라이프처럼 문서 전체가 "N.(제목)" 형식만 쓰는 경우. alt를 채택한다.
      2) primary가 충분히 많은데(>=5) alt는 소수(개수 기준 <5 이면서 primary의 20% 미만)
         → alt 매칭이 "1.(생략) 2.(생략)" 같은 예시문 등 우연한 노이즈일 가능성이 높다.
           (실제로 삼성화재 약관에서 이런 노이즈 2건이 발견됨) primary만 사용한다.
      3) 그 외(둘 다 적거나 비슷한 규모) → 두 형식이 섞여 쓰였을 수 있으므로,
         primary와 겹치지 않는 alt 매칭만 추가로 병합한다.
    """
    primary = list(ARTICLE_PATTERN.finditer(text))
    alt = list(ALT_ARTICLE_PATTERN.finditer(text))

    if len(alt) > len(primary):
        return alt
    if len(primary) >= 5 and len(alt) < max(5, len(primary) * 0.2):
        return primary

    occupied = [(m.start(), m.end()) for m in primary]

    def _overlaps(m: re.Match) -> bool:
        return any(not (m.end() <= s or m.start() >= e) for s, e in occupied)

    merged = primary + [m for m in alt if not _overlaps(m)]
    merged.sort(key=lambda m: m.start())
    return merged


def split_articles(text: str) -> list[Article]:
    """텍스트를 조항 단위로 분리해 Article 리스트로 반환한다.

    조항 헤더 위치를 모두 찾은 뒤, 헤더 사이 구간을 본문으로 묶는다.
    첫 헤더 앞부분(표지/목차 등)은 버린다.

    헤더 형식은 "제N조(제목)"(ARTICLE_PATTERN)이 기본이지만, 보험사마다
    "N.(제목)"(ALT_ARTICLE_PATTERN) 형식만 쓰는 경우도 있어 _select_header_matches가
    문서 특성에 맞는 형식을 고르거나 필요 시 섞어 쓴다.
    """
    matches = _select_header_matches(text)
    if not matches:
        return []

    articles: list[Article] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        number = int(m.group(1))
        title = (m.group(2) or "").strip()
        raw_header = m.group(0).strip()
        # 본문 = 헤더 끝 ~ 다음 헤더 시작 (뒤에 흡수된 별표/부록 표는 제거)
        body = _strip_appendix(text[m.end():end].strip())

        articles.append(
            Article(
                number=number,
                title=title,
                body=body,
                raw_header=raw_header,
            )
        )
    return articles


def extract_articles(pdf_path: str) -> list[Article]:
    """PDF 경로를 받아 텍스트 추출 → 정리 → 조항 분리까지 수행한다."""
    raw = extract_text(pdf_path)
    cleaned = clean_text(raw)
    return split_articles(cleaned)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("사용법: python -m parser.pdf_extractor <pdf_경로>")
        sys.exit(1)

    arts = extract_articles(sys.argv[1])
    print(f"총 {len(arts)}개 조항 추출")
    for a in arts[:5]:
        print(f"  제{a.number}조 {a.title} ({len(a.body)}자)")
