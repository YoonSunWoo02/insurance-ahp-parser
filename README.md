# 보험 약관 PDF 파서

보험 약관 PDF에서 보장 정보(보장명·금액·지급조건·주계약/특약·급여구분·본인부담금)를
추출하고, 원문과 대조해 신뢰도 점수를 부여하는 파이프라인. 독소조항(계약자에게
불리한 조항) 탐지와 Supabase 적재까지 지원한다.

## 파이프라인 (5단계)

```
PDF ─▶ pdf_extractor ─▶ rule_extractor ─▶ gpt_classifier ─▶ gpt_classifier ─▶ verifier ─▶ JSON
      텍스트/제N조 분리   규칙 1차 후보     GPT 1차 판단      GPT 2차 상세      원문대조·신뢰도
                        (보장키워드 OR 금액) (보장 관련 선별)   (보장정보 추출)
```

1. **pdf_extractor** — pdfplumber로 텍스트 추출, `제N조(제목)` 단위 조항 분리
2. **rule_extractor** — 보장 키워드 OR 금액이 있는 조항을 후보로 선별(노이즈 제목 제외)
3. **gpt_classifier.filter_by_gpt** — GPT 1차 판단으로 '보장 관련' 조항만 빠르게 선별
4. **gpt_classifier.classify_candidates** — GPT 2차로 보장 정보를 구조화 JSON으로 상세 추출
5. **verifier** — 추출 결과를 원문과 대조해 0~100 신뢰도 점수 부여

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
| `parser/gpt_classifier.py` | GPT 1차 판단(filter_by_gpt) + 2차 상세 추출(보장명/금액/급여구분/본인부담금 등) |
| `validator/verifier.py` | 원문 대조 검증 + 신뢰도 점수(0~100) |
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
