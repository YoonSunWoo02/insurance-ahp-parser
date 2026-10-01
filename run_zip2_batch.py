"""db손보(2).zip에서 푼 data/images_new/db손보(2)/ 전체를 파싱한다.

- PNG: 조합(combo) 폴더별로 이미지 여러 장(2~3장)을 묶어 한 번에 GPT에 넘기고,
  결과를 data/image_results/db손보(2)/{같은 상대경로}/{combo명}_screen.json 로 저장.
- PDF: 진짜 약관 파일이라 기존 main.py PDF 파이프라인(--toxic 포함)으로 처리하고
  data/parsed/ 밑에 표준 _result.json/_toxic.json으로 저장.

이미 결과 파일이 있으면 건너뛴다(멱등 재실행 가능).
"""

import io
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from parser.image_extractor import extract_from_combo, parse_zip2_combo_metadata  # noqa: E402

ZIP_ROOT = Path(r"data/images_new/db손보(2)")
OUT_ROOT = Path(r"data/image_results/db손보(2)")
LOG_PATH = Path("zip2_batch_log.txt")


def process_images(log) -> tuple[int, int, int]:
    pngs = list(ZIP_ROOT.rglob("*.png"))
    combos: dict[Path, list[Path]] = defaultdict(list)
    for p in pngs:
        combos[p.parent].append(p)

    combo_dirs = sorted(combos.keys())
    log.write(f"이미지 조합 총 {len(combo_dirs)}개\n")
    log.flush()

    ok = failed = skipped = 0
    for i, combo_dir in enumerate(combo_dirs, 1):
        rel = combo_dir.relative_to(ZIP_ROOT)
        out_path = OUT_ROOT / rel.parent / f"{combo_dir.name}_screen.json"
        if out_path.exists():
            skipped += 1
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        image_paths = [str(p) for p in combos[combo_dir]]

        log.write(f"[{i}/{len(combo_dirs)}] {rel}\n")
        log.flush()
        start = time.time()
        try:
            meta = parse_zip2_combo_metadata(image_paths[0], str(ZIP_ROOT))
            result = extract_from_combo(image_paths, meta)
            elapsed = time.time() - start
            if result.get("error"):
                # 실패 결과는 파일로 남기지 않는다 — 저장해두면 다음 재실행 때
                # "이미 처리됨"으로 오판해 건너뛰게 된다(1차 배치에서 실제로 이 버그로
                # 187개가 재시도 없이 전부 스킵됐다).
                failed += 1
                log.write(f"  → FAIL ({result['error']}), {elapsed:.1f}s\n")
            else:
                with io.open(out_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                ok += 1
                log.write(f"  → OK, 담보 {len(result['coverages'])}건, {elapsed:.1f}s\n")
        except Exception as e:
            failed += 1
            log.write(f"  → EXCEPTION: {e}\n")
        log.flush()

    return ok, failed, skipped


def process_pdfs(log) -> tuple[int, int]:
    pdfs = sorted(ZIP_ROOT.rglob("*.pdf"))
    log.write(f"\nPDF 총 {len(pdfs)}개\n")
    log.flush()

    ok = failed = 0
    for i, pdf_path in enumerate(pdfs, 1):
        result_path = Path("data/parsed") / f"{pdf_path.stem}_result.json"
        if result_path.exists():
            log.write(f"[PDF {i}/{len(pdfs)}] {pdf_path.name} → 이미 처리됨, 건너뜀\n")
            continue

        log.write(f"[PDF {i}/{len(pdfs)}] {pdf_path.name}\n")
        log.flush()
        start = time.time()
        proc = subprocess.run(
            [sys.executable, "main.py", str(pdf_path), "--toxic"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.time() - start
        log.write(proc.stderr)
        if proc.returncode == 0:
            ok += 1
            log.write(f"  → OK, {elapsed:.1f}s\n")
        else:
            failed += 1
            log.write(f"  → FAIL(code={proc.returncode}), {elapsed:.1f}s\n")
        log.flush()

    return ok, failed


def main() -> None:
    with io.open(LOG_PATH, "w", encoding="utf-8") as log:
        log.write("db손보(2) 배치 시작\n")
        log.flush()

        img_ok, img_failed, img_skipped = process_images(log)
        pdf_ok, pdf_failed = process_pdfs(log)

        log.write("\n===== 배치 요약 =====\n")
        log.write(f"이미지: 성공 {img_ok}, 실패 {img_failed}, 건너뜀 {img_skipped}\n")
        log.write(f"PDF: 성공 {pdf_ok}, 실패 {pdf_failed}\n")


if __name__ == "__main__":
    main()
