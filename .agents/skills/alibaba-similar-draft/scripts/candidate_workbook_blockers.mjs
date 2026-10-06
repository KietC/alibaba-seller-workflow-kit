const asObject = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

const countOrNull = (value) => (Number.isInteger(value) && value >= 0 ? value : null);

const printable = (value) => {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(printable).filter(Boolean).join(" | ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

/**
 * Produce review blockers exclusively from the current candidate package.
 * Missing or ambiguous inputs are rendered as blockers; no product-specific
 * fallback facts are supplied here.
 */
export const buildCandidateWorkbookBlockers = ({ review, sourceTemplate, assets }) => {
  const reviewDoc = asObject(review);
  const sourceDoc = asObject(sourceTemplate);
  const assetDoc = asObject(assets);
  const fields = Array.isArray(sourceDoc.fields) ? sourceDoc.fields.filter((row) => row && typeof row === "object") : [];
  const spray = fields.find((row) => row.field === "spray_valve");
  const sprayValue = printable(spray?.candidate_value);
  const sprayResolved = Boolean(
    spray
      && spray.configuration_resolution
      && spray.configuration_resolution !== "UNRESOLVED"
      && sprayValue,
  );
  const sprayBlocker = sprayResolved
    ? `喷枪配置仅按本次候选包证据解析为“${sprayValue}”（${spray.configuration_resolution}）；仍须逐字段人工批准后才可写入。`
    : "本次候选包未唯一解析喷枪的标准/可选配置；候选值保持为空，必须补充并批准目标型号证据。";

  const inputCounts = asObject(reviewDoc.input_counts);
  const visualCount = countOrNull(inputCounts.visual_target_records);
  const visualBlocker = visualCount === null
    ? "候选包未提供目标型号识图命中计数；不得声称已完成视觉佐证，结构化规格仍为主证据。"
    : `本地识图命中 ${visualCount} 条目标型号记录，只能作为候选排序佐证，不能覆盖结构化规格。`;

  const assetSummary = asObject(assetDoc.summary);
  const officialCount = countOrNull(assetSummary.official_current_unique_count);
  const derivationCount = countOrNull(assetSummary.hash_bound_derivation_count);
  const incompleteCount = countOrNull(assetSummary.source_chain_incomplete_count);
  const assetBlocker = [officialCount, derivationCount, incompleteCount].some((value) => value === null)
    ? "素材汇总缺少官网唯一图、哈希绑定派生图或来源链缺失计数；素材审核按阻断处理。"
    : `本次目标有 ${officialCount} 张官网唯一哈希图、${derivationCount} 张哈希绑定派生图，${incompleteCount} 张来源链不完整；每张素材必须按自身来源链单独审核。`;

  const commercialStatus = printable(reviewDoc.commercial_fields_status) || "UNKNOWN";
  const commercialBlocker = `本次候选包商业字段状态为 ${commercialStatus}；价格、MOQ、库存、交期、贸易条款、包装、样品、售后、证书和保修不得从其他型号默认继承。`;

  const candidateRows = Array.isArray(reviewDoc.candidates)
    ? reviewDoc.candidates.filter((row) => row && typeof row === "object")
    : null;
  const strictCandidates = candidateRows?.filter((row) => row.price_eligible === true) ?? null;
  let priceBlocker;
  if (strictCandidates === null) {
    priceBlocker = "候选包未提供同类候选明细；无法验证三条严格、归一化可比价格，商业字段保持阻断。";
  } else {
    const labels = strictCandidates
      .map((row) => printable(row.model) || printable(row.product_id))
      .filter(Boolean);
    const evidenceSuffix = labels.length ? `（${labels.join("、")}）` : "";
    priceBlocker = strictCandidates.length < 3
      ? `严格价格可比候选仅 ${strictCandidates.length} 条${evidenceSuffix}，未达到至少 3 条门禁；历史值不能直接视为目标报价。`
      : `严格价格可比候选有 ${strictCandidates.length} 条${evidenceSuffix}；仍须统一币种、计价单位、MOQ 与贸易口径并形成独立商业审批。`;
  }

  return [
    sprayBlocker,
    visualBlocker,
    assetBlocker,
    commercialBlocker,
    priceBlocker,
    "Excel 勾选只是审核输入，不构成写入授权；必须编译哈希绑定的 approved_source_manifest.json 并通过 preflight。",
  ];
};
