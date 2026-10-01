"""data/raw_pdfs/ 안의 모든 PDF를 순회하며 main.py 파이프라인을 돌려
data/parsed/ 밑에 결과 JSON을 만든다.

텍스트 추출이 안 되는(이미지 스캔) 파일, 손상된 파일은 건너뛴다.
각 파일은 별도 프로세스(subprocess)로 실행해 한 파일에서 예외가 나도
전체 배치가 죽지 않게 한다. 진행 상황과 최종 요약은 batch_log.txt에 기록한다.
"""

import io
import subprocess
import sys
import time
from pathlib import Path

RAW_DIR = Path("data/raw_pdfs")
LOG_PATH = Path("batch_log.txt")

# 텍스트 추출 불가(이미지 스캔) 또는 파일 손상 확인된 파일 — 사전 조사로 확인됨
SKIP_FILES = {
    "Notes_260328_220920.pdf",
    "Notes_260328_221237.pdf",
    "kakaoTalkSendForm_20260323103727975e8dkc07qvm8wu.pdf",
}


def main() -> None:
    pdfs = sorted(
        p for p in RAW_DIR.iterdir()
        if p.suffix.lower() == ".pdf" and p.name not in SKIP_FILES
    )

    with io.open(LOG_PATH, "w", encoding="utf-8") as log:
        log.write(f"배치 시작: 총 {len(pdfs)}개 파일\n")
        log.flush()

        results = []
        for i, pdf in enumerate(pdfs, 1):
            log.write(f"\n[{i}/{len(pdfs)}] {pdf.name}\n")
            log.flush()
            start = time.time()
            proc = subprocess.run(
                [sys.executable, "main.py", str(pdf), "--toxic"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.time() - start
            log.write(proc.stderr)
            status = "OK" if proc.returncode == 0 else f"FAIL(code={proc.returncode})"
            log.write(f"  → {status}, {elapsed:.1f}s\n")
            log.flush()
            results.append((pdf.name, status, elapsed))

        log.write("\n\n===== 배치 요약 =====\n")
        ok = sum(1 for _, s, _ in results if s == "OK")
        log.write(f"성공 {ok}/{len(results)}\n")
        for name, status, elapsed in results:
            log.write(f"  [{status}] {name} ({elapsed:.1f}s)\n")
        log.write("배치 종료\n")


if __name__ == "__main__":
    main()
