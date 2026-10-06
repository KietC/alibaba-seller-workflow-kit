from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ALLOWED_HOSTS = {host.strip().lower() for host in os.environ.get("SELLERKIT_SITE_ALLOWED_HOSTS", "catalog.example.invalid").split(",") if host.strip()}
SUCCESS_STATUSES = {
    "downloaded",
    "downloaded_cached",
    "downloaded_hash_verified",
    "downloaded_hash_changed",
    "downloaded_hash_new",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 SellerKitHashRefresh/1.0"
)
TIMEOUT_SECONDS = 90
MAX_ASSET_BYTES = 512 * 1024 * 1024
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
CHECKPOINT_EVERY = 10
REQUEST_DELAY_SECONDS = 0.08


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


def write_assets_csv(path: Path, rows: list[dict]) -> None:
    preferred = [
        "url",
        "final_url",
        "status",
        "category",
        "extension",
        "content_type",
        "size_bytes",
        "local_file",
        "referenced_by_count",
        "referenced_by",
        "note",
        "sha256",
        "previous_sha256",
        "hash_scan_action",
        "hash_scanned_at",
    ]
    extras = sorted({key for row in rows for key in row} - set(preferred))
    fields = preferred + extras
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def build_request(url: str) -> Request:
    return Request(
        url,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
            "Cache-Control": "no-cache",
        },
    )


def load_resume_rows(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("url"):
            rows[row["url"]] = row
    return rows


def hash_scan_one(row: dict) -> dict:
    url = row.get("url") or row.get("final_url") or ""
    local_text = row.get("local_file") or ""
    local_path = Path(local_text) if local_text else None
    previous_sha = sha256_file(local_path) if local_path and local_path.exists() else ""
    result = {
        "url": url,
        "final_url": row.get("final_url") or "",
        "local_file": local_text,
        "previous_sha256": previous_sha,
        "sha256": "",
        "action": "",
        "size_bytes": 0,
        "content_type": "",
        "http_status": None,
        "scanned_at": now_iso(),
        "error": "",
    }

    if not url or urlparse(url).netloc.lower() not in ALLOWED_HOSTS:
        result["action"] = "SKIPPED_NOT_ALLOWED_HOST"
        return result

    last_error = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        temp_path = None
        try:
            with urlopen(build_request(url), timeout=TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 200))
                result["http_status"] = status
                result["final_url"] = response.geturl()
                result["content_type"] = response.headers.get("Content-Type", "")
                content_length = int(response.headers.get("Content-Length") or 0)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                if content_length > MAX_ASSET_BYTES:
                    raise ValueError(f"asset content-length exceeds {MAX_ASSET_BYTES} bytes")
                if local_path is None:
                    raise ValueError("asset report has no local_file path")

                local_path.parent.mkdir(parents=True, exist_ok=True)
                temp_name = ".hashscan-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12] + ".part"
                temp_path = local_path.with_name(temp_name)
                digest = hashlib.sha256()
                total = 0
                with temp_path.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ASSET_BYTES:
                            raise ValueError(f"asset exceeds {MAX_ASSET_BYTES} bytes")
                        digest.update(chunk)
                        handle.write(chunk)

                new_sha = digest.hexdigest()
                result["sha256"] = new_sha
                result["size_bytes"] = total
                if previous_sha and previous_sha == new_sha:
                    temp_path.unlink(missing_ok=True)
                    result["action"] = "UNCHANGED_HASH_MATCH"
                elif previous_sha:
                    temp_path.replace(local_path)
                    result["action"] = "UPDATED_HASH_CHANGED"
                else:
                    temp_path.replace(local_path)
                    result["action"] = "NEW_OR_RECOVERED"
                return result
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError, ValueError) as error:
            last_error = repr(error)
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
        except Exception as error:
            last_error = repr(error)
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            break

    result["action"] = "ERROR_REMOTE_HASH_SCAN"
    result["error"] = last_error
    return result


def update_asset_row(row: dict, result: dict) -> None:
    row["previous_sha256"] = result.get("previous_sha256") or ""
    row["sha256"] = result.get("sha256") or row["previous_sha256"]
    row["hash_scan_action"] = result.get("action") or ""
    row["hash_scanned_at"] = result.get("scanned_at") or ""
    action = result.get("action")
    if action == "UNCHANGED_HASH_MATCH":
        row["status"] = "downloaded_hash_verified"
    elif action == "UPDATED_HASH_CHANGED":
        row["status"] = "downloaded_hash_changed"
        row["size_bytes"] = result.get("size_bytes") or row.get("size_bytes") or 0
        row["content_type"] = result.get("content_type") or row.get("content_type") or ""
    elif action == "NEW_OR_RECOVERED":
        row["status"] = "downloaded_hash_new"
        row["size_bytes"] = result.get("size_bytes") or row.get("size_bytes") or 0
        row["content_type"] = result.get("content_type") or row.get("content_type") or ""
    elif action == "ERROR_REMOTE_HASH_SCAN":
        existing_note = row.get("note") or ""
        row["note"] = (existing_note + " | " if existing_note else "") + (
            "Hash scan failed; retained prior local asset: " + (result.get("error") or "")
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify every same-domain SellerKit asset by SHA-256 and replace only changed/new content."
    )
    parser.add_argument("--root", required=True, help="Writable crawl candidate root")
    parser.add_argument("--baseline-release", default="sellerkit-stainless-public-replica-20000101_000000-r3")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    reports = root / "reports"
    assets_json = reports / "assets.json"
    assets_csv = reports / "assets.csv"
    if not assets_json.exists():
        raise FileNotFoundError(f"Missing asset report: {assets_json}")

    rows = json.loads(assets_json.read_text(encoding="utf-8"))
    records_path = reports / "hash_scan_records.jsonl"
    receipt_path = reports / "hash_scan_receipt.json"
    resume = load_resume_rows(records_path)
    results: list[dict] = []
    started_at = now_iso()
    counts = {
        "UNCHANGED_HASH_MATCH": 0,
        "UPDATED_HASH_CHANGED": 0,
        "NEW_OR_RECOVERED": 0,
        "ERROR_REMOTE_HASH_SCAN": 0,
        "SKIPPED_NOT_ALLOWED_HOST": 0,
        "RESUMED_ALREADY_VERIFIED": 0,
    }

    candidates = [
        row
        for row in rows
        if urlparse((row.get("url") or row.get("final_url") or "")).netloc.lower() in ALLOWED_HOSTS
        and row.get("status") in SUCCESS_STATUSES
    ]
    total = len(candidates)
    print(f"HASH_SCAN start total={total} root={root}", flush=True)

    for index, row in enumerate(candidates, start=1):
        url = row.get("url") or row.get("final_url") or ""
        prior = resume.get(url)
        local_path = Path(row["local_file"]) if row.get("local_file") else None
        if (
            prior
            and prior.get("sha256")
            and local_path
            and local_path.exists()
            and sha256_file(local_path) == prior["sha256"]
            and prior.get("action") in {"UNCHANGED_HASH_MATCH", "UPDATED_HASH_CHANGED", "NEW_OR_RECOVERED"}
        ):
            result = dict(prior)
            result["resume_action"] = "RESUMED_ALREADY_VERIFIED"
            counts["RESUMED_ALREADY_VERIFIED"] += 1
        else:
            result = hash_scan_one(row)
            counts[result["action"]] = counts.get(result["action"], 0) + 1
        update_asset_row(row, result)
        results.append(result)

        if index % CHECKPOINT_EVERY == 0 or index == total:
            write_jsonl(records_path, results)
            atomic_write_json(
                receipt_path,
                {
                    "schema_version": "sellerkit.website_asset_hash_refresh.v1",
                    "status": "RUNNING" if index < total else "COMPLETE",
                    "baseline_release": args.baseline_release,
                    "candidate_root": str(root),
                    "started_at": started_at,
                    "updated_at": now_iso(),
                    "processed": index,
                    "total": total,
                    "counts": counts,
                },
            )
            print(
                "HASH_SCAN "
                f"processed={index}/{total} unchanged={counts['UNCHANGED_HASH_MATCH']} "
                f"changed={counts['UPDATED_HASH_CHANGED']} new={counts['NEW_OR_RECOVERED']} "
                f"errors={counts['ERROR_REMOTE_HASH_SCAN']} resumed={counts['RESUMED_ALREADY_VERIFIED']}",
                flush=True,
            )
        time.sleep(REQUEST_DELAY_SECONDS)

    atomic_write_json(assets_json, rows)
    write_assets_csv(assets_csv, rows)
    completed_at = now_iso()
    effective_counts = {
        action: sum(1 for result in results if result.get("action") == action)
        for action in (
            "UNCHANGED_HASH_MATCH",
            "UPDATED_HASH_CHANGED",
            "NEW_OR_RECOVERED",
            "ERROR_REMOTE_HASH_SCAN",
            "SKIPPED_NOT_ALLOWED_HOST",
        )
    }
    receipt = {
        "schema_version": "sellerkit.website_asset_hash_refresh.v1",
        "status": "COMPLETE",
        "baseline_release": args.baseline_release,
        "candidate_root": str(root),
        "started_at": started_at,
        "completed_at": completed_at,
        "processed": total,
        "total": total,
        "counts": counts,
        "effective_counts": effective_counts,
        "records_jsonl": str(records_path),
        "assets_json": str(assets_json),
        "assets_csv": str(assets_csv),
    }
    atomic_write_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
