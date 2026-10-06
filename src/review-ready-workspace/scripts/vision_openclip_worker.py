from __future__ import annotations

"""Isolated OpenCLIP worker.

The parent process communicates only through JSON files.  Keeping every Torch
and OpenCLIP import in this executable prevents CUDA DLL state from colliding
with Paddle/ONNX OCR runtimes.
"""

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
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


def write_json_atomic(path: Path, value: Any) -> None:
    write_text_atomic(path, stable_json(value) + "\n")


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_text_atomic(path, "".join(stable_json(row) + "\n" for row in rows))


def encode_images(request: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], Any]:
    # Framework imports intentionally occur only inside this child process.
    import open_clip
    import torch
    from PIL import Image

    allow_cpu = bool(request.get("allow_cpu"))
    if torch.cuda.is_available():
        device = "cuda"
    elif allow_cpu:
        device = "cpu"
    else:
        raise RuntimeError("CUDA is unavailable and CPU fallback is disabled")

    weight_path = Path(str(request["weight_path"])).resolve()
    if not weight_path.is_file():
        raise FileNotFoundError(weight_path)
    model_name = str(request.get("model_name") or "ViT-B-32")
    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name,
        pretrained=str(weight_path),
        device=device,
    )
    model.eval()
    requested_batch = max(1, int(request.get("batch_size") or 128))
    current_batch = requested_batch
    inputs = list(request.get("images") or [])
    output_rows: list[dict[str, Any]] = []
    embedding_map: dict[str, Any] = {}
    successful_records: list[dict[str, Any]] = []
    failed_decode: list[dict[str, Any]] = []

    # Decode first so corrupt files cannot poison a whole GPU batch.
    prepared: list[tuple[dict[str, Any], Any]] = []
    for record in inputs:
        image_id = str(record.get("image_id") or "")
        path = Path(str(record.get("local_path") or ""))
        try:
            if not image_id or not path.is_file():
                raise FileNotFoundError(path)
            with Image.open(path) as image:
                tensor = preprocess(image.convert("RGB"))
            prepared.append((record, tensor))
        except Exception as exc:
            row = {
                "image_id": image_id,
                "source": record.get("source"),
                "product_key": record.get("product_key"),
                "status": "FAILED_IMAGE_DECODE",
                "error": f"{type(exc).__name__}: {exc}",
            }
            output_rows.append(row)
            failed_decode.append(row)

    position = 0
    oom_reductions: list[int] = []
    while position < len(prepared):
        subset = prepared[position : position + current_batch]
        try:
            batch = torch.stack([tensor for _, tensor in subset]).to(device, non_blocking=device == "cuda")
            started = time.perf_counter()
            with torch.inference_mode():
                if device == "cuda":
                    with torch.autocast("cuda", dtype=torch.float16):
                        features = model.encode_image(batch)
                else:
                    features = model.encode_image(batch)
                features = features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            elapsed = time.perf_counter() - started
            vectors = features.float().cpu()
            for index, (record, _) in enumerate(subset):
                image_id = str(record["image_id"])
                vector = vectors[index]
                embedding_map[image_id] = vector
                successful_records.append(record)
                output_rows.append(
                    {
                        "image_id": image_id,
                        "source": record.get("source"),
                        "product_key": record.get("product_key"),
                        "status": "PASS",
                        "dimensions": int(vector.numel()),
                        "embedding": [round(float(value), 8) for value in vector.tolist()],
                        "batch_infer_ms_per_image": round(elapsed * 1000 / max(1, len(subset)), 3),
                    }
                )
            position += len(subset)
            del batch, features, vectors
        except torch.cuda.OutOfMemoryError as exc:
            if device == "cuda":
                torch.cuda.empty_cache()
            if current_batch <= 1:
                record, _ = subset[0]
                output_rows.append(
                    {
                        "image_id": record.get("image_id"),
                        "source": record.get("source"),
                        "product_key": record.get("product_key"),
                        "status": "FAILED_GPU_OOM_AT_BATCH_1",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                position += 1
            else:
                current_batch = max(1, current_batch // 2)
                oom_reductions.append(current_batch)

    metadata = {
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "torch_version": torch.__version__,
        "openclip_version": getattr(open_clip, "__version__", "unknown"),
        "model_name": model_name,
        "weight_path": str(weight_path),
        "weight_sha256": sha256_file(weight_path),
        "requested_batch_size": requested_batch,
        "final_batch_size": current_batch,
        "oom_batch_reductions": oom_reductions,
        "requested_images": len(inputs),
        "embedded_images": len(embedding_map),
        "failed_images": len(inputs) - len(embedding_map),
    }
    return output_rows, metadata, (embedding_map, successful_records, torch)


def product_similarity(request: dict[str, Any], state: Any) -> list[dict[str, Any]]:
    embedding_map, _, torch = state
    groups = request.get("product_index") or {}
    website = groups.get("website") or {}
    alibaba = groups.get("alibaba") or {}
    image_ids = sorted(embedding_map)
    positions = {image_id: index for index, image_id in enumerate(image_ids)}
    if image_ids:
        matrix = torch.stack([embedding_map[image_id] for image_id in image_ids])
        cosine = matrix @ matrix.T
    else:
        cosine = None
    rows: list[dict[str, Any]] = []
    for website_key, website_ids_raw in sorted(website.items()):
        website_ids = [image_id for image_id in website_ids_raw if image_id in positions]
        for alibaba_key, alibaba_ids_raw in sorted(alibaba.items(), key=lambda item: (len(str(item[0])), str(item[0]))):
            alibaba_ids = [image_id for image_id in alibaba_ids_raw if image_id in positions]
            if cosine is None or not website_ids or not alibaba_ids:
                rows.append(
                    {
                        "website_product_key": website_key,
                        "alibaba_product_key": alibaba_key,
                        "status": "INSUFFICIENT_IMAGE_EMBEDDINGS",
                        "max_cosine": None,
                        "mean_top3_cosine": None,
                        "best_pair": None,
                    }
                )
                continue
            scores: list[tuple[float, str, str]] = []
            for left in website_ids:
                for right in alibaba_ids:
                    scores.append((float(cosine[positions[left], positions[right]].item()), left, right))
            scores.sort(key=lambda value: (-value[0], value[1], value[2]))
            top = scores[:3]
            rows.append(
                {
                    "website_product_key": website_key,
                    "alibaba_product_key": alibaba_key,
                    "status": "PASS",
                    "website_embedding_count": len(website_ids),
                    "alibaba_embedding_count": len(alibaba_ids),
                    "image_pair_count": len(scores),
                    "max_cosine": round(scores[0][0], 8),
                    "mean_top3_cosine": round(sum(item[0] for item in top) / len(top), 8),
                    "best_pair": [scores[0][1], scores[0][2]],
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    response_path = Path(str(request["response_path"]))
    started = time.perf_counter()
    try:
        embedding_rows, metadata, state = encode_images(request)
        embedding_path = Path(str(request["embedding_output_path"]))
        similarity_path = Path(str(request["similarity_output_path"]))
        write_jsonl_atomic(embedding_path, embedding_rows)
        similarity_rows = product_similarity(request, state)
        write_jsonl_atomic(similarity_path, similarity_rows)
        response = {
            "schema_version": request.get("schema_version"),
            "artifact_type": "openclip_worker_receipt",
            "status": "PASS" if metadata["failed_images"] == 0 else "PASS_WITH_IMAGE_FAILURES",
            "created_at_utc": utc_now(),
            "feature_only": True,
            "decision_generated": False,
            "process_id": os.getpid(),
            "framework_isolation": "TORCH_OPENCLIP_CHILD_PROCESS_ONLY",
            "metadata": metadata,
            "embedding_output_path": str(embedding_path),
            "embedding_output_sha256": sha256_file(embedding_path),
            "similarity_output_path": str(similarity_path),
            "similarity_output_sha256": sha256_file(similarity_path),
            "similarity_rows": len(similarity_rows),
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
        write_json_atomic(response_path, response)
        return 0 if response["status"] == "PASS" else 3
    except Exception as exc:
        response = {
            "status": "FAILED_OPENCLIP_WORKER",
            "created_at_utc": utc_now(),
            "feature_only": True,
            "decision_generated": False,
            "process_id": os.getpid(),
            "framework_isolation": "TORCH_OPENCLIP_CHILD_PROCESS_ONLY",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
        write_json_atomic(response_path, response)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
