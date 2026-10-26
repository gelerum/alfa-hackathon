// No browser/server needed: exercise automatic login wiring and HTTP helper.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return { style: {}, dataset: {}, classList: { add() {}, remove() {} },
    addEventListener() {}, appendChild() {}, innerHTML: '', textContent: '' };
}
const nodes = new Map();
const document = {
  querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  },
  querySelectorAll() { return []; },
  createElement() { return element(); },
};
const calls = [];
let demoFailure = null;
const storage = new Map();
const context = vm.createContext({
  document, FormData, console,
  setInterval() { return 1; }, clearInterval() {}, setTimeout() { return 1; }, clearTimeout() {},
  localStorage: { getItem(k) { return storage.get(k); }, setItem(k, v) { storage.set(k, v); } },
  async fetch(url, options) {
    calls.push({ url, options });
    if (url === '/api/demo/session' && demoFailure) return {ok:false,status:demoFailure,json:async()=>({detail:'unavailable'})};
    let data;
    let status = 200;
    if (url === '/api/demo/session') data = { token: 'demo-token', display_name: 'Демо-пользователь' };
    else if (url === '/api/companies') {
      assert.equal(options.headers.Authorization, 'Bearer demo-token');
      data = [{ company_id: 'test-company', legal_name: 'Демо' }];
    } else if (url.endsWith('/reports')) data = [];
    else if (url.endsWith('/context')) data = { status: 'ready' };
    else if (url.endsWith('/documents')) data = [];
    else if (url.endsWith('/pipeline')) data = { stages: [], busy: false, profile: null, user_context: null };
    else if (url === '/api/accepted') { data = { job_id: 'test-job' }; status = 202; }
    else if (url === '/api/upload') data = { document_id: 'test-document' };
    else throw new Error('Unexpected URL: ' + url);
    return { ok: true, status, async json() { return data; } };
  },
});

(async () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  await vm.runInContext(source, context);
  assert.equal(vm.runInContext('state.activeCompanyId', context), 'test-company');
  assert.equal(storage.get('alpha_token'), 'demo-token');
  assert.equal(nodes.get('#generateBtn').disabled, false);
  assert.ok(!nodes.get('#notice')?.textContent);
  assert.equal(nodes.get('#logoutBtn').hidden, true);
  assert.equal(nodes.get('#loginForm').hidden, true);
  assert.equal(nodes.get('#retryConnectionBtn').hidden, true);
  assert.match(nodes.get('#userName').textContent, /Демо-пользователь/);
  assert.equal(calls[0].url, '/api/demo/session');
  assert.equal(calls[0].options.method, 'POST');
  const accepted = await vm.runInContext('api("/accepted", {method:"POST"})', context);
  assert.equal(accepted.job_id, 'test-job');
  await vm.runInContext('api("/upload", {method:"POST", body:new FormData()})', context);
  assert.equal(calls.at(-1).options.headers['Content-Type'], undefined);
  assert.ok(calls.every(call => !call.url.includes('//')));
  demoFailure = 503;
  await vm.runInContext('initialize()', context);
  assert.equal(nodes.get('#loginForm').hidden, true);
  assert.equal(nodes.get('#retryConnectionBtn').hidden, false);
  assert.equal(nodes.get('#retryConnectionBtn').disabled, false);
  demoFailure = 404;
  await vm.runInContext('initialize()', context);
  assert.equal(nodes.get('#loginForm').hidden, false);
  assert.equal(nodes.get('#retryConnectionBtn').hidden, true);
  demoFailure = null;
  await vm.runInContext('initialize()', context);
  assert.equal(nodes.get('#loginForm').hidden, true);
  assert.equal(nodes.get('#retryConnectionBtn').hidden, true);
  console.log('Automatic demo login without token flash, retry, manual fallback, API paths and multipart: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
