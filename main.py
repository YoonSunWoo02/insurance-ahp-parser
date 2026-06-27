"""보험 약관 PDF 파싱 파이프라인 진입점.

사용법:
    python main.py data/raw_pdfs/약관.pdf
    python main.py data/raw_pdfs/약관.pdf --out result.json
    python main.py data/raw_pdfs/약관.pdf --no-gpt   # GPT 없이 규칙 기반만

파이프라인:
    1. pdfplumber로 텍스트 추출 → 제N조 단위 분리
    2. 정규식으로 보장/금액 후보 조항 1차 분류
    3. GPT로 보장명/금액/지급조건/주계약·특약 구분 JSON 추출
    4. 원문 대조 검증 → 신뢰도 점수(0~100) 부여
"""

import argparse
import json
import sys
from pathlib import Path

# .env 자동 로드 (있으면)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from parser.pdf_extractor import extract_articles
from parser.rule_extractor import filter_candidates, select_candidate_articles
from parser.gpt_classifier import classify_candidates, filter_by_gpt
from validator.verifier import verify_all
from toxic_detector import detect_toxic_clauses, summarize as summarize_toxic


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="보험 약관 PDF에서 보장 정보를 추출/검증한다."
    )
    p.add_argument("pdf", help="파싱할 약관 PDF 경로")
    p.add_argument(
        "--out",
        default=None,
        help="결과 JSON 저장 경로 (생략 시 'data/parsed/PDF이름_result.json'으로 저장)",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="파일로 저장하지 않고 결과를 stdout으로 출력",
    )
    p.add_argument(
        "--no-gpt",
        action="store_true",
        help="GPT 분석을 생략하고 규칙 기반 1차 분류 결과만 출력",
    )
    p.add_argument(
        "--toxic",
        action="store_true",
        help="보장 추출과 함께 독소조항 탐지도 실행",
    )
    p.add_argument(
        "--toxic-only",
        action="store_true",
        help="보장 추출 없이 독소조항 탐지만 실행",
    )
    return p.parse_args(argv)


def _run_toxic(articles, result: dict) -> None:
    """독소조항 탐지를 실행해 result에 toxic_summary/toxic_clauses를 추가한다."""
    print("[독소조항 탐지] 키워드 후보 선별 + GPT 정밀 판단", file=sys.stderr)
    toxic, filter_stats = detect_toxic_clauses(articles)
    result["toxic_summary"] = summarize_toxic(toxic, filter_stats)
    result["toxic_clauses"] = toxic


def run(
    pdf_path: str,
    use_gpt: bool = True,
    detect_toxic: bool = False,
    toxic_only: bool = False,
) -> dict:
    """전체 파이프라인을 실행하고 결과 dict를 반환한다."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    # 1) 추출 + 조항 분리
    print(f"[1/5] PDF 텍스트 추출 및 조항 분리: {path.name}", file=sys.stderr)
    articles = extract_articles(str(path))
    print(f"      → {len(articles)}개 조항", file=sys.stderr)

    # 독소조항만 탐지하는 경우: 보장 추출은 건너뛴다
    if toxic_only:
        result = {"source": path.name, "total_articles": len(articles)}
        _run_toxic(articles, result)
        return result

    # 2) 규칙 기반 1차 분류 (완화된 조건: 보장 키워드 OR 금액)
    print("[2/5] 정규식 1차 분류 (보장 키워드 OR 금액 후보 선별)", file=sys.stderr)
    candidates = filter_candidates(articles)
    print(f"      → 후보 {len(candidates)}개 조항", file=sys.stderr)

    if not use_gpt:
        result = {
            "source": path.name,
            "total_articles": len(articles),
            "candidates": candidates,
        }
        if detect_toxic:
            _run_toxic(articles, result)
        return result

    # 3) GPT 1차 판단 (규칙 후보 중 '보장 관련' 조항만 빠르게 선별)
    print("[3/5] GPT 1차 판단 (보장 관련 조항 선별)", file=sys.stderr)
    rule_candidates = select_candidate_articles(articles)
    coverage_articles = filter_by_gpt(rule_candidates)

    # 4) GPT 2차 상세 추출 (선별된 조항만 — classify_candidates는 그대로 사용)
    print("[4/5] GPT 보장 정보 상세 추출", file=sys.stderr)
    gpt_results = classify_candidates(coverage_articles)

    # 5) 원문 대조 검증 (각 결과에 동봉된 원본 조항과 1:1 대조)
    print("[5/5] 원문 대조 검증 및 신뢰도 점수 부여", file=sys.stderr)
    verified = verify_all(gpt_results)

    total_cov = sum(len(v["coverages"]) for v in verified)
    result = {
        "source": path.name,
        "total_articles": len(articles),
        "candidate_articles": len(candidates),
        "coverage_filtered_articles": len(coverage_articles),
        "extracted_coverages": total_cov,
        "results": verified,
    }
    if detect_toxic:
        _run_toxic(articles, result)
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = run(
            args.pdf,
            use_gpt=not args.no_gpt,
            detect_toxic=args.toxic,
            toxic_only=args.toxic_only,
        )
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    if args.stdout:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # 기본 저장 폴더: data/parsed/ (없으면 자동 생성)
    parsed_dir = Path("data/parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(args.pdf).stem

    # 독소조항은 별도 파일(PDF이름_toxic.json)로 분리 저장
    toxic_keys = ("toxic_summary", "toxic_clauses")
    has_toxic = any(k in result for k in toxic_keys)

    def _save(path: Path, data: dict) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"결과 저장: {path}", file=sys.stderr)

    if args.toxic_only:
        # 독소조항만: _toxic.json 하나만 저장 (--out 지정 시 그 경로 사용)
        toxic_path = Path(args.out) if args.out else parsed_dir / f"{stem}_toxic.json"
        _save(toxic_path, result)
        return 0

    # 보장 결과(_result.json): 독소조항 키는 제외하고 저장
    coverage = {k: v for k, v in result.items() if k not in toxic_keys}
    result_path = Path(args.out) if args.out else parsed_dir / f"{stem}_result.json"
    _save(result_path, coverage)

    # 독소조항이 있으면 같은 폴더에 _toxic.json으로 분리 저장
    if has_toxic:
        toxic_data = {
            "source": result.get("source"),
            "total_articles": result.get("total_articles"),
            "toxic_summary": result.get("toxic_summary"),
            "toxic_clauses": result.get("toxic_clauses"),
        }
        toxic_path = result_path.parent / f"{stem}_toxic.json"
        _save(toxic_path, toxic_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
