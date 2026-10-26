'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token = '';
let timer = null;
try { token = sessionStorage.getItem('alpha_admin_token') || ''; } catch (_) {}

async function admin(path) {
  const response = await fetch('/api/admin/' + path, {headers: {Authorization: 'Bearer ' + token}});
  if (!response.ok) throw new Error(response.status === 401 ? 'Неверный ADMIN_TOKEN' : 'HTTP ' + response.status);
  return response.json();
}
function when(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  const age = Math.round((Date.now() - date.getTime()) / 1000);
  const ago = age < 0 ? '' : age < 90 ? `${age} с назад` : age < 5400 ? `${Math.round(age / 60)} мин назад` : age < 172800 ? `${Math.round(age / 3600)} ч назад` : `${Math.round(age / 86400)} д назад`;
  return `<span title="${esc(date.toISOString())}">${esc(ago || date.toLocaleString('ru-RU'))}</span>`;
}
function seconds(value) {
  if (value === null || value === undefined) return '—';
  const s = Number(value);
  return s < 120 ? `${s} с` : s < 7200 ? `${Math.round(s / 60)} мин` : `${(s / 3600).toFixed(1)} ч`;
}
function state(value) { return `<span class="state ${esc(value)}">${esc(value)}</span>`; }
function table(rows, columns, rowAttrs = () => '') {
  if (!rows?.length) return '<p class="muted">Нет данных</p>';
  return `<table><thead><tr>${columns.map(c => `<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(row =>
    `<tr ${rowAttrs(row)}>${columns.map(c => { const v = c[2] ? c[2](row[c[1]], row) : esc(row[c[1]] ?? '—'); return `<td class="${typeof row[c[1]] === 'number' && !c[2] ? 'num' : ''}">${v}</td>`; }).join('')}</tr>`).join('')}</tbody></table>`;
}

function render(snapshot) {
  $('#services').innerHTML = snapshot.services.map(s => `<div class="badge ${esc(s.status)}" title="${esc(s.url)}"><b>${esc(s.name)}</b>
    <small>${esc(s.status)}${s.latency_ms !== undefined ? ' · ' + esc(s.latency_ms) + ' мс' : ''}${s.detail ? ' · ' + esc(s.detail) : ''}</small></div>`).join('');
  const workers = snapshot.workers;
  $('#missingWorkers').hidden = !workers.missing_roles.length;
  $('#missingWorkers').textContent = 'Нет живых воркеров для ролей: ' + workers.missing_roles.join(', ') + ' — задачи этих типов не будут выполняться.';
  $('#workers').innerHTML = table(workers.items, [
    ['Воркер', 'worker_id'], ['Роль', 'role'], ['Жив', 'alive', v => state(v ? 'ok' : 'down')],
    ['Последний сигнал', 'last_seen', when], ['Аптайм', 'uptime_s', seconds],
    ['Выполняет', 'running_jobs', v => (v || []).map(j => esc(j.kind)).join(', ') || '—'],
    ['Готово', 'processed'], ['Ошибок', 'failed'],
  ]);
  $('#queues').innerHTML = table(snapshot.queues, [
    ['Тип', 'kind'], ['Готовы', 'ready'], ['Повтор', 'retry_scheduled'], ['В работе', 'running'],
    ['Ошибки 24ч', 'failed_24h'], ['Готово 1ч', 'completed_1h'], ['Старейшая', 'oldest_pending_s', seconds],
    ['Ожидание', 'avg_wait_s', seconds], ['Среднее', 'avg_run_s', seconds], ['p95', 'p95_run_s', seconds],
  ]);
  $('#failures').innerHTML = table(snapshot.failures, [
    ['Тип', 'kind'], ['Статус', 'status', state], ['Попытки', 'attempts', (v, r) => `${esc(v)}/${esc(r.max_attempts)}`],
    ['Компания', 'company_id'], ['Ошибка', 'last_error', v => `<span class="err-text">${esc(v)}</span>`],
    ['Когда', 'finished_at', (v, r) => when(v || r.available_at)],
  ]);
  $('#agents').innerHTML = table(snapshot.agents, [
    ['Агент', 'agent_name'], ['Запуски', 'runs'], ['Успех', 'completed'], ['Ошибки', 'failed'], ['Идут', 'running'],
    ['Среднее', 'avg_duration_s', seconds], ['p95', 'p95_duration_s', seconds], ['Токены', 'tokens'],
    ['Инстр./запуск', 'avg_tool_calls'], ['Исправл. валидации', 'validation_errors'], ['Ошибки инстр.', 'tool_errors'],
  ]);
  $('#tools').innerHTML = table(snapshot.tools, [
    ['Инструмент', 'tool_name'], ['Вызовы', 'calls'], ['Ошибки', 'errors'], ['Ср. результатов', 'avg_results'], ['Последний', 'last_call', when],
  ]);
  const data = snapshot.data, news = data.news;
  $('#data').innerHTML = `<p><b>Новости:</b> ${esc(news.total)} статей, за 24 ч ${esc(news.fetched_24h)}, опубликовано за 7 дн ${esc(news.published_7d)},
    дубликатов ${esc(news.duplicates)}; версий ${esc(news.revisions)}, с эмбеддингами ${esc(news.embedded_revisions)}; последнее поступление ${when(news.last_fetch)}</p>
    ${table(news.sources, [['Источник', 'source_name'], ['Статей', 'articles'], ['За 24ч', 'fetched_24h'], ['Последнее поступление', 'last_fetch', when], ['Последняя публикация', 'last_published', when]])}
    <p><b>Документы:</b> ${data.documents.map(d => `${esc(d.source_type)}/${esc(d.processing_status)}: ${esc(d.count)}`).join(', ') || 'нет'}
    · <b>RAG-фрагменты:</b> ${esc(data.rag_chunks.total)}, без эмбеддинга ${esc(data.rag_chunks.without_embedding)}</p>
    <p><b>Профили:</b> компаний ${esc(data.profiles.company_profiles)}, контекстов пользователей ${esc(data.profiles.user_contexts)}, неудачных ${esc(data.profiles.failed_profiles)}
    · <b>Ранжирование 7 дн:</b> ${data.ranking.map(r => `${esc(r.status)} ${esc(r.count)}`).join(', ') || 'нет'}
    · <b>Отчёты 7 дн:</b> ${esc(data.reports.reports_7d)}, опубликовано ${esc(data.reports.published_7d)}, бюллетеней о неполноте ${esc(data.reports.deadline_bulletins)}</p>`;
  $('#updated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
}


function accessMode(isAdmin) {
  for (const id of ['failureSection', 'agentSection', 'runSection', 'toolSection', 'dataSection', 'prometheusSection']) {
    $('#' + id).hidden = !isAdmin;
  }
  $('#accessMode').textContent = isAdmin
    ? 'Режим администратора: подробная диагностика и трассы агентов.'
    : 'Общий обзор тестового стенда. Данные компаний и содержимое запросов здесь не раскрываются.';
}
function renderPublic(snapshot) {
  const names = {'postgres': 'База данных', 'tool-gateway': 'Поиск и инструменты', 'embeddings': 'Поиск по смыслу',
    'retrieval-ranking': 'Отбор новостей', 'bank-materials-source': 'Аналитика банка', 'searxng': 'Поиск в интернете', 'llm': 'ИИ-аналитик'};
  const statuses = {'ok': 'Работает', 'down': 'Недоступен', 'error': 'Ошибка', 'not_configured': 'Не настроен', 'configured': 'Настроен; готовность проверяется при запросе'};
  const roles = {'backend': 'Планировщик', 'documents': 'Документы', 'profiler': 'Профили компаний', 'insight': 'Инсайты и ответы'};
  const kinds = {'profile': 'Обновление профиля', 'question': 'Ответ на вопрос', 'document': 'Обработка документа',
    'ranking': 'Отбор новостей', 'insight': 'Подготовка инсайтов'};
  $('#services').innerHTML = snapshot.services.map(s => `<div class="badge ${esc(s.status)}"><b>${esc(names[s.name] || s.name)}</b>
    <small>${esc(statuses[s.status] || s.status)}${s.latency_ms !== undefined ? ' · ' + esc(s.latency_ms) + ' мс' : ''}</small></div>`).join('');
  const missing = snapshot.workers.filter(w => !w.alive);
  $('#missingWorkers').hidden = !missing.length;
  $('#missingWorkers').textContent = 'Ожидаем подключения: ' + missing.map(w => roles[w.role] || w.role).join(', ') + '.';
  $('#workers').innerHTML = table(snapshot.workers, [
    ['Назначение', 'role', v => esc(roles[v] || v)], ['Состояние', 'alive', v => v ? 'Работает' : 'Нет сигнала'],
    ['В работе', 'running'], ['Готово', 'processed'], ['Ошибок', 'failed'],
  ]);
  $('#queues').innerHTML = table(snapshot.queues, [
    ['Задача', 'kind', v => esc(kinds[v] || v)], ['Ожидают', 'ready'], ['В работе', 'running'],
    ['Повторная попытка', 'retry_scheduled'], ['Готово за час', 'completed_1h'], ['Ошибок за сутки', 'failed_24h'],
    ['Среднее время', 'avg_run_s', seconds],
  ]);
  $('#updated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
}

async function loadRuns() {
  if (!token) return;
  const params = new URLSearchParams({limit: '30'});
  for (const [key, id] of [['agent', '#runAgent'], ['status', '#runStatus'], ['company_id', '#runCompany']]) {
    if ($(id).value.trim()) params.set(key, $(id).value.trim());
  }
  const runs = await admin('agent-runs?' + params);
  $('#runs').innerHTML = table(runs, [
    ['Старт', 'started_at', when], ['Агент', 'agent_name'], ['Статус', 'status', state], ['Компания', 'company_id'],
    ['Длит.', 'duration_s', seconds], ['Шаги', 'steps'], ['Ошибки шагов', 'errors'], ['Инстр.', 'tool_calls'], ['Токены', 'llm_tokens'], ['Модель', 'llm_model'],
  ], row => `class="clickable" data-run="${esc(row.agent_run_id)}"`);
}
async function showTrace(id) {
  if (!token) return;
  const trace = await admin('agent-runs/' + encodeURIComponent(id));
  const run = trace.run;
  $('#trace').innerHTML = `<h3>Трасса ${esc(run.agent_name)} · ${esc(run.agent_run_id)} · ${state(run.status)}</h3>
    <details><summary>Входной снимок (input_snapshot)</summary><pre>${esc(JSON.stringify(run.input_snapshot, null, 2))}</pre></details>
    ${table(trace.steps, [
      ['#', 'step_no'], ['Тип', 'step_kind'], ['Инструмент', 'tool_name'], ['Статус', 'status', state],
      ['Аргументы', 'arguments', v => v ? `<pre>${esc(JSON.stringify(v, null, 1))}</pre>` : '—'],
      ['Результат', 'result_summary', v => v ? `<details><summary>${esc(String(v).slice(0, 80))}…</summary><pre>${esc(v)}</pre></details>` : '—'],
      ['Время', 'started_at', when],
    ])}
    <h3>Аудит инструментов</h3>${table(trace.tool_audit, [['Инструмент', 'tool_name'], ['Статус', 'status', state], ['Результатов', 'result_count'], ['Аргументы', 'arguments', v => `<pre>${esc(JSON.stringify(v))}</pre>`], ['Время', 'created_at', when]])}`;
  $('#trace').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function refresh() {
  clearTimeout(timer);
  accessMode(Boolean(token));
  try {
    if (token) {
      render(await admin('observability'));
      await loadRuns();
    } else {
      const response = await fetch('/api/dev/overview');
      if (!response.ok) throw new Error(response.status === 404
        ? 'Общий обзор доступен только на тестовом стенде. Для подробной диагностики откройте доступ администратора.'
        : 'HTTP ' + response.status);
      renderPublic(await response.json());
    }
    $('#error').hidden = true;
  } catch (error) {
    $('#error').hidden = false; $('#error').textContent = 'Не удалось получить метрики: ' + error.message;
  }
  if ($('#autoRefresh').checked) timer = setTimeout(refresh, 5000);
}

$('#tokenInput').value = token;
$('#tokenForm').addEventListener('submit', event => {
  event.preventDefault(); token = $('#tokenInput').value.trim();
  try { sessionStorage.setItem('alpha_admin_token', token); } catch (_) {}
  refresh();
});
$('#publicMode').addEventListener('click', () => {
  token = ''; $('#tokenInput').value = '';
  try { sessionStorage.removeItem('alpha_admin_token'); } catch (_) {}
  refresh();
});
$('#autoRefresh').addEventListener('change', refresh);
$('#runFilter').addEventListener('submit', event => { event.preventDefault(); loadRuns().catch(error => { $('#error').hidden = false; $('#error').textContent = error.message; }); });
$('#runs').addEventListener('click', event => {
  const row = event.target.closest('[data-run]');
  if (row) showTrace(row.dataset.run).catch(error => { $('#error').hidden = false; $('#error').textContent = error.message; });
});
refresh();
