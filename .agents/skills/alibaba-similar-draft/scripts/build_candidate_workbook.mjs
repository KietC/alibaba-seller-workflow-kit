import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { buildCandidateWorkbookBlockers } from "./candidate_workbook_blockers.mjs";
import {
  USAGE,
  assertNewOutputTargets,
  dataRegion,
  loadBrowserEvidence,
  parseCandidateWorkbookArgs,
  publishNewFileFromTemp,
  resolveArtifactTool,
  writeNewFile,
} from "./candidate_workbook_cli.mjs";

const options = parseCandidateWorkbookArgs(process.argv.slice(2));
if (options.help) {
  console.log(USAGE);
  process.exit(0);
}
await assertNewOutputTargets(options);
const artifactTool = resolveArtifactTool({ explicitEntry: options.artifactToolEntry });
const { SpreadsheetFile, Workbook } = artifactTool.module;
const candidateDir = options.candidateDir;
const outputPath = options.outputPath;

const readJson = async (name) => JSON.parse(await fs.readFile(path.join(candidateDir, name), "utf8"));
const runSpecPath = path.join(candidateDir, "run_spec.json");
const [review, sourceTemplate, assets, evidence, runSpecBytes] = await Promise.all([
  readJson("candidate_review.json"),
  readJson("approved_source_manifest.template.json"),
  readJson("asset_manifest.candidates.json"),
  readJson("evidence_manifest.json"),
  fs.readFile(runSpecPath),
]);
const runSpec = JSON.parse(runSpecBytes.toString("utf8"));
const browserEvidence = await loadBrowserEvidence(options.browserEvidenceDir, { runSpec, runSpecBytes });

const colors = {
  navy: "#17324D",
  teal: "#177E89",
  orange: "#D97706",
  red: "#B42318",
  green: "#207A4B",
  paleBlue: "#EAF2F8",
  paleTeal: "#E7F4F3",
  paleOrange: "#FFF3D6",
  paleRed: "#FDECEC",
  paleGreen: "#EAF6EF",
  grey: "#667085",
  paleGrey: "#F4F6F8",
  white: "#FFFFFF",
  line: "#D0D5DD",
};

const workbook = Workbook.create();
workbook.comments.setSelf({ displayName: "User" });
const summary = workbook.worksheets.add("审核摘要");
const candidates = workbook.worksheets.add("同类候选");
const fields = workbook.worksheets.add("字段决策");
const assetSheet = workbook.worksheets.add("素材审核");
const prices = workbook.worksheets.add("价格统计");
const evidenceSheet = workbook.worksheets.add("证据路径");

const jsonCell = (value) => {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map(jsonCell).join(" | ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

const titleBand = (sheet, lastCol, title, subtitle) => {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastCol}1`).merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${lastCol}1`).format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 18 },
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastCol}1`).format.rowHeight = 34;
  sheet.getRange(`A2:${lastCol}2`).merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${lastCol}2`).format = {
    fill: colors.paleBlue,
    font: { color: colors.navy, italic: true, size: 10 },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastCol}2`).format.rowHeight = 34;
};

const styleHeader = (range) => {
  range.format = {
    fill: colors.teal,
    font: { bold: true, color: colors.white },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: colors.line },
  };
  range.format.rowHeight = 30;
};

const styleData = (range) => {
  range.format = {
    verticalAlignment: "top",
    wrapText: true,
    borders: { insideHorizontal: { style: "thin", color: colors.line } },
  };
};

const addDecisionFormatting = (range) => {
  range.conditionalFormats.add("containsText", { text: "通过", format: { fill: colors.paleGreen, font: { color: colors.green, bold: true } } });
  range.conditionalFormats.add("containsText", { text: "拒绝", format: { fill: colors.paleRed, font: { color: colors.red, bold: true } } });
  range.conditionalFormats.add("containsText", { text: "待审核", format: { fill: colors.paleOrange, font: { color: colors.orange, bold: true } } });
  range.conditionalFormats.add("containsText", { text: "阻断", format: { fill: colors.paleRed, font: { color: colors.red, bold: true } } });
};

// 审核摘要
titleBand(summary, "H", `${review.target_model} Alibaba 草稿候选审核包`, `只读候选阶段｜模板 ${review.template_product_id} 仅作类目与表单骨架｜未获明确批准前禁止填表、上传、保存草稿或发布`);
summary.getRange("A4:B4").values = [["门禁项目", "当前状态"]];
styleHeader(summary.getRange("A4:B4"));
const summaryRows = [
  ["目标型号", review.target_model],
  ["模板 Product ID", review.template_product_id],
  ["模板事实/素材继承", "禁止"],
  ["官网事实状态", review.official_source.approval_status],
  ["可审候选（展示/总数）", `${review.candidate_count_presented} / ${review.candidate_count_total}`],
  ["废记录硬排除", `${review.hard_exclusions.length} 条`],
  ["视觉模型有效命中", `${review.input_counts.visual_target_records} 条（仅作候选排序佐证）`],
  ["已批准同类候选", null],
  ["已批准素材", null],
  ["已批准字段", null],
  ["商业字段", review.commercial_fields_status],
  ["当前状态", runSpec.status],
  ["外部写入", runSpec.external_write_enabled ? "已启用" : "关闭"],
  ["最终发布", runSpec.final_write_enabled ? "已启用" : "永久关闭"],
];
summary.getRange(`A5:B${4 + summaryRows.length}`).values = summaryRows;
summary.getRange("B6").format.numberFormat = "0";
summary.getRange("B12").formulas = [[review.candidates.length > 0 ? `=COUNTIF('同类候选'!$B$5:$B$${4 + review.candidates.length},"通过")` : "=0"]];
summary.getRange("B13").formulas = [[assets.assets.length > 0 ? `=COUNTIF('素材审核'!$E$5:$E$${4 + assets.assets.length},"通过")` : "=0"]];
summary.getRange("B14").formulas = [[sourceTemplate.fields.length > 0 ? `=COUNTIF('字段决策'!$E$5:$E$${4 + sourceTemplate.fields.length},"通过")` : "=0"]];
styleData(summary.getRange(`A5:B${4 + summaryRows.length}`));
summary.getRange(`A5:A${4 + summaryRows.length}`).format.font = { bold: true, color: colors.navy };
summary.getRange("B15:B18").format = { fill: colors.paleRed, font: { bold: true, color: colors.red } };
summary.getRange("D4:H4").merge();
summary.getRange("D4").values = [["必须先解决的阻断项"]];
styleHeader(summary.getRange("D4:H4"));
const blockers = buildCandidateWorkbookBlockers({ review, sourceTemplate, assets });
summary.getRange(`D5:H${4 + blockers.length}`).merge(true);
summary.getRange(`D5:D${4 + blockers.length}`).values = blockers.map((x) => [x]);
summary.getRange(`D5:H${4 + blockers.length}`).format = {
  fill: colors.paleOrange,
  font: { color: colors.navy },
  wrapText: true,
  verticalAlignment: "center",
  borders: { insideHorizontal: { style: "thin", color: colors.line } },
};
summary.getRange(`D5:H${4 + blockers.length}`).format.rowHeight = 44;
summary.getRange("D13:H13").merge();
summary.getRange("D13").values = [["下一步：先在“字段决策 / 同类候选 / 素材审核”完成逐项审核；未通过 preflight 前不进入写入阶段。"]];
summary.getRange("D13:H13").format = { fill: colors.paleTeal, font: { bold: true, color: colors.teal }, wrapText: true };
summary.getRange("D13:H13").format.rowHeight = 42;
summary.getRange("A:H").format.font = { name: "Microsoft YaHei", size: 10 };
summary.getRange("A:A").format.columnWidth = 24;
summary.getRange("B:B").format.columnWidth = 28;
summary.getRange("C:C").format.columnWidth = 3;
summary.getRange("D:H").format.columnWidth = 17;
summary.freezePanes.freezeRows(4);

// 同类候选
titleBand(candidates, "Z", "历史 Alibaba 同类候选", "全部为 HISTORICAL_REFERENCE_ONLY；批量价与样品价已分栏，只有严格配置近同构候选才可进入审核统计");
const candidateHeaders = ["审核顺序", "审核决定", "Product ID", "型号", "标题", "类目ID", "结构分", "视觉分", "综合分", "批量价下限", "批量价中点", "批量价上限", "样品价下限", "样品价上限", "价格口径", "MOQ", "贸易口径", "可交易", "修改日期", "批准后批量价中点", "证据理由", "冲突/缺失", "Alibaba URL", "记录处置", "可复制", "严格价格可比候选"];
candidates.getRange("A4:Z4").values = [candidateHeaders];
styleHeader(candidates.getRange("A4:Z4"));
const candidateRows = review.candidates.map((c) => [
  c.review_order,
  "待审核",
  c.product_id,
  c.model || "",
  c.subject || "",
  c.category_id || "",
  c.structured_score,
  c.visual_score,
  c.final_score,
  c.price?.low ?? null,
  c.price?.midpoint ?? null,
  c.price?.high ?? null,
  c.price?.sample?.low ?? null,
  c.price?.sample?.high ?? null,
  c.price?.price_basis || "",
  c.minimum_order_quantity ?? null,
  c.trade_price_type || "",
  c.trade_available === true ? "是" : c.trade_available === false ? "否" : "未知",
  c.gmt_modified_proxy || "",
  null,
  jsonCell(c.evidence_reasons),
  jsonCell(c.conflicts_or_missing),
  c.detail_url || "",
  c.record_disposition,
  c.copy_allowed ? "是" : "否",
  c.price_eligible ? "是" : "否",
]);
const candidateRegion = dataRegion(5, candidateRows.length);
const candidateEnd = candidateRegion?.endRow ?? 4;
if (candidateRegion) {
  candidates.getRange(`A5:Z${candidateEnd}`).values = candidateRows;
  candidates.getRange("T5").formulas = [[`=IF(AND(B5="通过",Z5="是"),K5,"")`]];
  candidates.getRange(`T5:T${candidateEnd}`).fillDown();
  candidates.getRange(`B5:B${candidateEnd}`).dataValidation = { rule: { type: "list", values: ["待审核", "通过", "拒绝"] } };
  addDecisionFormatting(candidates.getRange(`B5:B${candidateEnd}`));
  styleData(candidates.getRange(`A5:Z${candidateEnd}`));
  candidates.getRange(`G5:I${candidateEnd}`).format.numberFormat = "0.0%";
  candidates.getRange(`J5:N${candidateEnd}`).format.numberFormat = '"US$"#,##0.00';
  candidates.getRange(`T5:T${candidateEnd}`).format.numberFormat = '"US$"#,##0.00';
  candidates.getRange(`A5:A${candidateEnd}`).format.numberFormat = "0";
  candidates.getRange(`C5:C${candidateEnd}`).format.numberFormat = "0";
  candidates.getRange(`P5:P${candidateEnd}`).format.numberFormat = "0.00";
  candidates.getRange(`A5:Z${candidateEnd}`).format.rowHeight = 48;
}
candidates.getRange("A:A").format.columnWidth = 10;
candidates.getRange("B:B").format.columnWidth = 12;
candidates.getRange("C:D").format.columnWidth = 16;
candidates.getRange("E:E").format.columnWidth = 48;
candidates.getRange("F:F").format.columnWidth = 14;
candidates.getRange("G:I").format.columnWidth = 11;
candidates.getRange("J:N").format.columnWidth = 13;
candidates.getRange("O:R").format.columnWidth = 15;
candidates.getRange("S:T").format.columnWidth = 17;
candidates.getRange("U:V").format.columnWidth = 34;
candidates.getRange("W:W").format.columnWidth = 46;
candidates.getRange("X:Z").format.columnWidth = 20;
candidates.freezePanes.freezeRows(4);
candidates.freezePanes.freezeColumns(4);
if (candidateRegion) candidates.tables.add(`A4:Z${candidateEnd}`, true, "ComparableCandidates");

// 字段决策
titleBand(fields, "K", `${review.target_model} 字段决策`, "“通过”只表示候选审核意见；生成带哈希批准清单并通过 preflight 之前，write_allowed 仍为 false");
const fieldHeaders = ["字段", "候选值", "官网证据值", "初始状态", "审核决定", "写入状态", "冲突/说明", "来源处置", "来源路径", "来源 SHA-256", "审核备注"];
fields.getRange("A4:K4").values = [fieldHeaders];
styleHeader(fields.getRange("A4:K4"));
const fieldRows = sourceTemplate.fields.map((f) => [
  f.field,
  jsonCell(f.candidate_value),
  jsonCell(f.evidence_value),
  f.decision,
  f.decision === "BLOCKED" ? "阻断" : "待审核",
  "禁止写入",
  f.note || "",
  f.source_disposition || "",
  f.source_path || "",
  f.source_sha256 || "",
  "",
]);
const fieldRegion = dataRegion(5, fieldRows.length);
const fieldEnd = fieldRegion?.endRow ?? 4;
if (fieldRegion) {
  fields.getRange(`A5:K${fieldEnd}`).values = fieldRows;
  fields.getRange(`E5:E${fieldEnd}`).dataValidation = { rule: { type: "list", values: ["待审核", "通过", "拒绝", "阻断"] } };
  addDecisionFormatting(fields.getRange(`E5:E${fieldEnd}`));
  styleData(fields.getRange(`A5:K${fieldEnd}`));
}
for (let i = 0; i < sourceTemplate.fields.length; i += 1) {
  const f = sourceTemplate.fields[i];
  if (f.field === "spray_valve") {
    fields.getRange(`A${i + 5}:K${i + 5}`).format.fill = colors.paleTeal;
  } else if (f.decision === "BLOCKED") {
    fields.getRange(`A${i + 5}:K${i + 5}`).format.fill = colors.paleRed;
  }
}
if (fieldRegion) fields.getRange(`A5:K${fieldEnd}`).format.rowHeight = 58;
fields.getRange("A:A").format.columnWidth = 25;
fields.getRange("B:C").format.columnWidth = 38;
fields.getRange("D:F").format.columnWidth = 14;
fields.getRange("G:G").format.columnWidth = 46;
fields.getRange("H:H").format.columnWidth = 33;
fields.getRange("I:I").format.columnWidth = 68;
fields.getRange("J:J").format.columnWidth = 68;
fields.getRange("K:K").format.columnWidth = 28;
fields.freezePanes.freezeRows(4);
fields.freezePanes.freezeColumns(2);
if (fieldRegion) fields.tables.add(`A4:K${fieldEnd}`, true, "FieldDecisions");

// 素材审核
titleBand(assetSheet, "P", `${review.target_model} 素材审核`, `${assets.summary.official_current_unique_count ?? 0} 张官网闭环图；${assets.summary.hash_bound_derivation_count ?? 0} 张产品锁派生图。原图缺失的派生图必须单独人工放行`);
const assetHeaders = ["顺序", "缩略图", "Asset ID", "类别", "审核决定", "文件存在", "哈希一致", "字节", "本地绝对路径", "SHA-256", "来源处置", "来源类型", "源链状态", "派生清单", "上传允许", "审核备注"];
assetSheet.getRange("A4:P4").values = [assetHeaders];
styleHeader(assetSheet.getRange("A4:P4"));
const assetRows = assets.assets.map((a) => [
  a.candidate_order,
  "",
  a.asset_id,
  a.category,
  a.default_decision === "REJECT" || !a.exists || !a.hash_match ? "拒绝" : "待审核",
  a.exists ? "是" : "否",
  a.hash_match ? "是" : "否",
  a.bytes ?? null,
  a.local_path || "",
  a.sha256 || "",
  a.source_disposition || "",
  a.source_type || "",
  a.source_chain_status || "",
  a.provenance_manifest_path || "",
  "否",
  a.provenance_note || (a.default_decision === "REJECT" ? "策略默认拒绝" : ""),
]);
const assetRegion = dataRegion(5, assetRows.length);
const assetEnd = assetRegion?.endRow ?? 4;
if (assetRegion) {
  assetSheet.getRange(`A5:P${assetEnd}`).values = assetRows;
  assetSheet.getRange(`E5:E${assetEnd}`).dataValidation = { rule: { type: "list", values: ["待审核", "通过", "拒绝"] } };
  addDecisionFormatting(assetSheet.getRange(`E5:E${assetEnd}`));
  styleData(assetSheet.getRange(`A5:P${assetEnd}`));
}
for (let i = 0; i < assets.assets.length; i += 1) {
  const a = assets.assets[i];
  if (a.source_chain_status === "ORIGINAL_SOURCE_MISSING_HASH_BOUND_DERIVATION") {
    assetSheet.getRange(`A${i + 5}:P${i + 5}`).format.fill = colors.paleOrange;
  }
}
if (assetRegion) assetSheet.getRange(`A5:P${assetEnd}`).format.rowHeight = 86;
assetSheet.getRange("A:A").format.columnWidth = 8;
assetSheet.getRange("B:B").format.columnWidth = 16;
assetSheet.getRange("C:C").format.columnWidth = 34;
assetSheet.getRange("D:E").format.columnWidth = 16;
assetSheet.getRange("F:G").format.columnWidth = 12;
assetSheet.getRange("H:H").format.columnWidth = 14;
assetSheet.getRange("I:I").format.columnWidth = 70;
assetSheet.getRange("J:J").format.columnWidth = 68;
assetSheet.getRange("K:M").format.columnWidth = 34;
assetSheet.getRange("N:N").format.columnWidth = 68;
assetSheet.getRange("O:O").format.columnWidth = 14;
assetSheet.getRange("P:P").format.columnWidth = 38;
if (assetRegion) assetSheet.getRange(`H5:H${assetEnd}`).format.numberFormat = "#,##0";
for (let i = 0; i < assets.assets.length; i += 1) {
  const a = assets.assets[i];
  if (!a.exists || !a.local_path) continue;
  try {
    const bytes = await fs.readFile(a.local_path);
    const ext = path.extname(a.local_path).toLowerCase();
    const mime = ext === ".png" ? "image/png" : "image/jpeg";
    assetSheet.images.add({
      dataUrl: `data:${mime};base64,${bytes.toString("base64")}`,
      anchor: { from: { row: i + 4, col: 1 }, extent: { widthPx: 104, heightPx: 76 } },
    });
  } catch {
    // The row already records the missing/unreadable path; never fabricate a thumbnail.
  }
}
assetSheet.freezePanes.freezeRows(4);
assetSheet.freezePanes.freezeColumns(2);
if (assetRegion) assetSheet.tables.add(`A4:P${assetEnd}`, true, "AssetReview");

// 价格统计
titleBand(prices, "H", "历史价格统计（审核后才计算）", `统计对象是历史 Alibaba 观察值，不是 ${review.target_model} 当前报价；必须先批准至少 3 个同口径候选`);
prices.getRange("A4:B4").values = [["指标", "结果"]];
styleHeader(prices.getRange("A4:B4"));
prices.getRange("A5:A11").values = [["已批准价格候选数"], ["统计状态"], ["P25"], ["P50"], ["P75"], ["IQR / P50"], ["价格结论"]];
prices.getRange("B5").formulas = [[candidateRegion ? `=COUNT('同类候选'!$T$5:$T$${candidateEnd})` : "=0"]];
prices.getRange("B6").formulas = [[`=IF(B5>=3,"可计算统计区间（仍非报价）","阻断：不足3个已批准同类候选")`]];
prices.getRange("B7").formulas = [[candidateRegion ? `=IF(B5>=3,QUARTILE.INC('同类候选'!$T$5:$T$${candidateEnd},1),"")` : '=""']];
prices.getRange("B8").formulas = [[candidateRegion ? `=IF(B5>=3,QUARTILE.INC('同类候选'!$T$5:$T$${candidateEnd},2),"")` : '=""']];
prices.getRange("B9").formulas = [[candidateRegion ? `=IF(B5>=3,QUARTILE.INC('同类候选'!$T$5:$T$${candidateEnd},3),"")` : '=""']];
prices.getRange("B10").formulas = [[`=IF(AND(B5>=3,B8>0),(B9-B7)/B8,"")`]];
prices.getRange("B11").formulas = [[`=IF(B5<3,"保持阻断",IF(B10>0.5,"分散度过大，保持阻断","仅供内部审核，仍需当前商业源确认"))`]];
styleData(prices.getRange("A5:B11"));
prices.getRange("A5:A11").format.font = { bold: true, color: colors.navy };
prices.getRange("B6:B11").format.fill = colors.paleOrange;
prices.getRange("B7:B9").format.numberFormat = '"US$"#,##0.00';
prices.getRange("B10").format.numberFormat = "0.0%";
prices.getRange("D4:H4").merge();
prices.getRange("D4").values = [["计算口径与否决条件"]];
styleHeader(prices.getRange("D4:H4"));
const priceRules = [
  `候选必须来自 allow_rag=true、HISTORICAL_REFERENCE_ONLY、gmt_modified < ${review.cutoff || "当前 workspace profile 截止日"} 的记录。`,
  "批量价 range/ladders 与 SKU 样品价完全分栏；样品价永不进入批量 P25/P50/P75。",
  "同一计价单位、MOQ 和贸易口径不一致时不得混算。",
  "单双柄、是否带加装摇摆嘴等配置不一致时只作扩展参考，不进入严格价格统计。",
  "价格相似不代表 MOQ、包装、交期、售后或证书可以整行复制。",
  "若 IQR/P50 > 50%，本表自动给出分散度过大提示，但最终仍由人工审核。",
  "即使统计区间出现，也必须绑定当前批准的内部商业来源后才能写入表单。",
];
prices.getRange(`D5:H${4 + priceRules.length}`).merge(true);
prices.getRange(`D5:D${4 + priceRules.length}`).values = priceRules.map((x) => [x]);
prices.getRange(`D5:H${4 + priceRules.length}`).format = { fill: colors.paleGrey, wrapText: true, font: { color: colors.navy }, borders: { insideHorizontal: { style: "thin", color: colors.line } } };
prices.getRange(`D5:H${4 + priceRules.length}`).format.rowHeight = 42;
prices.getRange("A:A").format.columnWidth = 28;
prices.getRange("B:B").format.columnWidth = 40;
prices.getRange("C:C").format.columnWidth = 3;
prices.getRange("D:H").format.columnWidth = 18;
prices.freezePanes.freezeRows(4);

// 证据路径
titleBand(evidenceSheet, "G", "证据路径与哈希", "浏览器证据仅在显式 --browser-evidence-dir 且逐文件/总描述符绑定本次 run 后纳入；不会读取相邻目录");
const evidenceHeaders = ["角色", "本地路径 / URL", "存在", "字节", "SHA-256", "使用边界", "备注"];
evidenceSheet.getRange("A4:G4").values = [evidenceHeaders];
styleHeader(evidenceSheet.getRange("A4:G4"));
const evidenceRows = [];
for (const item of evidence.inputs || []) {
  evidenceRows.push([item.role, item.path, item.exists ? "是" : "否", item.bytes ?? null, item.sha256 || "", "只读输入", ""]);
}
for (const item of evidence.generated_files || []) {
  evidenceRows.push([item.role, item.path, "是", item.bytes ?? null, item.sha256 || "", "候选审核包", "未获批准前不得写入 Alibaba"]);
}
evidenceRows.push(["official_current_url", review.official_source.source_url, "网络来源", null, review.official_source.payload_sha256, "官网事实候选", review.official_source.approval_status]);
if (browserEvidence.supplied) {
  evidenceRows.push([
    "browser_evidence_descriptor",
    browserEvidence.descriptor.path,
    "是",
    browserEvidence.descriptor.bytes,
    browserEvidence.descriptor.sha256,
    "显式且绑定的浏览器证据总描述符",
    `run_id=${browserEvidence.descriptor.bindings.run_id}; target_model=${browserEvidence.descriptor.bindings.target_model}; template_product_id=${browserEvidence.descriptor.bindings.template_product_id}; descriptor_body_sha256=${browserEvidence.descriptor.descriptorBodySha256}`,
  ]);
  for (const item of browserEvidence.rows) {
    evidenceRows.push([item.role, item.path, "是", item.bytes, item.sha256, "显式且逐文件绑定的浏览器证据", item.note]);
  }
}
const evidenceEnd = 4 + evidenceRows.length;
evidenceSheet.getRange(`A5:G${evidenceEnd}`).values = evidenceRows;
styleData(evidenceSheet.getRange(`A5:G${evidenceEnd}`));
evidenceSheet.getRange(`A5:G${evidenceEnd}`).format.rowHeight = 48;
evidenceSheet.getRange(`D5:D${evidenceEnd}`).format.numberFormat = "#,##0";
evidenceSheet.getRange("A:A").format.columnWidth = 34;
evidenceSheet.getRange("B:B").format.columnWidth = 85;
evidenceSheet.getRange("C:C").format.columnWidth = 12;
evidenceSheet.getRange("D:D").format.columnWidth = 16;
evidenceSheet.getRange("E:E").format.columnWidth = 68;
evidenceSheet.getRange("F:F").format.columnWidth = 24;
evidenceSheet.getRange("G:G").format.columnWidth = 36;
evidenceSheet.freezePanes.freezeRows(4);
evidenceSheet.freezePanes.freezeColumns(1);
evidenceSheet.tables.add(`A4:G${evidenceEnd}`, true, "EvidenceLedger");

for (const sheet of [summary, candidates, fields, assetSheet, prices, evidenceSheet]) {
  const used = sheet.getUsedRange();
  used.format.font = { name: "Microsoft YaHei", size: 10 };
}

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await fs.mkdir(path.dirname(outputPath), { recursive: true });
const temporaryOutput = path.join(
  path.dirname(outputPath),
  `.${path.basename(outputPath)}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp.xlsx`,
);
try {
  await xlsx.save(temporaryOutput);
  await publishNewFileFromTemp(temporaryOutput, outputPath);
} finally {
  await fs.rm(temporaryOutput, { force: true });
}

await fs.mkdir(path.dirname(options.previewDir), { recursive: true });
await fs.mkdir(options.previewDir);
for (const sheetName of ["审核摘要", "同类候选", "字段决策", "素材审核", "价格统计", "证据路径"]) {
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  await writeNewFile(path.join(options.previewDir, `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const keySummary = await workbook.inspect({
  kind: "table",
  range: "审核摘要!A1:H18",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 10,
  maxChars: 7000,
});
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
  maxChars: 4000,
});
await writeNewFile(options.inspectSummaryPath, keySummary.ndjson, "utf8");
await writeNewFile(options.formulaErrorsPath, formulaErrors.ndjson, "utf8");

const workbookBytes = await fs.readFile(outputPath);
console.log(JSON.stringify({
  outputPath,
  bytes: workbookBytes.length,
  sha256: crypto.createHash("sha256").update(workbookBytes).digest("hex"),
  sheets: ["审核摘要", "同类候选", "字段决策", "素材审核", "价格统计", "证据路径"],
  renderedSheetCount: 6,
  previewDir: options.previewDir,
  inspectSummaryPath: options.inspectSummaryPath,
  formulaErrorsPath: options.formulaErrorsPath,
  browserEvidenceSupplied: browserEvidence.supplied,
  artifactToolDiscovery: artifactTool.discovery,
  artifactToolResolvedEntry: artifactTool.resolvedEntry,
}, null, 2));
