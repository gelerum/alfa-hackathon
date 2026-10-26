const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map();
const element = () => ({hidden: false, value: '', checked: false, innerHTML: '', textContent: '', listeners: {},
  addEventListener(type, handler) {this.listeners[type] = handler;}, scrollIntoView() {}});
const $ = selector => {if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector);};
const calls = [];
const sandbox = vm.createContext({document: {querySelector: $}, console, URLSearchParams, Date, Math, Number, String, JSON,
  setTimeout() {return 1;}, clearTimeout() {}, sessionStorage: {getItem() {return null;}},
  async fetch(url, options) {
    calls.push({url, options});
    assert.equal(url, '/api/dev/overview');
    return {ok: true, status: 200, async json() {return {
      mode: 'demo', services: [{name: 'postgres', status: 'ok'}, {name: 'llm', status: 'configured'}],
      workers: [{role: 'profiler', alive: true, running: 1, processed: 3, failed: 0}],
      queues: [{kind: 'profile', ready: 1, running: 1, avg_run_s: 20}],
    };}};
  },
});
(async () => {
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'dev.js'), 'utf8'), sandbox);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options, undefined);
  assert.match($('#services').innerHTML, /База данных[\s\S]*Работает/);
  assert.match($('#services').innerHTML, /готовность проверяется при запросе/);
  assert.match($('#workers').innerHTML, /Профили компаний/);
  assert.equal($('#runSection').hidden, true);
  assert.equal($('#failureSection').hidden, true);
  assert.equal($('#error').hidden, true);
  console.log('Passwordless demo overview and private diagnostics separation: OK');
})().catch(error => {console.error(error); process.exit(1);});
