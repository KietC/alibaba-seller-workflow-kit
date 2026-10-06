import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BROWSER_EVIDENCE_MANIFEST,
  USAGE,
  assertNewOutputTargets,
  dataRegion,
  loadBrowserEvidence,
  parseCandidateWorkbookArgs,
  resolveArtifactTool,
  sha256Bytes,
  sha256Canonical,
} from "./candidate_workbook_cli.mjs";

async function temporaryDirectory(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "candidate-workbook-cli-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

async function makeEvidenceFixture(t, mutate = null) {
  const root = await temporaryDirectory(t);
  const evidenceDir = path.join(root, "browser-evidence");
  await fs.mkdir(evidenceDir);
  const runSpec = {
    run_id: "run-zx900-001",
    target_model: "ZX900",
    template_product_id: "100000000001",
  };
  const runSpecBytes = Buffer.from(`${JSON.stringify(runSpec, null, 2)}\n`, "utf8");
  const payload = Buffer.from('{"status":"PASS_ZERO_WRITE"}\n', "utf8");
  await fs.writeFile(path.join(evidenceDir, "probe.json"), payload);
  const file = {
    role: "browser_probe",
    relative_path: "probe.json",
    bytes: payload.length,
    sha256: sha256Bytes(payload),
    run_id: runSpec.run_id,
    target_model: runSpec.target_model,
    template_product_id: runSpec.template_product_id,
  };
  const descriptor = {
    schema_version: "sellerkit.browser-evidence-manifest.v1",
    descriptor_id: "browser-evidence-zx900-001",
    run_id: runSpec.run_id,
    target_model: runSpec.target_model,
    template_product_id: runSpec.template_product_id,
    run_spec_sha256: sha256Bytes(runSpecBytes),
    files: [file],
  };
  descriptor.files_descriptor_sha256 = sha256Canonical(descriptor.files);
  await mutate?.({ root, evidenceDir, runSpec, runSpecBytes, descriptor, file });
  descriptor.descriptor_body_sha256 = sha256Canonical(descriptor);
  await fs.writeFile(
    path.join(evidenceDir, BROWSER_EVIDENCE_MANIFEST),
    `${JSON.stringify(descriptor, null, 2)}\n`,
    "utf8",
  );
  return { root, evidenceDir, runSpec, runSpecBytes, descriptor };
}

async function makeFakeArtifactTool(root) {
  const packageRoot = path.join(root, "node_modules", "@oai", "artifact-tool");
  await fs.mkdir(packageRoot, { recursive: true });
  await fs.writeFile(path.join(packageRoot, "package.json"), JSON.stringify({
    name: "@oai/artifact-tool",
    version: "0.0.0-test",
    main: "index.cjs",
  }), "utf8");
  await fs.writeFile(path.join(packageRoot, "index.cjs"), String.raw`
const fs = require("node:fs");
class Range {
  constructor(address) {
    const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(address);
    if (match && Number(match[4]) < Number(match[2])) throw new Error("reversed range: " + address);
    this.format = {};
    this.conditionalFormats = { add() {} };
  }
  merge() { return this; }
  fillDown() { return this; }
}
class Sheet {
  constructor(name) {
    this.name = name;
    this.freezePanes = { freezeRows() {}, freezeColumns() {} };
    this.tables = { add() {} };
    this.images = { add() {} };
  }
  getRange(address) { return new Range(address); }
  getUsedRange() { return new Range("A1:A1"); }
}
class FakeWorkbook {
  constructor() {
    this.comments = { setSelf() {} };
    this.worksheets = { add: (name) => new Sheet(name) };
  }
  async render() { return { async arrayBuffer() { return Buffer.from("png"); } }; }
  async inspect() { return { ndjson: "{}\n" }; }
}
module.exports = {
  Workbook: { create() { return new FakeWorkbook(); } },
  SpreadsheetFile: { async exportXlsx() { return { async save(target) { fs.writeFileSync(target, "xlsx-fixture"); } }; } },
};
`, "utf8");
  return packageRoot;
}

test("usage is complete and names every supported path", () => {
  for (const token of [
    "--candidate-dir",
    "--output",
    "--preview-dir",
    "--inspect-summary",
    "--formula-errors",
    "--browser-evidence-dir",
    "--artifact-tool-entry",
  ]) assert.match(USAGE, new RegExp(token));
  assert.match(USAGE, /Browser evidence is never inferred/);
});

test("named arguments produce output-specific, non-colliding defaults", () => {
  const cwd = path.resolve("C:/fixture");
  const parsed = parseCandidateWorkbookArgs([
    "--candidate-dir", "candidate",
    "--output", "review-v5.xlsx",
  ], { cwd });
  assert.equal(parsed.candidateDir, path.resolve(cwd, "candidate"));
  assert.equal(parsed.outputPath, path.resolve(cwd, "review-v5.xlsx"));
  assert.equal(parsed.previewDir, path.resolve(cwd, "review-v5.rendered_sheets"));
  assert.equal(parsed.inspectSummaryPath, path.resolve(cwd, "review-v5.inspect_summary.ndjson"));
  assert.equal(parsed.formulaErrorsPath, path.resolve(cwd, "review-v5.formula_errors.ndjson"));
  assert.equal(parsed.browserEvidenceDir, null);
});

test("positional, duplicate, missing, and non-xlsx arguments fail closed", () => {
  assert.throws(() => parseCandidateWorkbookArgs(["candidate", "output.xlsx"]), /positional argument/);
  assert.throws(() => parseCandidateWorkbookArgs(["--candidate-dir", "a", "--candidate-dir", "b", "--output", "x.xlsx"]), /duplicate option/);
  assert.throws(() => parseCandidateWorkbookArgs(["--candidate-dir", "a"]), /are required/);
  assert.throws(() => parseCandidateWorkbookArgs(["--candidate-dir", "a", "--output", "x.csv"]), /must end in .xlsx/);
  assert.throws(
    () => parseCandidateWorkbookArgs(["--candidate-dir", "a", "--output", "preview/x.xlsx", "--preview-dir", "preview"]),
    /may not be inside/,
  );
});

test("output, preview, and inspect targets are new-only", async (t) => {
  const root = await temporaryDirectory(t);
  for (const occupiedKey of ["outputPath", "previewDir", "inspectSummaryPath", "formulaErrorsPath"]) {
    const output = path.join(root, `${occupiedKey}.xlsx`);
    const options = parseCandidateWorkbookArgs([
      "--candidate-dir", path.join(root, "candidate"),
      "--output", output,
    ]);
    await assertNewOutputTargets(options);
    if (occupiedKey === "previewDir") await fs.mkdir(options[occupiedKey]);
    else await fs.writeFile(options[occupiedKey], "occupied", "utf8");
    await assert.rejects(() => assertNewOutputTargets(options), /already exists/);
  }
});

test("zero-row regions never create reversed spreadsheet ranges", () => {
  assert.equal(dataRegion(5, 0), null);
  assert.deepEqual(dataRegion(5, 1), { startRow: 5, endRow: 5 });
  assert.deepEqual(dataRegion(5, 3), { startRow: 5, endRow: 7 });
  assert.throws(() => dataRegion(5, -1), /non-negative/);
});

test("artifact tool resolves from an explicit package root without cwd coupling", async (t) => {
  const root = await temporaryDirectory(t);
  const packageRoot = path.join(root, "node_modules", "@oai", "artifact-tool");
  await fs.mkdir(packageRoot, { recursive: true });
  await fs.writeFile(path.join(packageRoot, "package.json"), JSON.stringify({
    name: "@oai/artifact-tool",
    version: "0.0.0-test",
    main: "index.cjs",
  }), "utf8");
  await fs.writeFile(path.join(packageRoot, "index.cjs"), "module.exports={Workbook:{fixture:true},SpreadsheetFile:{fixture:true}};\n", "utf8");
  const resolved = resolveArtifactTool({ explicitEntry: root, env: {} });
  assert.equal(resolved.discovery, "EXPLICIT_ARGUMENT");
  assert.equal(resolved.module.Workbook.fixture, true);
  const direct = resolveArtifactTool({ explicitEntry: packageRoot, env: {} });
  assert.equal(direct.module.SpreadsheetFile.fixture, true);
  const automatic = resolveArtifactTool({ cwd: root, env: {}, execPath: process.execPath, scriptUrl: import.meta.url });
  assert.equal(automatic.discovery, "AUTOMATIC_BUNDLED_RUNTIME");
  assert.equal(automatic.module.Workbook.fixture, true);
});

test("browser evidence is absent unless explicitly supplied", async () => {
  const result = await loadBrowserEvidence(null, { runSpec: {}, runSpecBytes: Buffer.alloc(0) });
  assert.deepEqual(result, { supplied: false, rows: [], descriptor: null });
});

test("explicit browser evidence validates total and per-file bindings and hashes", async (t) => {
  const fixture = await makeEvidenceFixture(t);
  const result = await loadBrowserEvidence(fixture.evidenceDir, fixture);
  assert.equal(result.supplied, true);
  assert.equal(result.rows.length, 1);
  assert.equal(result.descriptor.bindings.run_id, fixture.runSpec.run_id);
  assert.equal(result.rows[0].sha256, fixture.descriptor.files[0].sha256);
});

test("mismatched run binding is rejected even when descriptor hashes are recomputed", async (t) => {
  const fixture = await makeEvidenceFixture(t, ({ descriptor }) => {
    descriptor.run_id = "foreign-run";
  });
  await assert.rejects(() => loadBrowserEvidence(fixture.evidenceDir, fixture), /run_id mismatch/);
});

test("tampered browser file is rejected", async (t) => {
  const fixture = await makeEvidenceFixture(t);
  await fs.writeFile(path.join(fixture.evidenceDir, "probe.json"), "tampered", "utf8");
  await assert.rejects(() => loadBrowserEvidence(fixture.evidenceDir, fixture), /byte\/hash mismatch/);
});

test("undeclared adjacent file is rejected rather than silently mixed", async (t) => {
  const fixture = await makeEvidenceFixture(t);
  await fs.writeFile(path.join(fixture.evidenceDir, "foreign.png"), "not-declared", "utf8");
  await assert.rejects(() => loadBrowserEvidence(fixture.evidenceDir, fixture), /undeclared or missing files/);
});

test("unsafe relative evidence paths are rejected", async (t) => {
  const fixture = await makeEvidenceFixture(t, ({ descriptor, file }) => {
    file.relative_path = "../probe.json";
    descriptor.files_descriptor_sha256 = sha256Canonical(descriptor.files);
  });
  await assert.rejects(() => loadBrowserEvidence(fixture.evidenceDir, fixture), /unsafe or reserved/);
});

test("zero candidates and zero assets build without reversed ranges and all artifacts are new-only", async (t) => {
  const root = await temporaryDirectory(t);
  const candidateDir = path.join(root, "candidate");
  const artifactPackage = await makeFakeArtifactTool(root);
  await fs.mkdir(candidateDir);
  const runSpec = {
    run_id: "run-empty-001",
    target_model: "EMPTY900",
    template_product_id: "100000000006",
    status: "PRECHECK",
    external_write_enabled: false,
    final_write_enabled: false,
  };
  const review = {
    target_model: runSpec.target_model,
    template_product_id: runSpec.template_product_id,
    official_source: {
      approval_status: "NEEDS_REVIEW",
      source_url: "https://example.invalid/EMPTY900",
      payload_sha256: "a".repeat(64),
    },
    candidate_count_presented: 0,
    candidate_count_total: 0,
    hard_exclusions: [],
    input_counts: { visual_target_records: 0 },
    commercial_fields_status: "BLOCKED_NO_COMPARABLES",
    candidates: [],
  };
  const sourceTemplate = { fields: [] };
  const assets = {
    summary: {
      official_current_unique_count: 0,
      hash_bound_derivation_count: 0,
      source_chain_incomplete_count: 0,
    },
    assets: [],
  };
  const files = {
    "run_spec.json": runSpec,
    "candidate_review.json": review,
    "approved_source_manifest.template.json": sourceTemplate,
    "asset_manifest.candidates.json": assets,
    "evidence_manifest.json": { inputs: [], generated_files: [] },
  };
  for (const [name, value] of Object.entries(files)) {
    await fs.writeFile(path.join(candidateDir, name), `${JSON.stringify(value, null, 2)}\n`, "utf8");
  }
  const adjacentBrowserDir = path.join(root, "browser_probe");
  await fs.mkdir(adjacentBrowserDir);
  await fs.writeFile(path.join(adjacentBrowserDir, "foreign.json"), '{"target_model":"FOREIGN"}', "utf8");
  const output = path.join(root, "empty-review.xlsx");
  const buildScript = path.join(path.dirname(fileURLToPath(import.meta.url)), "build_candidate_workbook.mjs");
  const args = [
    buildScript,
    "--candidate-dir", candidateDir,
    "--output", output,
    "--artifact-tool-entry", artifactPackage,
  ];
  const first = spawnSync(process.execPath, args, { cwd: root, encoding: "utf8" });
  assert.equal(first.status, 0, `stdout=${first.stdout}\nstderr=${first.stderr}`);
  const receipt = JSON.parse(first.stdout);
  assert.equal(receipt.browserEvidenceSupplied, false);
  assert.equal(await fs.readFile(output, "utf8"), "xlsx-fixture");
  await fs.stat(path.join(root, "empty-review.rendered_sheets"));
  await fs.stat(path.join(root, "empty-review.inspect_summary.ndjson"));
  await fs.stat(path.join(root, "empty-review.formula_errors.ndjson"));
  const second = spawnSync(process.execPath, args, { cwd: root, encoding: "utf8" });
  assert.notEqual(second.status, 0);
  assert.match(second.stderr, /already exists/);
});
