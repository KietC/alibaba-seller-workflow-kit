import { createHash } from "node:crypto";
import {
  copyFile,
  link,
  mkdir,
  open,
  readFile,
  rename,
  stat,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CAPTURE_FILES = [
  "public_response.html",
  "public_rendered.html",
  "public_page_dom.json",
  "public_fullpage.png",
  "capture_receipt.json",
];

export function safePageUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== "https:" || !/(^|\.)alibaba\.com$/i.test(url.hostname)) {
    throw new Error(`unexpected public page origin: ${url.origin}`);
  }
  return `${url.origin}${url.pathname}`;
}

export function isChallengeState(state) {
  const title = String(state?.title || "");
  const bodyText = String(state?.body_text || "");
  const markers = Number(state?.challenge_markers || 0);
  return (
    markers > 0 ||
    /captcha|verify|robot|punish|验证码拦截/i.test(title) ||
    /拖动.{0,12}滑块|通过验证以确保正常访问|complete verification/i.test(bodyText)
  );
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function captureFullPagePng(cdp, contentSize) {
  const scales = [1, 0.75, 0.5, 0.375, 0.25];
  let lastError;
  for (const scale of scales) {
    try {
      const result = await cdp.send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
        captureBeyondViewport: true,
        clip: {
          x: 0,
          y: 0,
          width: Math.max(1, Math.ceil(contentSize.width)),
          height: Math.max(1, Math.ceil(contentSize.height)),
          scale,
        },
      });
      const screenshot = Buffer.from(result.data, "base64");
      if (!screenshot.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
        throw new Error("screenshot_not_png");
      }
      return { screenshot, scale };
    } catch (error) {
      lastError = error;
      if (!/frame limit|message.*limit|too large/i.test(String(error?.message || error))) {
        throw error;
      }
    }
  }
  throw lastError || new Error("screenshot_capture_failed");
}

async function existsNonempty(file) {
  try {
    return (await stat(file)).size > 0;
  } catch {
    return false;
  }
}

async function atomicWrite(file, value) {
  await mkdir(path.dirname(file), { recursive: true });
  const temporary = `${file}.partial`;
  const handle = await open(temporary, "w");
  try {
    await handle.writeFile(value);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(temporary, file);
}

async function appendJsonl(file, row) {
  await mkdir(path.dirname(file), { recursive: true });
  const handle = await open(file, "a");
  try {
    await handle.writeFile(`${JSON.stringify(row)}\n`);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function readJson(file) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch {
    return null;
  }
}

async function receiptMatches(target, receipt) {
  if (receipt?.status !== "captured") return false;
  const checks = [
    ["public_response.html", "response_sha256"],
    ["public_rendered.html", "rendered_sha256"],
    ["public_page_dom.json", "dom_sha256"],
    ["public_fullpage.png", "screenshot_sha256"],
  ];
  for (const [name, key] of checks) {
    const file = path.join(target, name);
    if (!(await existsNonempty(file))) return false;
    if (!/^[0-9a-f]{64}$/i.test(String(receipt[key] || ""))) return false;
    if (sha256(await readFile(file)) !== String(receipt[key]).toLowerCase()) return false;
  }
  return true;
}

async function preserveExistingGeneration(target, receipt) {
  if (!receipt || !(await existsNonempty(path.join(target, "capture_receipt.json")))) return null;
  const stamp = String(receipt.captured_at || "unknown")
    .replace(/[^0-9A-Za-z.-]+/g, "_")
    .slice(0, 80);
  const generation = path.join(target, "capture_generations", stamp || "unknown");
  await mkdir(generation, { recursive: true });
  for (const name of CAPTURE_FILES) {
    const source = path.join(target, name);
    const destination = path.join(generation, name);
    if (!(await existsNonempty(source)) || (await existsNonempty(destination))) continue;
    try {
      await link(source, destination);
    } catch {
      await copyFile(source, destination);
    }
  }
  return generation;
}

function productUrl(product) {
  const raw = product.detailUrl || product.detail_url || product.productDetailUrl;
  if (!raw) throw new Error(`missing public detail URL for product ${product.id || product.product_id}`);
  return safePageUrl(raw);
}

function productId(product) {
  return String(product.id || product.product_id || product.productId || "");
}

async function writeFailureReceipt(target, existing, failure) {
  if (existing?.status === "captured") {
    await atomicWrite(
      path.join(target, "capture_failure_latest.json"),
      `${JSON.stringify(failure, null, 2)}\n`,
    );
  } else {
    await atomicWrite(
      path.join(target, "capture_receipt.json"),
      `${JSON.stringify(failure, null, 2)}\n`,
    );
  }
}

async function appendAttempt(target, attempt) {
  await appendJsonl(path.join(target, "capture_attempts.jsonl"), attempt);
}

async function appendRequestLedger(runRoot, { productId, ordinal, url, method, status, outcome }) {
  const parsed = new URL(url);
  await appendJsonl(path.join(runRoot, "07_审计", "public_request_ledger.jsonl"), {
    schema_version: "1.0.0",
    observed_at: new Date().toISOString(),
    product_id: productId,
    ordinal,
    method,
    host: parsed.hostname.toLowerCase(),
    path_sha256: sha256(Buffer.from(parsed.pathname || "/", "utf8")),
    http_status: Number.isFinite(Number(status)) ? Number(status) : null,
    outcome,
    external_write: false,
  });
}

async function writeBrowserCheckpoint(runRoot, { runId, products, lastIndex, nextIndex, status, reason }) {
  const helperPath = fileURLToPath(import.meta.url);
  const productSequence = products.map((product) => productId(product)).join("\n") + "\n";
  const checkpoint = {
    schema_version: "1.0.0",
    run_id: runId,
    updated_at: new Date().toISOString(),
    status,
    reason: reason || null,
    total_products: products.length,
    catalog_product_id_sequence_sha256: sha256(Buffer.from(productSequence, "utf8")),
    capture_helper_path: helperPath,
    capture_helper_sha256: sha256(await readFile(helperPath)),
    last_attempted_ordinal: lastIndex === null ? null : lastIndex + 1,
    next_zero_based_index: nextIndex,
    next_ordinal: nextIndex === null ? null : nextIndex + 1,
    next_product_id: nextIndex === null ? null : productId(products[nextIndex]),
    stop_on_challenge: true,
    external_writes: false,
  };
  await atomicWrite(
    path.join(runRoot, "00_运行状态", "browser_capture_checkpoint.json"),
    `${JSON.stringify(checkpoint, null, 2)}\n`,
  );
}

export async function capturePublicProductBatch({
  tab,
  cdp,
  products,
  runRoot,
  runId,
  startIndex = 0,
  count = 10,
  stopOnChallenge = true,
}) {
  if (!tab || !cdp) throw new Error("tab and CDP capability are required");
  if (!Array.isArray(products)) throw new Error("products must be an array");
  if (!Number.isInteger(startIndex) || startIndex < 0) throw new Error("invalid startIndex");
  if (!Number.isInteger(count) || count < 1) throw new Error("invalid count");
  await cdp.send("Network.enable", {});
  await cdp.send("Page.enable", {});

  const results = [];
  const endIndex = Math.min(products.length, startIndex + count);
  for (let index = startIndex; index < endIndex; index += 1) {
    const product = products[index];
    const id = productId(product);
    if (!/^\d+$/.test(id)) throw new Error(`invalid product id at ordinal ${index + 1}`);
    const url = productUrl(product);
    const target = path.join(runRoot, "03_商品标准化", id);
    const receiptFile = path.join(target, "capture_receipt.json");
    await mkdir(target, { recursive: true });
    const existing = await readJson(receiptFile);
    if (await receiptMatches(target, existing)) {
      results.push({ ordinal: index + 1, product_id: id, status: "skipped_verified" });
      continue;
    }

    try {
      const eventCursor = await cdp.readEvents({
        methods: ["Network.requestWillBeSent", "Network.responseReceived"],
        limit: 1,
      });
      await tab.goto(url);
      await tab.playwright.waitForLoadState({ state: "domcontentloaded", timeoutMs: 30_000 });
      const state = await tab.playwright.evaluate(() => ({
        title: document.title,
        body_text: (document.body?.innerText || "").slice(0, 4000),
        challenge_markers: document.querySelectorAll(
          'iframe[src*="punish"],iframe[src*="captcha"],[class*="captcha"],[id*="captcha"]',
        ).length,
      }));
      const events = await cdp.readEvents({
        afterSequence: eventCursor.cursor,
        methods: ["Network.requestWillBeSent", "Network.responseReceived"],
        limit: 1000,
        timeoutMs: 5000,
      });
      const documentRequests = events.events.filter(
        (event) => event.method === "Network.requestWillBeSent" && event.params?.type === "Document",
      );
      const documentResponses = events.events.filter(
        (event) => event.method === "Network.responseReceived" && event.params?.type === "Document",
      );
      const requestEvent = [...documentRequests].reverse().find(
        (event) => String(event.params?.request?.url || "").includes(id),
      ) || documentRequests.at(-1);
      const documentEvent = [...documentResponses].reverse().find(
        (event) => String(event.params?.response?.url || "").includes(id),
      ) || documentResponses.at(-1);
      const documentMethod = String(requestEvent?.params?.request?.method || "GET").toUpperCase();
      if (!["GET", "HEAD"].includes(documentMethod)) {
        throw new Error(`unexpected_document_method:${documentMethod}`);
      }
      const observedUrl = String(
        requestEvent?.params?.request?.url || documentEvent?.params?.response?.url || url,
      );
      const observedStatus = documentEvent?.params?.response?.status;
      if (isChallengeState(state)) {
        const failure = {
          schema_version: "1.1.0",
          run_id: runId,
          product_id: id,
          ordinal: index + 1,
          status: "failed",
          captured_at: new Date().toISOString(),
          error_code: "challenge_detected",
          stop_on_challenge: stopOnChallenge,
        };
        await writeFailureReceipt(target, existing, failure);
        await appendAttempt(target, failure);
        await appendRequestLedger(runRoot, {
          productId: id,
          ordinal: index + 1,
          url: observedUrl,
          method: documentMethod,
          status: observedStatus,
          outcome: "challenge_detected",
        });
        await writeBrowserCheckpoint(runRoot, {
          runId,
          products,
          lastIndex: index,
          nextIndex: index,
          status: "BLOCKED_HUMAN_VERIFICATION",
          reason: "challenge_detected",
        });
        results.push({ ordinal: index + 1, product_id: id, status: "failed", error: "challenge_detected" });
        if (stopOnChallenge) {
          return {
            start_ordinal: startIndex + 1,
            end_ordinal: index + 1,
            stopped: true,
            stop_reason: "challenge_detected",
            next_zero_based_index: index,
            results,
          };
        }
        continue;
      }

      await tab.playwright.waitForTimeout(350);
      if (!documentEvent) throw new Error("document_response_missing");
      const body = await cdp.send("Network.getResponseBody", {
        requestId: documentEvent.params.requestId,
      });
      const responseBuffer = body.base64Encoded
        ? Buffer.from(body.body, "base64")
        : Buffer.from(body.body, "utf8");
      const dom = await tab.playwright.evaluate(() => {
        const query = (selector) => Array.from(document.querySelectorAll(selector));
        const ancestry = (element) => {
          const output = [];
          let node = element;
          for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
            output.push([node.id || "", typeof node.className === "string" ? node.className : ""].join(" "));
          }
          return output.join(" | ");
        };
        return {
          captured_at: new Date().toISOString(),
          url: `${location.origin}${location.pathname}`,
          title: document.title,
          lang: document.documentElement.lang || null,
          canonical:
            document.querySelector('link[rel="canonical"]')?.href ||
            document.querySelector('meta[property="og:url"]')?.content ||
            null,
          h1: document.querySelector("h1")?.textContent?.trim() || null,
          json_ld: query('script[type="application/ld+json"]').map((script) => script.textContent || ""),
          images: query("img").map((image, position) => ({
            index: position + 1,
            src: image.getAttribute("src"),
            current_src: image.currentSrc || null,
            data_src:
              image.getAttribute("data-src") ||
              image.getAttribute("data-lazy-src") ||
              image.getAttribute("data-original"),
            srcset: image.getAttribute("srcset") || image.getAttribute("data-srcset"),
            alt: image.getAttribute("alt"),
            class_name: image.className || "",
            parent_class: image.parentElement?.className || "",
            ancestor_context: ancestry(image),
          })),
          videos: query("video").map((video, position) => ({
            index: position + 1,
            src: video.currentSrc || video.getAttribute("src"),
            poster: video.poster || video.getAttribute("poster"),
            class_name: video.className || "",
            ancestor_context: ancestry(video),
            sources: Array.from(video.querySelectorAll("source")).map(
              (source) => source.src || source.getAttribute("src"),
            ),
          })),
          document_links: query("a[href]")
            .map((anchor) => anchor.href)
            .filter((href) => /\.(?:pdf|docx?|xlsx?|zip)(?:$|[?#])/i.test(href)),
          counts: {
            images: query("img").length,
            videos: query("video").length,
            json_ld: query('script[type="application/ld+json"]').length,
          },
        };
      });
      const rendered = await tab.playwright.evaluate(
        () => `<!DOCTYPE html>\n${document.documentElement.outerHTML}`,
      );
      const metrics = await cdp.send("Page.getLayoutMetrics", {});
      const contentSize = metrics.cssContentSize || metrics.contentSize;
      const screenshotCapture = await captureFullPagePng(cdp, contentSize);
      const screenshot = screenshotCapture.screenshot;
      const domBuffer = Buffer.from(`${JSON.stringify(dom, null, 2)}\n`, "utf8");
      const renderedBuffer = Buffer.from(rendered, "utf8");

      if (existing) await preserveExistingGeneration(target, existing);
      await atomicWrite(path.join(target, "public_response.html"), responseBuffer);
      await atomicWrite(path.join(target, "public_rendered.html"), renderedBuffer);
      await atomicWrite(path.join(target, "public_page_dom.json"), domBuffer);
      await atomicWrite(path.join(target, "public_fullpage.png"), screenshot);
      const receipt = {
        schema_version: "1.1.0",
        run_id: runId,
        product_id: id,
        ordinal: index + 1,
        status: "captured",
        captured_at: new Date().toISOString(),
        http_status: documentEvent.params.response.status,
        page_url: safePageUrl(dom.url),
        response_bytes: responseBuffer.length,
        response_sha256: sha256(responseBuffer),
        rendered_bytes: renderedBuffer.length,
        rendered_sha256: sha256(renderedBuffer),
        dom_bytes: domBuffer.length,
        dom_sha256: sha256(domBuffer),
        screenshot_bytes: screenshot.length,
        screenshot_sha256: sha256(screenshot),
        screenshot_capture_scale: screenshotCapture.scale,
        screenshot_css_width: Math.ceil(contentSize.width),
        screenshot_css_height: Math.ceil(contentSize.height),
        dom_counts: dom.counts,
        acquisition: {
          browser: "Codex in-app browser",
          method: "read-only GET and rendered DOM capture",
          external_writes: false,
        },
      };
      await atomicWrite(receiptFile, `${JSON.stringify(receipt, null, 2)}\n`);
      await appendAttempt(target, receipt);
      await appendRequestLedger(runRoot, {
        productId: id,
        ordinal: index + 1,
        url: observedUrl,
        method: documentMethod,
        status: observedStatus,
        outcome: "captured",
      });
      await writeBrowserCheckpoint(runRoot, {
        runId,
        products,
        lastIndex: index,
        nextIndex: index + 1 < products.length ? index + 1 : null,
        status: index + 1 < products.length ? "PARTIAL" : "CAPTURE_COMPLETE_PENDING_POSTPROCESS",
        reason: null,
      });
      results.push({ ordinal: index + 1, product_id: id, status: "captured" });
    } catch (error) {
      const code = String(error?.message || error).slice(0, 160);
      const failure = {
        schema_version: "1.1.0",
        run_id: runId,
        product_id: id,
        ordinal: index + 1,
        status: "failed",
        captured_at: new Date().toISOString(),
        error_code: code,
      };
      await writeFailureReceipt(target, existing, failure);
      await appendAttempt(target, failure);
      await writeBrowserCheckpoint(runRoot, {
        runId,
        products,
        lastIndex: index,
        nextIndex: index,
        status: "PARTIAL",
        reason: code,
      });
      results.push({ ordinal: index + 1, product_id: id, status: "failed", error: code });
    }
  }
  return {
    start_ordinal: startIndex + 1,
    end_ordinal: endIndex,
    stopped: false,
    stop_reason: null,
    next_zero_based_index: endIndex < products.length ? endIndex : null,
    results,
  };
}
