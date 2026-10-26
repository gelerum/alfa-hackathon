// No browser/server needed: demo company choice, editable prefill and KPI submission.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  const listeners = {};
  return { style: {}, dataset: {}, classList: { add() {}, remove() {} }, listeners,
    addEventListener(type, handler) { listeners[type] = handler; }, appendChild() {}, reset() {},
    innerHTML: '', textContent: '', value: '' };
}
const nodes = new Map();
const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
const document = { querySelector: $, querySelectorAll() { return []; }, createElement: element };
const storage = new Map();
const posted = [];
const fish = {role: 'Финансовый директор', responsibilities: ['Ликвидность'], goals: ['Снизить ставку'],
  kpis: ['Оборачиваемость запасов, дней', 'Валовая маржа, %'], note: 'Закупки в Чили'};
let context = {status: 'needs_onboarding'};
let pipeline = {busy: false, profile: null, user_context: null, stages: [
  {key: 'focus', title: 'Ваш фокус', state: 'idle', detail: 'Не заполнен'}]};
const sandbox = vm.createContext({
  document, FormData, console, crypto: require("node:crypto").webcrypto,
  setTimeout() { return 1; }, clearTimeout() {},
  localStorage: { getItem(k) { return storage.get(k); }, setItem(k, v) { storage.set(k, v); } },
  async fetch(url, options) {
    let data;
    if (url === '/api/demo/session') data = {token: 't', company_id: 'U_FISH'};
    else if (url === '/api/companies') data = [
      {company_id: 'U_AUTO', legal_name: 'Автокомпонент Опт', registration_city: 'г. Москва', suggested_answers: {role: 'CFO'}},
      {company_id: 'U_FISH', legal_name: 'Нордаква Импорт', registration_city: 'г. Санкт-Петербург',
        main_okved_name: 'Торговля рыбой', revenue_value: '54767618087', suggested_answers: fish},
    ];
    else if (url.endsWith('/onboarding')) { posted.push(JSON.parse(options.body)); data = {job_id: 'j'}; }
    else if (url.endsWith('/context')) data = context;
    else if (url.endsWith('/reports') || url.endsWith('/documents')) data = [];
    else if (url.endsWith('/pipeline')) data = pipeline;
    else throw new Error('Unexpected URL: ' + url);
    return { ok: true, status: 200, async json() { return data; } };
  },
});

(async () => {
  await vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8'), sandbox);
  assert.equal(vm.runInContext('state.activeCompanyId', sandbox), 'U_FISH');
  assert.match($('#companySelect').innerHTML, /Нордаква Импорт · Санкт-Петербург/);
  assert.match($('#dashCompanyDetails').textContent, /^Торговля рыбой · г. Санкт-Петербург · выручка 54,8 млрд$/);

  vm.runInContext('settingsView()', sandbox);
  assert.equal($('#obRole').value, 'Финансовый директор');
  assert.equal($('#obKpis').value, 'Оборачиваемость запасов, дней\nВаловая маржа, %');
  assert.equal($('#obNote').value, 'Закупки в Чили');
  assert.equal($('#obPrefillHint').hidden, false);

  $('#obKpis').value = 'Валовая маржа, %\nДоля просрочки, %';
  await $('#onboardingForm').listeners.submit({preventDefault() {}});
  assert.deepEqual(posted.at(-1).kpis, ['Валовая маржа, %', 'Доля просрочки, %']);
  assert.equal(posted.at(-1).role, 'Финансовый директор');

  $('#obKpis').value = 'a\nb\nc\nd';
  await $('#onboardingForm').listeners.submit({preventDefault() {}});
  assert.equal(posted.length, 1);
  assert.match($('#obSubmitStatus').textContent, /Не более 3 строк/);

  assert.equal($('#onboardingPrompt').hidden, false);
  pipeline = {busy: true, user_context: null, stages: [
    {key: 'focus', title: 'Ваш фокус', state: 'done', detail: 'Сохранён: CFO', at: '2026-10-26T07:00:00Z'},
    {key: 'profile', title: 'Профиль компании', state: 'active', detail: 'Агент собирает профиль',
      steps: [{text: 'Читает банковские данные компании (CSV)', status: 'ok'}, {text: 'Проверка <b>', status: 'error'}]}],
    profile: {version: 3, published_at: '2026-10-26T07:05:00Z', summary: 'Импортёр рыбы', missing_data: ['Валюта выручки'],
      conflicts: [], facts: [
        {field: 'main_okved_name', value: 'Торговля рыбой', source_type: 'bank_import'},
        {field: 'main_okved_code', value: '46.38', source_type: 'bank_import'},
        {field: 'registration_city', value: 'г. Санкт-Петербург', source_type: 'bank_import'},
        {field: 'registration_region', value: 'Санкт-Петербург', source_type: 'bank_import'},
        {field: 'revenue_value', value: '54767618087.0', source_type: 'bank_import'},
        {field: 'employees_fns_band', value: '101-250', source_type: 'bank_import'},
        {field: 'cl_type', value: 'CLIENT', source_type: 'bank_import'},
        {field: 'loan_mark', value: 'true', source_type: 'bank_import'},
        {field: 'leasing_mark', value: 'true', source_type: 'bank_import'},
        {field: 'factoring_mark', value: 'false', source_type: 'bank_import'},
        {field: 'fts_country_name', value: 'ПЕРУ, ЧИЛИ, ЮЖНАЯ КОРЕЯ, ОАЭ', source_type: 'bank_import'},
        {field: 'group_id', value: 'group_1_sb', source_type: 'bank_import'},
        {field: 'corporate_group', value: '{"source_snapshot_id": "x", "group_id": "g", "companies": 3}', source_type: 'derived'},
        {field: 'payments_incoming', value: '{"direction": "incoming", "edges": 1244, "amount": "3511832000.0", "payments": "81164"}', source_type: 'derived'},
        {field: 'supplier_terms', value: 'Отсрочка 60 дней', source_type: 'document'}],
      facets: [{facet_type: 'foreign_trade', text: 'Импорт из Чили', keywords: ['лосось']}]}};
  await vm.runInContext('loadDashboard()', sandbox);
  assert.equal($('#onboardingPrompt').hidden, true);
  assert.match($('#contextStatus').textContent, /Фокус сохранён/);
  assert.match($('#pipelineStages').innerHTML, /stage active[\s\S]*Читает банковские данные компании \(CSV\)/);
  assert.match($('#pipelineStages').innerHTML, /class="error">Проверка &lt;b&gt;/);
  const card = $('#fixedProfile').innerHTML;
  assert.match(card, /Профиль компании<small>✓ Обновлён/);
  assert.doesNotMatch(card, /v3|group_1_sb|source_snapshot_id|CLIENT|_mark|okved|\{/);
  assert.match(card, /Торговля рыбой \(ОКВЭД 46\.38\)/);
  assert.match(card, /Регистрация<\/span><div>Санкт-Петербург</);
  assert.match(card, /Выручка<\/span><div>54,8 млрд \(валюта или период не указаны\)/);
  assert.match(card, /101–250 человек/);
  assert.match(card, /действующий клиент/);
  assert.match(card, /Продукты<\/span><div>кредит, лизинг</);
  assert.match(card, /Перу, Чили, Южная Корея, ОАЭ/);
  assert.match(card, /3 компании в группе/);
  assert.match(card, /3,5 млрд от 1\s244 контрагентов, 81\s164 операций/);
  assert.match(card, /Условия поставщиков<\/span><div>Отсрочка 60 дней/);
  assert.match(card, /ВЭД:<\/b> Импорт из Чили[\s\S]*лосось[\s\S]*Валюта выручки/);
  const savedRandom = sandbox.crypto.randomUUID;
  sandbox.crypto = {getRandomValues: require('node:crypto').webcrypto.getRandomValues.bind(require('node:crypto').webcrypto)};
  assert.match(vm.runInContext('requestId()', sandbox), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  sandbox.crypto = require('node:crypto').webcrypto; void savedRandom;
  context = {status: 'ready', user_context: {raw_answers: {role: 'Казначей', kpis: ['Ликвидность']}}};
  await vm.runInContext('loadDashboard()', sandbox);
  vm.runInContext('settingsView()', sandbox);
  assert.equal($('#obRole').value, 'Казначей');
  assert.equal($('#obKpis').value, 'Ликвидность');
  assert.equal($('#obPrefillHint').hidden, true);

  storage.set('alpha_company', 'U_AUTO');
  await vm.runInContext("useCompanies(state.companies, true, 'U_FISH')", sandbox);
  assert.equal(vm.runInContext('state.activeCompanyId', sandbox), 'U_AUTO');
  const originalFetch = sandbox.fetch;
  let releases = [];
  sandbox.fetch = (...args) => new Promise(resolve => releases.push(() => originalFetch(...args).then(resolve)));
  $('#fixedProfile').hidden = false; $('#fixedProfile').innerHTML = 'OLD_COMPANY_PROFILE';
  $('#pipelinePanel').hidden = false; $('#pipelineStages').innerHTML = 'OLD_COMPANY_PROGRESS';
  $('#reportsList').innerHTML = 'OLD_COMPANY_REPORT'; $('#overviewSummary').textContent = 'OLD_COMPANY_SUMMARY';
  $('#companySelect').listeners.change({target:{value:'U_FISH'}});
  assert.equal($('#fixedProfile').hidden, true);
  assert.equal($('#fixedProfile').innerHTML, '');
  assert.equal($('#pipelinePanel').hidden, true);
  assert.equal($('#pipelineStages').innerHTML, '');
  assert.equal($('#reportsList').innerHTML, '');
  assert.doesNotMatch($('#overviewSummary').textContent, /OLD_COMPANY/);
  await Promise.all(releases.map(release => release()));
  for (let n=0;n<15;n++) await Promise.resolve();
  assert.equal($('#fixedProfile').hidden, false);
  assert.equal($('#pipelinePanel').hidden, false);
  const currentProfile = $('#fixedProfile').innerHTML;
  releases = [];
  const poll = vm.runInContext('loadDashboard(false)', sandbox);
  assert.equal($('#fixedProfile').hidden, false);
  assert.equal($('#fixedProfile').innerHTML, currentProfile);
  assert.equal($('#pipelinePanel').hidden, false);
  await Promise.all(releases.map(release => release())); await poll;
  sandbox.fetch = originalFetch;
  console.log('Company choice, editable prefill, KPI submission and no stale company data while switching: OK');
})().catch(error => { console.error(error); process.exit(1); });
