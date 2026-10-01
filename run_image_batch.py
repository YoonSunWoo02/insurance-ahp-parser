"""data/images/db손보/ 아래 모든 스크린샷을 파싱해 data/image_results/{직업}/{나이}/
밑에 파일당 JSON 하나씩 저장한다.

이미 테스트로 확인한 2개 샘플도 포함해 전체를 다시 도는 게 아니라, 아직 처리 안 된
파일만 순회한다(멱등 재실행 가능하도록 결과 파일이 이미 있으면 건너뛴다).
"""

import io
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from parser.image_extractor import extract_from_image, _parse_path_metadata  # noqa: E402

SRC_ROOT = Path("data/images/db손보")
OUT_ROOT = Path("data/image_results")
LOG_PATH = Path("image_batch_log.txt")


def main() -> None:
    images = sorted(SRC_ROOT.rglob("*.png"))

    with io.open(LOG_PATH, "w", encoding="utf-8") as log:
        log.write(f"배치 시작: 총 {len(images)}개 이미지\n")
        log.flush()

        ok = 0
        skipped = 0
        failed = 0
        for i, img_path in enumerate(images, 1):
            meta = _parse_path_metadata(str(img_path))
            job = meta["job"] or "미상"
            age_folder = meta["age_raw"] or "미상"
            out_dir = OUT_ROOT / job / age_folder
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{img_path.stem}_screen.json"

            if out_path.exists():
                skipped += 1
                continue

            log.write(f"[{i}/{len(images)}] {img_path.name}\n")
            log.flush()
            start = time.time()
            try:
                result = extract_from_image(str(img_path))
                with io.open(out_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                elapsed = time.time() - start
                if result.get("error"):
                    failed += 1
                    log.write(f"  → FAIL (error={result['error']}), {elapsed:.1f}s\n")
                else:
                    ok += 1
                    log.write(f"  → OK, 담보 {len(result['coverages'])}건, {elapsed:.1f}s\n")
            except Exception as e:
                failed += 1
                log.write(f"  → EXCEPTION: {e}\n")
            log.flush()

        log.write("\n===== 배치 요약 =====\n")
        log.write(f"성공 {ok}, 실패 {failed}, 이미 처리돼 건너뜀 {skipped}, 총 {len(images)}\n")


if __name__ == "__main__":
    main()
