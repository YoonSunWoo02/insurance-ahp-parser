"""가입제안서 PDF에 약관용 보장·독소조항 파싱을 적용한다.

가입제안서는 "제N조" 조항 구조가 없어서 약관 파이프라인(조항 단위)에 그대로 넣으면
조항이 0개로 나온다. 대신 보장내용(지급사유)·유의사항이 페이지 단위로 실려 있으므로
**페이지 한 장을 조항 하나처럼** 넘긴다. 결과의 number는 조 번호가 아니라 쪽 번호다.

사용법:
    python run_proposal_terms.py "data/raw_pdfs/제안서.pdf"
"""

import json
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pdfplumber

from parser.amount_structurer import structure_all_amounts
from parser.gpt_classifier import classify_candidates
from parser.pdf_extractor import Article
from parser.rule_extractor import filter_candidates, select_candidate_articles
from postprocess import postprocess
from toxic_detector import detect_toxic_clauses, summarize
from validator.verifier import verify_all


def pages_as_articles(pdf_path: str) -> list[Article]:
    articles = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                articles.append(Article(number=i, title=f"{i}쪽", body=text, raw_header=""))
    return articles


# 보장내용(지급사유) 표가 있는 페이지 신호. 담보별 보험료 표만 있는 페이지는
# _premium.json과 내용이 겹치고 지급조건이 없어 보장 파싱 대상에서 뺀다.
_BENEFIT_PAGE = re.compile(r"지급사유|보장내용|보장내역|보장상세|지급금액")


def _premium_values(path: Path) -> set[int]:
    """같은 제안서의 _premium.json에 있는 담보별 보험료 값들."""
    prem = Path("data/parsed") / f"{path.stem}_premium.json"
    if not prem.exists():
        return set()
    data = json.loads(prem.read_text(encoding="utf-8"))
    return {c["premium"] for c in data.get("coverages", []) if isinstance(c.get("premium"), int)}


def _clear_premium_as_amount(verified: list[dict], premiums: set[int]) -> int:
    """보장내용 표의 '보험료' 칸을 가입금액으로 잘못 읽은 값(원 단위, 만원 미만 표기)을 지운다."""
    cleared = 0
    for article in verified:
        for cov in article.get("coverages", []):
            raw = str(cov.get("amount") or "")
            if re.fullmatch(r"[\d,]+\s*원", raw) and int(re.sub(r"[^\d]", "", raw)) in premiums:
                cov["amount"] = None
                cleared += 1
    return cleared


def run(pdf_path: str) -> tuple[dict, dict]:
    path = Path(pdf_path)
    articles = pages_as_articles(str(path))
    candidates = filter_candidates(articles)
    coverage_articles = [a for a in select_candidate_articles(articles) if _BENEFIT_PAGE.search(a.body)]
    print(f"[제안서 페이지 단위] {path.name}: {len(articles)}쪽, 보장 후보 {len(coverage_articles)}쪽", file=sys.stderr)

    gpt_results = classify_candidates(coverage_articles)
    # GPT가 보장 표가 있는 페이지를 통째로 빈 결과로 돌려주는 경우가 있어 한 번 더 시도한다.
    empty = [r["_article"] for r in gpt_results if not r.get("coverages") and not r.get("error")]
    if empty:
        retried = {id(r["_article"]): r for r in classify_candidates(empty)}
        gpt_results = [retried.get(id(r["_article"]), r) if not r.get("coverages") else r for r in gpt_results]
    verified = verify_all(gpt_results)
    cleared = _clear_premium_as_amount(verified, _premium_values(path))
    print(f"      → 보험료를 금액으로 읽은 값 제거: {cleared}건", file=sys.stderr)
    postprocess(verified)
    structure_all_amounts(verified)
    result = {
        "source": path.name,
        "unit": "page",
        "total_articles": len(articles),
        "candidate_articles": len(candidates),
        "coverage_filtered_articles": len(coverage_articles),
        "extracted_coverages": sum(len(v["coverages"]) for v in verified),
        "results": verified,
    }

    toxic_clauses, stats = detect_toxic_clauses(articles)
    toxic = {
        "source": path.name,
        "unit": "page",
        "total_articles": len(articles),
        "toxic_summary": summarize(toxic_clauses, stats),
        "toxic_clauses": toxic_clauses,
    }
    return result, toxic


def main() -> int:
    pdf_path = sys.argv[1]
    result, toxic = run(pdf_path)
    out_dir = Path("data/parsed")
    stem = Path(pdf_path).stem
    for suffix, data in (("_result.json", result), ("_toxic.json", toxic)):
        out = out_dir / f"{stem}{suffix}"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"결과 저장: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
