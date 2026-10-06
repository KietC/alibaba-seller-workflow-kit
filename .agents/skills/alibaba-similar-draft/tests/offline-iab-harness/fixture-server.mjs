import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PAGE = await readFile(join(ROOT, 'publish-replica.html'));
const LOCALES = JSON.parse(await readFile(join(ROOT, 'fixtures', 'locales.json'), 'utf8'));

function json(response, status, value) {
  const payload = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {'content-type':'application/json; charset=utf-8', 'content-length':payload.length, 'cache-control':'no-store'});
  response.end(payload);
}

async function body(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks);
}

export async function startFixtureServer() {
  const state = {
    nextDraftId: 9900000000001n,
    drafts: new Map(),
    onlineIds: ['8800000000001'], pendingIds: [],
    received: {draft:0, final:0, unknown:0},
  };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (request.method === 'GET' && url.pathname === '/product/publish.htm') {
      response.writeHead(200, {'content-type':'text/html; charset=utf-8', 'content-length':PAGE.length, 'cache-control':'no-store'});
      response.end(PAGE); return;
    }
    if (request.method === 'GET' && url.pathname === '/api/init') {
      const referer = new URL(request.headers.referer || 'http://127.0.0.1/product/publish.htm');
      const draftId = referer.searchParams.get('itemId');
      const draft = draftId ? state.drafts.get(draftId) : null;
      json(response, 200, {locales:LOCALES, values:draft?.titles || {}, draftId:draft ? draftId : null}); return;
    }
    if (request.method === 'GET' && url.pathname === '/api/inventory') {
      json(response, 200, {
        page:1, totalPages:1, hasNext:false, nextCursor:null,
        onlineIds:[...state.onlineIds], pendingIds:[...state.pendingIds], draftIds:[...state.drafts.keys()],
      }); return;
    }
    if (request.method === 'POST' && url.pathname === '/product/submitDraft.json' && !url.search) {
      state.received.draft += 1;
      let parsed;
      try { parsed = JSON.parse((await body(request)).toString('utf8')); }
      catch { json(response, 400, {success:false,error:'INVALID_JSON'}); return; }
      const locales = new Set(LOCALES.map(row => row.locale));
      if (!parsed || typeof parsed.titles !== 'object' || Object.keys(parsed.titles).length !== locales.size || Object.keys(parsed.titles).some(key => !locales.has(key))) {
        json(response, 422, {success:false,error:'INCOMPLETE_LOCALE_SET'}); return;
      }
      const draftId = String(state.nextDraftId++);
      state.drafts.set(draftId, {titles:structuredClone(parsed.titles)});
      json(response, 200, {success:true,businessStatus:'SUCCESS',draftId}); return;
    }
    if (url.pathname === '/product/submitFinal.json' || url.pathname === '/product/submit.htm') {
      state.received.final += 1; json(response, 500, {success:false,error:'FINAL_TRAP_REACHED'}); return;
    }
    state.received.unknown += 1;
    json(response, 404, {success:false,error:'UNKNOWN_TRAP_REACHED'});
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  const origin = `http://127.0.0.1:${address.port}`;
  return {
    origin, state,
    snapshot() { return {draftIds:[...state.drafts.keys()], onlineIds:[...state.onlineIds], pendingIds:[...state.pendingIds], received:{...state.received}}; },
    async close() { await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())); },
  };
}
