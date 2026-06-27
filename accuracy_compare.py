"""GPT 단독(전체 텍스트) vs 우리 파이프라인 정확도 비교.

같은 PDF에 대해 두 방식의 보장 추출 결과를 뽑고, 동일한 원문(PDF 전체
텍스트)에 대조해 세 지표로 비교한다.

  - 필드 완결성(%): 추출된 보장들의 필수 필드가 얼마나 채워졌는가
  - 원문일치율(%): 추출값이 원문과 얼마나 일치하는가 (검증 신뢰도 평균)
  - 노이즈 비율(%): 추출된 보장 중 신뢰 불가/쓰레기로 판정된 비율

[A] GPT 단독  : PDF 전체 텍스트를 청크로 나눠 그대로 GPT에 던져 추출
                (규칙 필터·후보 선별 없음)
[B] 파이프라인: pdf_extractor → rule_extractor(후보 선별) → gpt_classifier

사용법:
    python accuracy_compare.py data/raw_pdfs/약관.pdf
    python accuracy_compare.py data/raw_pdfs/약관.pdf --max-chunks 5
"""

import argparse
import json
import sys
from pathlib import Path

# .env 자동 로드
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from parser.pdf_extractor import extract_text, extract_articles
from parser.rule_extractor import (
    is_noise_amount,
    is_noise_title,
    select_candidate_articles,
)
from parser.gpt_classifier import (
    MODEL,
    REQUEST_TIMEOUT,
    SYSTEM_PROMPT,
    _get_client,
    classify_candidates,
    filter_by_gpt,
)
from validator.verifier import verify_coverage


# 보장 1건이 갖춰야 할 필수 필드 (실손보험 항목 포함)
REQUIRED_FIELDS = [
    "coverage_name",
    "amount",
    "payment_condition",
    "contract_type",
    "coverage_type",
    "benefit_type",
    "self_payment_ratio",
    "source_quote",
]

# 노이즈/원문일치 판정 임계 신뢰도
NOISE_CONFIDENCE_THRESHOLD = 50

# GPT 단독 방식의 입력 청크 크기(문자)
CHUNK_SIZE = 8000

BASELINE_USER_TEMPLATE = """다음은 보험 약관 본문 일부다. 등장하는 모든 보장 정보를 추출하라.

[본문]
{text}

[출력 JSON 스키마]
{{
  "coverages": [
    {{
      "coverage_name": "보장명",
      "amount": "보장금액 원문 표기, 없으면 null",
      "payment_condition": "지급조건 요약",
      "contract_type": "주계약 | 특약 | 불명",
      "coverage_type": "입원 | 통원 | 수술 | 불명",
      "benefit_type": "급여 | 비급여 | 급여+비급여 | 불명",
      "self_payment_ratio": "본인부담금 비율 (예: 20%, 없음, 불명)",
      "source_quote": "근거가 된 원문 문장 일부 (원문 그대로)"
    }}
  ]
}}

반드시 위 스키마의 JSON만 출력하라."""


# ---------------------------------------------------------------------------
# [A] GPT 단독 추출
# ---------------------------------------------------------------------------
def _chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def extract_baseline(full_text: str, max_chunks: int | None = None) -> list[dict]:
    """PDF 전체 텍스트를 청크로 나눠 GPT에 그대로 던져 보장을 추출한다."""
    client = _get_client()
    chunks = _chunk_text(full_text)
    if max_chunks:
        chunks = chunks[:max_chunks]

    coverages: list[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"[A:GPT단독 {i}/{len(chunks)}] 청크 분석 중...", file=sys.stderr)
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": BASELINE_USER_TEMPLATE.format(text=chunk),
                    },
                ],
                temperature=0,
                response_format={"type": "json_object"},
                timeout=REQUEST_TIMEOUT,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            coverages.extend(data.get("coverages", []))
        except Exception as e:
            print(f"      → 건너뜀 (오류: {e})", file=sys.stderr)
    return coverages


# ---------------------------------------------------------------------------
# [B] 파이프라인 추출
# ---------------------------------------------------------------------------
def extract_pipeline(pdf_path: str) -> list[dict]:
    """우리 파이프라인으로 보장 목록을 추출·평탄화한다.

    규칙 후보(절차성 제목은 노이즈로 제거됨) → GPT 2차 상세 추출.
    보장 회수율(coverage 수)을 유지하면서, 노이즈는 규칙 단계의 절차성
    제목 필터(rule_extractor.NOISE_TITLE_KEYWORDS)로 줄인다.
    """
    articles = extract_articles(pdf_path)
    results = classify_candidates(articles)
    return [cov for r in results for cov in r.get("coverages", [])]


# ---------------------------------------------------------------------------
# 지표 계산 (두 방식 모두 동일한 full_text에 대조)
# ---------------------------------------------------------------------------
def _is_filled(value) -> bool:
    """필드가 의미 있게 채워졌는지 (None/빈문자/'null' 제외)."""
    if value is None:
        return False
    s = str(value).strip()
    return bool(s) and s.lower() != "null"


def field_completeness(coverages: list[dict]) -> float:
    """필드 완결성(%): 전체 (보장 × 필수필드) 중 채워진 비율."""
    if not coverages:
        return 0.0
    total = len(coverages) * len(REQUIRED_FIELDS)
    filled = sum(
        1 for c in coverages for f in REQUIRED_FIELDS if _is_filled(c.get(f))
    )
    return round(100 * filled / total, 1)


def source_match_rate(coverages: list[dict], source_text: str) -> float:
    """원문일치율(%): 각 보장의 원문 대조 신뢰도(0~100) 평균."""
    if not coverages:
        return 0.0
    scores = [verify_coverage(c, source_text)["confidence"] for c in coverages]
    return round(sum(scores) / len(scores), 1)


def noise_rate(coverages: list[dict], source_text: str) -> float:
    """노이즈 비율(%): 신뢰 불가/쓰레기로 판정된 보장의 비율.

    노이즈 조건(하나라도 해당):
      - 원문 대조 신뢰도 < 임계값
      - 보장명이 비어 있음
      - 보장명이 노이즈 제목(용어정의/예금보험/준용규정 등)
      - 금액이 노이즈 금액(000만원/100원 등)
    """
    if not coverages:
        return 0.0
    noisy = 0
    for c in coverages:
        conf = verify_coverage(c, source_text)["confidence"]
        name = (c.get("coverage_name") or "").strip()
        amount = c.get("amount")
        if (
            conf < NOISE_CONFIDENCE_THRESHOLD
            or not name
            or is_noise_title(name)
            or (amount is not None and is_noise_amount(str(amount)))
        ):
            noisy += 1
    return round(100 * noisy / len(coverages), 1)


def compute_metrics(coverages: list[dict], source_text: str) -> dict:
    return {
        "추출_보장_수": len(coverages),
        "필드_완결성(%)": field_completeness(coverages),
        "원문일치율(%)": source_match_rate(coverages, source_text),
        "노이즈_비율(%)": noise_rate(coverages, source_text),
    }


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def print_table(baseline: dict, pipeline: dict) -> None:
    rows = [
        ("추출 보장 수", "추출_보장_수", ""),
        ("필드 완결성", "필드_완결성(%)", "%"),
        ("원문일치율", "원문일치율(%)", "%"),
        ("노이즈 비율", "노이즈_비율(%)", "%"),
    ]
    print("\n" + "=" * 52, file=sys.stderr)
    print(f"{'지표':<14}{'GPT 단독':>12}{'파이프라인':>14}", file=sys.stderr)
    print("-" * 52, file=sys.stderr)
    for label, key, unit in rows:
        b = baseline[key]
        p = pipeline[key]
        print(f"{label:<14}{str(b) + unit:>12}{str(p) + unit:>14}", file=sys.stderr)
    print("=" * 52, file=sys.stderr)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GPT 단독 vs 파이프라인 추출 정확도 비교."
    )
    p.add_argument("pdf", help="비교할 약관 PDF 경로")
    p.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="GPT 단독 방식의 청크 수 상한 (비용 제한용, 생략 시 전체)",
    )
    p.add_argument(
        "--out",
        default=None,
        help="비교 결과 JSON 저장 경로 (생략 시 'PDF이름_compare.json')",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    path = Path(args.pdf)
    if not path.exists():
        print(f"오류: PDF 파일을 찾을 수 없습니다: {args.pdf}", file=sys.stderr)
        return 1

    print(f"PDF 전체 텍스트 추출: {path.name}", file=sys.stderr)
    full_text = extract_text(str(path))

    # [A] GPT 단독
    print("\n=== [A] GPT 단독 (전체 텍스트) ===", file=sys.stderr)
    baseline_cov = extract_baseline(full_text, max_chunks=args.max_chunks)

    # [B] 파이프라인
    print("\n=== [B] 파이프라인 ===", file=sys.stderr)
    pipeline_cov = extract_pipeline(str(path))

    # 두 방식 모두 동일한 원문(full_text)에 대조해 지표 계산
    baseline_metrics = compute_metrics(baseline_cov, full_text)
    pipeline_metrics = compute_metrics(pipeline_cov, full_text)

    print_table(baseline_metrics, pipeline_metrics)

    report = {
        "source": path.name,
        "gpt_only": baseline_metrics,
        "pipeline": pipeline_metrics,
    }
    out_path = Path(args.out) if args.out else Path(f"{path.stem}_compare.json")
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n비교 결과 저장: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
