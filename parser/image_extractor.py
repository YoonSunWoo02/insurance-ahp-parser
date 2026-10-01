"""보험 가입화면 스크린샷(PNG)에서 담보 표를 읽어 JSON으로 추출하는 모듈.

PDF 약관과 달리 소스가 화면 캡처 이미지라 텍스트 레이어가 없다. GPT-4o-mini의
비전 입력으로 표(담보명/가입금액/보험료/가입토글)와 우측 요약(총보험료/갱신조건)을
직접 읽는다.

폴더 계층(data/images/{보험사}/{상품세분화}/{직업}/{나이}/사진.png)과 파일명
(납입주기/의료급여)에 이미 있는 정보는 이미지를 다시 읽지 않고 결정적으로
파싱한다 — 이 정보들은 스크린샷마다 표기가 일관되고, 이미지 인식보다 훨씬
신뢰도가 높다.

가입(보장)금액은 이 화면에서 "가입금액" 컬럼 헤더 자체가 총 가입한도라는 뜻이라
(PDF 약관처럼 회당/공제금액이 섞여 나오지 않는다) amount_structurer의 문맥 기반
분류를 그대로 재사용하지 않고, annual_limit에 직접 채운다 — 근거 문장이 없는 화면
표에 그 분류기를 그대로 돌리면 오히려 전부 애매(null)로 빠진다.
"""

import base64
import io
import json
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from openai import OpenAI
from PIL import Image

from parser.amount_structurer import structure_listed_amount
from validator.verifier import parse_won_amount

# 표 글자가 작아 비전 모델이 시각적으로 비슷한 글자를 혼동하는 사례가 실제로 있었다
# (예: "중증"→"종신", "의료비"→"일당"). 2배 업스케일해서 보내면 인식률이 올라간다.
_UPSCALE_FACTOR = 2

# gpt-4o-mini로는 표 글자를 종종 잘못 읽었다(실측: "중증"→"종신", "의료비"→"일당"/
# "일의료비", "비급여"→"급여" 등). 업스케일+detail:high를 같이 줘도 mini는 오류가
# 남았고, gpt-4o(풀모델)로 바꿨더니 샘플 2개(12개 담보명) 전부 정확히 일치했다.
MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
REQUEST_TIMEOUT = 60

# 1단계 폴더명(보험사 약칭, 대소문자 무관) → 정식 명칭. 새 보험사가 추가되면 여기에 보탠다.
_INSURER_MAP = {
    "db손보": "DB손해보험",
    "db": "DB손해보험",
}


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 설정되어 있지 않습니다.")
    return OpenAI(api_key=api_key)


def is_screenshot_image(path: str) -> bool:
    return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}


_AGE_FOLDER_PATTERN = re.compile(r"^\d+$")


def _parse_age(age_folder: str) -> int | None:
    """나이 폴더명이 순수 숫자("25")면 정수로, "20대"/"20-29" 같은 구간이면 None을
    반환한다 (구간은 하나의 정수로 억지로 대표시키지 않는다 — raw는 항상 별도 보존)."""
    if age_folder and _AGE_FOLDER_PATTERN.match(age_folder):
        return int(age_folder)
    return None


def _parse_path_metadata(image_path: str) -> dict:
    """4단계 폴더 계층(보험사/상품세분화/직업/나이)과 파일명(납입주기/의료급여)을
    결정적으로 파싱한다.

    data/images/{보험사}/{상품세분화}/{직업}/{나이}/사진.png
    """
    path = Path(image_path)
    age_folder = path.parent.name  # 4단계: 나이
    job_folder = path.parent.parent.name  # 3단계: 직업
    product_case_folder = path.parent.parent.parent.name  # 2단계: 상품 세분화
    insurer_folder = path.parent.parent.parent.parent.name  # 1단계: 보험사
    stem = path.stem  # 예: "DB실손보험(월납, 의료급여 비수급자)"

    insurer = _INSURER_MAP.get(insurer_folder.lower(), insurer_folder)

    payment_cycle = "월납" if "월납" in stem else ("연납" if "연납" in stem else None)
    # "비수급자"가 "수급자"를 부분 문자열로 포함하므로 반드시 먼저 검사한다.
    medical_aid = "비수급자" if "비수급자" in stem else ("수급자" if "수급자" in stem else None)

    return {
        "insurer": insurer,
        "product_name": "실손보험",
        "product_case": product_case_folder,
        "job": job_folder,
        # 가입화면 캡처엔 성별 표시가 없다 — 팀 규칙상 정보가 없으면 "상관없음".
        "gender": "상관없음",
        "age": _parse_age(age_folder),
        "age_raw": age_folder,
        "payment_cycle": payment_cycle,
        "medical_aid": medical_aid,
    }


_SYSTEM_PROMPT = """너는 한국 보험사 온라인 가입화면 스크린샷을 읽는 전문가다.
화면에 보이는 "보장내용/가입금액/보험료" 표와 우측 요약 박스(총보험료, 갱신조건)를
정확히 읽어 JSON으로만 답한다. 표에 없거나 흐릿해서 확신할 수 없는 값은 추측하지
말고 null로 남겨라. 가입 토글(초록색 원형 스위치, "가입"이라는 라벨)이 켜져 있으면
true, 꺼져(회색) 있으면 false, 그 행에 토글 자체가 없으면(기본형처럼 항상 포함되는
필수 담보) true로 본다.

담보명은 한 글자도 틀리면 안 된다. 특히 아래처럼 생김새가 비슷해서 혼동하기 쉬운
글자들을 조심해라 — 한 글자씩 또박또박 확인하고 옮겨 적어라:
- "중증"과 "종신"은 다른 단어다. 괄호 안 분류 표시(예: "특약형1,중증")를 "종신"으로
  잘못 읽지 않도록 주의해라.
- "의료비"와 "일당"은 다른 단어다. "OO의료비(간편고지)"를 "OO일당(간편고지)"으로
  잘못 읽지 않도록 주의해라.
- "비급여"와 "비중증"도 서로 다른 단어이니 앞뒤 문맥(괄호 안/밖)을 보고 구분해라."""

_USER_TEMPLATE = """이 스크린샷에서 아래 JSON 스키마로 추출해라.

{
  "total_premium_raw": "우측 요약 박스 맨 아래 보이는 총보험료 원문 그대로 (예: '월 8,340 원')",
  "renewal": "갱신조건 원문 그대로 (예: '매년 갱신, 5년 재가입, 최대 100세 보장')",
  "coverages": [
    {
      "coverage_name": "표의 담보명 원문 그대로 (괄호 포함, 절대 줄이거나 의역하지 말 것)",
      "subscribed_amount_raw": "가입(보장)금액 원문 그대로 (예: '5천만원')",
      "premium_raw": "보험료 원문 그대로 (예: '1,449 원')",
      "is_enrolled": true
    }
  ]
}

표에 보이는 행 전부를 위에서 아래 순서대로 담아라. 개수를 미리 정하지 말고 실제
보이는 만큼만 담아라. 반드시 위 스키마의 JSON만 출력해라."""


def _image_to_data_uri(image_path: str) -> str:
    """표 글자를 더 또렷하게 읽도록 업스케일한 뒤 PNG로 인코딩한다."""
    img = Image.open(image_path).convert("RGB")
    img = img.resize(
        (img.width * _UPSCALE_FACTOR, img.height * _UPSCALE_FACTOR),
        Image.LANCZOS,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _extract_table_with_gpt(image_path: str, client: OpenAI) -> dict:
    data_uri = _image_to_data_uri(image_path)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _USER_TEMPLATE},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=REQUEST_TIMEOUT,
    )
    return json.loads(resp.choices[0].message.content or "{}")


_COVERAGE_CATEGORY_PATTERN = re.compile(r"^\(([^)]+)\)")

# "[실손의료비]", "[진단비]" 같은 표의 구획 제목. GPT가 이걸 첫 담보 행 이름으로
# 붙여 같은 금액의 중복 행을 만든 사례가 있었다. 담보명 전체가 대괄호 하나뿐인 경우만
# 거른다("[건강]상해 사망"처럼 뒤에 이름이 이어지는 제안서 담보는 해당 없음).
_CATEGORY_HEADER_PATTERN = re.compile(r"^\s*\[[^\]]+\]\s*$")


def _is_category_header(name) -> bool:
    return bool(_CATEGORY_HEADER_PATTERN.match(name or ""))


def _coverage_category(coverage_name) -> str | None:
    m = _COVERAGE_CATEGORY_PATTERN.match(coverage_name or "")
    return m.group(1) if m else None


# "실손의료비보험"/"유병력자간편실손보험" 두 상품은 직업·나이별로 보험료 숫자만
# 다를 뿐 담보명 텍스트 자체는 고정 템플릿이다(표 순서도 항상 동일). GPT 비전이
# 매번 새로 읽으면 생김새 비슷한 글자를 오독할 여지가 있어(실측: "중증"→"종종",
# "자기"→"자가" 등), 학생 샘플에서 원본 이미지를 직접 대조해 확정한 담보명으로
# 화이트리스트를 만들어 표 순서 기준으로 보정한다.
_CANONICAL_COVERAGE_NAMES: dict[str, list[str]] = {
    "실손의료비보험": [
        "(기본형)상해급여의료비(갱신형)",
        "(기본형)질병급여의료비(갱신형)",
        "(특약형1,중증)상해비급여의료비(갱신형)",
        "(특약형1,중증)질병비급여의료비(갱신형)",
        "(특약형1,중증)3대비급여의료비(갱신형)",
        "(특약형2,비중증)상해비급여의료비(갱신형)",
        "(특약형2,비중증)질병비급여의료비(갱신형)",
        "(특약형2,비중증)비급여자기공명영상진단(갱신형)",
    ],
    "유병력자간편실손보험": [
        "상해입원의료비(간편고지)(1년갱신)",
        "상해외래의료비(간편고지)(1년갱신)",
        "질병입원의료비(간편고지)(1년갱신)",
        "질병외래의료비(간편고지)(1년갱신)",
    ],
}

# 이 유사도 미만이면 완전히 다른 행일 수 있으니 억지로 보정하지 않고 원본을 둔다.
_NAME_MATCH_THRESHOLD = 0.6


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a or "", b or "").ratio()


def _canonicalize_coverage_names(coverages: list[dict], product_case: str | None) -> None:
    """product_case의 고정 담보명 화이트리스트로 GPT가 오독한 이름을 보정한다.

    coverages를 in-place로 수정한다. 원본과 다르게 보정된 경우에만 그 항목에
    "coverage_name_raw"를 남긴다(바뀐 게 없으면 스키마를 불필요하게 늘리지 않는다).
    """
    canonical = _CANONICAL_COVERAGE_NAMES.get(product_case or "")
    if not canonical:
        return

    if len(coverages) == len(canonical):
        # 표 순서가 항상 고정이므로 위치 기반 매핑이 텍스트 유사도 매칭보다 안전하다.
        pairs = list(zip(coverages, canonical))
    else:
        # 행 개수가 다르면(누락/추가 의심) 위치 매핑을 포기하고 항목별 최적 매칭만 쓴다.
        pairs = [
            (cov, max(canonical, key=lambda c: _similarity(cov.get("coverage_name"), c)))
            for cov in coverages
        ]

    for cov, target in pairs:
        raw_name = cov.get("coverage_name")
        if raw_name == target:
            continue
        if _similarity(raw_name, target) >= _NAME_MATCH_THRESHOLD:
            cov["coverage_name"] = target
            cov["coverage_name_raw"] = raw_name


def _restructure_amounts(coverages: list[dict]) -> None:
    """담보명이 확정된 뒤 가입금액을 담보 성격(총한도/1일·1회당)에 맞게 다시 구조화한다."""
    for cov in coverages:
        sa = cov.get("subscribed_amount") or {}
        value = sa.get("annual_limit") if sa.get("annual_limit") is not None else sa.get("per_time")
        cov["subscribed_amount"] = structure_listed_amount(cov.get("coverage_name"), sa.get("raw"), value)


def _to_total_premium(raw, payment_cycle: str | None) -> int | None:
    """총보험료 원문을 월납 기준 정수로 정규화한다. 연납이면 12로 나눈다."""
    value = parse_won_amount(raw)
    if value is None:
        return None
    is_annual = payment_cycle == "연납" or (raw and "연" in str(raw) and "월" not in str(raw))
    return round(value / 12) if is_annual else value


def extract_from_image(image_path: str, client: OpenAI | None = None) -> dict:
    """스크린샷 1장을 읽어 가입화면 결과 dict를 반환한다."""
    client = client or _get_client()
    meta = _parse_path_metadata(image_path)

    try:
        gpt_data = _extract_table_with_gpt(image_path, client)
    except Exception as e:
        print(f"      → 이미지 인식 실패 (오류: {e})", file=sys.stderr)
        return {
            "source": Path(image_path).name,
            "document_type": "가입화면",
            "contract_summary": meta,
            "coverages": [],
            "error": str(e),
        }

    total_premium_raw = gpt_data.get("total_premium_raw")
    contract_summary = dict(meta)
    contract_summary["total_premium"] = _to_total_premium(total_premium_raw, meta["payment_cycle"])
    contract_summary["total_premium_raw"] = total_premium_raw
    contract_summary["renewal"] = gpt_data.get("renewal")

    coverages = []
    for row in gpt_data.get("coverages", []):
        name = row.get("coverage_name")
        if _is_category_header(name):
            continue
        amount_raw = row.get("subscribed_amount_raw")
        premium_raw = row.get("premium_raw")
        amount_value = parse_won_amount(amount_raw) if amount_raw else None
        premium_value = parse_won_amount(premium_raw) if premium_raw else None
        coverages.append(
            {
                "coverage_name": name,
                "subscribed_amount": {
                    "per_time": None,
                    "annual_limit": amount_value,
                    "deductible": None,
                    "visit_limit": None,
                    "is_shared_limit": False,
                    "raw": amount_raw,
                },
                "premium": premium_value,
                "is_enrolled": row.get("is_enrolled", True),
            }
        )

    # 담보명이 고정 화이트리스트에 있는 상품이면 GPT 오독을 보정 — coverage_category는
    # 보정된(정확한) 이름 기준으로 뽑아야 하므로 반드시 그 뒤에 계산한다.
    _canonicalize_coverage_names(coverages, meta.get("product_case"))
    for cov in coverages:
        cov["coverage_category"] = _coverage_category(cov.get("coverage_name"))
    _restructure_amounts(coverages)

    return {
        "source": Path(image_path).name,
        "document_type": "가입화면",
        "contract_summary": contract_summary,
        "coverages": coverages,
        "error": None,
    }


# ===========================================================================
# db손보(2).zip 전용: 한 조합이 스크린샷 여러 장(2~3장, 표를 스크롤하며 이어
# 찍은 것)으로 구성되고, 상품별로 폴더 계층 깊이가 다른(표준형처럼 등급 폴더가
# 끼는 경우/안 끼는 경우) 데이터셋을 처리한다. 기존 extract_from_image/
# _parse_path_metadata(db손보/ 4단계 고정 구조, 조합당 이미지 1장)는 이미 검증
# 완료된 경로라 건드리지 않고, 아래에 별도 경로로 추가한다.
# ===========================================================================

_MULTI_SYSTEM_PROMPT = """너는 한국 보험사 온라인 가입화면 스크린샷을 읽는 전문가다.
여러 장의 스크린샷은 같은 화면을 위에서 아래로 스크롤하며 이어 찍은 것이다
(하나의 연속된 표). 전부 합쳐 전체 담보 표와 우측 요약 박스(총보험료, 갱신조건)를
정확히 읽어 JSON으로만 답한다.

각 행의 열 순서는 항상 "담보명 | 가입금액 | 보험료 | 가입상태표시" 4개다. 담보명
다음에 오는 첫 번째 숫자가 무조건 가입금액, 두 번째 숫자가 무조건 보험료다.
"필수"라는 네모 테두리 배지는 가입상태표시 칸의 라벨일 뿐 금액이 아니다 — 그 뒤에
나오는 두 숫자를 각각 가입금액/보험료로 읽어라. 예를 들어
"상해사망·후유장해(20-100%)(갱신형) [필수] 1백만원 130원"처럼 보이면
subscribed_amount_raw="1백만원", premium_raw="130원", is_enrolled=true이지,
"필수"를 subscribed_amount_raw 자리에 넣으면 안 된다.
가입 토글이 초록색 "가입"이면 true, 회색 "미가입"이면 false, "필수"면(토글 없이
항상 포함되는 담보) true로 본다.

담보명은 괄호 안 숫자·단어를 포함해 한 글자도 틀리면 안 된다 — 생김새가 비슷한
한글 글자(예: "감액"과 "간액", "중증"과 "종신")를 혼동하지 말고 한 글자씩 또박또박
확인해라. 확신할 수 없는 값은 추측하지 말고 null로 남겨라.

"미가입" 상태라 가입금액/보험료 칸이 "-"로 표시된 행도 절대 건너뛰지 말고 반드시
포함해라 — subscribed_amount_raw="-", premium_raw="-", is_enrolled=false로 채워서
넣어라. 숫자가 없는 행이라고 생략하면 안 된다. 표에 보이는 담보 행은 가입 여부와
상관없이 전부 다 담아야 한다."""

_MULTI_USER_TEMPLATE = """아래 JSON 스키마로 추출해라.

{
  "total_premium_raw": "우측 요약 박스에 보이는 총보험료 원문 그대로 (예: '월 5,440원')",
  "renewal": "갱신조건 원문 그대로 (예: '갱신형 (갱신주기 10년, 최대 100세 보장)')",
  "coverages": [
    {
      "coverage_name": "표의 담보명 원문 그대로 (괄호 포함, 절대 줄이거나 의역하지 말 것)",
      "subscribed_amount_raw": "가입(보장)금액 원문 그대로 (예: '1천만원')",
      "premium_raw": "보험료 원문 그대로 (예: '370원')",
      "is_enrolled": true
    }
  ]
}

카테고리 헤더 행(예: "암", "뇌심")은 담보가 아니니 빼고, 실제 담보 행 전부를
위에서 아래 순서대로 담아라. 개수를 미리 정하지 말고 실제 보이는 만큼만 담아라.
반드시 위 스키마의 JSON만 출력해라."""


def _extract_table_with_gpt_multi(image_paths: list[str], client: OpenAI) -> dict:
    content = [{"type": "text", "text": _MULTI_USER_TEMPLATE}]
    for p in image_paths:
        content.append(
            {"type": "image_url", "image_url": {"url": _image_to_data_uri(p), "detail": "high"}}
        )
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _MULTI_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        timeout=120,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _normalize_insurer_folder(name: str) -> str:
    """폴더명 끝의 "(2)" 같은 중복본 표시를 떼고 정규화한다."""
    return re.sub(r"\(\d+\)\s*$", "", name).strip()


_RENEWAL_TYPE_PATTERN = re.compile(r"비갱신형|갱신형")
_MATURITY_PATTERN = re.compile(r"(\d+)\s*(?:세|년)\s*만기")
_PAYMENT_PERIOD_PATTERN = re.compile(r"(\d+)\s*년납")


def parse_zip2_combo_metadata(image_path: str, zip_root: str) -> dict:
    """db손보(2).zip 전용 경로 메타 파서.

    두 가지 폴더 구조를 모두 지원한다:
      - {root}/{product}/{job}/{age}/{combo}/사진.png
        (예: 실손의료비보험+암보험 — 기존 db손보/ 4단계와 동일한 스타일)
      - {root}/{product}/{job}/{tier}/{age}/{combo}/사진.png
        (예: 걸을수록THE좋은건강보험 — 표준형 같은 등급 폴더가 하나 더 낀다)

    combo 폴더 뒤쪽에 있는 실제 이미지 개수는 조합마다 2장 또는 3장으로 다르다
    (표 스크롤 분할 방식이 표 길이에 따라 달라짐) — 그건 이 함수가 아니라
    호출부에서 같은 combo 폴더의 파일들을 묶어 처리한다.
    """
    path = Path(image_path)
    root = Path(zip_root)
    rel_parts = path.parent.relative_to(root).parts  # combo 폴더까지의 상대 경로 조각

    product = rel_parts[0]
    job = rel_parts[1]
    combo_name = rel_parts[-1]

    if len(rel_parts) == 4:
        # {product}/{job}/{age}/{combo}
        tier = None
        age_folder = rel_parts[2]
    elif len(rel_parts) == 5:
        # {product}/{job}/{tier}/{age}/{combo}
        tier = rel_parts[2]
        age_folder = rel_parts[3]
    else:
        tier = None
        age_folder = None

    insurer_folder = _normalize_insurer_folder(root.name)
    insurer = _INSURER_MAP.get(insurer_folder.lower(), insurer_folder)

    payment_cycle = "월납" if "월납" in combo_name else ("연납" if "연납" in combo_name else None)
    medical_aid = "비수급자" if "비수급자" in combo_name else ("수급자" if "수급자" in combo_name else None)

    renewal_m = _RENEWAL_TYPE_PATTERN.search(combo_name)
    renewal_type = renewal_m.group(0) if renewal_m else None
    maturity_m = _MATURITY_PATTERN.search(combo_name)
    maturity = f"{maturity_m.group(1)}세" if maturity_m else None
    period_m = _PAYMENT_PERIOD_PATTERN.search(combo_name)
    payment_period = f"{period_m.group(1)}년납" if period_m else None

    # 키 구성·순서는 팀에서 확정한 가입화면 양식(product_case까지 포함)을 따르고,
    # 이 데이터셋에만 있는 등급/갱신·만기·납입기간은 그 뒤에 덧붙인다.
    return {
        "insurer": insurer,
        "product_name": product,
        "product_case": product,
        "job": job,
        "gender": "상관없음",
        "age": _parse_age(age_folder) if age_folder else None,
        "age_raw": age_folder,
        "payment_cycle": payment_cycle,
        "medical_aid": medical_aid,
        "tier": tier,
        "renewal_type": renewal_type,
        "maturity": maturity,
        "payment_period": payment_period,
    }


# 걸을수록THE좋은건강보험은 GPT가 같은 글자를 "매번 똑같이" 틀리게 읽는 경우가 많아
# (예: "감액"→"간액"이 280건 중 대부분) 여러 파일 결과의 다수결로는 정답을 못 고른다.
# 원본 스크린샷을 확대해 직접 읽어 확정한 담보명이다. 표준형/프리미엄형은 담보명이
# 같고 금액만 다르며, 갱신형 10년납은 "허혈심장질환수술비(갱신형)"이 빠진 28개다.
_CANONICAL_COMBO_NAMES: dict[tuple[str, str], list[str]] = {
    ("걸을수록THE좋은건강보험", "갱신형"): [
        "암진단비 II (유사암제외)(소액암1년감액지급)(갱신형)",
        "유사암진단비 II (1년감액지급)(갱신형)",
        "암수술비(유사암제외)(갱신형)",
        "유사암수술비(갱신형)",
        "항암방사선약물치료비(유사암포함)(갱신형)",
        "표적항암약물허가치료비(최초1회한)(갱신형)",
        "하이클래스항암약물치료비 II (연간1회한)(10년지급대상)(갱신형)",
        "하이클래스암주요치료비 II (연간1회한)(10년지급대상)(갱신형)",
        "뇌혈관질환진단비(갱신형)",
        "뇌혈관질환수술비(갱신형)",
        "허혈심장질환진단비(갱신형)",
        "허혈심장질환수술비(갱신형)",
        "혈전용해치료비III(연간1회한)(갱신형)",
        "통풍진단비(갱신형)",
        "천식진단비(최초1회한)(갱신형)",
        "만성호흡기질환진단비(갱신형)",
        "대상포진진단비(갱신형)",
        "대상포진눈병진단비(갱신형)",
        "주요바이러스질환진단비(갱신형)",
        "아나필락시스쇼크진단비(갱신형)",
        "폐렴진단비(최초1회한)(갱신형)",
        "상해수술비(동일사고당1회지급)(갱신형)",
        "질병수술비(상급종합병원)(동일질병당1회지급)(갱신형)",
        "이비인후과질환수술비(갱신형)",
        "양전자단층촬영(PET)검사지원비(급여,연간1회한)(갱신형)",
        "특정전염병치료비(갱신형)",
        "응급실내원보험금(응급환자)(갱신형)",
        "상해사망·후유장해(20-100%)(갱신형)",
        "독감(인플루엔자)항바이러스제치료비 II (10일면책,연간1회한)(갱신형)",
    ],
    ("걸을수록THE좋은건강보험", "비갱신형"): [
        "암진단비 II (유사암제외)(소액암1년감액지급)",
        "유사암진단비 II (1년감액지급)",
        "표적항암약물허가치료비(최초1회한)(갱신형)",
        "하이클래스항암약물치료비 II (연간1회한)(10년지급대상)",
        "하이클래스암주요치료비 II (연간1회한)(10년지급대상)",
        "뇌졸중진단비",
        "뇌혈관질환진단비",
        "뇌혈관질환수술비",
        "급성심근경색증진단비",
        "허혈심장질환진단비",
        "허혈심장질환수술비",
        "혈전용해치료비III(연간1회한)",
        "통풍진단비",
        "폐렴진단비(최초1회한)",
        "양전자단층촬영(PET)검사지원비(급여,연간1회한)(갱신형)",
        "독감(인플루엔자)항바이러스제치료비 II (10일면책,연간1회한)(갱신형)",
        "상해사망·후유장해(20-100%)",
    ],
}

# 실손의료비보험+암보험: 앞 8개는 실손의료비보험과 같고 뒤 24개가 참좋은암보험 담보다
# (화면 헤더 "실손 보장8개 + 암 보장24개"와 일치).
_CANONICAL_COMBO_NAMES[("실손의료비보험+암보험", None)] = _CANONICAL_COVERAGE_NAMES["실손의료비보험"] + [
    "암진단비 II (유사암제외)(갱신형)",
    "유사암진단비 II (갱신형)",
    "암수술비(유사암제외)(갱신형)",
    "유사암수술비(갱신형)",
    "다빈치로봇암수술비III(최초1회한,특정암제외)(갱신형)",
    "다빈치로봇암수술비III(최초1회한,특정암)(갱신형)",
    "암직접치료통원비 II (상급종합병원제외)(연간30회한도)(갱신형)",
    "암직접치료통원비 II (상급종합병원)(연간30회한도)(갱신형)",
    "암입원일당(요양병원)(1일이상90일한도)(갱신형)",
    "암직접치료입원일당(요양병원제외)(1일이상180일한도)(갱신형)",
    "간병인사용질병입원일당(1일이상180일한도)(요양병원)(갱신형)",
    "간병인사용질병입원일당(요양병원제외)(1일당8시간이상)(1일이상180일한도)(갱신형)",
    "간호·간병통합서비스사용질병입원일당(1일이상180일한도)(갱신형)",
    "간병인사용상해입원일당(1일이상180일한도)(요양병원)(갱신형)",
    "간병인사용상해입원일당(요양병원제외)(1일당8시간이상)(1일이상180일한도)(갱신형)",
    "간호·간병통합서비스사용상해입원일당(1일이상180일한도)(갱신형)",
    "항암방사선,약물치료비(갱신형)",
    "암주요치료비III(유사암제외)(연간1회한)(갱신형)",
    "기타피부암,갑상선암주요치료비III(연간1회한)(갱신형)",
    "계속받는항암방사선·약물치료비 II (급여,연간1회한)(갱신형)",
    "항암양성자방사선치료비 II (최초1회한)(갱신형)",
    "표적항암방사선치료비 II (항암세기조절방사선)(최초1회한)(갱신형)",
    "표적항암약물허가치료비 II (최초1회한)(갱신형)",
    "암사망(갱신형)",
]

# 순서 정렬 매칭에서 이 유사도 미만인 쌍은 짝으로 인정하지 않는다(원본 이름 유지).
# 순서 제약이 같이 걸려 있어 "진단비"/"수술비"처럼 비슷한 담보끼리 뒤바뀔 위험이 낮다.
_ALIGN_THRESHOLD = 0.4
# 순서 정렬 밖에서 짝짓는 보완 매칭은 순서 제약이 없으니 기준을 더 엄격하게 둔다.
_FALLBACK_THRESHOLD = 0.7


def _normalize_name(name: str | None) -> str:
    s = (name or "").replace("Ⅲ", "III").replace("Ⅱ", "II").replace("Ⅰ", "I")
    return re.sub(r"\s+", "", s)


def _align_to_canonical(coverages: list[dict], canonical: list[str]) -> int:
    """추출된 담보 순서와 정답 목록 순서를 정렬해(행 누락/추가 허용) 짝지은 뒤
    이름을 정답으로 바꾼다. 원래 GPT가 읽은 이름은 coverage_name_raw에 남긴다.
    바꾼 건수를 반환한다."""
    ext = [_normalize_name(c.get("coverage_name")) for c in coverages]
    can = [_normalize_name(c) for c in canonical]
    n, m = len(ext), len(can)
    sim = [[SequenceMatcher(None, ext[i], can[j]).ratio() for j in range(m)] for i in range(n)]

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            s = sim[i - 1][j - 1]
            if s >= _ALIGN_THRESHOLD:
                best = max(best, dp[i - 1][j - 1] + s)
            dp[i][j] = best

    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        s = sim[i - 1][j - 1]
        if s >= _ALIGN_THRESHOLD and dp[i][j] == dp[i - 1][j - 1] + s:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif dp[i][j] == dp[i - 1][j]:
            i -= 1
        else:
            j -= 1

    # GPT가 두 장을 합치며 행 순서를 섞은 경우 순서 정렬에서 빠지는 행이 생긴다.
    # 남은 정답명 중 충분히 비슷한 게 있으면 그걸로 한 번 더 짝짓는다.
    matched_e = {e for e, _ in pairs}
    used_c = {c for _, c in pairs}
    for ei in range(n):
        if ei in matched_e:
            continue
        free = [cj for cj in range(m) if cj not in used_c]
        if not free:
            break
        cj = max(free, key=lambda k: sim[ei][k])
        if sim[ei][cj] >= _FALLBACK_THRESHOLD:
            pairs.append((ei, cj))
            used_c.add(cj)

    changed = 0
    for ei, cj in pairs:
        cov = coverages[ei]
        if cov.get("coverage_name") == canonical[cj]:
            continue
        # "Ⅱ"↔"II", 띄어쓰기 같은 표기 차이만 있으면 표기만 통일하고, 글자 자체가
        # 달랐을 때만 GPT 원문을 coverage_name_raw로 남긴다.
        if ext[ei] != can[cj]:
            cov.setdefault("coverage_name_raw", cov.get("coverage_name"))
        cov["coverage_name"] = canonical[cj]
        changed += 1
    return changed


def canonicalize_combo_result(result: dict) -> int:
    """db손보(2) 조합 결과의 담보명을 정답 목록으로 보정한다(in-place, 바꾼 건수 반환).

    - 실손의료비보험+암보험: 실손 8개 + 암 24개 정답 목록과 순서 정렬 매칭.
    - 걸을수록THE좋은건강보험: 갱신형/비갱신형별 정답 목록과 순서 정렬 매칭.
    """
    summary = result.get("contract_summary", {})
    coverages = result.get("coverages", [])
    product = summary.get("product_name")
    changed = 0

    canonical = _CANONICAL_COMBO_NAMES.get((product, summary.get("renewal_type")))
    if canonical:
        changed += _align_to_canonical(coverages, canonical)

    for cov in coverages:
        cov["coverage_category"] = _coverage_category(cov.get("coverage_name"))
    _restructure_amounts(coverages)
    return changed


def extract_from_combo(image_paths: list[str], meta: dict, client: OpenAI | None = None) -> dict:
    """같은 조합(combo) 폴더의 스크린샷 여러 장을 합쳐 하나의 결과로 추출한다."""
    client = client or _get_client()
    image_paths = sorted(image_paths)

    try:
        gpt_data = _extract_table_with_gpt_multi(image_paths, client)
    except Exception as e:
        print(f"      → 이미지 인식 실패 (오류: {e})", file=sys.stderr)
        return {
            "source": Path(image_paths[0]).parent.name,
            "document_type": "가입화면",
            "contract_summary": meta,
            "coverages": [],
            "error": str(e),
        }

    total_premium_raw = gpt_data.get("total_premium_raw")
    contract_summary = dict(meta)
    if not contract_summary.get("payment_cycle"):
        # 폴더명에 납입주기가 없는 상품(걸을수록THE좋은건강보험)은 화면 표기로 채운다.
        screen = f"{gpt_data.get('renewal') or ''} {total_premium_raw or ''}"
        if "월납" in screen or str(total_premium_raw or "").strip().startswith("월"):
            contract_summary["payment_cycle"] = "월납"
        elif "연납" in screen or str(total_premium_raw or "").strip().startswith("연"):
            contract_summary["payment_cycle"] = "연납"
    contract_summary["total_premium"] = _to_total_premium(total_premium_raw, contract_summary.get("payment_cycle"))
    contract_summary["total_premium_raw"] = total_premium_raw
    contract_summary["renewal"] = gpt_data.get("renewal")

    coverages = []
    for row in gpt_data.get("coverages", []):
        name = row.get("coverage_name")
        if _is_category_header(name):
            continue
        amount_raw = row.get("subscribed_amount_raw")
        premium_raw = row.get("premium_raw")
        amount_value = parse_won_amount(amount_raw) if amount_raw else None
        premium_value = parse_won_amount(premium_raw) if premium_raw else None
        coverages.append(
            {
                "coverage_name": name,
                "subscribed_amount": {
                    "per_time": None,
                    "annual_limit": amount_value,
                    "deductible": None,
                    "visit_limit": None,
                    "is_shared_limit": False,
                    "raw": amount_raw,
                },
                "premium": premium_value,
                "is_enrolled": row.get("is_enrolled", True),
            }
        )

    result = {
        "source": Path(image_paths[0]).parent.name,
        "document_type": "가입화면",
        "contract_summary": contract_summary,
        "coverages": coverages,
        "error": None,
    }
    canonicalize_combo_result(result)
    return result


if __name__ == "__main__":
    result = extract_from_image(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
