"""PDF 텍스트/조항 추출 모듈.

pdfplumber로 보험 약관 PDF에서 텍스트를 추출하고,
"제N조" 단위로 조항(article)을 분리한다.
"""

import re
from dataclasses import dataclass, field

import pdfplumber


# "제 12 조(보험금의 지급)" 같은 조항 헤더 패턴.
# 두 가지 조건을 모두 만족할 때만 조항 헤더로 인정한다.
#   1) 줄 시작(앞 공백 허용)에 위치  → 본문 속 상호참조 제외
#   2) 괄호 제목 "(...)"이 뒤따름     → 제목 없는 단순 언급 제외
# 이렇게 좁혀야 "제3조의 규정에 따라..." 같은 참조나
# 표/목차 속 숫자가 새 조항으로 잘못 분리되지 않는다.
ARTICLE_PATTERN = re.compile(
    r"^[ \t]*제\s*(\d+)\s*조(?:\s*의\s*\d+)?\s*\(([^)]*)\)",
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


def extract_text(pdf_path: str) -> str:
    """PDF 전체 페이지의 텍스트를 하나의 문자열로 추출한다."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n".join(pages)


def clean_text(text: str) -> str:
    """추출 텍스트 정리: 과도한 공백/페이지 번호성 빈 줄 정돈."""
    # 줄 끝 공백 제거
    text = re.sub(r"[ \t]+\n", "\n", text)
    # 3줄 이상 연속 빈 줄 → 2줄
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_articles(text: str) -> list[Article]:
    """텍스트를 "제N조" 단위로 분리해 Article 리스트로 반환한다.

    조항 헤더 위치를 모두 찾은 뒤, 헤더 사이 구간을 본문으로 묶는다.
    첫 헤더 앞부분(표지/목차 등)은 버린다.
    """
    matches = list(ARTICLE_PATTERN.finditer(text))
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
