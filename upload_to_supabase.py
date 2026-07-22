"""파싱 결과 → Supabase 적재 스크립트.

result.json(보장 항목)과 toxic_result.json(독소조항)을 읽어
Supabase DB에 저장한다.

사용법:
    # 보장 항목만
    python upload_to_supabase.py --result result.json

    # 독소조항만
    python upload_to_supabase.py --toxic toxic_result.json

    # 둘 다 (같은 상품)
    python upload_to_supabase.py --result result.json --toxic toxic_result.json

    # 상품 기본정보 직접 입력
    python upload_to_supabase.py --result result.json --insurer 삼성화재 --category 실손

.env에 아래 두 줄 추가 필요:
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your-service-role-key
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Windows 기본 cp949 콘솔에서도 한글/이모지 출력이 깨지지 않도록 UTF-8로 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from supabase import create_client
except ImportError:
    print("오류: supabase 패키지가 없습니다. 아래 명령어로 설치하세요.")
    print("  pip install supabase")
    sys.exit(1)

# 후처리는 main.py(JSON 저장 전)와 공유한다. main.py에서 이미 적용된 JSON을
# 다시 넣어도 멱등이므로 안전하다.
from postprocess import MIN_CONFIDENCE, postprocess

# 금액 문자열 → 원 단위 정수 변환은 validator/verifier.py와 로직을 공유한다.
# (검증 단계의 "5,000만원"='5천만원' 동치 비교와 DB 적재용 변환이 서로 어긋나지
# 않도록 파싱 규칙을 한 곳(parse_won_amount)에만 둔다.)
from validator.verifier import parse_won_amount as parse_amount


# ── Supabase 클라이언트 ────────────────────────────────────────
def _get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("오류: .env에 SUPABASE_URL과 SUPABASE_KEY를 입력하세요.")
        sys.exit(1)
    return create_client(url, key)


def _test_parse_amount() -> bool:
    """parse_amount 단위 테스트. 전부 통과하면 True."""
    cases = [
        ("5천만원", 50000000),
        ("5,000만원", 50000000),
        ("3천만원", 30000000),
        ("100만원", 1000000),
        ("10만원", 100000),
        ("1일 평균금액 10만원", 100000),
        ("5", None),
        (None, None),
    ]
    all_ok = True
    for raw, expected in cases:
        got = parse_amount(raw)
        ok = got == expected
        all_ok = all_ok and ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] parse_amount({raw!r}) = {got}  (기대값: {expected})")
    print("=> 전체 통과" if all_ok else "=> 실패 케이스 있음")
    return all_ok


# ── contract_type → is_rider 변환 ───────────────────────────
def to_is_rider(contract_type: str | None) -> bool:
    return str(contract_type or "").strip() == "특약"


# ── 1단계: insurance_product 적재 ────────────────────────────
def upsert_product(client, source_pdf: str, insurer: str, category: str, raw_text: str = "") -> str:
    """상품을 DB에 넣고 product_id를 반환한다. 같은 파일명이면 업데이트."""
    data = {
        "insurer_name": insurer,
        "product_name": Path(source_pdf).stem,
        "category": category if category else None,
        "source_pdf": source_pdf,
        "raw_text": raw_text,
    }
    res = (
        client.table("insurance_product")
        .upsert(data, on_conflict="source_pdf")
        .execute()
    )
    product_id = res.data[0]["product_id"]
    print(f"[상품] '{data['product_name']}' → product_id: {product_id}")
    return product_id


# ── 2단계: product_coverage 적재 ─────────────────────────────
def insert_coverages(client, product_id: str, results: list[dict]) -> int:
    """result.json의 results 배열에서 보장 항목을 추출해 적재한다."""
    # DB 적재 전 후처리 (postprocess 모듈 — main.py의 JSON 저장 전 단계와 동일)
    # 저신뢰 금액 무효화 → null 금액 채움 → 동일 보장 중복 제거.
    # main.py에서 이미 적용된 JSON이면 변화 없음(멱등).
    postprocess(results, min_confidence=MIN_CONFIDENCE)

    rows = []
    for article in results:
        for cov in article.get("coverages", []):
            amount = parse_amount(cov.get("amount"))
            row = {
                "product_id": product_id,
                "coverage_type": cov.get("coverage_name"),   # code_master 연동 전엔 이름 그대로
                "coverage_amount": amount,
                "condition": cov.get("payment_condition"),
                "is_rider": to_is_rider(cov.get("contract_type")),
                "rider_summary": cov.get("coverage_type"),   # 입원/통원/수술
                "rider_original": cov.get("source_quote"),
                "confidence": cov.get("confidence"),
            }
            rows.append(row)

    if not rows:
        print("[보장] 적재할 항목 없음")
        return 0

    # 기존 데이터 삭제 후 재적재 (중복 방지)
    client.table("product_coverage").delete().eq("product_id", product_id).execute()
    client.table("product_coverage").insert(rows).execute()
    print(f"[보장] {len(rows)}건 적재 완료")
    return len(rows)


# ── 3단계: product_toxic_clause 적재 ─────────────────────────
def insert_toxic_clauses(client, product_id: str, toxic_clauses: list[dict]) -> int:
    """toxic_result.json의 toxic_clauses 배열을 적재한다."""
    rows = []
    for article in toxic_clauses:
        for clause in article.get("toxic_clauses", []):
            row = {
                "product_id": product_id,
                "article_title": article.get("article_title"),
                "clause_text": clause.get("clause_summary"),
                "risk_note": clause.get("reason"),
                "severity": clause.get("severity"),
                "source_quote": clause.get("source_quote"),
            }
            rows.append(row)

    if not rows:
        print("[독소조항] 적재할 항목 없음")
        return 0

    # 기존 데이터 삭제 후 재적재
    client.table("product_toxic_clause").delete().eq("product_id", product_id).execute()
    client.table("product_toxic_clause").insert(rows).execute()
    print(f"[독소조항] {len(rows)}건 적재 완료")
    return len(rows)


# 파싱 결과 JSON 기본 저장/탐색 폴더
PARSED_DIR = Path("data/parsed")


def _resolve_path(path_str: str) -> Path:
    """경로를 해석한다. 그대로 없으면 data/parsed/ 안에서 다시 찾는다."""
    p = Path(path_str)
    if p.exists():
        return p
    candidate = PARSED_DIR / path_str
    if candidate.exists():
        return candidate
    # 둘 다 없으면 원본 경로 반환 (이후 단계에서 오류 메시지로 처리)
    return p


def list_parsed() -> None:
    """data/parsed/ 폴더의 JSON 파일 목록을 출력한다."""
    if not PARSED_DIR.exists():
        print(f"'{PARSED_DIR}' 폴더가 없습니다. 먼저 main.py로 파싱을 실행하세요.")
        return
    files = sorted(PARSED_DIR.glob("*.json"))
    if not files:
        print(f"'{PARSED_DIR}'에 JSON 파일이 없습니다.")
        return
    print(f"[{PARSED_DIR}] JSON 파일 {len(files)}개:")
    for f in files:
        kb = f.stat().st_size / 1024
        print(f"  - {f.name}  ({kb:.1f} KB)")


# ── 메인 ─────────────────────────────────────────────────────
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="파싱 결과를 Supabase에 적재한다.")
    p.add_argument("--result", default=None, help="보장 항목 JSON 경로 (없으면 data/parsed/에서 탐색)")
    p.add_argument("--toxic", default=None, help="독소조항 JSON 경로 (없으면 data/parsed/에서 탐색)")
    p.add_argument("--insurer", default="미입력", help="보험사명 (예: 삼성화재)")
    p.add_argument("--category", default=None, help="보험 분류 코드 (code_master의 code)")
    p.add_argument("--list", action="store_true", help="data/parsed/ 폴더의 JSON 파일 목록만 출력")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # --list: 폴더 목록만 출력하고 종료 (DB 연결 불필요)
    if args.list:
        list_parsed()
        return 0

    if not args.result and not args.toxic:
        print("오류: --result 또는 --toxic 중 하나는 필요합니다. (--list로 목록 확인 가능)")
        return 1

    client = _get_client()

    # source_pdf 이름 결정 (result 우선, 없으면 toxic에서)
    source_pdf = None
    result_data = None
    toxic_data = None

    if args.result:
        result_path = _resolve_path(args.result)
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        source_pdf = result_data.get("source", result_path.name)

    if args.toxic:
        toxic_path = _resolve_path(args.toxic)
        toxic_data = json.loads(toxic_path.read_text(encoding="utf-8"))
        if not source_pdf:
            source_pdf = toxic_data.get("source", toxic_path.name)

    # 상품 적재
    product_id = upsert_product(
        client,
        source_pdf=source_pdf,
        insurer=args.insurer,
        category=args.category,
        raw_text="",  # raw_text는 용량이 커서 별도 처리 필요 시 추가
    )

    # 보장 항목 적재
    if result_data:
        insert_coverages(client, product_id, result_data.get("results", []))

    # 독소조항 적재
    if toxic_data:
        clauses = toxic_data.get("toxic_clauses", [])
        insert_toxic_clauses(client, product_id, clauses)

    print("\n✅ 적재 완료!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())