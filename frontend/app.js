'use strict';
const API = '/api';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valueText = value => value === true || value === 'true' ? 'Да' : value === false || value === 'false' ? 'Нет' : typeof value === 'object' ? JSON.stringify(value) : String(value ?? '');
const factLabels = {activity:'Вид деятельности', main_okved_name:'Вид деятельности', industry:'Отрасль', loan_mark:'Наличие кредитного продукта', revenue_value:'Выручка (единицы и период — по источнику)', registration_region:'Регион регистрации', fts_flag:'Наличие данных ФТС'};
let _token = '';
let state = {companies: [], activeCompanyId: null, reports: [], context: null, documents: [], demo: false};
let generation = 0;
let timer = null;
let pollingUntil = 0;
let reportView = null;
let pendingFiles = [];
// crypto.randomUUID exists only in secure contexts (HTTPS/localhost); plain http over Tailscale lacks it.
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
const sourceLabels = {bank_import:'банковские данные', document:'документ', user_answer:'ответ пользователя', derived:'расчёт', public_web:'веб'};
function renderPipeline(pipeline) {
  $('#pipelinePanel').hidden = false;
  $('#pipelineUpdated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) + (pipeline.busy ? ' · обновляется автоматически' : '');
  $('#pipelineStages').innerHTML = pipeline.stages.map(stage => `<li class="stage ${esc(stage.state)}" data-stage="${esc(stage.key)}">
    <span class="stage-icon" aria-hidden="true">${stageIcons[stage.state] || '○'}</span>
    <div><b>${esc(stage.title)}</b>${stage.at ? `<time>${esc(date(stage.at))}</time>` : ''}<p>${esc(stage.detail || '')}</p>
    ${stage.steps?.length ? `<ul class="stage-steps">${stage.steps.map(step => `<li class="${step.status === 'error' ? 'error' : ''}">${esc(step.text)}</li>`).join('')}</ul>` : ''}</div></li>`).join('');
}
function listBlock(title, values) {
  const items = (values || []).filter(Boolean);
  return items.length ? `<h4>${esc(title)}</h4><ul>${items.map(v => `<li>${esc(v)}</li>`).join('')}</ul>` : '';
}
const clientTypes = {CLIENT:'действующий клиент', PROSPECT:'потенциальный клиент', CLOSE_CLIENT:'клиент, закрывший счёт'};
const fieldNames = {activity:'Вид деятельности', industry:'Отрасль', debt:'Долг', products:'Продукция', markets:'Рынки', customers:'Клиенты', suppliers:'Поставщики'};
function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '');
  const [div, unit] = number >= 1e9 ? [1e9, 'млрд'] : number >= 1e6 ? [1e6, 'млн'] : number >= 1e3 ? [1e3, 'тыс.'] : [1, ''];
  return `${(number / div).toLocaleString('ru-RU', {maximumFractionDigits: 1})} ${unit} ₽`.replace('  ', ' ');
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
  if (bank.revenue_value) about.push({label: 'Выручка', value: money(bank.revenue_value)});
  if (bank.employees_fns_band) about.push({label: 'Сотрудники', value: band(bank.employees_fns_band) + ' человек'});
  if (bank.company_age_band) about.push({label: 'Возраст компании', value: band(bank.company_age_band) + ' лет'});
  if (bank.eco_mark_label) about.push({label: 'Стадия развития', value: bank.eco_mark_label});
  if (bank.okfs_name) about.push({label: 'Форма собственности', value: sentence(bank.okfs_name)});
  if (bank.cl_type) relation.push({label: 'Статус', value: clientTypes[bank.cl_type] || bank.cl_type});
  const products = [['loan_mark', 'кредит'], ['leasing_mark', 'лизинг'], ['factoring_mark', 'факторинг']];
  if (products.some(([key]) => key in bank)) relation.push({label: 'Продукты', value: products.filter(([key]) => String(bank[key]) === 'true').map(([, name]) => name).join(', ') || 'кредитных продуктов нет'});
  if (bank.fts_country_name) links.push({label: 'Внешняя торговля', value: countries(bank.fts_country_name)});
  else if (String(bank.fts_flag) === 'true') links.push({label: 'Внешняя торговля', value: 'есть таможенные операции'});
  const group = parsed(bank.corporate_group);
  if (group?.companies) links.push({label: 'Группа компаний', value: group.companies > 1 ? `${group.companies} компании в группе` : 'не входит в группу'});
  for (const [key, label, preposition] of [['payments_incoming', 'Поступления', 'от'], ['payments_outgoing', 'Платежи', 'в адрес']]) {
    const item = parsed(bank[key]);
    if (item?.amount) links.push({label, value: `${money(item.amount)} ${preposition} ${count(item.edges)} контрагентов, ${count(item.payments)} операций`});
  }
  const docs = other.map(fact => ({label: fieldNames[fact.field] || sentence(String(fact.field).replace(/_/g, ' ')), value: valueText(fact.value)}));
  return [
    {title: 'О компании', rows: about, source: 'По данным банка'},
    {title: 'Отношения с банком', rows: relation, source: 'По данным банка'},
    {title: 'ВЭД и связи', rows: links, source: 'По данным банка и платежам за период выгрузки'},
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
    <h4>Кратко</h4><p>${esc(profile.summary)}</p>
    ${sections.map(section => `<h4>${esc(section.title)}</h4>${section.rows.map(row => `<div class="fact-row"><span>${esc(row.label)}</span><div>${esc(row.value)}</div></div>`).join('')}<p class="fact-source">${esc(section.source)}</p>`).join('')}
    ${profile.facets.length ? `<h4>Что отслеживаем в новостях</h4>${profile.facets.map(f => `<p><b>${esc(facetLabels[f.facet_type] || 'Другое')}:</b> ${esc(f.text)}</p>${f.keywords?.length ? `<div class="keywords">${f.keywords.map(k => `<span>${esc(k)}</span>`).join('')}</div>` : ''}`).join('')}` : ''}
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
function revenueText(value) {
  const number = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(number)) return '';
  return 'выручка ' + (number / 1e9).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' млрд';
}
function companyLabel(c) { return (c.legal_name || c.company_id) + (c.registration_city ? ' · ' + c.registration_city.replace(/^г\.\s*/, '') : ''); }
function companyDetails(c) {
  if (!c) return '';
  return [c.main_okved_name, c.registration_city, revenueText(c.revenue_value)].filter(Boolean).join(' · ');
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
  let demo = false, preferred = null;
  try {
    const session = await api('/demo/session', {method:'POST'});
    setToken(session.token); demo = true; preferred = session.company_id;
  } catch (error) {
    if (error.status !== 404) { $('#loginError').textContent = 'Не удалось открыть демо: ' + error.message; return; }
  }
  if (!_token) return;
  try { await useCompanies(await api('/companies'), demo, preferred); }
  catch (error) { $('#loginError').textContent = error.message; show('loginScreen'); }
}
$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault(); setToken($('#tokenInput').value.trim());
  try { await useCompanies(await api('/companies')); } catch (error) { $('#loginError').textContent = error.message; }
});
$('#logoutBtn').addEventListener('click', () => {
  cancelView(); setToken(''); rememberCompany('');
  state = {companies: [], activeCompanyId: null, reports: [], context: null, documents: [], demo: false};
  pendingFiles = []; reportView = null;
  ['#reportBody', '#reportsList', '#documentsList', '#fileList', '#companySelect'].forEach(id => { $(id).innerHTML = ''; });
  $('#onboardingForm').reset(); notice(''); show('loginScreen');
});
$('#companySelect').addEventListener('change', event => {
  state.activeCompanyId = event.target.value; rememberCompany(state.activeCompanyId); state.context = null; state.documents = []; pendingFiles = [];
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
function sourceHTML(source) {
  const locator = source.source_ref?.locator || source.metadata || {};
  const page = locator.page ? ` · стр. ${locator.page}` : locator.sheet ? ` · лист ${locator.sheet}` : '';
  const url = safeUrl(source.source_url);
  const link = source.document_id ? `<button class="source-link" data-document="${esc(source.document_id)}">Скачать документ${esc(page)}</button>` : url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Открыть первоисточник ↗</a>` : '<span>Источник: данные компании</span>';
  return `<details class="source"><summary>${link} <small>${source.source_date ? esc(date(source.source_date)) : 'Дата не указана — актуальность требует проверки'}</small></summary><blockquote>${esc(source.excerpt || source.content || 'Текст источника не сохранён')}</blockquote></details>`;
}
const labels = {verified_fact:'Факт из источника', bank_position:'Позиция Альфа-Банка', ai_inference:'Вывод AI', risk:'Возможный риск', opportunity:'Возможность', recommendation:'Что проверить'};
const certainty = {confirmed:'Подтверждено источником', qualified:'Условный вывод', unverified:'Требует проверки'};
function cardHTML(card) {
  const claims = card.claims || [];
  const causal = claims.find(c => c.fact_ids?.length || c.company_facts?.length);
  const bank = claims.filter(c => c.kind === 'bank_position');
  return `<article class="insight-card"><details><summary class="insight-heading"><span class="eyebrow">${bank.length ? 'С позицией банка' : 'Интерпретация AI · позиция банка не приведена'}</span><h2>${esc(card.title)}</h2><p>${esc(causal?.statement || card.business_impact)}</p><span class="expand-label">Почему важно · основания · уточнить ↓</span></summary>
    <div class="insight-body"><section><h3>Что произошло</h3><p>${esc(card.event_summary)}</p></section>
    <section><h3>Как событие может повлиять</h3><p>${esc(card.details?.mechanism || card.business_impact)}</p><p class="muted">Это условная интерпретация AI, не прогноз и не подтверждённый финансовый результат.</p></section>
    ${card.details?.role_action ? `<section class="role-action"><h3>Следующий шаг для вашей роли</h3><p>${esc(card.details.role_action)}</p></section>` : ''}
    ${card.details?.assumptions?.length ? `<section class="warning"><h3>Допущения</h3><ul>${card.details.assumptions.map(a => `<li>${esc(a)}</li>`).join('')}</ul></section>` : ''}
    <section><h3>На чём основан вывод</h3>${claims.map(claim => `<div class="claim ${esc(claim.kind)}"><div class="claim-label">${esc(labels[claim.kind] || claim.kind)} <small>${esc(certainty[claim.certainty] || '')}</small></div><p>${esc(claim.statement)}</p>
      ${(claim.company_facts || []).map(f => `<div class="company-fact">Факт о компании: ${esc(factLabels[f.field] || f.field)} — ${esc(valueText(f.value))}${(f.sources || []).map(sourceHTML).join('')}</div>`).join('')}
      ${(claim.sources || []).map(sourceHTML).join('')}</div>`).join('')}</section>
    <section class="question-section"><h3>Уточнить эту карточку</h3><p class="muted">Ответ привязан к этой версии обзора. Он не изменит опубликованные выводы.</p>
      <form class="question-form" data-insight="${esc(card.insight_id)}"><input name="question" maxlength="2000" aria-label="Вопрос по карточке" placeholder="Какое допущение стоит проверить первым?" required><button class="secondary">Уточнить</button></form><div class="answer" aria-live="polite"></div>
    </section></div></details></article>`;
}
async function loadReport(id, version = null, refresh = false) {
  const epoch = refresh ? generation : cancelView();
  if (!refresh) { show('reportScreen'); $('#reportBody').innerHTML = ''; pollingUntil = Date.now() + 10 * 60 * 1000; }
  try {
    const [data, versions] = await Promise.all([api(`/reports/${id}${version ? '?version=' + version : ''}`), api(`/reports/${id}/versions`)]);
    if (epoch !== generation) return;
    reportView = {id, version: data.version?.report_version_id};
    $('#reportTitle').textContent = 'Важное за неделю';
    $('#reportMeta').textContent = `${date(data.report.window_start)} — ${date(data.report.window_end)} МСК` + (data.version ? ` · опубликовано ${date(data.version.published_at)}` : '');
    $('#versionSelect').innerHTML = versions.map(v => `<option value="${v.version}" ${v.version === data.version?.version ? 'selected' : ''}>Версия ${v.version} · ${esc(date(v.published_at))}</option>`).join('');
    $('#versionSelect').disabled = !versions.length;
    $('#reportCard').hidden = data.status !== 'preparing';
    $('#reportCard').textContent = 'Подготовка продолжается. Для результата нужны готовый профиль, ranker и работающий insight-worker.';
    const v = data.version;
    $('#reportBody').innerHTML = v ? `<section class="panel ${data.status === 'availability_notice' ? 'warning' : ''}"><span class="eyebrow">${data.status === 'availability_notice' ? 'Уведомление о неполноте — не готовая аналитика' : 'Кратко'}</span><h2>${esc(v.summary)}</h2>
      ${data.company_context_metadata ? `<p class="muted">Профиль компании от ${esc(date(data.company_context_metadata.published_at))}</p>` : ''}
      ${v.limitations?.length ? `<details open><summary>Ограничения данных</summary><ul>${v.limitations.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>` : ''}
      ${v.changes?.length ? `<details><summary>Что изменилось</summary><ul>${v.changes.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>` : ''}</section>${(data.cards || []).map(cardHTML).join('')}${!data.cards?.length && data.status === 'ready' ? '<p class="panel">Обоснованных карточек для этого периода не опубликовано. См. сводку и ограничения.</p>' : ''}` : '';
    if (data.status === 'preparing' || data.status === 'availability_notice') schedule(() => loadReport(id, version, true), epoch);
  } catch (error) { if (epoch === generation) notice('Не удалось открыть обзор: ' + error.message, true); }
}
$('#versionSelect').addEventListener('change', event => { if (reportView) loadReport(reportView.id, Number(event.target.value)); });
async function waitForJob(id, epoch) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (epoch !== generation) throw new Error('Запрос отменён при переходе на другой экран');
    const job = await api('/jobs/' + id);
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw new Error(job.last_error || 'Анализ не завершился');
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  throw new Error('Ответ ещё готовится. Он сохранится в истории анализа.');
}
$('#reportBody').addEventListener('submit', async event => {
  const form = event.target.closest('.question-form'); if (!form || !reportView) return;
  event.preventDefault(); const epoch = generation; const view = {...reportView};
  const input = form.querySelector('input'); const button = form.querySelector('button');
  const answer = form.parentElement.querySelector('.answer');
  input.disabled = button.disabled = true; answer.textContent = 'Проверяем источники…';
  try {
    const job = await api(`/reports/${view.id}/questions`, {method:'POST', body:JSON.stringify({request_id:requestId(), report_version_id:view.version, insight_id:form.dataset.insight, question:input.value.trim()})});
    const result = await waitForJob(job.job_id, epoch);
    if (epoch !== generation) return;
    answer.innerHTML = `<p>${esc(result.answer)}</p>${(result.source_details || []).map(sourceHTML).join('')}`;
  } catch (error) { if (epoch === generation) answer.textContent = 'Не удалось получить ответ: ' + error.message; }
  finally { input.disabled = button.disabled = false; }
});
$('#reportBody').addEventListener('click', async event => {
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
});
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
