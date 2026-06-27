# 보험 약관 PDF 파서

보험 약관 PDF에서 보장 정보(보장명·금액·지급조건·주계약/특약 구분)를 추출하고,
원문과 대조해 신뢰도 점수를 부여하는 파이프라인.

## 파이프라인

```
PDF ──▶ pdf_extractor ──▶ rule_extractor ──▶ gpt_classifier ──▶ verifier ──▶ JSON
       텍스트/제N조 분리    정규식 1차 분류      GPT JSON 추출     원문대조·신뢰도
```

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY 입력
```

## 사용법

```bash
# data/raw_pdfs/ 에 약관 PDF를 넣고:
python main.py data/raw_pdfs/약관.pdf

# 결과를 파일로 저장
python main.py data/raw_pdfs/약관.pdf --out result.json

# GPT 없이 규칙 기반 1차 분류만 (API 키 불필요)
python main.py data/raw_pdfs/약관.pdf --no-gpt
```

## 구조

| 파일 | 역할 |
|------|------|
| `parser/pdf_extractor.py` | pdfplumber 텍스트 추출, `제N조` 단위 조항 분리 |
| `parser/rule_extractor.py` | 진단비/수술비/입원비/골절/사망 키워드 + 금액 패턴 감지 |
| `parser/gpt_classifier.py` | GPT로 보장명/금액/지급조건/주계약·특약 JSON 추출 |
| `validator/verifier.py` | 원문 대조 검증 + 신뢰도 점수(0~100) |
| `main.py` | PDF 경로를 인자로 받아 전체 실행 |

## 신뢰도 점수 (0~100)

| 항목 | 배점 |
|------|------|
| 보장명 원문 근거 | 25 |
| 금액 원문 일치 | 30 (금액 없는 보장은 15 부분 인정) |
| 지급조건 원문 근거 | 20 |
| `source_quote` 원문 포함 | 25 |

80↑ 높음 · 50↑ 보통 · 그 외 낮음
