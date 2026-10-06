from __future__ import annotations

"""Isolated PP-OCRv4 / PaddleOCR-VL worker.

Each invocation loads exactly one engine.  PP-OCRv4 (Paddle) and PaddleOCR-VL
(Torch/Transformers) therefore never share a process with one another or with
the OpenCLIP worker.
"""

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
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


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(child) for child in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def normalize_rapidocr(result: Any) -> tuple[str, float | None, list[dict[str, Any]]]:
    if not result:
        return "", None, []
    if isinstance(result, tuple):
        result = result[0]
    lines: list[str] = []
    scores: list[float] = []
    boxes: list[dict[str, Any]] = []
    for item in result or []:
        try:
            box, text, score = item[0], str(item[1]).strip(), float(item[2])
        except Exception:
            continue
        if text:
            lines.append(text)
        scores.append(score)
        boxes.append({"box": json_safe(box), "text": text, "score": score})
    return "\n".join(lines), (sum(scores) / len(scores) if scores else None), boxes


def run_ppocrv4(request: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Imports are deliberately local to the PP-OCR child invocation.
    import cv2
    import numpy as np
    import paddle
    import rapidocr_paddle
    from rapidocr_paddle import RapidOCR

    package_root = Path(rapidocr_paddle.__file__).resolve().parent
    requested_model_path = str(request.get("model_path") or "").strip()
    model_candidates = [
        Path(requested_model_path) if requested_model_path else None,
        Path(os.environ["SELLERKIT_PPOCR_MODEL_ROOT"]) if os.environ.get("SELLERKIT_PPOCR_MODEL_ROOT") else None,
        # Paddle inference on this Windows build cannot open pdmodel files below
        # a non-ASCII path.  The verified runtime therefore keeps an ASCII copy.
        Path(r"D:/SellerKit/synthetic/81b6f59e447b/sample"),
        package_root / "models",
    ]
    models = next((candidate.resolve() for candidate in model_candidates if candidate and candidate.is_dir()), None)
    if models is None:
        raise FileNotFoundError("No local PP-OCRv4 model root is available")
    required = {
        "det": models / "ch_PP-OCRv4_det_infer",
        "cls": models / "ch_ppocr_mobile_v2_cls_infer",
        "rec": models / "ch_PP-OCRv4_rec_infer",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"PP-OCRv4 local model directories missing: {missing}")
    use_cuda = bool(request.get("use_cuda", True))
    ocr = RapidOCR(
        det_model_path=str(required["det"]),
        cls_model_path=str(required["cls"]),
        rec_model_path=str(required["rec"]),
        det_use_cuda=use_cuda,
        cls_use_cuda=use_cuda,
        rec_use_cuda=use_cuda,
        det_gpu_mem=6000,
        cls_gpu_mem=1000,
        rec_gpu_mem=6000,
    )
    rows: list[dict[str, Any]] = []
    decoder_counts: Counter[str] = Counter()
    for record in request.get("images") or []:
        started = time.perf_counter()
        base = {
            "image_id": record.get("image_id"),
            "source": record.get("source"),
            "product_key": record.get("product_key"),
            "engine": "rapidocr-paddle-ppocrv4",
        }
        try:
            path = Path(str(record.get("local_path") or ""))
            if not path.is_file():
                raise FileNotFoundError(path)
            data = np.fromfile(str(path), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                # Alibaba gallery blobs are valid AVIF files.  This OpenCV
                # build cannot decode AVIF, while the isolated runtime's
                # Pillow build has verified AVIF support.
                from PIL import Image

                with Image.open(path) as source_image:
                    rgb = np.asarray(source_image.convert("RGB"))
                image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                decoder = "PIL_RGB_FALLBACK"
            else:
                decoder = "OPENCV_IMDECODE"
            decoder_counts[decoder] += 1
            text, confidence, boxes = normalize_rapidocr(ocr(image))
            rows.append(
                {
                    **base,
                    "status": "PASS",
                    "text": text,
                    "avg_confidence": round(confidence, 8) if confidence is not None else None,
                    "boxes": boxes,
                    "decoder": decoder,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **base,
                    "status": "FAILED",
                    "text": "",
                    "avg_confidence": None,
                    "boxes": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
    metadata = {
        "engine": "rapidocr-paddle-ppocrv4",
        "framework": "paddle",
        "paddle_version": paddle.__version__,
        "opencv_version": cv2.__version__,
        "model_root": str(models),
        "model_components": {key: str(path) for key, path in required.items()},
        "use_cuda": use_cuda,
        "decoder_counts": dict(sorted(decoder_counts.items())),
    }
    return rows, metadata


def validate_vl_batch_gate(request: dict[str, Any], model_path: Path) -> dict[str, Any]:
    receipt_path = Path(str(request.get("smoke_receipt") or ""))
    if not receipt_path.is_file():
        raise PermissionError("PaddleOCR-VL batch run requires a real-product smoke receipt")
    expected_hash = str(request.get("smoke_receipt_sha256") or "")
    actual_hash = sha256_file(receipt_path)
    if not expected_hash or actual_hash != expected_hash:
        raise PermissionError("PaddleOCR-VL smoke receipt SHA-256 binding failed")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("status") != "PASS_REAL_PRODUCT_IMAGE_SMOKE":
        raise PermissionError("PaddleOCR-VL smoke receipt is not PASS_REAL_PRODUCT_IMAGE_SMOKE")
    if receipt.get("models_loaded") is not True or receipt.get("real_product_images") is not True:
        raise PermissionError("PaddleOCR-VL smoke receipt lacks model-load or real-product proof")
    if int(receipt.get("succeeded_images") or 0) < 1:
        raise PermissionError("PaddleOCR-VL smoke receipt has no successful real-product inference")
    if str(receipt.get("model_path") or "") != str(model_path):
        raise PermissionError("PaddleOCR-VL smoke receipt is bound to a different model path")
    return {"path": str(receipt_path), "sha256": actual_hash}


def run_paddleocr_vl(request: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # This engine uses its own Torch/Transformers environment, but still runs in
    # a process separate from OpenCLIP and PP-OCR/Paddle.
    import torch
    import transformers
    from PIL import Image
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model_path = Path(str(request.get("model_path") or "")).resolve()
    if not model_path.is_dir():
        raise FileNotFoundError(model_path)
    mode = str(request.get("mode") or "batch")
    if mode not in {"smoke", "batch"}:
        raise ValueError(f"Unsupported PaddleOCR-VL mode: {mode}")
    if mode == "batch":
        gate = validate_vl_batch_gate(request, model_path)
    else:
        gate = {"mode": "REAL_PRODUCT_SMOKE_IN_PROGRESS"}
        images = list(request.get("images") or [])
        if not images or not all(item.get("source") in {"website", "alibaba"} for item in images):
            raise PermissionError("Smoke mode requires indexed real website/Alibaba product images")

    if not torch.cuda.is_available():
        raise RuntimeError("PaddleOCR-VL real-product run requires CUDA")
    processor = AutoProcessor.from_pretrained(
        str(model_path), trust_remote_code=True, local_files_only=True
    )
    model = AutoModelForImageTextToText.from_pretrained(
        str(model_path),
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        local_files_only=True,
    ).to("cuda").eval()
    rows: list[dict[str, Any]] = []
    for record in request.get("images") or []:
        started = time.perf_counter()
        base = {
            "image_id": record.get("image_id"),
            "source": record.get("source"),
            "product_key": record.get("product_key"),
            "engine": "paddleocr-vl-1.6",
        }
        try:
            path = Path(str(record.get("local_path") or ""))
            with Image.open(path) as source_image:
                image = source_image.convert("RGB")
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": "OCR:"},
                    ],
                }
            ]
            inputs = processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                images_kwargs={
                    "size": {
                        "shortest_edge": processor.image_processor.min_pixels,
                        "longest_edge": 1280 * 28 * 28,
                    }
                },
            ).to(model.device)
            with torch.inference_mode():
                outputs = model.generate(**inputs, max_new_tokens=int(request.get("max_new_tokens") or 512), do_sample=False)
            prompt_length = inputs["input_ids"].shape[-1]
            text = processor.decode(outputs[0][prompt_length:], skip_special_tokens=True).strip()
            rows.append(
                {
                    **base,
                    "status": "PASS",
                    "text": text,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **base,
                    "status": "FAILED",
                    "text": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
    metadata = {
        "engine": "paddleocr-vl-1.6",
        "framework": "torch-transformers-isolated",
        "torch_version": torch.__version__,
        "transformers_version": transformers.__version__,
        "gpu": torch.cuda.get_device_name(0),
        "model_path": str(model_path),
        "mode": mode,
        "gate": gate,
        "models_loaded": True,
    }
    return rows, metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    response_path = Path(str(request["response_path"]))
    output_path = Path(str(request.get("output_path") or response_path.with_name("ocr_results.jsonl")))
    engine = str(request.get("engine") or "")
    mode = str(request.get("mode") or "batch")
    started = time.perf_counter()
    try:
        if engine == "ppocrv4":
            rows, metadata = run_ppocrv4(request)
        elif engine == "paddleocr-vl":
            rows, metadata = run_paddleocr_vl(request)
        else:
            raise ValueError(f"Unsupported OCR engine: {engine}")
        write_jsonl_atomic(output_path, rows)
        failures = sum(row["status"] != "PASS" for row in rows)
        if engine == "paddleocr-vl" and mode == "smoke":
            status = "PASS_REAL_PRODUCT_IMAGE_SMOKE" if rows and failures == 0 else "FAILED_REAL_PRODUCT_IMAGE_SMOKE"
        else:
            status = "PASS" if failures == 0 else "PASS_WITH_IMAGE_FAILURES"
        response = {
            "schema_version": request.get("schema_version"),
            "artifact_type": "ocr_worker_receipt",
            "status": status,
            "created_at_utc": utc_now(),
            "feature_only": True,
            "decision_generated": False,
            "engine": engine,
            "mode": mode,
            "process_id": os.getpid(),
            "framework_isolation": f"{engine.upper()}_CHILD_PROCESS_ONLY",
            "real_product_images": engine == "paddleocr-vl" and mode == "smoke",
            "models_loaded": bool(metadata.get("models_loaded", engine == "ppocrv4")),
            "model_path": metadata.get("model_path"),
            "attempted_images": len(rows),
            "succeeded_images": len(rows) - failures,
            "failed_images": failures,
            "metadata": metadata,
            "output_path": str(output_path),
            "output_sha256": sha256_file(output_path),
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
        write_json_atomic(response_path, response)
        return 0 if status in {"PASS", "PASS_REAL_PRODUCT_IMAGE_SMOKE"} else 3
    except Exception as exc:
        if engine == "paddleocr-vl" and "smoke" in str(exc).lower():
            status = "BLOCKED_REAL_PRODUCT_SMOKE_REQUIRED"
        else:
            status = "FAILED_OCR_WORKER"
        response = {
            "status": status,
            "created_at_utc": utc_now(),
            "feature_only": True,
            "decision_generated": False,
            "engine": engine,
            "mode": mode,
            "process_id": os.getpid(),
            "framework_isolation": f"{engine.upper()}_CHILD_PROCESS_ONLY",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
        write_json_atomic(response_path, response)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
