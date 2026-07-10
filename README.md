# 보험 약관 PDF 파서

보험 약관 PDF에서 보장 정보(보장명·금액·지급조건·주계약/특약·급여구분·본인부담금)를
추출하고, 원문과 대조해 신뢰도 점수를 부여하는 파이프라인. 독소조항(계약자에게
불리한 조항) 탐지와 Supabase 적재까지 지원한다.

## 파이프라인 (4단계)

```
PDF ─▶ pdf_extractor ─▶ rule_extractor ─▶ gpt_classifier ─▶ verifier ─▶ postprocess ─▶ JSON
      텍스트/제N조 분리   규칙 기반 선별     GPT 상세 추출     원문대조·신뢰도   정제
                        (보장키워드 OR 금액) (보장정보 추출)
```

1. **pdf_extractor** — pdfplumber로 텍스트 추출, `제N조(제목)` 단위 조항 분리
2. **rule_extractor** — 보장 키워드 OR 금액이 있는 조항을 후보로 선별(노이즈 제목 제외)
3. **gpt_classifier.classify_candidates** — GPT로 보장 정보를 구조화 JSON으로 상세 추출
4. **verifier** — 추출 결과를 원문과 대조해 0~100 신뢰도 점수 부여

> **GPT 1차 판단(`filter_by_gpt`)은 현재 비활성화됨.** 회수율(recall)을 우선하기로 결정해
> 파이프라인에서 제외했다(보장 추출 38건 → ~60건). 함수 자체는 `parser/gpt_classifier.py`에
> 남아 있어 필요 시 다시 켤 수 있다.

### 후처리 (`postprocess.py`)

verifier 결과를 JSON으로 쓰기 직전(그리고 DB 적재 직전)에 아래 3단계를 **순서대로** 적용한다.
`main.py`와 `upload_to_supabase.py`가 같은 모듈을 공유하며, 여러 번 실행해도 결과가 같다(멱등).

1. **저신뢰 금액 무효화** — `confidence < 50`이면 `amount`를 `null`로 지운다(항목은 유지).
   원문 대조에 실패한 금액은 환각일 수 있어 신뢰하지 않는다.
2. **amount 채우기** — 같은 `coverage_name` 그룹에 신뢰 가능한 금액이 있으면 `null` 항목에 채운다.
   1번을 먼저 돌려야 환각 금액이 donor가 되어 그룹 전체로 번지는 것을 막을 수 있다.
3. **중복 제거(dedup)** — `(coverage_name, amount, contract_type)`이 같으면 같은 보장으로 보고
   `confidence`가 가장 높은 1건만 남긴다. `payment_condition`은 GPT가 조항마다 다르게 인용하므로
   키에 넣지 않는다.

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY 입력
```

## 사용법

```bash
# 1) 파싱 (data/raw_pdfs/ 에 약관 PDF를 넣고 실행)
python main.py "data/raw_pdfs/약관.pdf"
#   → data/parsed/약관_result.json 저장

# 보장 추출 + 독소조항 탐지 동시
python main.py "data/raw_pdfs/약관.pdf" --toxic
#   → data/parsed/약관_result.json + data/parsed/약관_toxic.json

# 독소조항만 / GPT 없이 규칙 분류만 / 경로 직접 지정
python main.py "data/raw_pdfs/약관.pdf" --toxic-only
python main.py "data/raw_pdfs/약관.pdf" --no-gpt
python main.py "data/raw_pdfs/약관.pdf" --out 경로.json

# 2) 파싱 결과 목록 확인
python upload_to_supabase.py --list

# 3) Supabase 적재
python upload_to_supabase.py --result "data/parsed/약관_result.json" --insurer 삼성화재 --category 실손

# (선택) GPT 단독 vs 파이프라인 정확도 비교
python accuracy_compare.py "data/raw_pdfs/약관.pdf"
```

## 구조

| 파일 | 역할 |
|------|------|
| `parser/pdf_extractor.py` | pdfplumber 텍스트 추출, `제N조` 단위 조항 분리 |
| `parser/rule_extractor.py` | 보장/질병 키워드 + 금액 패턴 감지, 노이즈 제목 필터 |
| `parser/gpt_classifier.py` | GPT 상세 추출(보장명/금액/급여구분/본인부담금 등). `filter_by_gpt`는 비활성화(미사용) |
| `validator/verifier.py` | 원문 대조 검증 + 신뢰도 점수(0~100) |
| `postprocess.py` | 저신뢰 금액 무효화 + amount 채우기 + 중복 보장 dedup (main/upload 공용) |
| `toxic_detector.py` | 독소조항(면책·제한·감액·기간제한·고지의무) 탐지 |
| `upload_to_supabase.py` | 파싱 결과 → Supabase 적재 (`--list`로 목록 확인) |
| `accuracy_compare.py` | GPT 단독 vs 파이프라인 정확도(완결성·원문일치·노이즈) 비교 |
| `main.py` | PDF 경로를 인자로 받아 전체 실행 |

## 출력 구조

파싱 결과는 `data/parsed/`에 자동 저장된다(폴더 없으면 자동 생성).

```
data/
├── raw_pdfs/   # 입력: 약관 PDF
└── parsed/     # 출력: PDF이름_result.json, PDF이름_toxic.json
```

## 신뢰도 점수 (0~100)

| 항목 | 배점 |
|------|------|
| 보장명 원문 근거 | 25 |
| 금액 원문 일치 | 30 (금액 없는 보장은 15 부분 인정) |
| 지급조건 원문 근거 | 20 |
| `source_quote` 원문 포함 | 25 (정확 일치 또는 어절 70% 겹침) |

80↑ 높음 · 50↑ 보통 · 그 외 낮음
