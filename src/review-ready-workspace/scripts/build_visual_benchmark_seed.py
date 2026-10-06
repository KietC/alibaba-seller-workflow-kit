from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT = Path(r"D:/SellerKit/synthetic/76fc1bc7aed5/sample")
RUN_ID = "43b5caa7ac0ace94d838"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def fit_image(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGB")
    return ImageOps.pad(image, size, method=Image.Resampling.LANCZOS, color="white")


def main() -> int:
    run_root = PROJECT / "work" / "full_match_runs" / f"run-{RUN_ID}"
    pairs = load_jsonl(run_root / "full_match_pairs.jsonl")
    visual_rows = load_jsonl(PROJECT / "work" / "vision" / "features" / "product_visual_similarity.jsonl")
    visual_by_pair = {
        (row["official_model"], row["alibaba_product_id"]): row for row in visual_rows
    }
    index = json.loads((PROJECT / "work" / "vision" / "visual_input_index.json").read_text(encoding="utf-8"))
    image_by_id = {row["image_id"]: Path(row["local_path"]) for row in index["images"]}

    by_model: dict[str, list[dict]] = {}
    for row in pairs:
        by_model.setdefault(row["official"]["canonical_model"], []).append(row)
    selected_models = sorted(
        model for model, rows in by_model.items() if any(row["exact_model"] for row in rows)
    )[:20]
    seeds: list[dict] = []
    for model in selected_models:
        rows = by_model[model]
        exact = min((row for row in rows if row["exact_model"]), key=lambda row: row["rank"])
        near = max(
            (row for row in rows if not row["exact_model"]),
            key=lambda row: (
                row["component_scores"]["model"],
                row["component_scores"]["visual"],
                row["retrieval_score"],
                -row["rank"],
            ),
        )
        different = min(
            (row for row in rows if not row["exact_model"]),
            key=lambda row: (
                row["component_scores"]["model"],
                row["component_scores"]["visual"],
                row["retrieval_score"],
                -row["rank"],
            ),
        )
        for seed_class, row in (
            ("SAME_IDENTIFIER_SEED", exact),
            ("NEAR_VARIANT_CANDIDATE_SEED", near),
            ("CLEARLY_DIFFERENT_CANDIDATE_SEED", different),
        ):
            product_id = row["alibaba"]["product_id"]
            visual = visual_by_pair[(model, product_id)]
            best_pair = visual.get("openclip_best_pair") or visual.get("phash_best_pair")
            if not best_pair or len(best_pair) != 2:
                raise ValueError(f"No auditable image pair for {model}/{product_id}")
            left_path = image_by_id[best_pair[0]]
            right_path = image_by_id[best_pair[1]]
            seeds.append(
                {
                    "schema_version": "sellerkit.visual_benchmark_seed.v1",
                    "seed_class": seed_class,
                    "label_status": "DERIVED_SEED_REQUIRES_HUMAN_CONFIRMATION",
                    "official_model": model,
                    "official_name": row["official"]["name"],
                    "alibaba_product_id": product_id,
                    "alibaba_model": row["alibaba"]["raw_model"],
                    "alibaba_subject": row["alibaba"]["subject"],
                    "rank": row["rank"],
                    "retrieval_score": row["retrieval_score"],
                    "visual_score": row["component_scores"]["visual"],
                    "model_score": row["component_scores"]["model"],
                    "exact_model": row["exact_model"],
                    "official_image": str(left_path),
                    "alibaba_image": str(right_path),
                    "official_image_sha256": sha256_file(left_path),
                    "alibaba_image_sha256": sha256_file(right_path),
                    "human_label": None,
                    "human_reviewer": None,
                    "human_reviewed_at_utc": None,
                }
            )

    if len(seeds) != 60:
        raise ValueError(f"Expected 60 seed pairs, observed {len(seeds)}")
    evidence_root = PROJECT / "evidence" / "visual_benchmark"
    output_root = PROJECT / "output" / "full_match" / "visual_benchmark_seed"
    seed_path = evidence_root / "frozen_visual_benchmark_seed_60.jsonl"
    write_atomic(seed_path, "".join(stable(row) + "\n" for row in seeds))

    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 20)
    small = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 15)
    sheet_paths: list[Path] = []
    for sheet_number, offset in enumerate(range(0, len(seeds), 10), start=1):
        page = seeds[offset : offset + 10]
        canvas = Image.new("RGB", (1700, 10 * 230 + 70), "#F6F8FB")
        draw = ImageDraw.Draw(canvas)
        draw.text((24, 20), f"SellerKit visual benchmark seed - sheet {sheet_number}/6", fill="#102A43", font=font)
        for row_index, row in enumerate(page):
            y = 70 + row_index * 230
            draw.rounded_rectangle((16, y, 1684, y + 216), radius=12, fill="white", outline="#D8E1EA", width=2)
            left = fit_image(Path(row["official_image"]), (210, 190))
            right = fit_image(Path(row["alibaba_image"]), (210, 190))
            canvas.paste(left, (32, y + 13))
            canvas.paste(right, (258, y + 13))
            draw.text((490, y + 18), row["seed_class"], fill="#0B7285", font=font)
            draw.text((490, y + 53), f"Official: {row['official_model']} | Alibaba: {row['alibaba_model']} ({row['alibaba_product_id']})", fill="#243B53", font=small)
            draw.text((490, y + 82), f"rank={row['rank']} retrieval={row['retrieval_score']:.6f} visual={row['visual_score']:.6f} model={row['model_score']:.6f}", fill="#486581", font=small)
            subject = row["alibaba_subject"][:125]
            draw.text((490, y + 113), subject, fill="#334E68", font=small)
            draw.text((490, y + 162), "Human label: [ ] same  [ ] near variant  [ ] different  [ ] unsure", fill="#9C2C2C", font=small)
        path = output_root / f"contact_sheet_{sheet_number:02d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(path, format="PNG", optimize=True)
        sheet_paths.append(path)

    html_path = output_root / "index.html"
    body = [
        "<!doctype html><meta charset='utf-8'><title>SellerKit visual benchmark seed</title>",
        "<style>body{font-family:Arial,sans-serif;background:#f6f8fb;color:#102a43;margin:32px}img{max-width:100%;display:block;margin:20px 0;border:1px solid #d8e1ea;border-radius:10px}code{background:#e9eef4;padding:2px 5px}</style>",
        "<h1>Visual benchmark seed: human review required</h1>",
        "<p>60 derived seed pairs are frozen. They are not ground truth. Precision and Top-5 recall remain unmeasured until a human reviewer fills the labels.</p>",
    ]
    for path in sheet_paths:
        body.append(f"<h2>{html.escape(path.stem)}</h2><img src='{html.escape(path.name)}'>")
    write_atomic(html_path, "\n".join(body) + "\n")

    receipt = {
        "schema_version": "sellerkit.visual_benchmark_seed_receipt.v1",
        "status": "BLOCKED_CALIBRATION_HUMAN_LABELS_REQUIRED_REVIEW_ONLY",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "seed_rows": len(seeds),
        "classes": {name: sum(row["seed_class"] == name for row in seeds) for name in sorted({row["seed_class"] for row in seeds})},
        "human_labeled_rows": 0,
        "confirmation_precision": None,
        "top5_recall": None,
        "required_confirmation_precision": 0.99,
        "required_top5_recall": 0.98,
        "seed_path": str(seed_path),
        "seed_sha256": sha256_file(seed_path),
        "html_path": str(html_path),
        "html_sha256": sha256_file(html_path),
        "contact_sheets": [{"path": str(path), "sha256": sha256_file(path)} for path in sheet_paths],
        "label_semantics": "DERIVED_REVIEW_SEED_NOT_HUMAN_GROUND_TRUTH",
        "automated_confirmation_enabled": False,
    }
    receipt_path = evidence_root / "visual_benchmark_seed_receipt.json"
    write_atomic(receipt_path, stable(receipt) + "\n")
    print(json.dumps({**receipt, "receipt_path": str(receipt_path), "receipt_sha256": sha256_file(receipt_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
