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
  $('#dashCompany').textContent = state.companies.find(c => c.company_id === company)?.legal_name || company;
  try {
    const [reports, context, documents] = await Promise.all([
      api(`/companies/${encodeURIComponent(company)}/reports`), api(`/companies/${encodeURIComponent(company)}/context`), api(`/companies/${encodeURIComponent(company)}/documents`)
    ]);
    if (epoch !== generation || company !== state.activeCompanyId) return;
    Object.assign(state, {reports, context, documents});
    renderReports(); renderDocuments();
    const ready = context.status === 'ready';
    $('#generateBtn').disabled = !ready;
    $('#onboardingPrompt').hidden = ready;
    const job = context.profile_job;
    $('#pendingProfile').hidden = !job || !['pending','running'].includes(job.status);
    $('#contextStatus').textContent = ready ? `Ваш фокус: ${context.user_context?.business_role || 'настроен'} · профиль компании v${context.profile?.profile_version || '—'}` : job?.status === 'failed' ? 'Профиль не подготовлен: откройте настройки и повторите сохранение' : 'Укажите роль и задачи, чтобы персонализировать обзор';
    const latest = reports.find(row => row.published_at && !row.availability_notice);
    $('#overviewSummary').textContent = latest?.summary || 'Значимые события, возможные последствия и следующий шаг для вашей роли.';
    if (!ready || reports.some(row => !row.published_at || row.availability_notice)) schedule(() => loadDashboard(false), epoch);
  } catch (error) { if (epoch === generation) notice('Не удалось обновить сводку: ' + error.message, true); }
}
async function useCompanies(data, demo = false) {
  if (!data.length) throw new Error('Нет доступных компаний');
  state.companies = data; state.activeCompanyId = data[0].company_id; state.demo = demo;
  $('#companySelect').innerHTML = data.map(c => `<option value="${esc(c.company_id)}">${esc(c.legal_name || c.company_id)}</option>`).join('');
  $('#companySelect').value = state.activeCompanyId;
  $('#userName').textContent = demo ? 'Демо-пользователь · тестовый режим' : 'Мой профиль';
  $('#logoutBtn').hidden = demo;
  await loadDashboard();
}
async function initialize() {
  loadToken();
  let demo = false;
  try {
    const session = await api('/demo/session', {method:'POST'});
    setToken(session.token); demo = true;
  } catch (error) {
    if (error.status !== 404) { $('#loginError').textContent = 'Не удалось открыть демо: ' + error.message; return; }
  }
  if (!_token) return;
  try { await useCompanies(await api('/companies'), demo); }
  catch (error) { $('#loginError').textContent = error.message; show('loginScreen'); }
}
$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault(); setToken($('#tokenInput').value.trim());
  try { await useCompanies(await api('/companies')); } catch (error) { $('#loginError').textContent = error.message; }
});
$('#logoutBtn').addEventListener('click', () => {
  cancelView(); setToken('');
  state = {companies: [], activeCompanyId: null, reports: [], context: null, documents: [], demo: false};
  pendingFiles = []; reportView = null;
  ['#reportBody', '#reportsList', '#documentsList', '#fileList', '#companySelect'].forEach(id => { $(id).innerHTML = ''; });
  $('#onboardingForm').reset(); notice(''); show('loginScreen');
});
$('#companySelect').addEventListener('change', event => {
  state.activeCompanyId = event.target.value; state.context = null; state.documents = []; pendingFiles = [];
  $('#documentsList').innerHTML = ''; $('#fileList').innerHTML = ''; notice(''); loadDashboard();
});
$('#homeBtn').addEventListener('click', () => loadDashboard());
$('#analyticsBtn').addEventListener('click', () => loadDashboard());
$('#backToDash').addEventListener('click', () => loadDashboard());
$('#backFromOnboarding').addEventListener('click', () => loadDashboard());
function settingsView() {
  cancelView(); show('onboardingScreen'); notice('');
  const answers = state.context?.user_context?.raw_answers || {};
  $('#obRole').value = answers.role || '';
  $('#obResponsibilities').value = (answers.responsibilities || []).join('\n');
  $('#obGoals').value = (answers.goals || []).join('\n'); $('#obNote').value = answers.note || '';
  $('#obSubmit').disabled = false; $('#obSubmitStatus').textContent = ''; renderDocuments();
}
$('#startOnboardingBtn').addEventListener('click', settingsView);
$('#settingsBtn').addEventListener('click', settingsView);
$('#reportsList').addEventListener('click', event => { const target = event.target.closest('[data-report]'); if (target) loadReport(target.dataset.report); });
$('#generateBtn').addEventListener('click', async () => {
  const company = state.activeCompanyId; const epoch = generation; $('#generateBtn').disabled = true;
  try {
    const result = await api(`/companies/${encodeURIComponent(company)}/reports`, {method:'POST', body:JSON.stringify({request_id:crypto.randomUUID()})});
    if (epoch !== generation) return;
    notice(result.status === 'awaiting_profile' ? 'Сначала нужно завершить подготовку профиля.' : 'Обзор поставлен в очередь. Можно продолжать работу — результат сохранится в сводке.');
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
      ${data.company_context_metadata ? `<p class="muted">Профиль компании v${data.company_context_metadata.profile_version} от ${esc(date(data.company_context_metadata.published_at))}</p>` : ''}
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
    const job = await api(`/reports/${view.id}/questions`, {method:'POST', body:JSON.stringify({request_id:crypto.randomUUID(), report_version_id:view.version, insight_id:form.dataset.insight, question:input.value.trim()})});
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
    const answers = {role:$('#obRole').value.trim(), responsibilities:lines('#obResponsibilities',8,200), goals:lines('#obGoals',3,500), note:$('#obNote').value.trim()};
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
