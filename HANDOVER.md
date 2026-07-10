# 인수인계 문서 — 보험 약관 PDF 파서

> 최종 정리일 기준. 다음 작업자가 바로 이어받을 수 있도록 현재 상태·이슈·다음 할 일을 정리.

## 1. 프로젝트 개요

보험 약관 PDF에서 **보장 정보**와 **독소조항**을 추출해 JSON으로 만들고, Supabase DB에 적재하는 파이프라인.

- **로컬 경로**: `D:\바탕화면\insurance-ahp-parser`
- **GitHub**: https://github.com/YoonSunWoo02/insurance-ahp-parser (브랜치 `main`)
- **언어/주요 의존성**: Python 3.12, pdfplumber, openai, pandas, supabase, python-dotenv

## 2. 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env      # 키 입력 (아래 4번 참고)

# 파싱 (보장 + 독소조항)
python main.py "data/raw_pdfs/약관.pdf" --toxic
#  → data/parsed/약관_result.json , data/parsed/약관_toxic.json

# 결과 목록 확인
python upload_to_supabase.py --list

# DB 적재
python upload_to_supabase.py --result "data/parsed/약관_result.json" --insurer 삼성화재 --category 실손
```

## 3. 파이프라인 (4단계)

```
PDF → pdf_extractor → rule_extractor → gpt_classifier → verifier → JSON
      조항 분리        규칙 기반 선별    GPT 상세 추출    원문대조·신뢰도
```

1. **pdf_extractor**: pdfplumber 추출 → `제N조(제목)` 단위 분리 (줄 시작 + 괄호 제목만 인정 → 목차/상호참조 노이즈 차단)
2. **rule_extractor**: 보장 키워드 **OR** 금액이 있으면 후보 (노이즈 제목 제외)
3. **gpt_classifier.classify_candidates**: GPT로 보장 정보 상세 추출 (표/목록은 항목별로 열거)
4. **verifier**: 원문 대조 0~100 신뢰도 (source_quote는 정확일치 **또는 어절 70% 겹침** 인정)

이후 **postprocess.py**가 JSON 저장 전(그리고 DB 적재 전)에 정제 3단계를 적용한다 → 저신뢰 금액 무효화 → amount 채우기 → 중복 dedup. (12번 참고)

> `gpt_classifier.filter_by_gpt`(GPT 1차 판단)는 회수율 우선 결정으로 **파이프라인에서 제외됨**. 함수는 남아 있음. (10번 참고)

## 4. 파일 구조 & 역할

| 파일 | 역할 |
|------|------|
| `main.py` | 진입점. `--toxic`/`--toxic-only`/`--no-gpt`/`--out`/`--stdout` |
| `parser/pdf_extractor.py` | 텍스트 추출 + 조항 분리 |
| `parser/rule_extractor.py` | 키워드/금액 감지, 노이즈 제목·금액 필터, 후보 선별 |
| `parser/gpt_classifier.py` | GPT 상세 추출. `filter_by_gpt`는 정의만 남고 미사용 |
| `validator/verifier.py` | 원문 대조 검증 + 신뢰도 |
| `postprocess.py` | 저신뢰 금액 무효화 + amount 채우기 + dedup (main/upload 공용, 멱등) |
| `toxic_detector.py` | 독소조항 탐지 (키워드 후보 → GPT 판단 → source_quote 검증 필터) |
| `upload_to_supabase.py` | Supabase 적재, `--list`, `data/parsed/` 자동 탐색 |
| `accuracy_compare.py` | GPT 단독 vs 파이프라인 정확도 비교 |
| `parser/csv_parser.py` | (별도) 통계 CSV → slider_init.json 변환 |
| `show_toxic.py` | `toxic_result.json` 콘솔 출력용 헬퍼 (구버전 경로 사용 주의) |

## 5. 환경설정

`.env` (git에 **올리지 않음**, `.gitignore`로 제외됨):
```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=<service-role-key>
```
모델/타임아웃: `gpt_classifier.py`의 `MODEL`, `REQUEST_TIMEOUT=30`.

## 6. 출력/저장 구조

```
data/
├── raw_pdfs/   # 입력 PDF (gitignore: *.pdf 제외, 폴더만 유지)
└── parsed/     # 출력 (gitignore: 내용 제외, 폴더는 .gitkeep으로 유지)
     ├── <PDF이름>_result.json   # 보장 추출 결과
     └── <PDF이름>_toxic.json    # 독소조항 (--toxic 시)
```

## 7. Supabase 적재

- 테이블: `insurance_product`, `product_coverage`, `product_toxic_clause`
- 삼성화재 실손 상품 `product_id`: **`d3defa99-2ec0-4af4-aae0-c05f2cc0e740`** (source_pdf 기준 upsert로 재사용)
- `parse_amount`: "5천만원"→50,000,000 등 금액 문자열→정수 변환 (`upload_to_supabase.py`)

## 8. 형상관리 현황

```
bc41da4 chore: data/parsed/ 폴더 .gitkeep 포함
7b9b88b docs: README 4→5단계 갱신
95aaedf 초기 커밋
```

## 9. 최종 테스트 결과 (무배당 삼성화재 실손 2501.5) — 2026-07-10 갱신

| 단계 | GPT 투입 조항 | 보장 건수 |
|---|---|---|
| (구) 1차 필터 있음 | 16 | 41 |
| 1차 필터 제거만 | 77 | 65 (**62%가 노이즈**) |
| + 노이즈 제목 필터 | 55 | 26 |
| **+ 후처리(dedup 포함) → 현재** | 55 | **19** |

- 독소조항: **47건 / 31조항** (변동 없음)
- 저장: `data/parsed/무배당 삼성화재 실손의료비보험(2501.5)_*.json`
- 건수는 줄었지만 내용은 실제 보장만 남음: `상해급여`·`질병급여`·`상해비급여`·`질병비급여`(각 5천만원), `본인부담금 상한제`, `상급병실료` 등.

## 10. ⚠️ 미해결 / 다음 작업자가 결정할 것

### ✅ 해결됨 (2026-07-10)

**1) GPT 1차 필터(filter_by_gpt) recall 트레이드오프 — 결정 완료**
- **회수율 우선으로 1차 필터를 파이프라인에서 제거함.** 파이프라인 5단계 → **4단계**.
- `main.py run()`에서 `filter_by_gpt` 호출 제거, `classify_candidates(select_candidate_articles(...))`로 직접 연결.
- 함수 자체는 `parser/gpt_classifier.py`에 **남겨둠** (비용 절감이 다시 필요하면 재활성화 가능).
- 대가: GPT 호출 16 → 55회(약 3.4배). 큰 약관은 비용 모니터링 필요.

**2) 필터 제거 후 노이즈 폭증 → 노이즈 제목 필터로 대응**
- 1차 필터가 사라지자 면책조항(`보상하지 않는 사항`)과 약관 뒤 법령 인용 조문이 GPT에 그대로 들어가 65건 중 **40건(62%)이 노이즈**가 됨. GPT가 "보장하지 **않는** 항목"을 보장으로 뒤집어 읽음.
- `rule_extractor.NOISE_TITLE_KEYWORDS`에 면책조항·법령 인용 조문 제목 추가 → 후보 77 → 55개.

**3) 저신뢰 금액 환각 → postprocess에서 무효화**
- `상해입원의료비 "1,000만원"` 등 confidence 0인데 금액이 적재되던 문제. `confidence < 50`이면 **amount만 null**로 지우고 항목은 유지(`postprocess.clear_low_confidence_amounts`).
- **순서 주의**: 무효화 → amount 채우기 순이어야 환각 금액이 donor로 그룹 전체에 번지지 않는다.

**4) 중복 보장 → postprocess에서 dedup**
- 같은 보장이 여러 조항에 서술되어 `product_coverage`에 중복 적재되던 문제. `(coverage_name, amount, contract_type)` 기준으로 **confidence 최고 1건만** 남김. 26 → 19건.
- `payment_condition`은 키에서 **뺐다**. GPT가 조항마다 원문을 다르게 인용해 넣으면 중복이 하나도 안 잡힌다(실측 26 → 26건).

**5) 법조문 인용/정의 조항이 보장으로 오탐 + GPT enum 필드 미검증 (2026-07-11)**
- **문제**: 무배당 참 편한 실손 1901 파싱 시 추출 보장 22건 중 **18건(82%)이 가짜**.
  - 제7조 "의료급여의 내용 등": 국민건강보험법상 요양급여 종류(진찰·검사, 약제, 처치·수술, 예방·재활, 입원, 간호, 이송 7개)를 보장으로 오탐.
  - 제2조 "응급환자": 응급의료법상 응급증상 판정 기준 11개를 보장으로 오탐.
  - 둘 다 신뢰도 85로 나와 기존 검수(신뢰도 50 미만만 의심)로 안 걸러짐.
  - 추가로 `contract_type`에 GPT 프롬프트 선택지 문자열(`"주계약 | 특약 | 불명"`)이 통째로 박히는 버그. `benefit_type`도 동일 패턴 가능.
- **원인**: 법령을 인용/정의하는 조항과 실제 보장 조항을 구분하는 로직 부재 + GPT 출력 enum 필드 검증 누락.
- **조치**:
  - `rule_extractor`: 노이즈 제목에 `의료급여의 내용`·`응급환자`·`응급증상` 추가 + **일반화 휴리스틱** `is_law_citation()` — 「○○법」/"법 제N조" 인용이 있고 **금액이 전혀 없는** 조항을 후보에서 제외(금액 있으면 보장으로 인정해 회수율 보호).
  - `gpt_classifier`: 프롬프트에 "법령을 인용/정의하는 조항이면 coverages를 빈 배열로 반환하라" 지시 추가.
  - `verifier`: `contract_type`/`coverage_type`/`benefit_type`이 허용값에 정확히 속하지 않으면 **"불명" 치환 + checks에 `<field>_valid` 기록 + 필드당 10점 감점**(`ALLOWED_ENUM_VALUES`, `ENUM_INVALID_PENALTY`).
  - 재현 테스트: `test_bugfix_law_enum.py` (pytest 불필요, `python test_bugfix_law_enum.py`).

### 남은 이슈

**a) GPT의 `contract_type` 라벨 불일치**
- `상해비급여`/`질병비급여`가 한 조항에선 `특약`(conf 85), 다른 조항에선 `주계약`(conf 100)으로 라벨링됨. 4세대 실손 기준 비급여는 **특약**이 맞으므로 conf 100쪽 라벨이 오히려 틀림.
- dedup 키에 `contract_type`이 있어 둘 다 살아남음(19건 중 4건). 임의 병합하지 않고 보존 중. → GPT 프롬프트에 주계약/특약 판단 기준을 명시하는 게 근본 해결.

**b) `parse_amount`가 범위 금액의 하한만 취함**
- `"81만원~584만원"` → `810,000`, `"5만원 | 80만원 | 120만원"` → `50,000`. 본인부담금 상한제는 소득분위별 구간이라 단일 값이 아님. JSON엔 원본 문자열이 남아 있어 손실은 없으나 `coverage_amount` 컬럼만 보면 오해 소지.

**c) Supabase 프로젝트 접속 불가 (2026-07-10 확인)**
- `kdkrtgpbxxevhvnnkarg.supabase.co` → **NXDOMAIN**(공용 DNS에서도 동일). 프로젝트 일시정지 또는 삭제로 추정. **DB에는 아직 구버전 41건이 남아 있음** — 복구 후 재적재 필요.

**d) enum 검증이 `coverage_type` 조합값도 "불명"으로 치환함 (2026-07-11)**
- `verifier`의 enum 검증은 허용값 **정확 일치**만 인정한다. 그런데 GPT는 입원·통원을 모두 보장하는 항목에 `coverage_type="입원 | 통원"`처럼 조합값을 정당하게 반환하는 경우가 있다(삼성화재 실손 다수). 이 조합값은 허용 목록(`입원|통원|수술|불명`)에 없어 **"불명"으로 치환되고 10점 감점**된다.
- 즉 이번 수정으로 실제 버그(선택지 문자열 통째 반환)는 잡지만, 정당한 조합값도 함께 희생된다. 필요하면 `ALLOWED_ENUM_VALUES` 검사에서 `coverage_type`에 한해 `" | "` 분해 후 각 조각이 모두 허용값이면 인정하도록 완화 검토. (현재는 명세대로 엄격 적용 상태)

## 11. ⚠️ 알려진 환경 이슈 (중요)

1. **한글 파일명 NFC/NFD**: 이 환경에서 한글 이름 JSON이 셸 열거(`os.listdir`/`Get-ChildItem`)에 **간헐적으로 안 보임**. 루트에 한글 결과 파일을 두면 접근이 불안정 → **`data/parsed/`에 두고 glob/`--list`로 접근**하면 정상. 가능하면 `--out`으로 ASCII 이름 권장.
2. **cp949 콘솔**: Windows 기본 콘솔에서 한글/이모지 stderr가 깨져 보임(출력만 문제, 데이터는 정상). `upload_to_supabase.py`는 `sys.stdout.reconfigure(utf-8)`로 해결됨. 스크립트 실행 시 `PYTHONUTF8=1` 또는 stdout utf-8 래핑 권장.
3. **`.env` 절대 커밋 금지**: 이미 `.gitignore` 처리됨. 혹시 public 노출 시 OpenAI/Supabase 키 즉시 재발급(rotate).
4. **비용**: GPT 호출이 조항 수만큼 발생. 큰 약관은 수십~수백 콜. 튜닝 시 `accuracy_compare.py --max-chunks 1`로 baseline 비용 줄이기.

## 12. 주요 의사결정 이력 (왜 이렇게 됐나)

- **조항 분리**: "줄 시작 + 괄호 제목"만 헤더로 인정 → 목차 stub(본문 50자 미만)·상호참조 노이즈 제거.
- **rule_extractor 완화**: 보장+금액 AND → OR. 회수율↑, 정밀도는 뒤 단계가 보강.
- **parse_amount 버그**: "5천만원"이 `5`로 적재되던 것 → `천만원` 패턴 우선 처리로 50,000,000 교정.
- **verifier 완화**: GPT가 인용을 살짝 바꿔도 인정하도록 source_quote를 "정확일치 OR 어절 70%"로. (노이즈 비율 8.3%→1.6%)
- **postprocess 공용 모듈화**: 정제 로직이 `upload_to_supabase.py`에만 있어 JSON 산출물은 정제되지 않는 문제가 있었음. `postprocess.py`로 빼서 `main.py`(JSON 저장 전)와 `upload_to_supabase.py`(DB 적재 전)가 공유. 멱등이라 두 번 돌아도 안전.
- **저신뢰 항목: 제거 대신 금액만 null**: 보장명 자체는 실재할 수 있으므로 항목을 버리지 않음. AHP 등 후속 분석에서 보장 목록이 필요하기 때문.
- **dedup 키에서 payment_condition 제외**: GPT가 조항마다 원문을 다르게 인용해 키로 쓰면 중복이 전혀 안 잡힘(실측 26→26건).
- **노이즈 제목 확장**: 배당금·약관해석·연대책임·해약환급금·보험료납입 등 절차성 조항 제외.
- **독소조항 필터**: source_quote 15자 미만 제외 + 원문 대조 실패(환각) 제외, summary에 제외 건수 기록.
