import crypto from "node:crypto";
import fsSync from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

export const BROWSER_EVIDENCE_MANIFEST = "browser_evidence_manifest.json";
export const ARTIFACT_TOOL_ENV = "SELLERKIT_ARTIFACT_TOOL_ENTRY";

export const USAGE = `Usage (all paths may be absolute or relative to the current directory):

  node "<SkillRoot>\\scripts\\build_candidate_workbook.mjs" --candidate-dir "<candidate-directory>" --output "<new-output.xlsx>" [--preview-dir "<new-preview-directory>"] [--inspect-summary "<new-inspect-summary.ndjson>"] [--formula-errors "<new-formula-errors.ndjson>"] [--browser-evidence-dir "<explicit-browser-evidence-directory>"] [--artifact-tool-entry "<artifact-tool-package-or-resolution-entry>"]

PowerShell example using automatic bundled-runtime discovery:

  $SkillRoot = '<absolute-directory-containing-SKILL.md>'
  $Candidate = '<absolute-new-candidate-directory>'
  $Output = '<absolute-new-output.xlsx>'
  node (Join-Path $SkillRoot 'scripts\\build_candidate_workbook.mjs') --candidate-dir $Candidate --output $Output

PowerShell example with explicit browser evidence and artifact-tool resolution:

  node (Join-Path $SkillRoot 'scripts\\build_candidate_workbook.mjs') --candidate-dir $Candidate --output $Output --browser-evidence-dir '<directory-with-browser_evidence_manifest.json>' --artifact-tool-entry '<package path or directory resolving @oai/artifact-tool>'

The environment variable ${ARTIFACT_TOOL_ENV} is the fallback for
--artifact-tool-entry. Browser evidence is never inferred from an adjacent
directory. Output, preview, and inspect targets must not already exist.`;

const VALUE_OPTIONS = new Set([
  "--candidate-dir",
  "--output",
  "--preview-dir",
  "--inspect-summary",
  "--formula-errors",
  "--browser-evidence-dir",
  "--artifact-tool-entry",
]);

function requireValue(argv, index, option) {
  const value = argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${option} requires a value\n\n${USAGE}`);
  }
  return value;
}

export function parseCandidateWorkbookArgs(argv, { cwd = process.cwd() } = {}) {
  if (argv.includes("--help") || argv.includes("-h")) {
    return { help: true };
  }
  const raw = {};
  for (let i = 0; i < argv.length; i += 1) {
    const option = argv[i];
    if (!VALUE_OPTIONS.has(option)) {
      throw new Error(`unknown or positional argument: ${option}\n\n${USAGE}`);
    }
    if (Object.hasOwn(raw, option)) {
      throw new Error(`duplicate option: ${option}\n\n${USAGE}`);
    }
    raw[option] = requireValue(argv, i, option);
    i += 1;
  }
  if (!raw["--candidate-dir"] || !raw["--output"]) {
    throw new Error(`--candidate-dir and --output are required\n\n${USAGE}`);
  }

  const resolvePath = (value) => path.resolve(cwd, value);
  const outputPath = resolvePath(raw["--output"]);
  if (path.extname(outputPath).toLowerCase() !== ".xlsx") {
    throw new Error(`--output must end in .xlsx: ${outputPath}`);
  }
  const stem = path.basename(outputPath, path.extname(outputPath));
  const parent = path.dirname(outputPath);
  const result = {
    help: false,
    candidateDir: resolvePath(raw["--candidate-dir"]),
    outputPath,
    previewDir: raw["--preview-dir"]
      ? resolvePath(raw["--preview-dir"])
      : path.join(parent, `${stem}.rendered_sheets`),
    inspectSummaryPath: raw["--inspect-summary"]
      ? resolvePath(raw["--inspect-summary"])
      : path.join(parent, `${stem}.inspect_summary.ndjson`),
    formulaErrorsPath: raw["--formula-errors"]
      ? resolvePath(raw["--formula-errors"])
      : path.join(parent, `${stem}.formula_errors.ndjson`),
    browserEvidenceDir: raw["--browser-evidence-dir"]
      ? resolvePath(raw["--browser-evidence-dir"])
      : null,
    artifactToolEntry: raw["--artifact-tool-entry"]
      ? resolvePath(raw["--artifact-tool-entry"])
      : null,
  };
  const targets = [result.outputPath, result.previewDir, result.inspectSummaryPath, result.formulaErrorsPath];
  if (new Set(targets.map((item) => process.platform === "win32" ? item.toLowerCase() : item)).size !== targets.length) {
    throw new Error("output, preview, and inspect targets must be distinct");
  }
  for (const fileTarget of [result.outputPath, result.inspectSummaryPath, result.formulaErrorsPath]) {
    const relative = path.relative(result.previewDir, fileTarget);
    if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
      throw new Error(`file target may not be inside --preview-dir: ${fileTarget}`);
    }
  }
  return result;
}

export async function assertNewOutputTargets(options) {
  const targets = [
    ["output", options.outputPath],
    ["preview", options.previewDir],
    ["inspect summary", options.inspectSummaryPath],
    ["formula errors", options.formulaErrorsPath],
  ];
  for (const [label, target] of targets) {
    try {
      await fs.lstat(target);
      throw new Error(`${label} target already exists; choose a new path: ${target}`);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

function isArtifactToolModule(value) {
  return value && typeof value === "object" && value.Workbook && value.SpreadsheetFile;
}

function resolverBases(entry) {
  const resolved = path.resolve(entry);
  if (!fsSync.existsSync(resolved)) return [resolved];
  const stat = fsSync.statSync(resolved);
  if (stat.isDirectory()) {
    return [path.join(resolved, "package.json"), path.join(resolved, "candidate-workbook-loader.cjs")];
  }
  return [resolved];
}

function loadArtifactToolFromEntry(entry, attempts) {
  const resolvedEntry = path.resolve(entry);
  if (fsSync.existsSync(resolvedEntry) && fsSync.statSync(resolvedEntry).isDirectory()) {
    const packageJson = path.join(resolvedEntry, "package.json");
    if (fsSync.existsSync(packageJson)) {
      try {
        const packageMeta = JSON.parse(fsSync.readFileSync(packageJson, "utf8"));
        if (packageMeta.name === "@oai/artifact-tool") {
          const direct = createRequire(packageJson)(resolvedEntry);
          if (isArtifactToolModule(direct)) return { module: direct, resolvedEntry };
        }
      } catch (error) {
        attempts.push(`${resolvedEntry}: direct package load failed: ${error?.code || error?.message || String(error)}`);
      }
    }
  }
  for (const base of resolverBases(entry)) {
    try {
      const localRequire = createRequire(base);
      if (fsSync.existsSync(base)) {
        try {
          const direct = localRequire(base);
          if (isArtifactToolModule(direct)) return { module: direct, resolvedEntry: base };
        } catch {
          // The entry may only be a resolution base, not the package itself.
        }
      }
      const resolvedPackage = localRequire.resolve("@oai/artifact-tool");
      const loaded = localRequire(resolvedPackage);
      if (isArtifactToolModule(loaded)) return { module: loaded, resolvedEntry: resolvedPackage };
      attempts.push(`${base}: module is missing Workbook/SpreadsheetFile exports`);
    } catch (error) {
      attempts.push(`${base}: ${error?.code || error?.message || String(error)}`);
    }
  }
  return null;
}

function addRootAndAncestors(target, roots) {
  let current = path.resolve(target);
  if (fsSync.existsSync(current) && !fsSync.statSync(current).isDirectory()) current = path.dirname(current);
  while (true) {
    roots.add(current);
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
}

export function resolveArtifactTool({
  explicitEntry = null,
  env = process.env,
  cwd = process.cwd(),
  execPath = process.execPath,
  scriptUrl = import.meta.url,
} = {}) {
  const attempts = [];
  const configured = explicitEntry || env[ARTIFACT_TOOL_ENV] || null;
  if (configured) {
    const loaded = loadArtifactToolFromEntry(configured, attempts);
    if (loaded) return { ...loaded, discovery: explicitEntry ? "EXPLICIT_ARGUMENT" : "ENVIRONMENT" };
    throw new Error(`cannot load @oai/artifact-tool from configured entry: ${configured}\n${attempts.join("\n")}`);
  }

  const roots = new Set();
  addRootAndAncestors(cwd, roots);
  addRootAndAncestors(path.dirname(execPath), roots);
  addRootAndAncestors(path.dirname(fileURLToPath(scriptUrl)), roots);
  for (const envName of ["CODEX_WORKSPACE_DEPENDENCIES", "CODEX_WORKSPACE_DEPENDENCY_ROOT", "NODE_PATH"]) {
    for (const entry of String(env[envName] || "").split(path.delimiter).filter(Boolean)) {
      addRootAndAncestors(entry, roots);
    }
  }
  for (const root of roots) {
    const loaded = loadArtifactToolFromEntry(root, attempts);
    if (loaded) return { ...loaded, discovery: "AUTOMATIC_BUNDLED_RUNTIME" };
  }
  throw new Error(
    `cannot discover @oai/artifact-tool. Pass --artifact-tool-entry or set ${ARTIFACT_TOOL_ENV}.\n` +
    `${attempts.slice(0, 20).join("\n")}\n\n${USAGE}`,
  );
}

export function dataRegion(startRow, count) {
  if (!Number.isInteger(startRow) || startRow < 1) throw new Error("startRow must be a positive integer");
  if (!Number.isInteger(count) || count < 0) throw new Error("count must be a non-negative integer");
  return count === 0 ? null : { startRow, endRow: startRow + count - 1 };
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalJson(value[key])]));
  }
  return value;
}

export function sha256Bytes(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

export function sha256Canonical(value) {
  return sha256Bytes(Buffer.from(JSON.stringify(canonicalJson(value)), "utf8"));
}

function assertBinding(value, expected, label) {
  for (const key of ["run_id", "target_model", "template_product_id"]) {
    if (value?.[key] !== expected[key]) {
      throw new Error(`${label}.${key} mismatch: expected ${expected[key]}, got ${value?.[key]}`);
    }
  }
}

async function listRelativeFiles(root, current = root) {
  const result = [];
  const entries = await fs.readdir(current, { withFileTypes: true });
  for (const entry of entries) {
    const absolute = path.join(current, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`browser evidence may not contain symbolic links: ${absolute}`);
    if (entry.isDirectory()) result.push(...await listRelativeFiles(root, absolute));
    else if (entry.isFile()) result.push(path.relative(root, absolute).split(path.sep).join("/"));
    else throw new Error(`unsupported browser evidence filesystem entry: ${absolute}`);
  }
  return result.sort();
}

function safeRelativePath(relativePath) {
  if (typeof relativePath !== "string" || !relativePath || path.isAbsolute(relativePath)) return false;
  const normalized = path.posix.normalize(relativePath.replaceAll("\\", "/"));
  return normalized === relativePath.replaceAll("\\", "/") && normalized !== ".." && !normalized.startsWith("../");
}

export async function loadBrowserEvidence(browserEvidenceDir, { runSpec, runSpecBytes }) {
  if (!browserEvidenceDir) return { supplied: false, rows: [], descriptor: null };
  const root = path.resolve(browserEvidenceDir);
  const rootStat = await fs.stat(root).catch(() => null);
  if (!rootStat?.isDirectory()) throw new Error(`--browser-evidence-dir is not a directory: ${root}`);
  const manifestPath = path.join(root, BROWSER_EVIDENCE_MANIFEST);
  const manifestBytes = await fs.readFile(manifestPath).catch((error) => {
    throw new Error(`explicit browser evidence requires ${BROWSER_EVIDENCE_MANIFEST}: ${error.message}`);
  });
  let descriptor;
  try {
    descriptor = JSON.parse(manifestBytes.toString("utf8"));
  } catch (error) {
    throw new Error(`invalid browser evidence descriptor JSON: ${error.message}`);
  }
  if (descriptor.schema_version !== "sellerkit.browser-evidence-manifest.v1") {
    throw new Error("browser evidence descriptor schema_version must be sellerkit.browser-evidence-manifest.v1");
  }
  const expected = {
    run_id: runSpec.run_id,
    target_model: runSpec.target_model,
    template_product_id: String(runSpec.template_product_id),
  };
  assertBinding(descriptor, expected, "browser evidence descriptor");
  const runSpecSha256 = sha256Bytes(runSpecBytes);
  if (descriptor.run_spec_sha256 !== runSpecSha256) {
    throw new Error(`browser evidence descriptor run_spec_sha256 mismatch: expected ${runSpecSha256}`);
  }
  if (!Array.isArray(descriptor.files) || descriptor.files.length === 0) {
    throw new Error("browser evidence descriptor files must be a non-empty array");
  }
  const descriptorWithoutHash = { ...descriptor };
  delete descriptorWithoutHash.descriptor_body_sha256;
  const expectedDescriptorHash = sha256Canonical(descriptorWithoutHash);
  if (descriptor.descriptor_body_sha256 !== expectedDescriptorHash) {
    throw new Error(`browser evidence descriptor_body_sha256 mismatch: expected ${expectedDescriptorHash}`);
  }
  const expectedFilesHash = sha256Canonical(descriptor.files);
  if (descriptor.files_descriptor_sha256 !== expectedFilesHash) {
    throw new Error(`browser evidence files_descriptor_sha256 mismatch: expected ${expectedFilesHash}`);
  }

  const names = new Set();
  const roles = new Set();
  const rows = [];
  for (const [index, file] of descriptor.files.entries()) {
    const label = `browser evidence files[${index}]`;
    assertBinding(file, expected, label);
    if (typeof file.role !== "string" || !file.role || roles.has(file.role)) throw new Error(`${label}.role must be non-empty and unique`);
    roles.add(file.role);
    if (!safeRelativePath(file.relative_path) || file.relative_path === BROWSER_EVIDENCE_MANIFEST) {
      throw new Error(`${label}.relative_path is unsafe or reserved: ${file.relative_path}`);
    }
    const relative = file.relative_path.replaceAll("\\", "/");
    if (names.has(relative)) throw new Error(`${label}.relative_path is duplicated: ${relative}`);
    names.add(relative);
    if (!Number.isSafeInteger(file.bytes) || file.bytes < 0) throw new Error(`${label}.bytes must be a non-negative safe integer`);
    if (!/^[0-9a-f]{64}$/.test(file.sha256 || "")) throw new Error(`${label}.sha256 must be lowercase SHA-256`);
    const absolute = path.resolve(root, ...relative.split("/"));
    const real = await fs.realpath(absolute).catch(() => null);
    const rootReal = await fs.realpath(root);
    const prefix = rootReal.endsWith(path.sep) ? rootReal : `${rootReal}${path.sep}`;
    if (!real || !(process.platform === "win32" ? real.toLowerCase().startsWith(prefix.toLowerCase()) : real.startsWith(prefix))) {
      throw new Error(`${label}.relative_path escapes or is missing: ${relative}`);
    }
    const bytes = await fs.readFile(real);
    const actualSha = sha256Bytes(bytes);
    if (bytes.length !== file.bytes || actualSha !== file.sha256) {
      throw new Error(`${label} byte/hash mismatch for ${relative}`);
    }
    rows.push({
      role: file.role,
      path: absolute,
      bytes: bytes.length,
      sha256: actualSha,
      note: `run_id=${expected.run_id}; target_model=${expected.target_model}; template_product_id=${expected.template_product_id}; descriptor_body_sha256=${descriptor.descriptor_body_sha256}`,
    });
  }
  const actualFiles = await listRelativeFiles(root);
  const expectedFiles = [BROWSER_EVIDENCE_MANIFEST, ...names].sort();
  if (JSON.stringify(actualFiles) !== JSON.stringify(expectedFiles)) {
    throw new Error(`browser evidence directory contains undeclared or missing files; expected=${JSON.stringify(expectedFiles)} actual=${JSON.stringify(actualFiles)}`);
  }
  return {
    supplied: true,
    rows,
    descriptor: {
      path: manifestPath,
      bytes: manifestBytes.length,
      sha256: sha256Bytes(manifestBytes),
      descriptorBodySha256: descriptor.descriptor_body_sha256,
      filesDescriptorSha256: descriptor.files_descriptor_sha256,
      bindings: expected,
    },
  };
}

export async function writeNewFile(target, data, encoding = undefined) {
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(target, data, encoding ? { encoding, flag: "wx" } : { flag: "wx" });
}

export async function publishNewFileFromTemp(tempPath, target) {
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.copyFile(tempPath, target, fsSync.constants.COPYFILE_EXCL);
}
