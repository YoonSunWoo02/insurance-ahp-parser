"""심사년도 멀티헤더 CSV 파서 (pandas 전용, GPT 불필요).

CSV 구조:
    1행: 타이틀                (스킵)
    2행: 빈 행                 (스킵)
    3행: 심사년도 멀티헤더      (2022 / 2023 / 2024 — 연도별 컬럼 그룹)
    4행: 실제 컬럼명           (항목, 성별구분, 연령구분10세, 환자수 ...)
    5행~: 데이터
    인코딩: cp949

파싱 조건:
    - 2024년 '환자수' 컬럼만 추출
    - 항목(질병명) 화이트리스트 필터
    - 성별구분: 남/여
    - 연령구분10세: 10대~70대 이상

출력: output/slider_init.json
    [{"disease": "...", "gender": "...", "age_range": "...", "patient_count": N}, ...]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd


# 헤더 행 위치 (0-기준): 3행=심사년도, 4행=컬럼명 → 인덱스 2, 3
YEAR_HEADER_ROW = 2
NAME_HEADER_ROW = 3
DATA_START_ROW = 4

TARGET_YEAR = "2024"
COUNT_COLUMN_NAME = "환자수"

ITEM_COL = "항목"
GENDER_COL = "성별구분"
AGE_COL = "연령구분10세"

# 추출 대상 질병명 화이트리스트 (공백 무시 비교용으로 정규화 키도 함께 보관)
DISEASES = [
    "골절",
    "대퇴경부골절",
    "발목염좌긴장",
    "반월상 연골판 손상",
    "손상",
    "화상",
    "동상",
    "한랭질환",
]
# 공백 제거 키 → 원래 표기. CSV의 띄어쓰기가 달라도 매칭되게 한다.
_DISEASE_LOOKUP = {re.sub(r"\s+", "", d): d for d in DISEASES}

GENDERS = {"남", "여"}

AGE_RANGES = {"10대", "20대", "30대", "40대", "50대", "60대", "70대 이상"}


def _norm(value) -> str:
    """셀 값을 비교용 문자열로 정규화 (None/NaN → '', 양끝 공백 제거)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _match_disease(raw_item: str) -> str | None:
    """항목 값이 화이트리스트 질병이면 표준 표기를 반환, 아니면 None."""
    key = re.sub(r"\s+", "", _norm(raw_item))
    return _DISEASE_LOOKUP.get(key)


def _parse_count(value) -> int | None:
    """환자수 셀을 정수로 변환. 빈값/'-'/숫자 아님 → None."""
    s = _norm(value).replace(",", "")
    if not s or s in {"-", "nan"}:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _find_column(name_row: pd.Series, target: str) -> int:
    """컬럼명 행에서 target과 일치하는 첫 컬럼 인덱스를 반환한다."""
    for idx, val in name_row.items():
        if _norm(val) == target:
            return idx
    raise ValueError(f"컬럼을 찾을 수 없습니다: '{target}'")


def _find_count_column(year_row: pd.Series, name_row: pd.Series) -> int:
    """심사년도=2024 이면서 컬럼명='환자수'인 컬럼 인덱스를 반환한다."""
    candidates = [
        idx
        for idx in name_row.index
        if _norm(name_row[idx]) == COUNT_COLUMN_NAME
        and _norm(year_row[idx]) == TARGET_YEAR
    ]
    if not candidates:
        raise ValueError(
            f"{TARGET_YEAR}년 '{COUNT_COLUMN_NAME}' 컬럼을 찾을 수 없습니다."
        )
    if len(candidates) > 1:
        print(
            f"경고: {TARGET_YEAR}년 '{COUNT_COLUMN_NAME}' 컬럼이 "
            f"{len(candidates)}개 발견되어 첫 번째({candidates[0]})를 사용합니다.",
            file=sys.stderr,
        )
    return candidates[0]


def _read_ragged(csv_path: str) -> pd.DataFrame:
    """행마다 컬럼 수가 달라도(타이틀 행이 짧은 등) 안전하게 읽는다.

    타이틀/빈 행은 데이터 행보다 컬럼이 적을 수 있어, pandas 기본 읽기는
    'Expected N fields' 오류를 낸다. 최대 컬럼 수를 먼저 파악해 고정 폭으로
    읽고 부족한 셀은 NaN으로 채운다.
    """
    with open(csv_path, encoding="cp949") as f:
        max_cols = max((line.count(",") + 1 for line in f), default=1)
    return pd.read_csv(
        csv_path,
        header=None,
        names=range(max_cols),
        encoding="cp949",
        dtype=str,
    )


def parse_csv(csv_path: str) -> list[dict]:
    """멀티헤더 CSV를 읽어 slider_init 레코드 리스트로 반환한다."""
    raw = _read_ragged(csv_path)

    # 심사년도 행은 연도 그룹의 첫 컬럼에만 값이 있으므로 가로 방향 ffill
    year_row = raw.iloc[YEAR_HEADER_ROW].ffill()
    name_row = raw.iloc[NAME_HEADER_ROW]

    item_idx = _find_column(name_row, ITEM_COL)
    gender_idx = _find_column(name_row, GENDER_COL)
    age_idx = _find_column(name_row, AGE_COL)
    count_idx = _find_count_column(year_row, name_row)

    records: list[dict] = []
    data = raw.iloc[DATA_START_ROW:]
    for _, row in data.iterrows():
        disease = _match_disease(row[item_idx])
        if disease is None:
            continue

        gender = _norm(row[gender_idx])
        if gender not in GENDERS:
            continue

        age_range = _norm(row[age_idx])
        if age_range not in AGE_RANGES:
            continue

        count = _parse_count(row[count_idx])
        if count is None:
            continue

        records.append(
            {
                "disease": disease,
                "gender": gender,
                "age_range": age_range,
                "patient_count": count,
            }
        )
    return records


def save_json(records: list[dict], out_path: str) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="심사년도 멀티헤더 CSV → slider_init.json 변환 (pandas)."
    )
    p.add_argument("csv", help="파싱할 CSV 경로 (cp949 인코딩)")
    p.add_argument(
        "--out",
        default="output/slider_init.json",
        help="출력 JSON 경로 (기본: output/slider_init.json)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not Path(args.csv).exists():
        print(f"오류: CSV 파일을 찾을 수 없습니다: {args.csv}", file=sys.stderr)
        return 1

    try:
        records = parse_csv(args.csv)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    save_json(records, args.out)
    print(
        f"{len(records)}개 레코드 추출 → {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
