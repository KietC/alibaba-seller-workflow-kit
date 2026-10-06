from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .utils import exclusive_file_lock, load_json, load_jsonl, normalize_model, sha256_file, write_json_atomic


FULL_MATCH_ENGINE_VERSION = "1.0.0"
ALLOWED_DISPOSITION = "HISTORICAL_REFERENCE_ONLY"
WASTE_DISPOSITION = "UNUSABLE_WASTE_RECORD"
MAX_WINDOWS_WORKERS = 61

COMPONENT_WEIGHTS: dict[str, float] = {
    "model": 0.25,
    "text": 0.10,
    "category": 0.05,
    "structured": 0.20,
    "numeric": 0.15,
    "visual": 0.20,
    "ocr": 0.05,
}

CALIBRATION_STATUS = "UNVALIDATED_HEURISTIC_REVIEW_ONLY"


_RIGHT_FEATURES: tuple[dict[str, Any], ...] = ()
_IDF: dict[str, float] = {}
_VISUAL_SCORES: dict[tuple[str, str], dict[str, Any]] = {}
_OCR_SCORES: dict[tuple[str, str], dict[str, Any]] = {}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _round(value: float) -> float:
    return round(float(value), 6)


def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _text_tokens(value: Any) -> frozenset[str]:
    """Return stable Latin/number tokens plus CJK bigrams.

    The website is predominantly English while some Alibaba snapshots contain
    Chinese.  CJK bigrams preserve useful lexical evidence without depending on
    an external segmenter.
    """

    text = _norm_text(value)
    result: set[str] = set()
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text):
        if re.fullmatch(r"[a-z0-9]+", token):
            if len(token) > 1:
                result.add(token)
            continue
        if len(token) == 1:
            result.add(token)
        else:
            result.update(token[index : index + 2] for index in range(len(token) - 1))
            if len(token) <= 8:
                result.add(token)
    return frozenset(result)


def _model_ngrams(model: str, size: int) -> frozenset[str]:
    if not model:
        return frozenset()
    if len(model) <= size:
        return frozenset({model})
    return frozenset(model[index : index + size] for index in range(len(model) - size + 1))


def _dice(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def _prefix_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    count = 0
    for lchar, rchar in zip(left, right):
        if lchar != rchar:
            break
        count += 1
    return count / max(len(left), len(right))


def _model_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_model = left["canonical_model"]
    right_model = right["canonical_model"]
    if left_model and left_model == right_model:
        return 1.0
    return _round(
        0.60 * _dice(left["model_trigrams"], right["model_trigrams"])
        + 0.30 * _dice(left["model_bigrams"], right["model_bigrams"])
        + 0.10 * _prefix_ratio(left_model, right_model)
    )


def _weighted_jaccard(
    left: frozenset[str], right: frozenset[str], idf: Mapping[str, float]
) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    denominator = math.fsum(idf.get(token, 1.0) for token in sorted(union))
    if denominator <= 0:
        return 0.0
    numerator = math.fsum(idf.get(token, 1.0) for token in sorted(left & right))
    return _round(numerator / denominator)


_ATTRIBUTE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model", ("model no", "model number", "model", "型号", "产品型号")),
    ("hole_size", ("hole size", "hole diameter", "孔径", "开孔尺寸")),
    ("holes", ("number of holes", "hole count", "安装孔数", "孔数")),
    ("material", ("body material", "handle material", "valve material", "material", "阀体材质", "主体材质", "材质")),
    ("mount", ("mounting", "installation type", "faucet mount", "mount", "安装方式", "安装类型", "龙头安装")),
    ("style", ("design style", "style", "设计风格", "款式", "风格")),
    ("function", ("feature", "function", "特性", "功能")),
    ("application", ("application scenario", "application", "应用场景", "用途", "应用")),
    ("finish", ("surface treatment", "surface finish", "finish", "表面处理", "表面加工")),
    ("flow", ("water flow", "flow rate", "flow", "流量")),
    ("pressure", ("working pressure", "water pressure", "pressure", "工作压力", "水压", "压力")),
    ("temperature", ("working temperature", "temperature", "工作温度", "温度")),
    ("dimensions", ("dimensions", "dimension", "product size", "size", "外形尺寸", "产品尺寸", "尺寸")),
    ("length", ("length", "长度")),
    ("width", ("width", "宽度")),
    ("height", ("height", "高度")),
    ("thickness", ("thickness", "厚度")),
    ("diameter", ("diameter", "直径")),
    ("voltage", ("voltage", "电压")),
    ("power", ("rated power", "power", "功率")),
    ("frequency", ("frequency", "频率")),
    ("type", ("sprayer type", "faucet type", "sink type", "type", "类型")),
)


def _canonical_attr_key(value: Any) -> str:
    text = _norm_text(value).replace("_", " ").replace("-", " ")
    for canonical, needles in _ATTRIBUTE_KEYS:
        if any(needle in text for needle in needles):
            return canonical
    tokens = sorted(_text_tokens(text))
    return "other:" + "_".join(tokens[:4]) if tokens else "other:unknown"


_SEMANTIC_PATTERNS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "product_kind": (
        ("faucet", ("faucet", "tap", "龙头", "水龙头")),
        ("sink", ("sink", "basin", "水槽", "洗手盆", "盆")),
        ("dispenser", ("dispenser", "皂液器", "分配器")),
        ("drain", ("drain", "waste fitting", "下水器", "排水")),
        ("accessory", ("accessory", "spare part", "配件", "附件")),
    ),
    "mount": (
        ("wall", ("wall mounted", "wall mount", "wall-mounted", "壁挂", "墙装")),
        ("deck", ("deck mounted", "deck mount", "countertop", "台面", "台式")),
        ("floor", ("floor mounted", "floor standing", "落地", "地装")),
    ),
    "material": (
        ("stainless", ("stainless steel", "stainless", "不锈钢")),
        ("brass", ("brass", "黄铜")),
        ("plastic", ("plastic", "abs", "塑料")),
        ("ceramic", ("ceramic", "陶瓷")),
    ),
    "material_grade": (
        ("304", ("sus304", "ss304", "304 stainless", "304不锈钢", "304")),
        ("316", ("sus316", "ss316", "316 stainless", "316不锈钢", "316")),
    ),
    "function": (
        ("sensor", ("sensor", "touchless", "automatic faucet", "感应", "无接触")),
        ("thermostatic", ("thermostatic", "temperature control", "恒温")),
        ("manual", ("manual faucet", "手动龙头")),
        ("pullout", ("pull out", "pull-out", "pull down", "抽拉")),
    ),
    "application": (
        ("kitchen", ("kitchen", "厨房")),
        ("bathroom", ("bathroom", "washroom", "浴室", "卫生间")),
        ("commercial", ("commercial", "restaurant", "hotel", "商用", "餐厅", "酒店")),
    ),
    "style": (
        ("modern", ("modern", "contemporary", "现代")),
        ("traditional", ("traditional", "classic", "传统", "古典")),
    ),
}


def _semantic_tags(value: Any) -> dict[str, frozenset[str]]:
    text = _norm_text(value)
    result: dict[str, frozenset[str]] = {}
    for dimension, entries in _SEMANTIC_PATTERNS.items():
        found = {tag for tag, needles in entries if any(needle in text for needle in needles)}
        result[dimension] = frozenset(found)

    holes: set[str] = set()
    handles: set[str] = set()
    count_words = {
        "1": ("one", "single", "1", "一", "单"),
        "2": ("two", "double", "2", "二", "双"),
        "3": ("three", "3", "三"),
        "4": ("four", "4", "四"),
    }
    for number, words in count_words.items():
        if any(re.search(rf"(?:{re.escape(word)})\s*(?:hole|holes|孔)", text) for word in words):
            holes.add(number)
        if any(re.search(rf"(?:{re.escape(word)})\s*(?:handle|handles|把手)", text) for word in words):
            handles.add(number)
    result["holes"] = frozenset(holes)
    result["handle_count"] = frozenset(handles)
    return result


_NUMERIC_KEY_ALLOWLIST = {
    "hole_size",
    "holes",
    "flow",
    "pressure",
    "temperature",
    "dimensions",
    "length",
    "width",
    "height",
    "thickness",
    "diameter",
    "voltage",
    "power",
    "frequency",
}

_MEASUREMENT_PATTERN = re.compile(
    r"(?<![a-z0-9])-?\d+(?:\.\d+)?\s*(?:"
    r"l\s*/\s*h|l\s*/\s*min|ml\s*/\s*min|gpm|mpa|kpa|psi|bar|kw|w|v|hz|"
    r"mm|cm|inch(?:es)?|in\.?|°c|℃|c\b|m\b)?",
    re.IGNORECASE,
)


def _canonical_measurement(number: float, unit: str, key: str) -> tuple[str, float]:
    normalized = re.sub(r"\s+", "", unit.casefold()).rstrip(".")
    if normalized == "mm":
        return "length_mm", number
    if normalized == "cm":
        return "length_mm", number * 10.0
    if normalized in {"inch", "inches", "in"}:
        return "length_mm", number * 25.4
    if normalized in {"l/h"}:
        return "flow_lph", number
    if normalized in {"l/min"}:
        return "flow_lph", number * 60.0
    if normalized in {"ml/min"}:
        return "flow_lph", number * 0.06
    if normalized == "gpm":
        return "flow_lph", number * 227.124707
    if normalized == "mpa":
        return "pressure_kpa", number * 1000.0
    if normalized == "kpa":
        return "pressure_kpa", number
    if normalized == "psi":
        return "pressure_kpa", number * 6.894757
    if normalized == "bar":
        return "pressure_kpa", number * 100.0
    if normalized == "kw":
        return "power_w", number * 1000.0
    if normalized == "w":
        return "power_w", number
    if normalized == "v":
        return "voltage_v", number
    if normalized == "hz":
        return "frequency_hz", number
    if normalized in {"°c", "℃", "c"}:
        return "temperature_c", number
    if key == "holes":
        return "count", number
    return f"unitless:{key}", number


def _extract_measurements(key: str, value: Any) -> dict[str, tuple[float, ...]]:
    if key not in _NUMERIC_KEY_ALLOWLIST:
        return {}
    result: dict[str, list[float]] = defaultdict(list)
    for match in _MEASUREMENT_PATTERN.finditer(_norm_text(value)):
        matched = match.group(0).strip()
        number_match = re.match(r"-?\d+(?:\.\d+)?", matched)
        if not number_match:
            continue
        number = float(number_match.group(0))
        unit = matched[number_match.end() :].strip()
        family, canonical = _canonical_measurement(number, unit, key)
        result[family].append(_round(canonical))
    return {family: tuple(sorted(set(values))) for family, values in sorted(result.items())}


def _iter_historical_properties(record: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    snapshot = record.get("safe_snapshot") or {}
    product = snapshot.get("product") or {}
    for collection_name in ("properties", "key_industry_properties", "other_properties"):
        for prop in product.get(collection_name) or []:
            if not isinstance(prop, dict):
                continue
            name = prop.get("attrName") or prop.get("name") or ""
            value = prop.get("attrValue") or prop.get("value") or ""
            if name or value:
                yield str(name), value
    sku = product.get("sku") or {}
    for attribute in sku.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = attribute.get("name") or ""
        values = attribute.get("values") or []
        for value in values:
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or ""
            if name or value:
                yield str(name), value


def _merge_measurements(
    destination: dict[str, dict[str, list[float]]],
    key: str,
    measurements: Mapping[str, Sequence[float]],
) -> None:
    for family, values in measurements.items():
        destination.setdefault(key, {}).setdefault(family, []).extend(float(value) for value in values)


def _finalize_measurements(
    source: Mapping[str, Mapping[str, Sequence[float]]]
) -> dict[str, dict[str, tuple[float, ...]]]:
    return {
        key: {
            family: tuple(sorted(set(_round(value) for value in values)))
            for family, values in sorted(families.items())
        }
        for key, families in sorted(source.items())
    }


def _official_features(record: Mapping[str, Any]) -> dict[str, Any]:
    canonical_model = normalize_model(record.get("canonical_model") or record.get("raw_model"))
    title_text = " ".join(
        str(record.get(key) or "")
        for key in (
            "name",
            "short_description_text",
            "description_text",
            "details_text",
        )
    )
    taxonomy = record.get("taxonomy") or {}
    category_text = " ".join(
        str(value or "")
        for value in (
            record.get("category"),
            record.get("subcategory"),
            taxonomy.get("category_slug"),
            taxonomy.get("subcategory_slug"),
        )
    )
    structured_tokens: set[str] = set()
    structured_text_parts: list[str] = []
    measurements: dict[str, dict[str, list[float]]] = {}
    for attribute in record.get("attributes") or []:
        if not isinstance(attribute, dict) or attribute.get("is_empty"):
            continue
        name = str(attribute.get("name") or "")
        value = attribute.get("raw_value") or ""
        key = _canonical_attr_key(name)
        if key == "model":
            continue
        structured_text_parts.append(f"{name} {value}")
        structured_tokens.add(f"key:{key}")
        structured_tokens.update(f"{key}:{token}" for token in _text_tokens(value))
        _merge_measurements(measurements, key, _extract_measurements(key, value))
    all_semantic_text = f"{title_text} {category_text} {' '.join(structured_text_parts)}"
    semantic = _semantic_tags(all_semantic_text)
    for dimension, values in semantic.items():
        structured_tokens.update(f"tag:{dimension}:{value}" for value in values)
    return {
        "side": "official",
        "product_id": str(record.get("product_id") or canonical_model),
        "canonical_model": canonical_model,
        "raw_model": str(record.get("raw_model") or ""),
        "name": str(record.get("name") or ""),
        "category": str(record.get("category") or ""),
        "subcategory": str(record.get("subcategory") or ""),
        "model_bigrams": _model_ngrams(canonical_model, 2),
        "model_trigrams": _model_ngrams(canonical_model, 3),
        "title_tokens": _text_tokens(title_text),
        "category_tokens": _text_tokens(category_text),
        "structured_tokens": frozenset(structured_tokens),
        "measurements": _finalize_measurements(measurements),
        "semantic": semantic,
    }


def _historical_features(record: Mapping[str, Any]) -> dict[str, Any]:
    canonical_model = normalize_model(record.get("canonical_model") or record.get("raw_model"))
    snapshot = record.get("safe_snapshot") or {}
    product = snapshot.get("product") or {}
    title_text = " ".join(
        str(value or "")
        for value in (
            record.get("subject"),
            " ".join(record.get("group_names") or []),
            snapshot.get("h1"),
            snapshot.get("title"),
            snapshot.get("description_text"),
            product.get("subject"),
        )
    )
    category_text = " ".join(
        str(value or "")
        for value in (
            " ".join(record.get("group_names") or []),
            record.get("subject"),
            product.get("category_id"),
            product.get("leaf_category_id"),
        )
    )
    structured_tokens: set[str] = set()
    structured_text_parts: list[str] = []
    measurements: dict[str, dict[str, list[float]]] = {}
    for name, value in _iter_historical_properties(record):
        key = _canonical_attr_key(name)
        if key == "model":
            continue
        structured_text_parts.append(f"{name} {value}")
        structured_tokens.add(f"key:{key}")
        structured_tokens.update(f"{key}:{token}" for token in _text_tokens(value))
        _merge_measurements(measurements, key, _extract_measurements(key, value))
    all_semantic_text = f"{title_text} {category_text} {' '.join(structured_text_parts)}"
    semantic = _semantic_tags(all_semantic_text)
    for dimension, values in semantic.items():
        structured_tokens.update(f"tag:{dimension}:{value}" for value in values)
    return {
        "side": "alibaba",
        "product_id": str(record.get("product_id") or ""),
        "ordinal": int(record.get("ordinal") or 0),
        "canonical_model": canonical_model,
        "raw_model": str(record.get("raw_model") or ""),
        "subject": str(record.get("subject") or ""),
        "record_disposition": str(record.get("record_disposition") or ""),
        "model_bigrams": _model_ngrams(canonical_model, 2),
        "model_trigrams": _model_ngrams(canonical_model, 3),
        "title_tokens": _text_tokens(title_text),
        "category_tokens": _text_tokens(category_text),
        "structured_tokens": frozenset(structured_tokens),
        "measurements": _finalize_measurements(measurements),
        "semantic": semantic,
    }


def _compute_idf(features: Sequence[dict[str, Any]]) -> dict[str, float]:
    document_frequency: Counter[str] = Counter()
    for feature in features:
        document_frequency.update(
            feature["title_tokens"] | feature["category_tokens"] | feature["structured_tokens"]
        )
    count = len(features)
    return {
        token: _round(math.log((count + 1) / (frequency + 1)) + 1.0)
        for token, frequency in sorted(document_frequency.items())
    }


def _numeric_similarity(
    left: Mapping[str, Mapping[str, Sequence[float]]],
    right: Mapping[str, Mapping[str, Sequence[float]]],
) -> tuple[float, bool, list[dict[str, Any]]]:
    comparisons: list[dict[str, Any]] = []
    scores: list[float] = []
    for key in sorted(set(left) & set(right)):
        for family in sorted(set(left[key]) & set(right[key])):
            left_values = tuple(float(value) for value in left[key][family])
            right_values = tuple(float(value) for value in right[key][family])
            if not left_values or not right_values:
                continue
            directional: list[float] = []
            for lvalue in left_values:
                best = max(
                    1.0 - abs(lvalue - rvalue) / max(abs(lvalue), abs(rvalue), 1.0)
                    for rvalue in right_values
                )
                directional.append(max(0.0, best))
            score = sum(directional) / len(directional)
            scores.append(score)
            comparisons.append(
                {
                    "key": key,
                    "family": family,
                    "left": [_round(value) for value in left_values],
                    "right": [_round(value) for value in right_values],
                    "similarity": _round(score),
                }
            )
    if not scores:
        return 0.0, False, []
    return _round(sum(scores) / len(scores)), True, comparisons


_CONFLICT_PENALTIES: dict[str, float] = {
    "product_kind": 0.15,
    "mount": 0.12,
    "material": 0.10,
    "material_grade": 0.08,
    "function": 0.08,
    "holes": 0.08,
    "handle_count": 0.06,
}


def _conflicts(
    left: dict[str, Any],
    right: dict[str, Any],
    numeric_score: float,
    numeric_observed: bool,
    numeric_comparisons: Sequence[Mapping[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    for dimension, penalty in _CONFLICT_PENALTIES.items():
        left_values = left["semantic"].get(dimension) or frozenset()
        right_values = right["semantic"].get(dimension) or frozenset()
        if left_values and right_values and left_values.isdisjoint(right_values):
            conflicts.append(
                {
                    "code": f"CONFLICT_{dimension.upper()}",
                    "left": sorted(left_values),
                    "right": sorted(right_values),
                    "penalty": penalty,
                    "severity": "CRITICAL" if penalty >= 0.10 else "MATERIAL",
                }
            )
    if numeric_observed and numeric_score < 0.45:
        conflicts.append(
            {
                "code": "CONFLICT_NUMERIC_SPEC",
                "left": [],
                "right": [],
                "penalty": 0.10,
                "severity": "CRITICAL",
                "comparisons": list(numeric_comparisons),
            }
        )
    return _round(min(0.35, sum(item["penalty"] for item in conflicts))), conflicts


def _scalar_evidence(
    scores: Mapping[tuple[str, str], dict[str, Any]],
    official_model: str,
    alibaba_product_id: str,
    not_provided: bool,
) -> tuple[float, bool, dict[str, Any]]:
    evidence = scores.get((official_model, alibaba_product_id))
    if evidence is None:
        return (
            0.0,
            False,
            {
                "status": "NOT_PROVIDED" if not_provided else "MISSING_FOR_PAIR",
                "score": None,
            },
        )
    return float(evidence["score"]), bool(evidence.get("observed", True)), dict(evidence)


def _score_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    model = _model_score(left, right)
    text = _weighted_jaccard(left["title_tokens"], right["title_tokens"], _IDF)
    category = _weighted_jaccard(left["category_tokens"], right["category_tokens"], _IDF)
    structured = _weighted_jaccard(
        left["structured_tokens"], right["structured_tokens"], _IDF
    )
    numeric, numeric_observed, numeric_comparisons = _numeric_similarity(
        left["measurements"], right["measurements"]
    )
    visual, visual_observed, visual_evidence = _scalar_evidence(
        _VISUAL_SCORES,
        left["canonical_model"],
        right["product_id"],
        not_provided=not bool(_VISUAL_SCORES),
    )
    ocr, ocr_observed, ocr_evidence = _scalar_evidence(
        _OCR_SCORES,
        left["canonical_model"],
        right["product_id"],
        not_provided=not bool(_OCR_SCORES),
    )
    component_scores = {
        "model": model,
        "text": text,
        "category": category,
        "structured": structured,
        "numeric": numeric,
        "visual": _round(visual),
        "ocr": _round(ocr),
    }
    observed = {
        "model": bool(left["canonical_model"] and right["canonical_model"]),
        "text": bool(left["title_tokens"] and right["title_tokens"]),
        "category": bool(left["category_tokens"] and right["category_tokens"]),
        "structured": bool(left["structured_tokens"] and right["structured_tokens"]),
        "numeric": numeric_observed,
        "visual": visual_observed,
        "ocr": ocr_observed,
    }
    unpenalized = sum(
        COMPONENT_WEIGHTS[name] * component_scores[name] for name in COMPONENT_WEIGHTS
    )
    observed_weight = sum(
        COMPONENT_WEIGHTS[name] for name in COMPONENT_WEIGHTS if observed[name]
    )
    penalty, conflicts = _conflicts(
        left,
        right,
        numeric_score=numeric,
        numeric_observed=numeric_observed,
        numeric_comparisons=numeric_comparisons,
    )
    retrieval = max(0.0, unpenalized - penalty)
    exact_model = bool(
        left["canonical_model"] and left["canonical_model"] == right["canonical_model"]
    )
    return {
        "schema_version": "sellerkit.full_match_pair.v1",
        "official": {
            "canonical_model": left["canonical_model"],
            "raw_model": left["raw_model"],
            "product_id": left["product_id"],
            "name": left["name"],
            "category": left["category"],
            "subcategory": left["subcategory"],
        },
        "alibaba": {
            "product_id": right["product_id"],
            "ordinal": right["ordinal"],
            "canonical_model": right["canonical_model"],
            "raw_model": right["raw_model"],
            "subject": right["subject"],
            "record_disposition": ALLOWED_DISPOSITION,
        },
        "rank": 0,
        "exact_model": exact_model,
        "retrieval_score": _round(retrieval),
        "unpenalized_score": _round(unpenalized),
        "available_evidence_score": _round(unpenalized / observed_weight) if observed_weight else 0.0,
        "observed_weight": _round(observed_weight),
        "component_scores": component_scores,
        "component_observed": observed,
        "component_weights": COMPONENT_WEIGHTS,
        "numeric_comparisons": numeric_comparisons,
        "conflict_penalty": penalty,
        "conflicts": conflicts,
        "visual_evidence": visual_evidence,
        "ocr_evidence": ocr_evidence,
        "calibration_status": CALIBRATION_STATUS,
        "confirmed_same_product": False,
        "external_write_enabled": False,
    }


def _worker_init(
    right_features: tuple[dict[str, Any], ...],
    idf: dict[str, float],
    visual_scores: dict[tuple[str, str], dict[str, Any]],
    ocr_scores: dict[tuple[str, str], dict[str, Any]],
) -> None:
    global _RIGHT_FEATURES, _IDF, _VISUAL_SCORES, _OCR_SCORES
    _RIGHT_FEATURES = right_features
    _IDF = idf
    _VISUAL_SCORES = visual_scores
    _OCR_SCORES = ocr_scores


def _score_official_worker(left: dict[str, Any]) -> tuple[str, list[dict[str, Any]], float, int]:
    started = time.perf_counter()
    rows = [_score_pair(left, right) for right in _RIGHT_FEATURES]
    elapsed = time.perf_counter() - started
    return left["canonical_model"], rows, elapsed, os.getpid()


def _resolve_workers(value: str | int, task_count: int) -> tuple[str, int]:
    requested = str(value).strip().lower()
    if requested == "auto":
        cpu_count = os.cpu_count() or 1
        resolved = min(16, MAX_WINDOWS_WORKERS, cpu_count, max(1, task_count))
        return "auto", resolved
    try:
        resolved = int(requested)
    except ValueError as exc:
        raise ValueError("workers must be 'auto' or an integer in [1, 61]") from exc
    if not 1 <= resolved <= MAX_WINDOWS_WORKERS:
        raise ValueError("workers must be in [1, 61]")
    return requested, min(resolved, max(1, task_count))


def _load_scalar_scores(
    path: Path | None,
    *,
    field_name: str,
    official_models: set[str],
    eligible_product_ids: set[str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    if path is None:
        return {}, {"status": "NOT_PROVIDED", "path": None, "sha256": None, "rows": 0}
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"{field_name} score file not found: {path}")
    scores: dict[tuple[str, str], dict[str, Any]] = {}
    for line_number, row in enumerate(load_jsonl(path), start=1):
        official_model = normalize_model(
            row.get("official_canonical_model")
            or row.get("official_model")
            or row.get("canonical_model")
        )
        product_id = str(row.get("alibaba_product_id") or row.get("product_id") or "")
        raw_score = row.get(f"{field_name}_score", row.get("score"))
        if official_model not in official_models:
            raise ValueError(f"{path}:{line_number} has unknown official model {official_model!r}")
        if product_id not in eligible_product_ids:
            raise ValueError(
                f"{path}:{line_number} has non-eligible or unknown Alibaba product {product_id!r}"
            )
        if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError(f"{path}:{line_number} has no numeric {field_name} score")
        score = float(raw_score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError(f"{path}:{line_number} {field_name} score must be in [0, 1]")
        key = (official_model, product_id)
        if key in scores:
            raise ValueError(f"{path}:{line_number} duplicates pair {key}")
        scores[key] = {
            "status": "AVAILABLE",
            "score": _round(score),
            "observed": bool(row.get("observed", True)),
            "method": str(row.get("method") or row.get(f"{field_name}_method") or "UNSPECIFIED"),
            "source_row": line_number,
        }
    return scores, {
        "status": "PROVIDED",
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "rows": len(scores),
    }


def _validate_inputs(
    official: Sequence[dict[str, Any]],
    historical: Sequence[dict[str, Any]],
    presence: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if not official or not historical or not presence:
        raise ValueError("official, historical, and presence inputs must all be non-empty")
    official_models = [normalize_model(row.get("canonical_model") or row.get("raw_model")) for row in official]
    if any(not model for model in official_models):
        raise ValueError("official input contains an empty canonical model")
    if len(set(official_models)) != len(official_models):
        raise ValueError("official canonical models are not unique")

    historical_ids = [str(row.get("product_id") or "") for row in historical]
    if any(not product_id for product_id in historical_ids):
        raise ValueError("historical input contains an empty product_id")
    if len(set(historical_ids)) != len(historical_ids):
        raise ValueError("historical product_ids are not unique")
    rejected = [
        row.get("product_id")
        for row in historical
        if row.get("record_disposition") != ALLOWED_DISPOSITION
    ]
    if rejected:
        raise ValueError(f"historical input contains non-eligible records: {rejected[:5]}")

    presence_ids = [str(row.get("product_id") or "") for row in presence]
    if len(set(presence_ids)) != len(presence_ids):
        raise ValueError("presence product_ids are not unique")
    disposition_counts = Counter(str(row.get("record_disposition") or "") for row in presence)
    unexpected = set(disposition_counts) - {ALLOWED_DISPOSITION, WASTE_DISPOSITION}
    if unexpected:
        raise ValueError(f"presence input contains unexpected dispositions: {sorted(unexpected)}")
    eligible_presence_ids = {
        str(row.get("product_id"))
        for row in presence
        if row.get("record_disposition") == ALLOWED_DISPOSITION
        and row.get("presence_dedup_only") is False
    }
    if eligible_presence_ids != set(historical_ids):
        missing = sorted(set(historical_ids) - eligible_presence_ids)
        extra = sorted(eligible_presence_ids - set(historical_ids))
        raise ValueError(f"historical/presence eligible ID mismatch; missing={missing[:5]}, extra={extra[:5]}")
    invalid_waste = [
        row.get("product_id")
        for row in presence
        if row.get("record_disposition") == WASTE_DISPOSITION
        and row.get("presence_dedup_only") is not True
    ]
    if invalid_waste:
        raise ValueError(f"waste records are not presence-only: {invalid_waste[:5]}")
    return {
        "official_count": len(official),
        "historical_eligible_count": len(historical),
        "presence_count": len(presence),
        "waste_presence_count": disposition_counts[WASTE_DISPOSITION],
        "expected_pair_count": len(official) * len(historical),
    }


def _pair_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        0 if row["exact_model"] else 1,
        -float(row["retrieval_score"]),
        -float(row["unpenalized_score"]),
        -float(row["component_scores"]["model"]),
        int(row["alibaba"]["ordinal"]),
        str(row["alibaba"]["product_id"]),
    )


def _decision_status(best: Mapping[str, Any], has_waste_exact: bool) -> str:
    if best["exact_model"]:
        return "EXACT_ELIGIBLE_MODEL"
    if has_waste_exact:
        return "WASTE_ONLY_EXACT_PRESENCE"
    score = float(best["retrieval_score"])
    model_score = float(best["component_scores"]["model"])
    critical_conflict = any(item.get("severity") == "CRITICAL" for item in best["conflicts"])
    if model_score >= 0.85 and score >= 0.40:
        return "MODEL_FAMILY_VARIANT_REVIEW"
    if score >= 0.50 and not critical_conflict:
        return "POSSIBLE_SAME_PRODUCT_REVIEW"
    if score >= 0.25:
        return "RELATED_OR_ACCESSORY_REVIEW"
    return "NO_EFFECTIVE_ARCHIVE_EVIDENCE"


def _candidate_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rank": row["rank"],
        "product_id": row["alibaba"]["product_id"],
        "canonical_model": row["alibaba"]["canonical_model"],
        "raw_model": row["alibaba"]["raw_model"],
        "subject": row["alibaba"]["subject"],
        "exact_model": row["exact_model"],
        "retrieval_score": row["retrieval_score"],
        "unpenalized_score": row["unpenalized_score"],
        "component_scores": row["component_scores"],
        "component_observed": row["component_observed"],
        "conflict_penalty": row["conflict_penalty"],
        "conflicts": row["conflicts"],
        "visual_evidence": row["visual_evidence"],
        "ocr_evidence": row["ocr_evidence"],
        "confirmed_same_product": False,
    }


def _confidence_tier(status: str, best: Mapping[str, Any], margin: float) -> str:
    if status == "EXACT_ELIGIBLE_MODEL":
        return "IDENTIFIER_EXACT_PRESENCE"
    if status == "WASTE_ONLY_EXACT_PRESENCE":
        return "PRESENCE_ONLY_UNUSABLE"
    if not best["component_observed"]["visual"]:
        return "UNVALIDATED_VISION_MISSING"
    score = float(best["retrieval_score"])
    if score >= 0.70 and margin >= 0.08 and not best["conflicts"]:
        return "REVIEW_HIGH"
    if score >= 0.45:
        return "REVIEW_MEDIUM"
    return "REVIEW_LOW"


def _build_decisions(
    rows_by_model: Mapping[str, list[dict[str, Any]]],
    official_by_model: Mapping[str, Mapping[str, Any]],
    historical: Sequence[Mapping[str, Any]],
    presence: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    historical_by_model: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in historical:
        historical_by_model[normalize_model(row.get("canonical_model") or row.get("raw_model"))].append(row)
    waste_by_model: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in presence:
        if row.get("record_disposition") == WASTE_DISPOSITION:
            waste_by_model[normalize_model(row.get("canonical_model") or row.get("raw_model"))].append(row)

    decisions: list[dict[str, Any]] = []
    for model in sorted(rows_by_model):
        rows = rows_by_model[model]
        if not rows:
            raise ValueError(f"official model {model} has no eligible Alibaba candidates")
        exact_ids = [
            str(row["product_id"])
            for row in sorted(
                historical_by_model.get(model, []),
                key=lambda item: (int(item.get("ordinal") or 0), str(item.get("product_id") or "")),
            )
        ]
        waste_ids = [
            str(row["product_id"])
            for row in sorted(waste_by_model.get(model, []), key=lambda item: str(item.get("product_id") or ""))
        ]
        best = rows[0]
        runner_up = rows[1] if len(rows) > 1 else None
        margin = _round(
            float(best["retrieval_score"])
            - (float(runner_up["retrieval_score"]) if runner_up else 0.0)
        )
        status = _decision_status(best, bool(waste_ids))
        top_five = rows[:5]
        visual_count = sum(bool(row["component_observed"]["visual"]) for row in top_five)
        ocr_count = sum(bool(row["component_observed"]["ocr"]) for row in top_five)
        official = official_by_model[model]
        decisions.append(
            {
                "schema_version": "sellerkit.full_match_decision.v1",
                "official": {
                    "canonical_model": model,
                    "raw_model": str(official.get("raw_model") or ""),
                    "product_id": str(official.get("product_id") or model),
                    "name": str(official.get("name") or ""),
                    "category": str(official.get("category") or ""),
                    "subcategory": str(official.get("subcategory") or ""),
                },
                "status": status,
                "confidence_tier": _confidence_tier(status, best, margin),
                "forced_best_candidate": _candidate_summary(best),
                "top_candidates": [_candidate_summary(row) for row in top_five],
                "top1_top2_score_margin": margin,
                "exact_eligible_product_ids": exact_ids,
                "waste_presence_product_ids": waste_ids,
                "eligible_candidate_count": len(rows),
                "visual_top5_status": (
                    "AVAILABLE_ALL" if visual_count == len(top_five) else "PARTIAL" if visual_count else "NOT_AVAILABLE"
                ),
                "ocr_top5_status": (
                    "AVAILABLE_ALL" if ocr_count == len(top_five) else "PARTIAL" if ocr_count else "NOT_AVAILABLE"
                ),
                "ranking_rule": "EXACT_MODEL_HARD_PRIORITY_THEN_RETRIEVAL_SCORE_DESC_STABLE_TIEBREAK",
                "calibration_status": CALIBRATION_STATUS,
                "forced_ranking_is_confirmation": False,
                "confirmed_same_product": False,
                "external_write_enabled": False,
            }
        )

    pair_rows_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    forced_by_product: dict[str, list[str]] = defaultdict(list)
    top5_by_product: dict[str, list[str]] = defaultdict(list)
    for model, rows in rows_by_model.items():
        for row in rows:
            pair_rows_by_product[row["alibaba"]["product_id"]].append(row)
        forced_by_product[rows[0]["alibaba"]["product_id"]].append(model)
        for row in rows[:5]:
            top5_by_product[row["alibaba"]["product_id"]].append(model)

    reverse: list[dict[str, Any]] = []
    for presence_ordinal, item in enumerate(presence, start=1):
        product_id = str(item.get("product_id") or "")
        disposition = str(item.get("record_disposition") or "")
        model = normalize_model(item.get("canonical_model") or item.get("raw_model"))
        exact_website_models = sorted(
            official_model for official_model in official_by_model if official_model and official_model == model
        )
        scored_rows = sorted(pair_rows_by_product.get(product_id, []), key=_pair_sort_key)
        reverse.append(
            {
                "schema_version": "sellerkit.full_match_reverse_coverage.v1",
                "presence_ordinal": presence_ordinal,
                "alibaba_product_id": product_id,
                "canonical_model": model,
                "raw_model": str(item.get("raw_model") or ""),
                "record_disposition": disposition,
                "presence_only": disposition == WASTE_DISPOSITION,
                "scored": disposition == ALLOWED_DISPOSITION,
                "pair_score_count": len(scored_rows),
                "forced_best_count": len(forced_by_product.get(product_id, [])),
                "forced_best_website_models": sorted(forced_by_product.get(product_id, [])),
                "top5_count": len(top5_by_product.get(product_id, [])),
                "top5_website_models": sorted(top5_by_product.get(product_id, [])),
                "exact_presence_website_models": exact_website_models,
                "best_reverse_candidates": [
                    {
                        "official_model": row["official"]["canonical_model"],
                        "official_name": row["official"]["name"],
                        "rank_for_official": row["rank"],
                        "exact_model": row["exact_model"],
                        "retrieval_score": row["retrieval_score"],
                    }
                    for row in scored_rows[:5]
                ],
                "content_fields_used_for_scoring": disposition == ALLOWED_DISPOSITION,
                "external_write_enabled": False,
            }
        )
    return decisions, reverse


def _write_jsonl_atomic(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(_compact(row))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _update_sqlite_atomic(
    database_path: Path,
    pair_rows: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    reverse: Sequence[Mapping[str, Any]],
    run_metadata: Mapping[str, Any],
) -> None:
    if not database_path.is_file():
        raise FileNotFoundError(f"base SQLite database is not built: {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.full-match-", suffix=".partial", dir=database_path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source: sqlite3.Connection | None = None
    connection: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(database_path)
        connection = sqlite3.connect(temporary)
        source.backup(connection)
        source.close()
        source = None
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            DROP TABLE IF EXISTS full_match_pairs;
            DROP TABLE IF EXISTS full_match_decisions;
            DROP TABLE IF EXISTS full_match_reverse_coverage;
            CREATE TABLE full_match_pairs (
                official_model TEXT NOT NULL REFERENCES official_products(canonical_model),
                alibaba_product_id TEXT NOT NULL REFERENCES alibaba_products(product_id),
                rank INTEGER NOT NULL,
                exact_model INTEGER NOT NULL,
                retrieval_score REAL NOT NULL,
                unpenalized_score REAL NOT NULL,
                model_score REAL NOT NULL,
                text_score REAL NOT NULL,
                category_score REAL NOT NULL,
                structured_score REAL NOT NULL,
                numeric_score REAL NOT NULL,
                visual_score REAL,
                ocr_score REAL,
                conflict_penalty REAL NOT NULL,
                pair_json TEXT NOT NULL,
                PRIMARY KEY (official_model, alibaba_product_id),
                UNIQUE (official_model, rank)
            );
            CREATE INDEX idx_full_match_pairs_product ON full_match_pairs(alibaba_product_id);
            CREATE INDEX idx_full_match_pairs_rank ON full_match_pairs(official_model, rank);
            CREATE INDEX idx_full_match_pairs_score ON full_match_pairs(retrieval_score DESC);
            CREATE TABLE full_match_decisions (
                official_model TEXT PRIMARY KEY REFERENCES official_products(canonical_model),
                status TEXT NOT NULL,
                forced_best_product_id TEXT NOT NULL REFERENCES alibaba_products(product_id),
                decision_json TEXT NOT NULL
            );
            CREATE TABLE full_match_reverse_coverage (
                alibaba_product_id TEXT PRIMARY KEY REFERENCES alibaba_products(product_id),
                record_disposition TEXT NOT NULL,
                scored INTEGER NOT NULL,
                forced_best_count INTEGER NOT NULL,
                top5_count INTEGER NOT NULL,
                reverse_json TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO full_match_pairs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    row["official"]["canonical_model"],
                    row["alibaba"]["product_id"],
                    row["rank"],
                    int(bool(row["exact_model"])),
                    row["retrieval_score"],
                    row["unpenalized_score"],
                    row["component_scores"]["model"],
                    row["component_scores"]["text"],
                    row["component_scores"]["category"],
                    row["component_scores"]["structured"],
                    row["component_scores"]["numeric"],
                    row["component_scores"]["visual"] if row["component_observed"]["visual"] else None,
                    row["component_scores"]["ocr"] if row["component_observed"]["ocr"] else None,
                    row["conflict_penalty"],
                    _compact(row),
                )
                for row in pair_rows
            ),
        )
        connection.executemany(
            "INSERT INTO full_match_decisions VALUES (?, ?, ?, ?)",
            (
                (
                    row["official"]["canonical_model"],
                    row["status"],
                    row["forced_best_candidate"]["product_id"],
                    _compact(row),
                )
                for row in decisions
            ),
        )
        connection.executemany(
            "INSERT INTO full_match_reverse_coverage VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    row["alibaba_product_id"],
                    row["record_disposition"],
                    int(bool(row["scored"])),
                    row["forced_best_count"],
                    row["top5_count"],
                    _compact(row),
                )
                for row in reverse
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("full_match_latest", _compact(run_metadata)),
        )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        connection.close()
        connection = None
        os.replace(temporary, database_path)
    except Exception:
        if source is not None:
            source.close()
        if connection is not None:
            connection.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _engine_source_binding() -> tuple[dict[str, str], str]:
    source_files = {
        path.name: sha256_file(path)
        for path in sorted(
            (Path(__file__).resolve(), Path(__file__).with_name("utils.py").resolve()),
            key=lambda item: item.name,
        )
    }
    digest = hashlib.sha256(_compact(source_files).encode("utf-8")).hexdigest()
    return source_files, digest


def _run_id(
    official_path: Path,
    historical_path: Path,
    presence_path: Path,
    visual_info: Mapping[str, Any],
    ocr_info: Mapping[str, Any],
    engine_source_binding_sha256: str,
) -> str:
    binding = {
        "engine_version": FULL_MATCH_ENGINE_VERSION,
        "engine_source_binding_sha256": engine_source_binding_sha256,
        "weights": COMPONENT_WEIGHTS,
        "official_sha256": sha256_file(official_path),
        "historical_sha256": sha256_file(historical_path),
        "presence_sha256": sha256_file(presence_path),
        "visual_sha256": visual_info.get("sha256"),
        "ocr_sha256": ocr_info.get("sha256"),
    }
    return hashlib.sha256(_compact(binding).encode("utf-8")).hexdigest()[:20]


def build_full_match(
    paths: Any,
    *,
    workers: str | int = "auto",
    visual_scores_path: str | Path | None = None,
    ocr_scores_path: str | Path | None = None,
    output_root: str | Path | None = None,
    update_database: bool = True,
    vision_required: bool = False,
) -> dict[str, Any]:
    """Build a deterministic website-left, exhaustive Alibaba match index.

    ``paths`` is the project's ``config.Paths`` instance.  Worker processes only
    calculate pair features.  JSONL and SQLite writes happen after all workers
    return and are performed exclusively by this parent process.

    Optional scalar files are JSONL with ``official_model``,
    ``alibaba_product_id`` and either ``visual_score``/``ocr_score`` or ``score``.
    Only eligible Alibaba IDs are accepted, preventing waste records from
    entering content or image scoring.
    """

    started = time.perf_counter()
    work_root = Path(paths.work_root)
    evidence_root = Path(paths.evidence_root)
    database_path = Path(paths.database_path)
    official_path = work_root / "official_current.jsonl"
    historical_path = work_root / "alibaba_historical_sanitized.jsonl"
    presence_path = work_root / "alibaba_presence_guard.jsonl"
    for path in (official_path, historical_path, presence_path):
        if not path.is_file():
            raise FileNotFoundError(f"required full-match input missing: {path}")

    load_started = time.perf_counter()
    official = load_jsonl(official_path)
    historical = load_jsonl(historical_path)
    presence = load_jsonl(presence_path)
    input_counts = _validate_inputs(official, historical, presence)
    official_by_model = {
        normalize_model(row.get("canonical_model") or row.get("raw_model")): row for row in official
    }
    eligible_product_ids = {str(row["product_id"]) for row in historical}
    official_models = set(official_by_model)
    visual_scores, visual_info = _load_scalar_scores(
        Path(visual_scores_path) if visual_scores_path is not None else None,
        field_name="visual",
        official_models=official_models,
        eligible_product_ids=eligible_product_ids,
    )
    ocr_scores, ocr_info = _load_scalar_scores(
        Path(ocr_scores_path) if ocr_scores_path is not None else None,
        field_name="ocr",
        official_models=official_models,
        eligible_product_ids=eligible_product_ids,
    )
    if vision_required and len(visual_scores) != input_counts["expected_pair_count"]:
        raise ValueError(
            "vision_required=True but visual score coverage is "
            f"{len(visual_scores)}/{input_counts['expected_pair_count']}"
        )
    load_elapsed = time.perf_counter() - load_started

    feature_started = time.perf_counter()
    left_features = tuple(sorted((_official_features(row) for row in official), key=lambda row: row["canonical_model"]))
    right_features = tuple(
        sorted(
            (_historical_features(row) for row in historical),
            key=lambda row: (row["ordinal"], row["product_id"]),
        )
    )
    idf = _compute_idf((*left_features, *right_features))
    feature_elapsed = time.perf_counter() - feature_started

    requested_workers, resolved_workers = _resolve_workers(workers, len(left_features))
    score_started = time.perf_counter()
    if resolved_workers == 1:
        _worker_init(right_features, idf, visual_scores, ocr_scores)
        score_results = [_score_official_worker(left) for left in left_features]
    else:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=resolved_workers,
            mp_context=context,
            initializer=_worker_init,
            initargs=(right_features, idf, visual_scores, ocr_scores),
        ) as executor:
            score_results = list(executor.map(_score_official_worker, left_features, chunksize=1))
    score_elapsed = time.perf_counter() - score_started

    rows_by_model: dict[str, list[dict[str, Any]]] = {}
    per_official_seconds: list[float] = []
    worker_pids: set[int] = set()
    for model, rows, elapsed, worker_pid in score_results:
        ordered = sorted(rows, key=_pair_sort_key)
        for rank, row in enumerate(ordered, start=1):
            row["rank"] = rank
        rows_by_model[model] = ordered
        per_official_seconds.append(elapsed)
        worker_pids.add(worker_pid)

    pair_rows = [row for model in sorted(rows_by_model) for row in rows_by_model[model]]
    decisions, reverse = _build_decisions(rows_by_model, official_by_model, historical, presence)

    expected_pairs = input_counts["expected_pair_count"]
    if len(pair_rows) != expected_pairs:
        raise ValueError(f"pair count mismatch: expected {expected_pairs}, observed {len(pair_rows)}")
    if len(decisions) != len(official):
        raise ValueError("decision coverage is not complete")
    if len(reverse) != len(presence):
        raise ValueError("reverse coverage is not complete")
    waste_ids = {
        str(row["product_id"])
        for row in presence
        if row.get("record_disposition") == WASTE_DISPOSITION
    }
    scored_ids = {str(row["alibaba"]["product_id"]) for row in pair_rows}
    waste_leakage = sorted(waste_ids & scored_ids)
    if waste_leakage:
        raise ValueError(f"waste records leaked into pair scoring: {waste_leakage[:5]}")
    if any(len(row["top_candidates"]) != min(5, len(historical)) for row in decisions):
        raise ValueError("top-5 decision coverage is incomplete")
    if any(row["forced_best_candidate"]["rank"] != 1 for row in decisions):
        raise ValueError("a forced best candidate is not rank 1")

    exact_eligible_id_count = sum(len(row["exact_eligible_product_ids"]) for row in decisions)
    exact_eligible_model_count = sum(bool(row["exact_eligible_product_ids"]) for row in decisions)
    waste_only_exact_model_count = sum(
        bool(row["waste_presence_product_ids"]) and not row["exact_eligible_product_ids"]
        for row in decisions
    )

    engine_source_files, engine_source_binding_sha256 = _engine_source_binding()
    run_id = _run_id(
        official_path,
        historical_path,
        presence_path,
        visual_info,
        ocr_info,
        engine_source_binding_sha256,
    )
    execution_id = (
        f"{run_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"-{os.getpid()}-{time.time_ns() % 1_000_000_000:09d}"
    )
    resolved_output_root = Path(output_root) if output_root is not None else work_root / "full_match_runs"
    run_root = resolved_output_root / f"run-{run_id}"
    pair_path = run_root / "full_match_pairs.jsonl"
    decisions_path = run_root / "full_match_decisions.jsonl"
    reverse_path = run_root / "full_match_reverse_coverage.jsonl"

    write_started = time.perf_counter()
    _write_jsonl_atomic(pair_path, pair_rows)
    _write_jsonl_atomic(decisions_path, decisions)
    _write_jsonl_atomic(reverse_path, reverse)
    output_hashes = {
        "pairs": {"path": str(pair_path.resolve()), "sha256": sha256_file(pair_path), "rows": len(pair_rows)},
        "decisions": {"path": str(decisions_path.resolve()), "sha256": sha256_file(decisions_path), "rows": len(decisions)},
        "reverse_coverage": {"path": str(reverse_path.resolve()), "sha256": sha256_file(reverse_path), "rows": len(reverse)},
    }
    database_before_sha256 = sha256_file(database_path) if database_path.is_file() else None
    database_metadata = {
        "schema_version": "sellerkit.full_match_sqlite_metadata.v1",
        "run_id": run_id,
        "engine_version": FULL_MATCH_ENGINE_VERSION,
        "engine_source_binding_sha256": engine_source_binding_sha256,
        "engine_source_files": engine_source_files,
        "calibration_status": CALIBRATION_STATUS,
        "output_hashes": output_hashes,
        "pair_count": len(pair_rows),
        "decision_count": len(decisions),
        "reverse_count": len(reverse),
        "visual_scores": visual_info,
        "ocr_scores": ocr_info,
        "waste_content_scored_pairs": 0,
        "external_write_enabled": False,
    }
    database_build_receipt_path = evidence_root / "database_build_receipt.json"
    database_build_receipt: dict[str, Any] | None = None
    database_build_receipt_metadata: dict[str, Any] = {
        "path": str(database_build_receipt_path.resolve()),
        "updated": False,
        "sha256": sha256_file(database_build_receipt_path)
        if database_build_receipt_path.is_file()
        else None,
    }
    database_after_sha256 = database_before_sha256
    if update_database:
        lock_path = work_root / ".database_mutation.lock"
        with exclusive_file_lock(lock_path):
            locked_before_sha256 = sha256_file(database_path) if database_path.is_file() else None
            if locked_before_sha256 != database_before_sha256:
                raise ValueError("Database changed between full-match preflight and locked commit")
            original_database_receipt: dict[str, Any] | None = None
            if database_build_receipt_path.is_file():
                database_build_receipt = load_json(database_build_receipt_path)
                original_database_receipt = json.loads(_compact(database_build_receipt))
                recorded_before = database_build_receipt.get("database_sha256")
                if recorded_before != database_before_sha256:
                    raise ValueError(
                        "database_build_receipt.json is not bound to the database state "
                        "that full-match attempted to extend"
                    )
            rollback_fd, rollback_name = tempfile.mkstemp(
                prefix=f".{database_path.name}.rollback-",
                suffix=".sqlite3",
                dir=database_path.parent,
            )
            os.close(rollback_fd)
            rollback_path = Path(rollback_name)
            shutil.copy2(database_path, rollback_path)
            try:
                _update_sqlite_atomic(database_path, pair_rows, decisions, reverse, database_metadata)
                database_after_sha256 = sha256_file(database_path)
                if database_build_receipt is not None:
                    database_build_receipt["database_sha256"] = database_after_sha256
                    database_build_receipt["supplemental_full_match"] = {
                        "schema_version": "sellerkit.database_full_match_binding.v1",
                        "run_id": run_id,
                        "engine_version": FULL_MATCH_ENGINE_VERSION,
                        "engine_source_binding_sha256": engine_source_binding_sha256,
                        "engine_source_files": engine_source_files,
                        "database_sha256_before": database_before_sha256,
                        "database_sha256_after": database_after_sha256,
                        "output_hashes": output_hashes,
                        "visual_scores": visual_info,
                        "ocr_scores": ocr_info,
                        "waste_content_scored_pairs": 0,
                        "external_write_enabled": False,
                    }
                    write_json_atomic(database_build_receipt_path, database_build_receipt)
                    database_build_receipt_metadata = {
                        "path": str(database_build_receipt_path.resolve()),
                        "updated": True,
                        "sha256": sha256_file(database_build_receipt_path),
                    }
            except Exception:
                os.replace(rollback_path, database_path)
                if original_database_receipt is not None:
                    write_json_atomic(database_build_receipt_path, original_database_receipt)
                raise
            else:
                rollback_path.unlink()
    write_elapsed = time.perf_counter() - write_started

    total_elapsed = time.perf_counter() - started
    visual_coverage = len(visual_scores)
    if visual_coverage == expected_pairs:
        status = "PASS_FULL_MATCH_WITH_COMPLETE_VISUAL_REVIEW_ONLY"
        visual_gate = "COMPLETE"
    elif visual_coverage:
        status = "PASS_FULL_MATCH_WITH_PARTIAL_VISUAL_REVIEW_ONLY"
        visual_gate = "PARTIAL"
    else:
        status = "PASS_STRUCTURED_FULL_MATCH_VISION_NOT_PROVIDED_REVIEW_ONLY"
        visual_gate = "NOT_PROVIDED"
    receipt = {
        "schema_version": "sellerkit.full_match_receipt.v1",
        "status": status,
        "run_id": run_id,
        "execution_id": execution_id,
        "generated_at_utc": _utc_now(),
        "engine_version": FULL_MATCH_ENGINE_VERSION,
        "engine_source_binding_sha256": engine_source_binding_sha256,
        "engine_source_files": engine_source_files,
        "inputs": {
            "official": {"path": str(official_path.resolve()), "sha256": sha256_file(official_path), "rows": len(official)},
            "historical_eligible": {"path": str(historical_path.resolve()), "sha256": sha256_file(historical_path), "rows": len(historical)},
            "presence_guard": {"path": str(presence_path.resolve()), "sha256": sha256_file(presence_path), "rows": len(presence)},
            "visual_scores": visual_info,
            "ocr_scores": ocr_info,
        },
        "outputs": output_hashes,
        "database": {
            "path": str(database_path.resolve()),
            "updated": update_database,
            "sha256_before": database_before_sha256,
            "sha256_after": database_after_sha256,
            "tables": [
                "full_match_pairs",
                "full_match_decisions",
                "full_match_reverse_coverage",
            ] if update_database else [],
            "database_build_receipt": database_build_receipt_metadata,
        },
        "counts": {
            **input_counts,
            "pair_count": len(pair_rows),
            "decision_count": len(decisions),
            "reverse_coverage_count": len(reverse),
            "exact_eligible_model_count": exact_eligible_model_count,
            "exact_eligible_product_id_count": exact_eligible_id_count,
            "waste_only_exact_model_count": waste_only_exact_model_count,
            "visual_pair_coverage_count": visual_coverage,
            "ocr_pair_coverage_count": len(ocr_scores),
            "waste_content_scored_pairs": 0,
        },
        "scoring": {
            "weights": COMPONENT_WEIGHTS,
            "ranking_rule": "EXACT_MODEL_HARD_PRIORITY_THEN_RETRIEVAL_SCORE_DESC_STABLE_TIEBREAK",
            "calibration_status": CALIBRATION_STATUS,
            "forced_ranking_is_confirmation": False,
            "visual_gate": visual_gate,
            "vision_required": vision_required,
        },
        "execution": {
            "workers_requested": requested_workers,
            "workers_resolved": resolved_workers,
            "worker_start_method": "spawn" if resolved_workers > 1 else "in_process",
            "worker_pids": sorted(worker_pids),
            "worker_pid_count": len(worker_pids),
            "load_seconds": _round(load_elapsed),
            "feature_precompute_seconds": _round(feature_elapsed),
            "scoring_seconds": _round(score_elapsed),
            "write_and_sqlite_seconds": _round(write_elapsed),
            "total_seconds": _round(total_elapsed),
            "pairs_per_second_scoring": _round(len(pair_rows) / score_elapsed) if score_elapsed else 0.0,
            "per_official_seconds_p50": _round(statistics.median(per_official_seconds)) if per_official_seconds else 0.0,
            "per_official_seconds_p95": _round(_percentile(per_official_seconds, 0.95)),
        },
        "safety": {
            "waste_records_presence_only": True,
            "waste_record_content_fields_read": False,
            "workers_write_outputs": False,
            "external_write_enabled": False,
            "browser_actions": 0,
            "remote_drafts": 0,
            "publish_requests": 0,
        },
    }
    receipt_path = evidence_root / "full_match_receipts" / f"full_match_receipt_{execution_id}.json"
    final_source_files, final_source_binding_sha256 = _engine_source_binding()
    if (
        final_source_files != engine_source_files
        or final_source_binding_sha256 != engine_source_binding_sha256
    ):
        raise RuntimeError("Full-match engine source changed during execution; receipt not committed")
    write_json_atomic(receipt_path, receipt)
    receipt["receipt_path"] = str(receipt_path.resolve())
    receipt["receipt_sha256"] = sha256_file(receipt_path)
    return receipt


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build exhaustive SellerKit-to-Alibaba local matches")
    parser.add_argument("--workers", default="auto", help="auto or an integer from 1 through 61")
    parser.add_argument("--visual-scores", type=Path)
    parser.add_argument("--ocr-scores", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--vision-required", action="store_true")
    parser.add_argument("--no-database-update", action="store_true")
    args = parser.parse_args()

    from .config import default_paths

    receipt = build_full_match(
        default_paths(),
        workers=args.workers,
        visual_scores_path=args.visual_scores,
        ocr_scores_path=args.ocr_scores,
        output_root=args.output_root,
        update_database=not args.no_database_update,
        vision_required=args.vision_required,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
