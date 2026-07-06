"""Download all swissALTIRegio tiles listed in a swisstopo CSV.

The CSV (e.g. ``data/ch.swisstopo.swissaltiregio-*.csv``) contains one tile
download URL per line (``.xyz.zip`` files, ~5.5 MB each). This script fetches
them in parallel, is fully resumable (skips files already present and valid),
retries transient failures, and records permanent failures to a log.

Purpose: build a real high-res DEM dataset to end-to-end verify the plugin's
download -> convert -> select pipeline.

Usage:
    python3 tools/download_swissaltiregio.py \
        --csv data/ch.swisstopo.swissaltiregio-4EKtpOb5.csv \
        --out data/swissaltiregio_tiles \
        --workers 12
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

_ZIP_MAGIC = b"PK\x03\x04"
_MAX_RETRIES = 4
_TIMEOUT_S = 60


def read_urls(csv_path: str) -> list[str]:
    urls: list[str] = []
    with open(csv_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and line.lower().startswith("http"):
                urls.append(line)
    return urls


def is_valid_zip(path: str) -> bool:
    """Cheap validity check: non-empty and starts with the ZIP magic bytes."""
    try:
        if os.path.getsize(path) < 64:
            return False
        with open(path, "rb") as fh:
            return fh.read(4) == _ZIP_MAGIC
    except OSError:
        return False


def download_one(url: str, out_dir: str) -> tuple[str, str, str]:
    """Return (url, status, detail). status in {skip, ok, fail}."""
    name = url.rsplit("/", 1)[-1]
    dst = os.path.join(out_dir, name)
    if is_valid_zip(dst):
        return (url, "skip", name)

    tmp = dst + ".part"
    last_err = "unknown"
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aether-qgis-dl/1.0"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = resp.read()
            if not data.startswith(_ZIP_MAGIC):
                last_err = "not a zip payload"
                raise ValueError(last_err)
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dst)
            return (url, "ok", name)
        except Exception as exc:  # noqa: BLE001 — log & retry any transient error
            last_err = str(exc)
            if attempt < _MAX_RETRIES:
                time.sleep(1.0 * attempt)  # linear backoff: 1s, 2s, 3s
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    return (url, "fail", last_err)


def main() -> int:
    ap = argparse.ArgumentParser(description="Download swissALTIRegio tiles from a swisstopo CSV.")
    ap.add_argument("--csv", required=True, help="Path to the swisstopo CSV of tile URLs.")
    ap.add_argument("--out", required=True, help="Output directory for downloaded tiles.")
    ap.add_argument("--workers", type=int, default=12, help="Parallel download workers.")
    args = ap.parse_args()

    urls = read_urls(args.csv)
    if not urls:
        print(f"[Error] No URLs found in {args.csv}", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    fail_log = os.path.join(args.out, "_failures.txt")

    total = len(urls)
    print(f"swissALTIRegio download: {total} tiles -> {args.out} ({args.workers} workers)")
    t0 = time.perf_counter()
    ok = skip = fail = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(download_one, u, args.out): u for u in urls}
        done = 0
        for fut in as_completed(futs):
            _url, status, detail = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1
                failures.append(f"{_url}\t{detail}")
            if done % 25 == 0 or done == total:
                elapsed = time.perf_counter() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(
                    f"  {done}/{total}  ok={ok} skip={skip} fail={fail}  "
                    f"{rate:.1f} tiles/s  ETA {eta/60:.1f} min",
                    flush=True,
                )

    if failures:
        with open(fail_log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(failures) + "\n")
        print(f"[Warn] {fail} tiles failed — logged to {fail_log}")

    dt = time.perf_counter() - t0
    print(f"Done in {dt/60:.1f} min: ok={ok} skip={skip} fail={fail} of {total}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
