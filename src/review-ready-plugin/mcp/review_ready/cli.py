from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import PRIORITY_MODELS, default_paths
from .package import (
    LOCAL_REVIEW_STATUS,
    RELEASE_STATUS,
    REMOTE_WRITE_STATUS,
    compile_review_package,
    export_evidence_receipt,
    record_review_decision,
)
from .store import build_database, inspect_sources
from .utils import assert_no_reparse_components, ensure_within, sha256_file, write_json_atomic


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _quarantine_unindexed_packages(paths, current_package_paths: set[Path]) -> list[dict[str, str]]:
    active_root = paths.output_root / "review_packages"
    quarantine_root = paths.output_root / "superseded_review_packages" / "auto"
    assert_no_reparse_components(active_root, "active_review_packages")
    assert_no_reparse_components(quarantine_root, "superseded_review_packages")
    quarantine_root.mkdir(parents=True, exist_ok=True)
    current = {path.resolve() for path in current_package_paths}
    moved: list[dict[str, str]] = []
    for directory in sorted((path for path in active_root.iterdir() if path.is_dir()), key=lambda path: path.name.lower()):
        source = ensure_within(directory, active_root)
        if source in current:
            continue
        receipt_path = source / "receipt.json"
        receipt_sha256 = sha256_file(receipt_path) if receipt_path.is_file() else ""
        suffix = receipt_sha256[:12] if receipt_sha256 else "no-receipt"
        target = ensure_within(quarantine_root / source.name, paths.output_root)
        if target.exists():
            target = ensure_within(quarantine_root / f"{source.name}-{suffix}", paths.output_root)
        if target.exists():
            raise FileExistsError(f"Superseded package quarantine target already exists: {target}")
        source.rename(target)
        moved.append({"source": str(source), "quarantined_to": str(target), "receipt_sha256": receipt_sha256})
    return moved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SellerKit Alibaba local review and full-match engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect-sources")
    subparsers.add_parser("reconcile-catalogs")
    materialize = subparsers.add_parser(
        "materialize-website-images",
        help="Materialize official SellerKit product images into the local SHA-256 store",
    )
    materialize.add_argument(
        "--roles",
        nargs="+",
        choices=("main_image", "gallery"),
        default=("main_image", "gallery"),
    )
    materialize.add_argument("--workers", type=int, choices=range(1, 9), default=8)
    materialize.add_argument("--no-network", action="store_true")
    materialize.add_argument("--timeout-seconds", type=float, default=30.0)
    materialize.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    materialize.add_argument("--attempts", type=int, choices=range(1, 6), default=3)
    materialize.add_argument("--no-strict-current-r3", action="store_true")
    materialize.add_argument(
        "--no-main-view",
        action="store_true",
        help="Do not build the derived one-file-per-model main-image view",
    )
    vision = subparsers.add_parser(
        "build-vision-features",
        help="Build local visual/OCR evidence in isolated worker processes",
    )
    vision.add_argument("--cpu-workers", type=int, choices=range(1, 62), default=16)
    vision.add_argument("--without-openclip", action="store_true")
    vision.add_argument("--with-ppocr", action="store_true")
    full_match = subparsers.add_parser(
        "build-full-match",
        help="Score every current website product against every eligible Alibaba history record",
    )
    full_match.add_argument("--workers", default="auto")
    full_match.add_argument("--visual-scores", type=Path)
    full_match.add_argument("--ocr-scores", type=Path)
    full_match.add_argument("--output-root", type=Path)
    full_match.add_argument("--vision-required", action="store_true")
    full_match.add_argument("--no-database-update", action="store_true")
    compile_parser = subparsers.add_parser("compile-review-package")
    compile_parser.add_argument("--model", required=True)
    priority = subparsers.add_parser("build-priority")
    priority.add_argument("--skip-rebuild", action="store_true")
    decision = subparsers.add_parser("record-review-decision")
    decision.add_argument("--model", required=True)
    decision.add_argument("--field", required=True)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--reviewer-id", required=True)
    decision.add_argument("--reviewer-role", required=True)
    decision.add_argument("--rationale", required=True)
    subparsers.add_parser("export-evidence-receipt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = default_paths()
    try:
        if args.command == "inspect-sources":
            result = inspect_sources(paths)
        elif args.command == "reconcile-catalogs":
            result = build_database(paths)
        elif args.command == "materialize-website-images":
            from .images import build_website_main_image_view, materialize_website_images

            result = materialize_website_images(
                paths,
                roles=tuple(args.roles),
                workers=args.workers,
                allow_network=not args.no_network,
                timeout_seconds=args.timeout_seconds,
                max_bytes=args.max_bytes,
                attempts=args.attempts,
                strict_current_r3=not args.no_strict_current_r3,
            )
            if "main_image" in args.roles and not args.no_main_view:
                result = {**result, "main_image_view": build_website_main_image_view(paths)}
        elif args.command == "build-vision-features":
            from .vision import (
                VisionRuntimePaths,
                build_visual_input_index,
                run_gate0,
                run_visual_feature_pipeline,
            )

            runtime = VisionRuntimePaths(project_root=paths.project_root)
            index_path = paths.vision_work_root / "visual_input_index.json"
            index = build_visual_input_index(output_path=index_path, runtime=runtime)
            gate0 = run_gate0(
                index,
                output_root=paths.vision_work_root / "gate0",
                runtime=runtime,
                include_openclip=not args.without_openclip,
                include_ppocr=args.with_ppocr,
            )
            if gate0.get("status") != "PASS_GATE0_FEATURES":
                raise RuntimeError(f"Vision Gate0 did not pass: {gate0.get('status')}")
            pipeline = run_visual_feature_pipeline(
                index_path=index_path,
                output_root=paths.vision_work_root / "features",
                runtime=runtime,
                cpu_workers=args.cpu_workers,
                include_openclip=not args.without_openclip,
                include_ppocr=args.with_ppocr,
            )
            incomplete = []
            if pipeline.get("status") != "PASS_FEATURES_ONLY_NO_MATCH_DECISION":
                incomplete.append(f"pipeline={pipeline.get('status')}")
            if not args.without_openclip and (pipeline.get("openclip") or {}).get("status") != "PASS":
                incomplete.append(f"openclip={(pipeline.get('openclip') or {}).get('status')}")
            if args.with_ppocr and (pipeline.get("ppocr") or {}).get("status") != "PASS":
                incomplete.append(f"ppocr={(pipeline.get('ppocr') or {}).get('status')}")
            if incomplete:
                raise RuntimeError("Vision feature pipeline incomplete: " + ", ".join(incomplete))
            result = {
                "status": pipeline.get("status"),
                "index": {
                    "path": str(index_path),
                    "sha256": index.get("artifact_sha256"),
                    "counts": index.get("counts"),
                },
                "gate0": gate0,
                "pipeline": pipeline,
                "external_write_enabled": False,
                "browser_actions": 0,
                "remote_drafts": 0,
                "publish_requests": 0,
            }
            write_json_atomic(paths.vision_receipt_path, result)
            result["evidence_receipt"] = {
                "path": str(paths.vision_receipt_path.resolve()),
                "sha256": sha256_file(paths.vision_receipt_path),
            }
        elif args.command == "build-full-match":
            from .full_match import build_full_match

            result = build_full_match(
                paths,
                workers=args.workers,
                visual_scores_path=args.visual_scores,
                ocr_scores_path=args.ocr_scores,
                output_root=args.output_root,
                update_database=not args.no_database_update,
                vision_required=args.vision_required,
            )
        elif args.command == "compile-review-package":
            result = compile_review_package(paths, args.model)
        elif args.command == "build-priority":
            database = None if args.skip_rebuild else build_database(paths)
            packages = [compile_review_package(paths, model, collection="priority") for model in PRIORITY_MODELS]
            index = {
                "schema_version": "sellerkit.priority_package_index.v2",
                "status": "PASS_PRIORITY_REVIEW_PACKAGES_BUILT_NO_REMOTE_WRITE",
                "build_status": "PASS_PRIORITY_REVIEW_PACKAGES_BUILT_NO_REMOTE_WRITE",
                "local_review_status": LOCAL_REVIEW_STATUS,
                "remote_write_status": REMOTE_WRITE_STATUS,
                "release_status": RELEASE_STATUS,
                "packages": [
                    {
                        "model": package["model"],
                        "package_id": package["package_id"],
                        "package_path": package["package_path"],
                        "receipt_sha256": package["receipt_sha256"],
                        "status": package["status"],
                    }
                    for package in packages
                ],
                "external_write_enabled": False,
                "browser_actions": 0,
                "remote_drafts": 0,
                "publish_requests": 0,
                "receipt_authentication": "UNSIGNED_LOCAL_INTEGRITY_ONLY_NOT_RELEASE_AUTHORIZATION",
            }
            index_path = paths.output_root / "current_priority_packages.json"
            write_json_atomic(index_path, index)
            current_package_paths = {Path(package["package_path"]) for package in packages}
            superseded = _quarantine_unindexed_packages(paths, current_package_paths)
            active_directories = sorted(
                str(path.resolve())
                for path in (paths.output_root / "review_packages").iterdir()
                if path.is_dir()
            )
            expected_directories = sorted(str(path.resolve()) for path in current_package_paths)
            if active_directories != expected_directories:
                raise ValueError("Active review package directory is not an exact match for the current index")
            index_receipt = {
                "schema_version": "sellerkit.priority_package_index_receipt.v2",
                "status": "PASS_PRIORITY_INDEX_BOUND_NO_REMOTE_WRITE",
                "local_review_status": LOCAL_REVIEW_STATUS,
                "remote_write_status": REMOTE_WRITE_STATUS,
                "release_status": RELEASE_STATUS,
                "index_path": str(index_path),
                "index_sha256": sha256_file(index_path),
                "current_package_count": len(packages),
                "current_package_receipts": {
                    package["model"]: package["receipt_sha256"] for package in packages
                },
                "active_directory_count": len(active_directories),
                "unindexed_active_directory_count": 0,
                "superseded_packages_moved": superseded,
                "plugin_source_tree_sha256": packages[0]["source_hashes"]["plugin_source_tree"],
                "receipt_authentication": "UNSIGNED_LOCAL_INTEGRITY_ONLY_NOT_RELEASE_AUTHORIZATION",
                "external_write_enabled": False,
            }
            index_receipt_path = paths.output_root / "current_priority_packages.receipt.json"
            write_json_atomic(index_receipt_path, index_receipt)
            result = {
                "status": "PASS_PRIORITY_REVIEW_PACKAGES_BUILT_NO_REMOTE_WRITE",
                "build_status": "PASS_PRIORITY_REVIEW_PACKAGES_BUILT_NO_REMOTE_WRITE",
                "local_review_status": LOCAL_REVIEW_STATUS,
                "remote_write_status": REMOTE_WRITE_STATUS,
                "release_status": RELEASE_STATUS,
                "database": database,
                "packages": packages,
                "index_path": str(index_path),
                "index_receipt_path": str(index_receipt_path),
                "index_receipt_sha256": sha256_file(index_receipt_path),
                "superseded_packages_moved": superseded,
                "external_write_enabled": False,
                "browser_actions": 0,
                "remote_drafts": 0,
                "publish_requests": 0,
            }
        elif args.command == "record-review-decision":
            result = record_review_decision(
                paths,
                args.model,
                args.field,
                args.decision,
                args.reviewer_id,
                args.reviewer_role,
                args.rationale,
            )
        elif args.command == "export-evidence-receipt":
            result = export_evidence_receipt(paths)
        else:
            raise ValueError(f"Unknown command: {args.command}")
        _print(result)
        return 0
    except Exception as error:
        _print(
            {
                "status": "BLOCKED_OR_FAILED",
                "error_type": type(error).__name__,
                "error": str(error),
                "external_write_enabled": False,
                "browser_actions": 0,
                "remote_drafts": 0,
                "publish_requests": 0,
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
