'use strict';
const API = '/api';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valueText = value => value === true || value === 'true' ? 'Да' : value === false || value === 'false' ? 'Нет' : value === null || value === undefined ? 'Не указано' : typeof value === 'object' ? 'Сведения приведены в источнике' : String(value);
const factLabels = {
  activity:'Вид деятельности', main_okved_name:'Вид деятельности', main_okved_code:'Код ОКВЭД', industry:'Отрасль',
  loan_mark:'Кредитный продукт', leasing_mark:'Лизинг', factoring_mark:'Факторинг', revenue_value:'Выручка',
  registration_city:'Город регистрации', registration_region:'Регион регистрации', fts_flag:'Сведения ФТС',
  fts_country_name:'Страны в сведениях ФТС', employees_fns_band:'Сотрудники по данным ФНС', employees_spark_band:'Сотрудники по данным СПАРК',
  company_age_band:'Возраст компании', okfs_name:'Форма собственности', org_type:'Тип организации', cl_type:'Отношения с банком',
  eco_mark_label:'Этап развития', corporate_group:'Группа компаний', payments_incoming:'Поступления', payments_outgoing:'Платежи',
  currency:'Валюта', revenue_year:'Период выручки', supplier_terms:'Условия поставщиков', debt:'Долг', products:'Продукция',
  markets:'Рынки', customers:'Клиенты', suppliers:'Поставщики', goals:'Цели', business_role:'Роль', note:'Дополнительные сведения'
};
const questionJobs = new Map();
function humanText(value) {
  let text = String(value ?? '');
  for (const [field, label] of Object.entries(factLabels)) text = text.replace(new RegExp('\\b' + field + '\\b', 'g'), label.charAt(0).toLowerCase() + label.slice(1));
  return text;
}
function factLabel(field) { return factLabels[field] || (/^[a-zA-Z0-9_]+$/.test(field || '') ? 'Сведения о компании' : field || 'Сведения о компании'); }

let _token = '';
let state = {companies: [], activeCompanyId: null, reports: [], context: null, documents: [], demo: false};
let generation = 0;
let timer = null;
let pollingUntil = 0;
let reportView = null;
let pendingFiles = [];
function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function setToken(token) { _token = token; try { localStorage.setItem('alpha_token', token); } catch (_) {} }
function loadToken() { try { _token = localStorage.getItem('alpha_token') || ''; } catch (_) {} }
async function api(path, options = {}) {
  const headers = {'Authorization': 'Bearer ' + _token, ...options.headers};
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(API + '/' + path.replace(/^\/+/, ''), {...options, headers});
  if (!response.ok) {
    let detail = 'HTTP ' + response.status;
    try { const body = await response.json(); detail = typeof body.detail === 'string' ? body.detail : body.error?.message || detail; } catch (_) {}
    throw Object.assign(new Error(detail), {status: response.status});
  }
  return response.status === 204 ? null : response.json();
}
function date(value) {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'дата не указана';
  return new Date(value).toLocaleString('ru-RU', {day:'numeric', month:'short', hour:'2-digit', minute:'2-digit', timeZone:'Europe/Moscow'});
}
function show(screen) {
  document.querySelectorAll('.screen').forEach(node => node.classList.remove('active'));
  $('#' + screen).classList.add('active');
}
function cancelView() { generation++; clearTimeout(timer); timer = null; return generation; }
function notice(message, error = false) {
  const node = $('#notice'); node.textContent = message; node.hidden = !message; node.className = error ? 'notice error' : 'notice';
}
function schedule(action, epoch) {
  clearTimeout(timer);
  if (Date.now() < pollingUntil && epoch === generation) timer = setTimeout(() => { if (epoch === generation) action(); }, 4000);
}
function reportState(row) {
  if (row.availability_notice) return 'Анализ не завершён';
  if (row.published_at) return 'Опубликован · версия ' + row.version;
  if (row.analysis_status === 'failed' || row.last_error) return 'Требуется повторная обработка';
  if (row.analysis_status) return 'Агент готовит выводы';
  if (row.ranking_status) return 'Отбираем значимые события';
  return 'Ожидаем контекст компании';
}
function renderReports() {
  $('#emptyState').hidden = state.reports.length !== 0;
  $('#reportsList').innerHTML = state.reports.map(row => `<button class="report-tile" data-report="${esc(row.report_id)}">
    <span class="eyebrow">${esc(reportState(row))}</span><h3>Важное за неделю</h3>
    <p>${esc(row.summary || 'Статус подготовки обновляется автоматически. Готовые выводы пока не опубликованы.')}</p>
    <span class="tile-footer">${esc(date(row.window_start))} — ${esc(date(row.window_end))} МСК <b>Открыть →</b></span>
  </button>`).join('');
}
function renderDocuments() {
  $('#documentsList').innerHTML = state.documents.length ? state.documents.map(doc => `<div class="document-row"><span>${esc(doc.title)}</span><small>${esc(({pending:'В очереди',processing:'Обработка',ready:'Готов',failed:'Ошибка'})[doc.processing_status] || doc.processing_status)}</small>${doc.processing_error ? `<p class="error">${esc(doc.processing_error)}</p>` : ''}</div>`).join('') : '<p class="muted">Документов пока нет. Можно начать с банковских данных.</p>';
}
const stageIcons = {done:'✓', active:'⟳', waiting:'…', error:'!', skipped:'–', idle:'○'};
const facetLabels = {activity:'Деятельность', counterparty_network:'Контрагенты', corporate_group:'Группа компаний', financial_exposure:'Финансовые риски', foreign_trade:'ВЭД', strategic_goals:'Цели компании', other:'Другое'};
function pipelineDetail(stage) {
  const text = String(stage.detail || '');
  if (/OPENAI_API_KEY|ADMIN_TOKEN|DATABASE_URL|\.env\b|Traceback|[A-Za-z]+Error|Connection refused/i.test(text)) return 'Для продолжения требуется настройка сервиса. Подробности — в разделе диагностики.';
  return text.replace(/insight-worker|workers?/gi, 'сервис анализа').replace(/ranker/gi, 'сервис подбора новостей');
}
function renderPipeline(pipeline) {
  const openSteps = new Set([...($('#pipelineStages').querySelectorAll?.('details[open]') || [])].map(node => node.dataset.stageDetails));
  $('#pipelinePanel').hidden = false;
  $('#pipelineUpdated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) + (pipeline.busy ? ' · обновляется автоматически' : '');
  $('#pipelineStages').innerHTML = pipeline.stages.map(stage => `<li class="stage ${esc(stage.state)}" data-stage="${esc(stage.key)}">
    <span class="stage-icon" aria-hidden="true">${stageIcons[stage.state] || '○'}</span>
    <div><b>${esc(stage.title)}</b>${stage.at ? `<time>${esc(date(stage.at))}</time>` : ''}<p>${esc(pipelineDetail(stage))}</p>
    ${stage.steps?.length ? `<details class="stage-details" data-stage-details="${esc(stage.key)}"${openSteps.has(stage.key) ? ' open' : ''}><summary>Подробности обработки</summary><ul class="stage-steps">${stage.steps.map(step => `<li class="${step.status === 'error' ? 'error' : ''}">${esc(step.text)}</li>`).join('')}</ul></details>` : ''}</div></li>`).join('');
}
function listBlock(title, values) {
  const items = (values || []).filter(Boolean);
  return items.length ? `<h4>${esc(title)}</h4><ul>${items.map(v => `<li>${esc(v)}</li>`).join('')}</ul>` : '';
}
const clientTypes = {CLIENT:'действующий клиент', PROSPECT:'потенциальный клиент', CLOSE_CLIENT:'клиент, закрывший счёт'};
function money(value, currency = null) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '');
  const [div, unit] = number >= 1e9 ? [1e9, 'млрд'] : number >= 1e6 ? [1e6, 'млн'] : number >= 1e3 ? [1e3, 'тыс.'] : [1, ''];
  const suffix = currency === 'RUB' || currency === 'руб.' ? ' ₽' : currency ? ' ' + currency : '';
  return `${(number / div).toLocaleString('ru-RU', {maximumFractionDigits: 1})} ${unit}${suffix}`.trim().replace('  ', ' ');
}
function count(value) { return Number(value).toLocaleString('ru-RU'); }
function sentence(value) { const text = String(value ?? '').toLowerCase(); return text.charAt(0).toUpperCase() + text.slice(1); }
function countries(value) {
  return String(value).split(',').map(name => name.trim()).filter(Boolean)
    .map(name => name.length <= 3 ? name : name.toLowerCase().replace(/(^|[\s-])\S/g, c => c.toUpperCase())).join(', ');
}
function band(value) { return String(value).replace('Более', 'более').replace('Менее', 'менее').replace(/(\d)-(\d)/, '$1–$2'); }
function parsed(value) { if (typeof value !== 'string') return value; try { return JSON.parse(value); } catch (_) { return null; } }
function humanFacts(facts) {
  const bank = {}, other = [];
  for (const fact of facts || []) {
    if (['bank_import', 'derived'].includes(fact.source_type)) bank[fact.field] = fact.value;
    else other.push(fact);
  }
  const about = [], relation = [], links = [];
  if (bank.main_okved_name) about.push({label: 'Вид деятельности', value: bank.main_okved_name + (bank.main_okved_code ? ` (ОКВЭД ${bank.main_okved_code})` : '')});
  const city = String(bank.registration_city || '').replace(/^г\.\s*/, '');
  if (city || bank.registration_region) about.push({label: 'Регистрация', value: [city, bank.registration_region !== city ? bank.registration_region : ''].filter(Boolean).join(', ')});
  if (bank.revenue_value !== undefined && bank.revenue_value !== null) about.push({label: 'Выручка', value: money(bank.revenue_value, bank.currency) + (bank.revenue_year ? ` · ${bank.revenue_year} г.` : '') + (!bank.currency || !bank.revenue_year ? ' (валюта или период не указаны)' : '')});
  if (bank.employees_fns_band) about.push({label: 'Сотрудники', value: band(bank.employees_fns_band) + ' человек'});
  if (bank.company_age_band) about.push({label: 'Возраст компании', value: band(bank.company_age_band) + ' лет'});
  if (bank.eco_mark_label) about.push({label: 'Стадия развития', value: bank.eco_mark_label});
  if (bank.okfs_name) about.push({label: 'Форма собственности', value: sentence(bank.okfs_name)});
  if (bank.cl_type) relation.push({label: 'Статус', value: clientTypes[bank.cl_type] || bank.cl_type});
  const products = [['loan_mark', 'кредит'], ['leasing_mark', 'лизинг'], ['factoring_mark', 'факторинг']];
  if (products.some(([key]) => key in bank)) relation.push({label: 'Продукты', value: products.filter(([key]) => String(bank[key]) === 'true').map(([, name]) => name).join(', ') || 'не отмечены в выгрузке'});
  if (bank.fts_country_name) links.push({label: 'Страны в сведениях ФТС', value: countries(bank.fts_country_name)});
  else if (String(bank.fts_flag) === 'true') links.push({label: 'Сведения ФТС', value: 'есть отметка в выгрузке'});
  const group = parsed(bank.corporate_group);
  if (group?.companies) links.push({label: 'Группа компаний', value: group.companies > 1 ? `${group.companies} компании в группе` : 'в выгрузке одна компания группы'});
  for (const [key, label, preposition] of [['payments_incoming', 'Поступления', 'от'], ['payments_outgoing', 'Платежи', 'в адрес']]) {
    const item = parsed(bank[key]);
    if (item?.amount) links.push({label, value: `${money(item.amount)} ${preposition} ${count(item.edges)} контрагентов, ${count(item.payments)} операций`});
  }
  const docs = other.map(fact => ({label: factLabel(fact.field), value: factValue(fact)}));
  return [
    {title: 'О компании', rows: about, source: 'По данным банка. Дата актуальности исходной выгрузки не указана.'},
    {title: 'Отношения с банком', rows: relation, source: 'По данным банка'},
    {title: 'ВЭД и связи', rows: links, source: 'По данным банка и платежам за период выгрузки. Страны ФТС не определяют направление импорта или экспорта, текущие маршруты и товары.'},
    {title: 'Из документов и ваших ответов', rows: docs, source: 'Загруженные документы и уточнения'},
  ].filter(section => section.rows.length);
}
function renderFixedProfile(pipeline) {
  const profile = pipeline.profile, focus = pipeline.user_context;
  $('#fixedProfile').hidden = !profile && !focus;
  const open = new Set([...($('#fixedProfile').querySelectorAll?.('details[open]') || [])].map(node => node.dataset.block));
  const attrs = block => `data-block="${block}"${open.has(block) ? ' open' : ''}`;
  const sections = profile ? humanFacts(profile.facts) : [];
  const profileHtml = profile ? `<details ${attrs('profile')}><summary>Профиль компании<small>✓ Обновлён ${esc(date(profile.published_at))} · по нему подбираются новости</small></summary>
    <h4>Кратко</h4><p>${esc(humanText(profile.summary))}</p>
    ${sections.map(section => `<h4>${esc(section.title)}</h4>${section.rows.map(row => `<div class="fact-row"><span>${esc(row.label)}</span><div>${esc(row.value)}</div></div>`).join('')}<p class="fact-source">${esc(section.source)}</p>`).join('')}
    ${profile.facets.length ? `<h4>Что отслеживаем в новостях</h4>${profile.facets.map(f => `<p><b>${esc(facetLabels[f.facet_type] || 'Другое')}:</b> ${esc(humanText(f.text))}</p>${f.keywords?.length ? `<div class="keywords">${f.keywords.map(k => `<span>${esc(k)}</span>`).join('')}</div>` : ''}`).join('')}` : ''}
    ${listBlock('Чего не хватает в данных', profile.missing_data)}${listBlock('Что стоит перепроверить', profile.conflicts)}</details>` : '';
  const focusHtml = focus ? `<details ${attrs('focus')}><summary>Ваш фокус<small>✓ Сохранён ${esc(date(focus.published_at))}</small></summary>
    <h4>Роль</h4><p>${esc(focus.role)}</p>${listBlock('За что отвечаете', focus.responsibilities)}${listBlock('Цели', focus.goals)}${listBlock('KPI', focus.kpis)}
    ${focus.note ? `<h4>Дополнительный фокус</h4><p>${esc(focus.note)}</p>` : ''}</details>` : '';
  $('#fixedProfile').innerHTML = profileHtml + focusHtml;
}
async function loadDashboard(fresh = true) {
  const epoch = fresh ? cancelView() : generation;
  const company = state.activeCompanyId;
  if (!company) return;
  show('dashboardScreen');
  if (fresh) {
    state.reports = []; $('#reportsList').innerHTML = ''; $('#emptyState').hidden = true;
    $('#generateBtn').disabled = true; $('#contextStatus').textContent = 'Проверяем контекст…';
    pollingUntil = Date.now() + 10 * 60 * 1000;
  }
  const current = state.companies.find(c => c.company_id === company);
  $('#dashCompany').textContent = current?.legal_name || company;
  $('#dashCompanyDetails').textContent = companyDetails(current);
  try {
    const base = `/companies/${encodeURIComponent(company)}`;
    const [reports, context, documents, pipeline] = await Promise.all([
      api(base + '/reports'), api(base + '/context'), api(base + '/documents'), api(base + '/pipeline')
    ]);
    if (epoch !== generation || company !== state.activeCompanyId) return;
    Object.assign(state, {reports, context, documents, pipeline});
    renderReports(); renderDocuments(); renderPipeline(pipeline); renderFixedProfile(pipeline);
    const ready = context.status === 'ready';
    const focusSaved = pipeline.stages.some(stage => stage.key === 'focus' && stage.state === 'done');
    const reportBusy = pipeline.stages.some(stage => stage.key === 'report' && (['active', 'waiting'].includes(stage.state) || stage.retrying));
    $('#generateBtn').disabled = !ready || reportBusy;
    $('#generateBtn').textContent = reportBusy ? 'Обзор готовится…' : 'Подготовить обзор';
    $('#onboardingPrompt').hidden = ready || focusSaved;
    $('#startOnboardingBtn').textContent = focusSaved ? 'Изменить мой фокус →' : 'Настроить мой фокус →';
    const job = context.profile_job;
    $('#contextStatus').textContent = ready ? `Ваш фокус: ${context.user_context?.business_role || 'настроен'} · профиль компании готов` : job?.status === 'failed' ? 'Профиль не подготовлен — причина указана ниже' : focusSaved ? 'Фокус сохранён. Теперь агент собирает профиль компании — ход работы ниже' : 'Укажите роль, задачи и KPI, чтобы персонализировать обзор';
    const latest = reports.find(row => row.published_at && !row.availability_notice);
    $('#overviewSummary').textContent = latest?.summary || 'Значимые события, возможные последствия и следующий шаг для вашей роли.';
    if (!ready || pipeline.busy || reports.some(row => !row.published_at || row.availability_notice)) schedule(() => loadDashboard(false), epoch);
  } catch (error) { if (epoch === generation) notice('Не удалось обновить сводку: ' + error.message, true); }
}
function storedCompany() { try { return localStorage.getItem('alpha_company') || ''; } catch (_) { return ''; } }
function rememberCompany(id) { try { localStorage.setItem('alpha_company', id); } catch (_) {} }
function revenueText(value, currency = null, year = null) {
  const number = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(number)) return '';
  return 'выручка ' + money(number, currency) + (year ? ` за ${year} год` : '');
}
function companyLabel(c) { return (c.legal_name || c.company_id) + (c.registration_city ? ' · ' + c.registration_city.replace(/^г\.\s*/, '') : ''); }
function companyDetails(c) {
  if (!c) return '';
  return [c.main_okved_name, c.registration_city, revenueText(c.revenue_value, c.currency, c.revenue_year)].filter(Boolean).join(' · ');
}
async function useCompanies(data, demo = false, preferred = null) {
  if (!data.length) throw new Error('Нет доступных компаний');
  const ids = data.map(c => c.company_id);
  const active = [storedCompany(), preferred].find(id => id && ids.includes(id)) || ids[0];
  state.companies = data; state.activeCompanyId = active; state.demo = demo;
  $('#companySelect').innerHTML = data.map(c => `<option value="${esc(c.company_id)}">${esc(companyLabel(c))}</option>`).join('');
  $('#companySelect').value = state.activeCompanyId;
  $('#userName').textContent = demo ? 'Демо-пользователь · тестовый режим' : 'Мой профиль';
  $('#logoutBtn').hidden = demo;
  await loadDashboard();
}
async function initialize() {
  loadToken();
  $('#loginForm').hidden = true;
  $('#retryConnectionBtn').hidden = true; $('#retryConnectionBtn').disabled = true;
  $('#loginTitle').textContent = 'Подключаем бизнес-обзор';
  $('#loginHint').textContent = 'Открываем тестовый стенд. Вход выполняется автоматически.';
  $('#loginError').textContent = '';
  let demo = false, preferred = null;
  try {
    const session = await api('/demo/session', {method:'POST'});
    setToken(session.token); demo = true; preferred = session.company_id;
  } catch (error) {
    if (error.status !== 404) {
      $('#loginTitle').textContent = 'Не удалось подключиться';
      $('#loginError').textContent = 'Сервис временно недоступен. Попробуйте подключиться ещё раз.';
      $('#retryConnectionBtn').hidden = false; $('#retryConnectionBtn').disabled = false;
      show('loginScreen'); return;
    }
    $('#loginTitle').textContent = 'Вход в бизнес-обзор';
    $('#loginHint').textContent = 'Автоматический вход отключён. Используйте выданный токен доступа.';
    $('#loginForm').hidden = false;
  }
  if (!_token) return;
  try { await useCompanies(await api('/companies'), demo, preferred); }
  catch (error) {
    $('#loginError').textContent = demo ? 'Не удалось загрузить компании. Повторите подключение.' : 'Не удалось войти. Проверьте токен доступа.';
    $('#retryConnectionBtn').hidden = !demo; $('#retryConnectionBtn').disabled = false;
    show('loginScreen');
  }
}
$('#retryConnectionBtn').addEventListener('click', initialize);
$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault(); setToken($('#tokenInput').value.trim());
  try { await useCompanies(await api('/companies')); } catch (error) { $('#loginError').textContent = error.message; }
});
$('#logoutBtn').addEventListener('click', () => {
  cancelView(); setToken(''); rememberCompany('');
  state = {companies: [], activeCompanyId: null, reports: [], context: null, documents: [], demo: false};
  pendingFiles = []; reportView = null; questionJobs.clear();
  ['#reportBody', '#reportsList', '#documentsList', '#fileList', '#companySelect'].forEach(id => { $(id).innerHTML = ''; });
  $('#onboardingForm').reset(); notice(''); show('loginScreen');
});
$('#companySelect').addEventListener('change', event => {
  state.activeCompanyId = event.target.value; rememberCompany(state.activeCompanyId); state.context = null; state.pipeline = null; state.documents = []; pendingFiles = [];
  $('#pipelinePanel').hidden = true; $('#pipelineStages').innerHTML = '';
  $('#fixedProfile').hidden = true; $('#fixedProfile').innerHTML = '';
  $('#onboardingPrompt').hidden = true;
  $('#overviewSummary').textContent = 'Значимые события, возможные последствия и следующий шаг для вашей роли.';
  $('#startOnboardingBtn').textContent = 'Настроить мой фокус →';
  $('#documentsList').innerHTML = ''; $('#fileList').innerHTML = ''; notice(''); loadDashboard();
});
$('#homeBtn').addEventListener('click', () => loadDashboard());
$('#analyticsBtn').addEventListener('click', () => loadDashboard());
$('#backToDash').addEventListener('click', () => loadDashboard());
$('#backFromOnboarding').addEventListener('click', () => loadDashboard());
function settingsView() {
  cancelView(); show('onboardingScreen'); notice('');
  const saved = state.context?.user_context?.raw_answers;
  const suggested = state.companies.find(c => c.company_id === state.activeCompanyId)?.suggested_answers;
  const answers = saved || suggested || {};
  $('#obPrefillHint').hidden = Boolean(saved) || !suggested;
  $('#obRole').value = answers.role || '';
  $('#obResponsibilities').value = (answers.responsibilities || []).join('\n');
  $('#obGoals').value = (answers.goals || []).join('\n'); $('#obKpis').value = (answers.kpis || []).join('\n');
  $('#obNote').value = answers.note || '';
  $('#obSubmit').disabled = false; $('#obSubmitStatus').textContent = ''; renderDocuments();
}
$('#startOnboardingBtn').addEventListener('click', settingsView);
$('#settingsBtn').addEventListener('click', settingsView);
$('#reportsList').addEventListener('click', event => { const target = event.target.closest('[data-report]'); if (target) loadReport(target.dataset.report); });
$('#generateBtn').addEventListener('click', async () => {
  const company = state.activeCompanyId; const epoch = generation; $('#generateBtn').disabled = true;
  try {
    const result = await api(`/companies/${encodeURIComponent(company)}/reports`, {method:'POST', body:JSON.stringify({request_id:requestId()})});
    if (epoch !== generation) return;
    notice(result.status === 'awaiting_profile' ? 'Сначала нужно завершить подготовку профиля.' : result.status === 'already_running' ? 'Обзор уже готовится — ход работы виден ниже.' : 'Обзор поставлен в очередь. Можно продолжать работу — результат сохранится в сводке.');
    await loadDashboard();
  } catch (error) { if (epoch === generation) { notice(error.message, true); $('#generateBtn').disabled = false; } }
});
function safeUrl(raw) { try { const url = new URL(raw); return ['https:','http:'].includes(url.protocol) ? url.href : null; } catch (_) { return null; } }
function factValue(fact) {
  const value = fact.value;
  if (fact.field === 'fts_country_name') return countries(value);
  if (fact.field === 'cl_type') return clientTypes[value] || 'Статус приведён в данных банка';
  if (fact.field === 'revenue_value') {
    const sourceRow = (fact.sources || []).map(source => parsed(source.excerpt)).find(row => row && typeof row === 'object' && (row.currency || row.revenue_year)) || {};
    const currency = fact.currency || sourceRow.currency, year = fact.revenue_year || sourceRow.revenue_year;
    return money(value, currency) + (year ? ` · ${year} г.` : '') + (!currency || !year ? ' · валюта или период не указаны' : '');
  }
  if (['employees_fns_band', 'employees_spark_band'].includes(fact.field)) return band(value) + ' человек';
  if (fact.field === 'company_age_band') return band(value) + ' лет';
  if (fact.field === 'org_type' && value === 'КОММ_ОРГ') return 'Коммерческая организация';
  const object = typeof value === 'object' ? value : parsed(value);
  if (object && typeof object === 'object') {
    if (fact.field === 'corporate_group') return object.companies ? `Компаний в группе по выгрузке: ${count(object.companies)}` : 'Сведения о группе есть в выгрузке';
    if (['payments_incoming', 'payments_outgoing'].includes(fact.field)) return [object.amount !== undefined ? 'Сумма: ' + money(object.amount, object.currency) : '', object.edges !== undefined ? 'контрагентов: ' + count(object.edges) : '', object.payments !== undefined ? 'операций: ' + count(object.payments) : ''].filter(Boolean).join(' · ');
    return 'Структурированные сведения из источника';
  }
  return humanText(valueText(value));
}
function sourceContent(source, fact) {
  const raw = source.excerpt || source.content || '';
  const object = typeof raw === 'object' ? raw : parsed(raw);
  if (object && typeof object === 'object' && !Array.isArray(object)) {
    const fields = fact && object[fact.field] !== undefined ? [fact.field] : Object.keys(factLabels).filter(key => object[key] !== undefined && object[key] !== null);
    const rows = fields.map(field => `<div class="source-fact"><dt>${esc(factLabel(field))}</dt><dd>${esc(factValue({field, value:object[field], currency:object.currency, revenue_year:object.revenue_year}))}</dd></div>`).join('');
    return rows ? `<dl class="source-fields">${rows}</dl><p class="muted">Значения из исходной записи компании.</p>` : '<p class="muted">Структурированная запись компании. Подтверждённое значение приведено выше.</p>';
  }
  if (typeof raw === 'object' || /^\s*[\[{]/.test(String(raw))) return '<p class="muted">Структурированные сведения из источника. Значимые для вывода данные приведены выше.</p>';
  return `<blockquote>${esc(raw || 'Текст источника не сохранён')}</blockquote>`;
}
function sourceHTML(source, fact = null) {
  const locator = source.source_ref?.locator || source.metadata || {};
  const page = locator.page ? ` · стр. ${locator.page}` : locator.sheet ? ` · лист ${locator.sheet}` : '';
  const url = safeUrl(source.source_url);
  const isCompany = ['bank_import', 'derived', 'user_answer'].includes(source.source_type) || (!source.document_id && !url);
  const title = source.source_type === 'bank_analytics' ? 'Аналитика Альфа-Банка' : source.document_id ? 'Документ компании' : url ? 'Первоисточник' : source.source_type === 'user_answer' ? 'Ваше уточнение' : 'Данные компании';
  const link = source.document_id ? `<button type="button" class="source-link" data-document="${esc(source.document_id)}">Скачать документ${esc(page)}</button>` : url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Открыть первоисточник ↗</a>` : '';
  const timestamp = source.source_date ? date(source.source_date) : isCompany ? 'Дата исходных данных не указана' : 'Дата публикации не указана';
  return `<details class="source"><summary><span>${esc(title + page)}</span><small>${esc(timestamp)}</small></summary><div class="source-content">${link}${sourceContent(source, fact)}</div></details>`;
}
const labels = {verified_fact:'Факт из источника', bank_position:'Позиция Альфа-Банка', ai_inference:'Вывод модели', risk:'Гипотеза о риске', opportunity:'Гипотеза о возможности', recommendation:'Предложение модели'};
const certainty = {confirmed:'Подтверждено источником', qualified:'Условный вывод', unverified:'Требует проверки'};
function claimGroup(claim) {
  if (claim.kind === 'bank_position') return 'bank';
  if (claim.kind === 'verified_fact' && claim.certainty === 'confirmed') return 'facts';
  if (claim.certainty === 'unverified' || ['risk', 'opportunity', 'verified_fact'].includes(claim.kind)) return 'hypotheses';
  return 'model';
}
function claimHTML(claim) {
  const group = claimGroup(claim);
  const label = claim.kind === 'verified_fact' && group !== 'facts' ? 'Сведение требует проверки' : claim.kind === 'ai_inference' && group === 'hypotheses' ? 'Гипотеза модели' : labels[claim.kind] || 'Комментарий модели';
  const statement = ['verified_fact', 'bank_position'].includes(claim.kind) ? claim.statement : humanText(claim.statement);
  return `<div class="claim ${esc(claim.kind)}"><div class="claim-label">${esc(label)} <small>${esc(certainty[claim.certainty] || '')}</small></div><p>${esc(statement)}</p>
    ${(claim.company_facts || []).map(f => `<div class="company-fact"><span class="fact-caption">Контекст вашей компании</span><p><b>${esc(factLabel(f.field))}</b> — ${esc(factValue(f))}</p>${(f.sources || []).map(source => sourceHTML(source, f)).join('')}</div>`).join('')}
    ${(claim.sources || []).map(source => sourceHTML(source)).join('')}</div>`;
}
function evidenceGroups(claims) {
  return [
    ['facts', 'Факты из источников', 'Подтверждённые сведения с первоисточниками.'],
    ['bank', 'Позиция Альфа-Банка', 'Содержание банковской аналитики приведено отдельно от интерпретации модели.'],
    ['model', 'Выводы модели', 'Интерпретация фактов для вашей компании. Это не официальная позиция Альфа-Банка.'],
    ['hypotheses', 'Гипотезы и непроверенные сведения', 'Возможные сценарии и риски, которые требуют проверки.']
  ].map(([key, title, description]) => {
    const items = claims.filter(claim => claimGroup(claim) === key);
    return items.length ? `<section class="evidence-category category-${key}"><h4>${esc(title)}</h4><p class="muted">${esc(description)}</p>${items.map(claimHTML).join('')}</section>` : '';
  }).join('');
}
function cardHTML(card) {
  const claims = card.claims || [];
  const causal = claims.find(c => c.fact_ids?.length || c.company_facts?.length);
  const bank = claims.filter(c => c.kind === 'bank_position');
  return `<article class="insight-card"><details><summary class="insight-heading"><span class="eyebrow">${bank.length ? 'Вывод модели · есть позиция Альфа-Банка' : 'Вывод модели · позиция банка не приведена'}</span><h2>${esc(card.title)}</h2><p>${esc(humanText(causal?.statement || card.business_impact))}</p><span class="expand-label">Почему важно · основания · уточнить ↓</span></summary>
    <div class="insight-body"><section><h3>Что произошло</h3><p>${esc(humanText(card.event_summary))}</p></section>
    <section><h3>Вывод модели: возможное влияние</h3><p>${esc(humanText(card.details?.mechanism || card.business_impact))}</p><p class="muted">Это интерпретация модели, а не официальная позиция Альфа-Банка. Возможное влияние требует проверки; финансовый результат не гарантирован.</p></section>
    ${card.details?.role_action ? `<section class="role-action"><h3>Предложение модели для вашей роли</h3><p>${esc(humanText(card.details.role_action))}</p></section>` : ''}
    ${card.details?.assumptions?.length ? `<section class="warning"><h3>Гипотезы и допущения</h3><p>Эти условия требуют проверки до принятия решения.</p><ul>${card.details.assumptions.map(a => `<li>${esc(a)}</li>`).join('')}</ul></section>` : ''}
    <section><details class="evidence-group"><summary>На чём основан вывод <small>Факты · позиция банка · выводы модели · гипотезы</small></summary>${evidenceGroups(claims)}</details></section>
    <section class="question-section"><h3>Есть вопрос по этому событию?</h3><p class="muted">Спросите о возможном влиянии или следующем шаге. Проверка источников может занять несколько минут.</p>
      <form class="question-form" data-insight="${esc(card.insight_id)}"><input name="question" maxlength="2000" aria-label="Вопрос по карточке" placeholder="Какое допущение стоит проверить первым?" required><button type="submit" class="secondary">Уточнить</button></form><div class="question-status" role="status" aria-live="polite" hidden></div><div class="answer" aria-live="polite"></div><div class="card-discussions" data-insight-history="${esc(card.insight_id)}"></div>
    </section></div></details></article>`;
}
async function loadReport(id, version = null, refresh = false) {
  const epoch = refresh ? generation : cancelView();
  if (!refresh) { show('reportScreen'); $('#reportBody').innerHTML = ''; $('#reportDiscussions').innerHTML = ''; pollingUntil = Date.now() + 10 * 60 * 1000; }
  try {
    const [data, versions] = await Promise.all([api(`/reports/${id}${version ? '?version=' + version : ''}`), api(`/reports/${id}/versions`)]);
    if (epoch !== generation) return;
    reportView = {id, version: data.version?.report_version_id, titles: Object.fromEntries((data.cards || []).map(card => [card.insight_id, card.title]))};
    $('#reportTitle').textContent = 'Важное за неделю';
    $('#reportMeta').textContent = `${date(data.report.window_start)} — ${date(data.report.window_end)} МСК` + (data.version ? ` · опубликовано ${date(data.version.published_at)}` : '');
    $('#versionSelect').innerHTML = versions.map(v => `<option value="${v.version}" ${v.version === data.version?.version ? 'selected' : ''}>Версия ${v.version} · ${esc(date(v.published_at))}</option>`).join('');
    $('#versionSelect').disabled = !versions.length;
    $('#reportCard').hidden = data.status !== 'preparing';
    $('#reportCard').textContent = 'Подбираем события и проверяем их значение для вашей компании. Обзор появится здесь автоматически; можно вернуться в сводку и продолжить работу.';
    const v = data.version;
    $('#reportBody').innerHTML = v ? `<section class="panel ${data.status === 'availability_notice' ? 'warning' : ''}"><span class="eyebrow">${data.status === 'availability_notice' ? 'Уведомление о неполноте — не готовая аналитика' : 'Кратко'}</span><h2>${esc(v.summary)}</h2>
      ${data.company_context_metadata ? `<p class="muted">Профиль компании от ${esc(date(data.company_context_metadata.published_at))}</p>` : ''}
      ${v.limitations?.length ? `<details open><summary>Ограничения данных</summary><ul>${v.limitations.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>` : ''}
      ${v.changes?.length ? `<details><summary>Что изменилось</summary><ul>${v.changes.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>` : ''}</section>${(data.cards || []).map(cardHTML).join('')}${!data.cards?.length && data.status === 'ready' ? '<p class="panel">Обоснованных карточек для этого периода не опубликовано. См. сводку и ограничения.</p>' : ''}` : '';
    if (v) { loadDiscussions(id, v.report_version_id, epoch); resumeQuestionJobs(epoch); }
    if (data.status === 'preparing' || data.status === 'availability_notice') schedule(() => loadReport(id, version, true), epoch);
  } catch (error) { if (epoch === generation) notice('Не удалось открыть обзор: ' + error.message, true); }
}
$('#versionSelect').addEventListener('change', event => { if (reportView) loadReport(reportView.id, Number(event.target.value)); });
async function loadDiscussions(id, version, epoch) {
  try {
    const messages = await api(`/reports/${id}/discussions`);
    if (epoch !== generation || reportView?.version !== version) return;
    const selected = messages.filter(message => message.report_version_id === version);
    const grouped = new Map();
    for (const message of selected) {
      if (!grouped.has(message.discussion_id)) grouped.set(message.discussion_id, []);
      grouped.get(message.discussion_id).push(message);
    }
    const renderGroup = items => `<div class="discussion-item">${items.map(message => `<div class="discussion-message ${message.speaker === 'user' ? 'from-user' : ''}"><b>${message.speaker === 'user' ? 'Ваш вопрос' : 'Ответ по источникам'}</b><p>${esc(humanText(message.content))}</p>${(message.source_details || []).map(source => sourceHTML(source)).join('')}</div>`).join('')}</div>`;
    const groups = [...grouped.values()];
    const targets = [...document.querySelectorAll('[data-insight-history]')];
    for (const target of targets) {
      const matches = groups.filter(items => items.some(item => item.insight_id === target.dataset.insightHistory));
      target.innerHTML = matches.length ? `<details class="discussion-history"><summary>Предыдущие уточнения по карточке <small>${matches.length}</small></summary>${matches.map(renderGroup).join('')}</details>` : '';
    }
    const unassigned = groups.filter(items => !items.some(item => targets.some(target => target.dataset.insightHistory === item.insight_id)));
    $('#reportDiscussions').innerHTML = `<details class="panel discussion-history"><summary>История этой версии обзора <small>${grouped.size ? `${grouped.size} уточнений · ответы по событиям находятся в карточках` : 'Пока нет сохранённых ответов'}</small></summary>
      ${unassigned.map(renderGroup).join('')}
      <button type="button" class="quiet" data-refresh-discussions>Обновить историю</button></details>`;
  } catch (error) {
    if (epoch === generation) $('#reportDiscussions').innerHTML = '<div class="panel"><p class="muted">История уточнений временно недоступна.</p><button type="button" class="quiet" data-refresh-discussions>Повторить загрузку</button></div>';
  }
}
$('#reportDiscussions').addEventListener('click', event => {
  if (event.target.closest('[data-refresh-discussions]') && reportView) loadDiscussions(reportView.id, reportView.version, generation);
});
function questionStatus(form, message, busy = false, error = false) {
  const status = form.parentElement.querySelector('.question-status');
  status.hidden = !message; status.textContent = message;
  status.className = 'question-status' + (busy ? ' working' : '') + (error ? ' error' : '');
  form.setAttribute?.('aria-busy', String(busy));
}
async function waitForJob(id, epoch, onProgress) {
  const started = Date.now();
  for (let attempt = 0; attempt < 360; attempt++) {
    if (epoch !== generation) throw Object.assign(new Error('Экран изменён'), {cancelled:true});
    const job = await api('/jobs/' + id);
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw Object.assign(new Error('Не удалось подготовить ответ. Попробуйте ещё раз.'), {terminal:true});
    const elapsed = Math.floor((Date.now() - started) / 1000);
    onProgress((job.status === 'running' ? 'Готовим ответ по источникам' : 'Вопрос принят, ожидаем начала обработки') + (elapsed >= 60 ? ` · ${Math.floor(elapsed / 60)} мин` : '') + '. Ответ появится здесь автоматически.');
    await new Promise(resolve => setTimeout(resolve, 2500));
  }
  throw Object.assign(new Error('Ответ ещё готовится. Нажмите «Проверить ответ» — новый запрос отправлен не будет.'), {pending:true});
}
function questionKey(view, insight) { return [view.id, view.version, insight].join(':'); }
async function followQuestion(form, record, epoch) {
  const input = form.querySelector('input'), button = form.querySelector('button');
  const answer = form.parentElement.querySelector('.answer');
  if (form.dataset.busy === 'true') return;
  form.dataset.busy = 'true'; input.disabled = button.disabled = true; button.textContent = 'Готовим ответ…';
  questionStatus(form, 'Вопрос принят. Проверяем статус ответа…', true);
  try {
    const result = await waitForJob(record.jobId, epoch, message => { if (epoch === generation) questionStatus(form, message, true); });
    questionJobs.delete(record.key);
    if (epoch !== generation) return;
    answer.innerHTML = `<div class="answer-label">Ответ по источникам</div><p>${esc(humanText(result.answer))}</p>${(result.source_details || []).map(source => sourceHTML(source)).join('')}`;
    questionStatus(form, 'Ответ готов и сохранён в истории этой версии обзора.');
    form.dataset.requestId = ''; input.value = '';
    loadDiscussions(record.view.id, record.view.version, epoch);
  } catch (error) {
    if (error.cancelled || epoch !== generation) return;
    if (error.terminal) { questionJobs.delete(record.key); form.dataset.requestId = ''; }
    questionStatus(form, error.pending || error.terminal ? error.message : 'Связь прервалась. Нажмите «Проверить ответ», чтобы продолжить ожидание.', false, Boolean(error.terminal));
  } finally {
    form.dataset.busy = 'false'; input.disabled = button.disabled = false;
    button.textContent = questionJobs.has(record.key) ? 'Проверить ответ' : 'Уточнить';
    if (questionJobs.has(record.key)) input.disabled = true;
    form.setAttribute?.('aria-busy', 'false');
  }
}
function resumeQuestionJobs(epoch) {
  for (const form of document.querySelectorAll('.question-form')) {
    const record = questionJobs.get(questionKey(reportView, form.dataset.insight));
    if (record) { form.querySelector('input').value = record.question; followQuestion(form, record, epoch); }
  }
}
$('#reportBody').addEventListener('submit', async event => {
  const form = event.target.closest('.question-form'); if (!form || !reportView) return;
  event.preventDefault();
  if (form.dataset.busy === 'true') return;
  const epoch = generation, view = {...reportView};
  const input = form.querySelector('input'), button = form.querySelector('button');
  const key = questionKey(view, form.dataset.insight), existing = questionJobs.get(key);
  if (existing) { followQuestion(form, existing, epoch); return; }
  const question = input.value.trim(); if (!question) { input.focus?.(); return; }
  form.dataset.busy = 'true'; input.disabled = button.disabled = true; button.textContent = 'Отправляем…';
  form.parentElement.querySelector('.answer').innerHTML = ''; questionStatus(form, 'Отправляем вопрос…', true);
  if (form.dataset.question !== question) form.dataset.requestId = '';
  form.dataset.question = question;
  form.dataset.requestId ||= requestId();
  try {
    const job = await api(`/reports/${view.id}/questions`, {method:'POST', body:JSON.stringify({request_id:form.dataset.requestId, report_version_id:view.version, insight_id:form.dataset.insight, question})});
    const record = {key, view, question, jobId:job.job_id}; questionJobs.set(key, record);
    form.dataset.busy = 'false';
    if (epoch !== generation) return;
    await followQuestion(form, record, epoch);
  } catch (error) {
    form.dataset.busy = 'false'; input.disabled = button.disabled = false; button.textContent = 'Повторить отправку';
    if (epoch === generation) questionStatus(form, 'Не удалось отправить вопрос. Повторите попытку.', false, true);
  }
});
async function downloadSource(event) {
  const button = event.target.closest('[data-document]'); if (!button) return;
  event.preventDefault();
  try {
    const response = await fetch(`/api/documents/${encodeURIComponent(button.dataset.document)}/content`, {headers:{Authorization:'Bearer ' + _token}});
    if (!response.ok) throw new Error('Документ недоступен');
    const url = URL.createObjectURL(await response.blob());
    const disposition = response.headers.get('Content-Disposition') || '';
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = disposition.match(/filename="?([^";]+)/i);
    let filename = plain?.[1] || 'первоисточник';
    if (encoded) { try { filename = decodeURIComponent(encoded[1]); } catch (_) {} }
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { notice(error.message, true); }
}
$('#reportBody').addEventListener('click', downloadSource);
$('#reportDiscussions').addEventListener('click', downloadSource);
$('#fileInput').addEventListener('change', event => {
  pendingFiles.push(...event.target.files); event.target.value = '';
  $('#fileList').innerHTML = pendingFiles.map(f => `<p class="document-row">${esc(f.name)} · ${(f.size / 1024 / 1024).toFixed(1)} МБ</p>`).join('');
});
function lines(selector, limit, maxLength) {
  const values = [...new Set($(selector).value.split('\n').map(t => t.trim()).filter(Boolean))];
  if (values.length > limit || values.some(t => t.length > maxLength)) throw new Error(`Не более ${limit} строк, до ${maxLength} символов каждая`);
  return values;
}
$('#onboardingForm').addEventListener('submit', async event => {
  event.preventDefault(); const company = state.activeCompanyId; const epoch = generation;
  const button = $('#obSubmit'); const status = $('#obSubmitStatus');
  button.disabled = true; $('#companySelect').disabled = true; status.textContent = 'Сохраняем…';
  try {
    const answers = {role:$('#obRole').value.trim(), responsibilities:lines('#obResponsibilities',8,200), goals:lines('#obGoals',3,500), kpis:lines('#obKpis',3,200), note:$('#obNote').value.trim()};
    while (pendingFiles.length) {
      const file = pendingFiles[0]; const body = new FormData(); body.append('file',file); body.append('title',file.name);
      await api(`/companies/${encodeURIComponent(company)}/documents`, {method:'POST',body}); pendingFiles.shift();
    }
    await api(`/companies/${encodeURIComponent(company)}/onboarding`, {method:'POST',body:JSON.stringify(answers)});
    if (epoch !== generation) return;
    $('#fileList').innerHTML = ''; notice('Фокус сохранён. Профиль обрабатывается в фоне.'); await loadDashboard();
  } catch (error) { if (epoch === generation) status.textContent = 'Не удалось сохранить: ' + error.message; }
  finally { button.disabled = false; $('#companySelect').disabled = false; }
});
initialize();
