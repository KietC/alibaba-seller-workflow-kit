from __future__ import annotations

import os
import ctypes
import hashlib
import json
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path

from .utils import assert_no_reparse_components

# No catalog or machine identity is bundled. A reviewed local config is mandatory.
# 不内置商品库或机器身份；必须提供已审核的本地配置。
_CONFIG_CACHE = None
PATH_KEYS = ("project_root", "website_replica_root", "website_candidate_root", "website_archive", "old_website_archive", "alibaba_archive_root", "disposition_overlay_root")
EXPECTED_KEYS = ('website_archive_sha256', 'website_archive_size_bytes', 'website_release_receipt_sha256', 'website_dist_tree_sha256', 'website_dist_file_count', 'website_dist_total_bytes', 'old_website_archive_sha256', 'old_website_archive_size_bytes', 'website_pages_sha256', 'website_assets_sha256', 'website_assets_size_bytes', 'alibaba_catalog_sha256', 'disposition_overlay_sha256', 'alibaba_snapshot_tree_sha256', 'alibaba_snapshot_file_count', 'alibaba_snapshot_total_bytes', 'website_products', 'website_specs', 'website_non_empty_specs', 'website_empty_specs', 'website_main_image_references', 'website_main_image_materialized', 'website_gallery_references', 'website_gallery_materialized', 'website_core_images_remote_unmaterialized', 'alibaba_products', 'alibaba_non_empty_models', 'historical_reference', 'unusable_waste', 'historical_structured_ladder_price_coverage', 'historical_catalog_sku_price_coverage', 'historical_public_price_range_coverage', 'historical_sku_attribute_coverage', 'historical_sku_sample_coverage', 'historical_lead_time_coverage', 'historical_packaging_coverage', 'exact_intersection', 'exact_missing_candidates')

def get_settings() -> dict:
    global _CONFIG_CACHE
    value = os.environ.get("SELLERKIT_ALIBABA_CONFIG")
    if not value:
        raise ValueError("SELLERKIT_ALIBABA_CONFIG must name an explicitly approved local JSON configuration")
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("configuration must be an absolute local path")
    assert_no_reparse_components(path, "configuration")
    if path.stat().st_size > 1024 * 1024 or path.stat().st_nlink != 1:
        raise ValueError("configuration size or link ownership is invalid")
    raw = path.read_bytes()
    frozen = (str(path.resolve()), hashlib.sha256(raw).hexdigest())
    if _CONFIG_CACHE is not None:
        if _CONFIG_CACHE[0] != frozen:
            raise ValueError("approved configuration changed during this process; stop and review")
        return _CONFIG_CACHE[1]
    settings = json.loads(raw)
    if settings.get("schema_version") != "sellerkit.review-config.v1" or settings.get("approved") is not True:
        raise ValueError("configuration is not explicitly approved; the example is not approval")
    policy = settings.get("policy", {})
    if policy.get("external_write_enabled") is not False or policy.get("final_write_enabled") is not False:
        raise ValueError("configuration cannot enable remote writes")
    anchors = settings.get("paths", {})
    if set(anchors) != set(PATH_KEYS):
        raise ValueError("configuration must bind all local source paths")
    for label, value in anchors.items():
        if not isinstance(value, str) or not Path(value).is_absolute() or value.startswith(("\\\\", "//")):
            raise ValueError(f"{label} must be an absolute local path")
        assert_no_reparse_components(Path(value), label)
    expected = settings.get("expected", {})
    if set(expected) != set(EXPECTED_KEYS):
        raise ValueError("configuration must bind every source digest and catalog count")
    for key, value in expected.items():
        if key.endswith("sha256"):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) or value == "0" * 64:
                raise ValueError(f"{key} needs an actual approved source digest, not a placeholder")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} needs an actual nonnegative integer")
    if not isinstance(settings.get("priority_models"), list) or not all(isinstance(model, str) and model for model in settings["priority_models"]):
        raise ValueError("priority_models must be an explicit list")
    hosts = settings.get("allowed_website_hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) for host in hosts):
        raise ValueError("allowed_website_hosts must bind exact lowercase DNS hosts")
    for name in ("website_release_id", "capture_snapshot"):
        if not isinstance(settings.get(name), str) or not settings[name]:
            raise ValueError(f"{name} must be explicitly bound")
    _CONFIG_CACHE = (frozen, settings)
    return settings


class _Expected(Mapping):
    def __getitem__(self, key):
        return get_settings()["expected"][key]

    def __iter__(self):
        return iter(EXPECTED_KEYS)

    def __len__(self):
        return len(EXPECTED_KEYS)


class _PriorityModels(Sequence):
    def __getitem__(self, index):
        return get_settings()["priority_models"][index]

    def __len__(self):
        return len(get_settings()["priority_models"])


class _WebsiteHosts(Set):
    def __contains__(self, host):
        return host in get_settings()["allowed_website_hosts"]

    def __iter__(self):
        return iter(get_settings()["allowed_website_hosts"])

    def __len__(self):
        return len(get_settings()["allowed_website_hosts"])


EXPECTED = _Expected()
PRIORITY_MODELS = _PriorityModels()


def configured_website_hosts():
    return _WebsiteHosts()


@dataclass(frozen=True)
class Paths:
    project_root: Path
    website_replica_root: Path
    website_candidate_root: Path
    website_archive: Path
    old_website_archive: Path
    alibaba_archive_root: Path
    disposition_overlay_root: Path

    @property
    def website_products_root(self) -> Path:
        return self.website_replica_root / "dist" / "client" / "legacy" / "Products"

    @property
    def website_legacy_root(self) -> Path:
        return self.website_replica_root / "dist" / "client" / "legacy"

    @property
    def website_assets_report(self) -> Path:
        return self.website_candidate_root / "reports" / "assets.json"

    @property
    def website_pages_report(self) -> Path:
        return self.website_candidate_root / "reports" / "pages.json"

    @property
    def website_release_receipt(self) -> Path:
        return self.website_archive.parent / "release_receipt-r3.json"

    @property
    def alibaba_catalog(self) -> Path:
        return self.alibaba_archive_root / "01_商品索引" / "catalog_index.jsonl"

    @property
    def alibaba_snapshots_root(self) -> Path:
        return self.alibaba_archive_root / "03_商品标准化"

    @property
    def disposition_records(self) -> Path:
        return self.disposition_overlay_root / "02_overlay" / "record_disposition.jsonl"

    @property
    def disposition_index(self) -> Path:
        return self.disposition_overlay_root / "02_overlay" / "record_disposition_index.json"

    @property
    def work_root(self) -> Path:
        return self.project_root / "work"

    @property
    def evidence_root(self) -> Path:
        return self.project_root / "evidence"

    @property
    def output_root(self) -> Path:
        return self.project_root / "output"

    @property
    def database_path(self) -> Path:
        return self.work_root / "sellerkit_alibaba_review_v1.sqlite3"

    @property
    def review_decisions_path(self) -> Path:
        return self.work_root / "review_decisions.jsonl"

    @property
    def website_image_blob_root(self) -> Path:
        return self.project_root / "artifacts" / "original" / "website_images" / "sha256"

    @property
    def website_main_images_path(self) -> Path:
        return self.work_root / "website_main_images.jsonl"

    @property
    def website_gallery_images_path(self) -> Path:
        return self.work_root / "website_gallery_images.jsonl"

    @property
    def website_image_receipt_path(self) -> Path:
        return self.evidence_root / "website_image_materialization_receipt.json"

    @property
    def vision_work_root(self) -> Path:
        return self.work_root / "vision"

    @property
    def vision_receipt_path(self) -> Path:
        return self.evidence_root / "vision_feature_receipt.json"

    @property
    def full_match_work_root(self) -> Path:
        return self.work_root / "full_match"

    @property
    def full_match_output_root(self) -> Path:
        return self.output_root / "full_match"

    @property
    def full_match_receipt_path(self) -> Path:
        return self.evidence_root / "full_match_run_receipt.json"


def default_paths() -> Paths:
    paths = Paths(**{key: Path(value) for key, value in get_settings()["paths"].items()})
    validate_path_contract(paths)
    return paths


def validate_path_contract(paths: Paths) -> None:
    # Keep exact path equality and reparse/fixed-drive checks; configuration is not a bypass.
    # 保留精确路径、重解析点和固定盘检查；配置不构成绕过。
    expected = {key: Path(value) for key, value in get_settings()["paths"].items()}
    for label, expected_path in expected.items():
        supplied = getattr(paths, label)
        if str(supplied).startswith(("\\\\", "//")):
            raise ValueError(f"{label} cannot be a UNC or network path")
        supplied_absolute = os.path.normcase(os.path.abspath(supplied))
        expected_absolute = os.path.normcase(os.path.abspath(expected_path))
        if supplied_absolute != expected_absolute:
            raise ValueError(f"{label} is outside the approved local path contract")
        assert_no_reparse_components(supplied, label)
    if os.name == "nt":
        checked_anchors = set()
        for label, expected_path in expected.items():
            anchor = expected_path.anchor
            if anchor in checked_anchors:
                continue
            checked_anchors.add(anchor)
            drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(anchor)))
            if drive_type != 3:
                raise ValueError(f"{label} must reside on a fixed local drive")

    protected_descendants = {
        "website_dist": paths.website_replica_root / "dist",
        "website_candidate_assets": paths.website_candidate_root / "assets",
        "alibaba_snapshots": paths.alibaba_snapshots_root,
        "project_artifacts": paths.project_root / "artifacts",
        "project_work": paths.work_root,
        "project_evidence": paths.evidence_root,
        "project_output": paths.output_root,
        "project_review_packages": paths.output_root / "review_packages",
        "project_ad_hoc_review_packages": paths.output_root / "ad_hoc_review_packages",
        "project_superseded_review_packages": paths.output_root / "superseded_review_packages",
        "project_website_image_blobs": paths.website_image_blob_root,
        "project_vision_work": paths.vision_work_root,
        "project_full_match_work": paths.full_match_work_root,
        "project_full_match_output": paths.full_match_output_root,
    }
    for label, path in protected_descendants.items():
        assert_no_reparse_components(path, label)
