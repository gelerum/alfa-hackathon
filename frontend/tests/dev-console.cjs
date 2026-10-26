// No browser/server needed: developer console renders the observability snapshot and agent trace.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const nodes = new Map();
const element = () => ({ hidden: false, value: '', checked: true, innerHTML: '', textContent: '', listeners: {},
  addEventListener(type, handler) { this.listeners[type] = handler; }, scrollIntoView() {} });
const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
const calls = [];
const snapshot = {
  services: [{name: 'postgres', status: 'ok', url: ''}, {name: 'llm', status: 'not_configured', detail: 'нет ключа'}],
  workers: {missing_roles: ['insight'], items: [{worker_id: 'profiler:h:1', role: 'profiler', alive: true, last_seen: new Date().toISOString(), uptime_s: 30, running_jobs: [{kind: 'profile'}], processed: 2, failed: 1}]},
  queues: [{kind: 'profile', ready: 0, retry_scheduled: 1, running: 1, failed_24h: 0, completed_1h: 1, oldest_pending_s: 400, avg_wait_s: 1.5, avg_run_s: 30, p95_run_s: 60}],
  failures: [{kind: 'profile', status: 'pending', attempts: 1, max_attempts: 5, company_id: 'U_1', last_error: '<Configure LLM>'}],
  agents: [{agent_name: 'company_profiler', runs: 1, tokens: 1200}], tools: [],
  data: {news: {total: 3, fetched_24h: 1, published_7d: 2, duplicates: 0, revisions: 3, embedded_revisions: 3, last_fetch: null, sources: []},
    documents: [], rag_chunks: {total: 0, without_embedding: 0}, profiles: {company_profiles: 1, user_contexts: 1, failed_profiles: 0},
    ranking: [], reports: {reports_7d: 0, published_7d: 0, deadline_bulletins: 0}},
};
const storage = new Map([['alpha_admin_token', 'admin']]);
const sandbox = vm.createContext({
  document: {querySelector: $}, console, URLSearchParams, Date, Math, Number, String, JSON,
  setTimeout() { return 1; }, clearTimeout() {},
  localStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
  async fetch(url, options) {
    calls.push({url, options});
    let data;
    if (url === '/api/admin/observability') data = snapshot;
    else if (url.startsWith('/api/admin/agent-runs?')) data = [{agent_run_id: 'run-1', agent_name: 'company_profiler', status: 'failed', steps: 2}];
    else if (url === '/api/admin/agent-runs/run-1') data = {run: {agent_run_id: 'run-1', agent_name: 'company_profiler', status: 'failed', input_snapshot: {}},
      steps: [{step_no: 1, step_kind: 'tool_call', tool_name: 'company_bank_data.get', status: 'ok', arguments: {}}], tool_audit: []};
    else throw new Error('Unexpected ' + url);
    return {ok: true, status: 200, async json() { return data; }};
  },
});
(async () => {
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'dev.js'), 'utf8'), sandbox);
  await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[0].options.headers.Authorization, 'Bearer admin');
  assert.match($('#services').innerHTML, /badge not_configured[\s\S]*нет ключа/);
  assert.equal($('#missingWorkers').hidden, false);
  assert.match($('#workers').innerHTML, /profiler:h:1[\s\S]*profile/);
  assert.match($('#queues').innerHTML, /7 мин/);
  assert.match($('#failures').innerHTML, /&lt;Configure LLM&gt;/);
  assert.match($('#runs').innerHTML, /data-run="run-1"/);
  await $('#runs').listeners.click({target: {closest: () => ({dataset: {run: 'run-1'}})}});
  await new Promise(resolve => setImmediate(resolve));
  assert.match($('#trace').innerHTML, /Трасса company_profiler[\s\S]*company_bank_data\.get/);
  console.log('Developer console snapshot, workers, failures and agent trace: OK');
})().catch(error => { console.error(error); process.exit(1); });
