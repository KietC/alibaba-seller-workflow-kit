import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { startFixtureServer } from './fixture-server.mjs';
import { SyntheticLocalizedAdapter } from './runtime-adapter.mjs';
import { SyntheticRequestGuard } from './request-guard.mjs';

const ROOT = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const playwrightEntry = process.env.SELLERKIT_PLAYWRIGHT_ENTRY || 'playwright';
const {chromium} = require(playwrightEntry);
const locales = JSON.parse(await readFile(join(ROOT, 'fixtures', 'locales.json'), 'utf8'));

function exactSet(actual, expected, label) {
  assert.deepEqual([...new Set(actual)].sort(), [...new Set(expected)].sort(), label);
}

const receipt = {
  schema_version:'sellerkit.alibaba.offline-iab-harness-receipt.v1',
  trust_status:'LOCAL_CONTRACT_VERIFIED',
  execution_authority:'NONE',
  environment:'SYNTHETIC_TEST_LOOPBACK_ONLY',
  browser_live_attested:false,
  alibaba_live_executed:false,
  implementation_ready:false,
  final_write_enabled:false,
  state:'BOOT',
  checks:[],
};

let server; let browser;
try {
  server = await startFixtureServer();
  const executablePath = process.env.SELLERKIT_CHROMIUM_EXECUTABLE || chromium.executablePath();
  browser = await chromium.launch({headless:true,executablePath,args:[
    '--disable-background-networking','--disable-component-update','--disable-default-apps',
    '--disable-domain-reliability','--disable-sync','--metrics-recording-only','--no-first-run',
  ]});
  const context = await browser.newContext({serviceWorkers:'block'});
  const expectedDraftBody = {titles:Object.fromEntries(locales.map(row => [row.locale,row.title]))};

  // Run the exact-query and body-semantics negatives on an isolated page so a
  // failed one-shot event can never be reused by the positive draft canary.
  const negativePage = await context.newPage();
  const negativeGuard = new SyntheticRequestGuard(negativePage, server.origin);
  await negativeGuard.installBeforeNavigation();
  await negativePage.goto(`${server.origin}/product/publish.htm?pubType=similarPost&behavior=copyNew&itemId=100000000014`, {waitUntil:'networkidle'});
  const unexpectedQuery = await negativePage.evaluate(async () => {
    try { await fetch('/product/submitDraft.json?unexpected=1',{method:'POST',headers:{'content-type':'application/json'},body:'{}'}); return 'UNEXPECTED'; }
    catch { return 'DENIED'; }
  });
  assert.equal(unexpectedQuery,'DENIED');
  await negativeGuard.armFromVisibleSaveButton(
    negativePage.getByRole('button',{name:'Save Draft',exact:true}), expectedDraftBody,
  );
  const wrongBody = await negativePage.evaluate(async () => {
    try { await fetch('/product/submitDraft.json',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({titles:{en:'wrong'}})}); return 'UNEXPECTED'; }
    catch { return 'DENIED'; }
  });
  assert.equal(wrongBody,'DENIED');
  assert(negativeGuard.log.some(row => row.decision === 'UNKNOWN_DENY'));
  assert(negativeGuard.log.some(row => row.decision === 'DRAFT_BODY_DENY'));
  assert.equal(server.snapshot().received.draft,0);
  await negativePage.close();
  receipt.checks.push({id:'DRAFT_EXACT_QUERY_AND_BODY_NEGATIVES',status:'PASS'});

  const page = await context.newPage();
  const guard = new SyntheticRequestGuard(page, server.origin);
  await guard.installBeforeNavigation(); receipt.state = 'GUARD_INSTALLED';

  await page.goto(`${server.origin}/product/publish.htm?pubType=similarPost&behavior=copyNew&itemId=100000000014`, {waitUntil:'networkidle'});
  receipt.state = 'PAGE_LOADED';
  assert.equal(await page.locator('#trust').innerText(), 'SYNTHETIC_TEST / LOCAL_CONTRACT_ONLY');
  await page.getByRole('button',{name:'Multilingual settings',exact:true}).click();
  const adapter = new SyntheticLocalizedAdapter(page, locales);
  const visibleLocales = await adapter.enumerateLocales();
  exactSet(visibleLocales.map(row => row.locale), locales.map(row => row.locale), '18 locale set');
  exactSet(locales.filter(row => row.direction === 'RTL').map(row => row.locale), ['ar','he'], 'RTL locale set');
  receipt.checks.push({id:'LOCALES_18_EXACT',status:'PASS',count:visibleLocales.length});
  receipt.state = 'LOCALES_ENUMERATED';

  const fillEvidence = [];
  for (const item of locales) fillEvidence.push(await adapter.fillLocale(item));
  assert.equal(fillEvidence.length, 18);
  assert.equal(new Set(fillEvidence.map(row => row.valueSha256)).size, 18, 'localized titles must not be identical copies');
  assert(new Set(fillEvidence.map(row => row.maximum)).size >= 3, 'synthetic live maxima must vary by locale');
  assert(fillEvidence.some(row => row.observedCount !== Array.from(row.value).length), 'PLATFORM_COUNTER must not be silently treated as Unicode code-point length');
  assert(fillEvidence.every(row => row.postWriteCounterReadback && row.observedCount <= row.maximum));
  receipt.checks.push({id:'LOCALIZED_DYNAMIC_DOM_RTL_COUNTER',status:'PASS',count:fillEvidence.length});
  receipt.state = 'FORM_FILLED';

  const before = await page.evaluate(async () => (await (await fetch('/api/inventory')).json()));
  assert.equal(before.draftIds.length, 0); assert.equal(before.hasNext, false); assert.equal(before.totalPages, 1);
  receipt.state = 'PRE_SAVE_VERIFIED';
  const saveButton = page.getByRole('button',{name:'Save Draft',exact:true});
  const visibleEventSha256 = await guard.armFromVisibleSaveButton(saveButton, expectedDraftBody);
  await saveButton.click();
  const pausedRequestInstanceSha256 = await guard.waitForPaused();
  receipt.state = 'SAVE_REQUEST_PAUSED';
  const ledger = guard.burnLedgerZeroToOne();
  assert.deepEqual(ledger,{before:0,after:1,maximum:1,retryAllowed:false});
  receipt.state = 'LEDGER_BURNED';
  guard.markCheckpointSaveAttempted(); receipt.state = 'CHECKPOINT_SAVE_ATTEMPTED';
  guard.releaseExactPausedRequest(pausedRequestInstanceSha256); receipt.state = 'EXACT_REQUEST_RELEASED_ONCE';
  await page.getByText(/^Draft saved \d+$/).waitFor();
  const status = await page.locator('#status').innerText();
  const draftId = status.match(/(\d+)$/)?.[1]; assert.match(draftId, /^\d+$/);
  receipt.state = 'RESPONSE_VERIFIED';

  const replay = await page.evaluate(async payload => {
    try { await fetch('/product/submitDraft.json',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)}); return 'UNEXPECTED'; }
    catch { return 'DENIED'; }
  }, {titles:Object.fromEntries(locales.map(row => [row.locale,row.title]))});
  assert.equal(replay,'DENIED');
  assert.throws(() => guard.burnLedgerZeroToOne(), /already burned/);
  assert.throws(() => guard.releaseExactPausedRequest(pausedRequestInstanceSha256), /already released/);

  const after = await page.evaluate(async () => (await (await fetch('/api/inventory')).json()));
  assert.deepEqual(after.onlineIds,before.onlineIds); assert.deepEqual(after.pendingIds,before.pendingIds);
  assert.deepEqual(after.draftIds,[draftId]);
  receipt.state = 'INVENTORY_DELTA_VERIFIED';

  await page.goto(`${server.origin}/product/publish.htm?itemId=${draftId}&pubAction=draft`,{waitUntil:'networkidle'});
  await page.getByText(`Draft reopened ${draftId}`,{exact:true}).waitFor();
  await page.getByRole('button',{name:'Multilingual settings',exact:true}).click();
  const reopenedAdapter = new SyntheticLocalizedAdapter(page, locales);
  await reopenedAdapter.enumerateLocales();
  const reopened = [];
  for (const item of locales) reopened.push(await reopenedAdapter.verifyLocaleReadback(item));
  assert.equal(reopened.length,18); receipt.state = 'PERSISTENCE_VERIFIED';

  await page.evaluate(() => {
    const original = document.querySelector('#language-list button[data-locale="en"]');
    const duplicate = original.cloneNode(true); duplicate.id = 'synthetic-duplicate-locale';
    original.parentElement.appendChild(duplicate);
  });
  await assert.rejects(() => reopenedAdapter.enumerateLocales(), /ambiguous/);
  await page.locator('#synthetic-duplicate-locale').evaluate(node => node.remove());
  await page.locator('#editor-host [role="status"]').evaluate(node => { node.textContent = '999 / 128'; });
  const activeLocale = await page.locator('#editor-host section').getAttribute('data-locale');
  const active = locales.find(row => row.locale === activeLocale);
  await assert.rejects(() => reopenedAdapter.verifyLocaleReadback(active,{select:false}), /counter mismatch/);
  receipt.checks.push({id:'DOM_DRIFT_AND_COUNTER_NEGATIVES',status:'PASS'});

  await page.getByRole('button',{name:'Submit Product',exact:true}).click();
  await page.getByText('Final write hard denied',{exact:true}).waitFor();
  const unknown = await page.evaluate(async () => { try { await fetch('/unknown/trap'); return 'UNEXPECTED'; } catch { return 'DENIED'; } });
  assert.equal(unknown,'DENIED');
  const external = await page.evaluate(async () => { try { await fetch('https://example.com/egress-trap'); return 'UNEXPECTED'; } catch { return 'DENIED'; } });
  assert.equal(external,'DENIED');
  const serverSnapshot = server.snapshot();
  assert.equal(serverSnapshot.received.draft,1); assert.equal(serverSnapshot.received.final,0); assert.equal(serverSnapshot.received.unknown,0);
  guard.assertNoFinalReachedServer(serverSnapshot);
  assert(guard.log.some(row => row.decision === 'FINAL_HARD_DENY'));
  assert(guard.log.some(row => row.decision === 'UNKNOWN_DENY'));
  assert(guard.log.some(row => row.decision === 'DIRECT_REPLAY_DENY'));
  assert.equal(guard.log.filter(row => row.decision === 'EXACT_DRAFT_RELEASED_ONCE').length,1);
  receipt.checks.push({id:'REQUEST_GUARD_DEFAULT_DENY_FINAL_HARD_DENY',status:'PASS'});
  receipt.checks.push({id:'DRAFT_ONE_SHOT_ZERO_TO_ONE',status:'PASS',visibleEventSha256,pausedRequestInstanceSha256,draftId});
  receipt.checks.push({id:'INVENTORY_AND_REOPEN',status:'PASS',draftId});
  receipt.state = 'FINAL_REVIEW_READY_LOCAL_CONTRACT';
  receipt.draft_id = draftId;
  receipt.request_counts = {
    allowedReads:guard.log.filter(row => row.decision === 'READ_ALLOW').length,
    draftPaused:guard.log.filter(row => row.decision === 'DRAFT_PAUSED').length,
    draftReleased:guard.log.filter(row => row.decision === 'EXACT_DRAFT_RELEASED_ONCE').length,
    finalDenied:guard.log.filter(row => row.decision === 'FINAL_HARD_DENY').length,
    unknownDenied:guard.log.filter(row => row.decision === 'UNKNOWN_DENY').length,
    replayDenied:guard.log.filter(row => row.decision === 'DIRECT_REPLAY_DENY').length,
    serverDraftWrites:serverSnapshot.received.draft,
    serverFinalWrites:serverSnapshot.received.final,
  };
  const outputDir = resolve(process.env.SELLERKIT_HARNESS_OUTPUT || join(ROOT,'out'));
  await mkdir(outputDir,{recursive:true});
  const outputPath = join(outputDir,'offline_iab_harness_receipt.json');
  await writeFile(outputPath,JSON.stringify(receipt,null,2)+'\n',{encoding:'utf8',flag:'wx'});
  console.log(JSON.stringify({...receipt,receipt_path:outputPath}));
} catch (error) {
  receipt.state = `BLOCKED_${receipt.state}`;
  receipt.trust_status = 'BLOCKED_LOCAL_CONTRACT';
  receipt.error = error?.stack || String(error);
  console.error(JSON.stringify(receipt));
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  if (server) await server.close();
}
