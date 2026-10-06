# V1 Data Contract

## Source domains

- `official_current`: approved frozen website payloads. Product claims remain `needs_review`.
- `alibaba_historical`: only `HISTORICAL_REFERENCE_ONLY` whitelist-sanitized derivative fields. They may be queried or indexed as a constrained historical retrieval layer, but are never approved facts. Raw filled pages are not training input, and V1 builds no embeddings/vector index.
- `presence_guard`: minimal model and product ID for all approved presence records. Waste records may prevent a duplicate but provide no reusable content.
- `approved_internal`: future field-level human approvals. V1 never creates this status automatically.

Image evidence is split into immutable content and reference manifests:

- `artifacts/original/website_images/sha256/`: SHA-256-addressed current-r3 website image bytes.
- `work/website_main_images.jsonl`: exactly the approved count of URL-bound main-image references.
- `work/website_gallery_images.jsonl`: exactly the approved count of URL-bound gallery references.
- `output/website_images/main_by_model/index.jsonl`: human-browsable per-model view; it is derived, not a second authority.

Full matching uses the complete website-to-eligible-Alibaba Cartesian product. `full_match_pairs.jsonl` is scored evidence, `full_match_decisions.jsonl` contains forced Top-1/Top-5 review rows, and `full_match_reverse_coverage.jsonl` covers all approved presence records. A forced rank never means confirmed same product. Visual or OCR evidence is usable only when its receipt reports the required coverage and model gate; otherwise keep the run `REVIEW_ONLY`.

## Match states

- `CONFIRMED_PRESENT`: exact normalized model exists in an eligible historical record.
- `CONFIRMED_PRESENT_UNUSABLE_REFERENCE`: exact model exists only in a waste record; do not use its content.
- `CANDIDATE_MISSING`: no exact or approved alias match; still requires human review.
- `AMBIGUOUS_ALIAS`: reserved for a future authoritative alias registry; do not auto-decide. V1 rejects any non-empty `aliases.jsonl` because no such registry is configured.

## Package files

- `candidate_product.json`: official website facts and source binding.
- `field_provenance.jsonl`: field-level source locators and hashes.
- `match_decision.json`: exact/alias decision and ranked historical candidates.
- `similar_products.json`: sanitized eligible historical candidates only.
- `commercial_suggestions.csv`: historical suggestions; every row needs human confirmation.
- `asset_manifest.csv`: URL/materialization/upload review state.
- `fill_plan.preview.json`: blocked local preview; never executable remotely.
- `review_state.json`: hash-bound, self-attested field-review audit events; V1 does not promote them into `approved_internal`.
- `review_report.html`: human-readable local report.
- `receipt.json`: deterministic content/source/review-state hashes and no-remote-write invariants; unsigned local integrity evidence, never release authorization.

The V1 status axes are `PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE`, `READY_FOR_LOCAL_HUMAN_REVIEW`, `DISABLED_NOT_REQUESTED`, and `NOT_RELEASED_V1_LOCAL_ONLY`. None of them indicates an Alibaba draft or publication.
