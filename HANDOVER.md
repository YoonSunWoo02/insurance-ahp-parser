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

> `gpt_classifier.filter_by_gpt`(GPT 1차 판단)는 회수율 우선 결정으로 **파이프라인에서 제외됨**. 함수는 남아 있음. (10번 참고)

## 4. 파일 구조 & 역할

| 파일 | 역할 |
|------|------|
| `main.py` | 진입점. `--toxic`/`--toxic-only`/`--no-gpt`/`--out`/`--stdout` |
| `parser/pdf_extractor.py` | 텍스트 추출 + 조항 분리 |
| `parser/rule_extractor.py` | 키워드/금액 감지, 노이즈 제목·금액 필터, 후보 선별 |
| `parser/gpt_classifier.py` | GPT 1차 판단(filter_by_gpt) + 2차 상세 추출 |
| `validator/verifier.py` | 원문 대조 검증 + 신뢰도 |
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

## 9. 최종 테스트 결과 (무배당 삼성화재 실손 2501.5)

| | 값 |
|---|---|
| 추출 보장 | **38건** (신뢰도 평균 78) |
| 독소조항 | **47건 / 31조항** |
| 저장 | `data/parsed/무배당 삼성화재 실손의료비보험(2501.5)_*.json` |

## 10. ⚠️ 미해결 / 다음 작업자가 결정할 것

**GPT 1차 필터(filter_by_gpt)의 recall 트레이드오프** — ✅ **결정 완료 (2026-07-10)**
- **결정: 회수율(recall) 우선으로 1차 필터를 파이프라인에서 제거함.** 보장 추출 38건 → ~60건.
- 배경: 1차 필터가 후보 77개 → 17개로 줄여 진짜 보장 조항까지 걸러냈음. GPT 호출 비용 절감용이었으나 본질적으로 recall을 깎음.
- 적용: `main.py run()`에서 `filter_by_gpt` 호출 제거, `classify_candidates(select_candidate_articles(...))`로 직접 연결. 파이프라인 5단계 → **4단계**.
- `filter_by_gpt` 함수 자체는 `parser/gpt_classifier.py`에 **남겨둠** (비용 절감이 다시 필요하면 재활성화 가능).
- 트레이드오프: GPT 호출 수가 늘어 비용 증가. 큰 약관 처리 시 비용 모니터링 필요.

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
- **노이즈 제목 확장**: 배당금·약관해석·연대책임·해약환급금·보험료납입 등 절차성 조항 제외.
- **독소조항 필터**: source_quote 15자 미만 제외 + 원문 대조 실패(환각) 제외, summary에 제외 건수 기록.
