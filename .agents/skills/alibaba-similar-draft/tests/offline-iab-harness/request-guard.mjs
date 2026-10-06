import { createHash, randomBytes } from 'node:crypto';

const canonicalize = value => {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonicalize(value[key])]));
  }
  return value;
};
const sha256 = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(canonicalize(value))).digest('hex');

export class SyntheticRequestGuard {
  constructor(page, origin) {
    this.page = page; this.origin = new URL(origin);
    this.installed = false; this.armed = null; this.pending = null;
    this.ledgerCount = 0; this.checkpoint = 'PRE_SAVE_VALIDATED';
    this.sequence = 0; this.log = []; this.releasedInstances = new Set();
  }

  classify(method, url) {
    const target = new URL(url);
    if (target.origin !== this.origin.origin) return 'UNKNOWN_DENY';
    if (target.pathname === '/product/submitFinal.json' || target.pathname === '/product/submit.htm') return 'FINAL_HARD_DENY';
    if (method === 'POST' && target.pathname === '/product/submitDraft.json' && !target.search && !target.hash) return 'DRAFT_PAUSE';
    if (method !== 'GET') return 'UNKNOWN_DENY';
    if (target.pathname === '/api/init' && !target.search) return 'READ_ALLOW';
    if (target.pathname === '/api/inventory' && !target.search) return 'READ_ALLOW';
    if (target.pathname === '/product/publish.htm') {
      const keys = [...target.searchParams.keys()].sort().join(',');
      const initial = keys === 'behavior,itemId,pubType' && target.searchParams.get('pubType') === 'similarPost' && target.searchParams.get('behavior') === 'copyNew' && /^\d+$/.test(target.searchParams.get('itemId') || '');
      const reopen = keys === 'itemId,pubAction' && target.searchParams.get('pubAction') === 'draft' && /^\d+$/.test(target.searchParams.get('itemId') || '');
      if (initial || reopen) return 'READ_ALLOW';
    }
    return 'UNKNOWN_DENY';
  }

  async installBeforeNavigation() {
    if (this.installed) throw new Error('guard already installed');
    await this.page.route('**/*', async route => {
      const request = route.request(); const method = request.method(); const url = request.url();
      const decision = this.classify(method, url); const sequence = ++this.sequence;
      if (decision === 'READ_ALLOW') { this.log.push({sequence,method,url,decision}); await route.continue(); return; }
      if (decision === 'FINAL_HARD_DENY' || decision === 'UNKNOWN_DENY') {
        this.log.push({sequence,method,url,decision}); await route.abort('blockedbyclient'); return;
      }
      if (!this.armed || this.armed.used) {
        this.log.push({sequence,method,url,decision:'DIRECT_REPLAY_DENY'}); await route.abort('blockedbyclient'); return;
      }
      this.armed.used = true;
      const contentType = String(request.headers()['content-type'] || '').toLowerCase().split(';',1)[0].trim();
      const rawBody = request.postData();
      let parsedBody = null;
      try { parsedBody = JSON.parse(rawBody || ''); } catch {}
      const bodySha256 = parsedBody === null ? null : sha256(parsedBody);
      if (contentType !== 'application/json' || bodySha256 !== this.armed.expectedBodySha256) {
        this.log.push({sequence,method,url,decision:'DRAFT_BODY_DENY',contentType,bodySha256});
        await route.abort('blockedbyclient'); return;
      }
      const requestInstanceSha256 = sha256({sequence,method,url,postData:request.postData(),eventNonce:this.armed.nonce});
      this.log.push({sequence,method,url,decision:'DRAFT_PAUSED',requestInstanceSha256,bodySha256});
      let resolve;
      const released = new Promise(next => { resolve = next; });
      this.pending = {route, requestInstanceSha256, sequence, resolve, released};
      await released;
      if (!this.pending?.approved || this.pending.requestInstanceSha256 !== requestInstanceSha256) {
        await route.abort('blockedbyclient'); return;
      }
      this.releasedInstances.add(requestInstanceSha256);
      this.log.push({sequence,method,url,decision:'EXACT_DRAFT_RELEASED_ONCE',requestInstanceSha256});
      await route.continue();
    });
    this.installed = true;
  }

  async armFromVisibleSaveButton(locator, expectedDraftBody) {
    if (!this.installed || !await locator.isVisible() || !await locator.isEnabled() || await locator.count() !== 1) throw new Error('save action is not one unique visible enabled button');
    if (!expectedDraftBody || typeof expectedDraftBody !== 'object' || Array.isArray(expectedDraftBody)) throw new Error('exact expected draft body is required');
    this.armed = {
      nonce:randomBytes(32).toString('hex'), used:false, label:await locator.innerText(),
      expectedBodySha256:sha256(expectedDraftBody),
    };
    return sha256(this.armed);
  }

  async waitForPaused(timeoutMs = 5000) {
    const started = Date.now();
    while (!this.pending && Date.now() - started < timeoutMs) await new Promise(resolve => setTimeout(resolve, 5));
    if (!this.pending) throw new Error('draft request was not paused');
    return this.pending.requestInstanceSha256;
  }

  burnLedgerZeroToOne() {
    if (this.ledgerCount !== 0) throw new Error('draft-save ledger is already burned; retry forbidden');
    this.ledgerCount = 1; return {before:0,after:1,maximum:1,retryAllowed:false};
  }

  markCheckpointSaveAttempted() {
    if (this.ledgerCount !== 1 || this.checkpoint !== 'PRE_SAVE_VALIDATED') throw new Error('invalid checkpoint transition');
    this.checkpoint = 'SAVE_ATTEMPTED';
  }

  releaseExactPausedRequest(instanceSha256) {
    if (!this.pending || this.pending.requestInstanceSha256 !== instanceSha256) throw new Error('paused request instance mismatch');
    if (this.ledgerCount !== 1 || this.checkpoint !== 'SAVE_ATTEMPTED') throw new Error('release requires burned ledger and SAVE_ATTEMPTED checkpoint');
    if (this.releasedInstances.has(instanceSha256) || this.pending.approved) throw new Error('request instance already released');
    this.pending.approved = true; this.pending.resolve();
  }

  assertNoFinalReachedServer(serverSnapshot) {
    if (serverSnapshot.received.final !== 0) throw new Error('final-write trap reached the server');
  }
}
