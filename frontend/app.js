/* ===== Alpha Business Frontend ===== */
'use strict';

const API = (typeof API_BASE !== 'undefined' ? API_BASE : '/api').replace(/\/+$/, '');

/* --- HTTP helpers --- */
let _token = '';

function getToken() { return _token; }
function setToken(t) { _token = t; try { localStorage.setItem('alpha_token', t); } catch (_) {} }
function loadToken() { try { _token = localStorage.getItem('alpha_token') || ''; } catch (_) {} }

async function api(path, opts = {}) {
  const headers = { 'Authorization': 'Bearer ' + _token, ...opts.headers };
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const res = await fetch(API + '/' + path.replace(/^\/+/, ''), { ...opts, headers });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw { status: res.status, detail: body };
  }
  if (res.status === 204) return null;
  return res.json();
}

/* --- State --- */
let state = {
  token: '',
  userId: null,
  companies: [],
  activeCompanyId: null,
  activeCompany: null,
  reports: [],
  loading: false,
  onboardingTags: [],
  onboardingGoals: [],
  pendingFiles: [],
};

/* --- UI helpers --- */
function $ (sel, parent = document) { return parent.querySelector(sel); }
function $$(sel, parent = document) { return [...parent.querySelectorAll(sel)]; }
function show(id) { $$('.screen').forEach(s => s.classList.remove('active')); const el = $('#' + id); if (el) el.classList.add('active'); }
function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }
function esc(s) { const d = document.createElement('span'); d.textContent = s; return d.innerHTML; }

function spinnerHTML() { return '<div class="spinner"></div>'; }

function formatDate(d) {
  if (!d) return '—';
  const dt = new Date(d);
  return dt.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/* --- Navigation --- */
function navigateTo(screen) { show(screen); }

/* --- Login --- */
$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const token = $('#tokenInput').value.trim();
  if (!token) return;
  setToken(token);
  try {
    const data = await api('/companies');
    if (!data || data.length === 0) {
      showLoginError('Нет доступных компаний');
      return;
    }
    state.token = token;
    state.companies = data;
    state.activeCompanyId = data[0].company_id;
    state.activeCompany = data[0];
    $('#userName').textContent = data[0].display_name || data[0].legal_name || '';
    loadDashboard();
    navigateTo('dashboardScreen');
  } catch (err) {
    if (err.status === 401) {
      setToken('');
      showLoginError('Неверный токен доступа');
    } else {
      showLoginError('Ошибка: ' + (err.detail || 'не удалось подключиться'));
    }
  }
});

function showLoginError(msg) {
  const el = $('#loginError');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 5000);
}

$('#logoutBtn').addEventListener('click', () => {
  setToken('');
  state = { token: '', userId: null, companies: [], activeCompanyId: null, activeCompany: null, reports: [], loading: false, onboardingTags: [], onboardingGoals: [], pendingFiles: [] };
  $('#tokenInput').value = '';
  navigateTo('loginScreen');
});

/* --- Company select --- */
$('#companySelect').addEventListener('change', async (e) => {
  const cid = e.target.value;
  if (!cid) return;
  state.activeCompanyId = cid;
  state.activeCompany = state.companies.find(c => c.company_id === cid) || null;
  $('#userName').textContent = state.activeCompany ? (state.activeCompany.display_name || state.activeCompany.legal_name || '') : '';
  loadDashboard();
});

async function loadCompanySelect() {
  const sel = $('#companySelect');
  sel.innerHTML = '';
  state.companies.forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.company_id;
    opt.textContent = c.legal_name || c.company_id;
    if (c.company_id === state.activeCompanyId) opt.selected = true;
    sel.appendChild(opt);
  });
}

/* --- Dashboard --- */
async function loadDashboard() {
  show('dashboardScreen');
  $('#dashCompany').textContent = state.activeCompany ? state.activeCompany.legal_name || state.activeCompany.company_id : '';
  await loadCompanySelect();
  await loadReports();
  checkOnboarding();
  startProfilePolling();
}

async function loadReports() {
  const loading = true;
  if (loading) { /* simple guard */ }
  try {
    const reports = await api('/companies/' + state.activeCompanyId + '/reports');
    state.reports = reports || [];
    renderReports();
  } catch (err) {
    console.error('Failed to load reports:', err);
  }
}

function renderReports() {
  const grid = $('#reportsList');
  const empty = $('#emptyState');
  grid.innerHTML = '';

  if (state.reports.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  state.reports.forEach(r => {
    const card = el('div', 'report-card', '');
    const version = r.version || {};
    const hasCards = version.summary || version.published_at;
    if (hasCards) card.classList.add('has-insights');

    card.innerHTML = `
      <div class="report-card-header">
        <span class="report-card-title">${hasCards ? '📊 Еженедельный обзор' : '⏳ Подготовка'}</span>
        <span class="report-card-date">${formatDate(r.window_end)}</span>
      </div>
      ${version.summary ? '<div class="report-card-summary">' + esc(version.summary) + '</div>' : ''}
      <div class="report-card-meta">
        ${version.published_at ? '<span>✅ Опубликовано ' + formatDate(version.published_at) + '</span>' : ''}
        ${hasCards ? '<span>📋 Карточек: ' + (r.cards ? r.cards.length : '?') + '</span>' : ''}
        ${r.availability_notice ? '<span>⚠️ Заполнители</span>' : ''}
      </div>
      <div class="report-card-actions">
        <button class="btn-sm open-report" data-id="${r.report_id}">Открыть обзор</button>
        <button class="btn-sm regenerate-report" data-id="${r.report_id}">🔄 Обновить</button>
      </div>
    `;

    card.addEventListener('click', (e) => {
      if (e.target.closest('.btn-sm')) return;
      openReport(r.report_id);
    });
    card.querySelector('.open-report').addEventListener('click', (e) => {
      e.stopPropagation();
      openReport(r.report_id);
    });
    card.querySelector('.regenerate-report').addEventListener('click', (e) => {
      e.stopPropagation();
      regenerateReport(r.report_id);
    });

    grid.appendChild(card);
  });
}

/* Generate report */
$('#generateBtn').addEventListener('click', async () => {
  try {
    const reqId = crypto.randomUUID();
    await api('/companies/' + state.activeCompanyId + '/reports', {
      method: 'POST',
      body: JSON.stringify({ request_id: reqId }),
    });
    $('#generateBtn').disabled = true;
    await loadReports();
    pollReportStatus();
  } catch (err) {
    alert('Ошибка создания отчёта: ' + (err.detail || err));
    $('#generateBtn').disabled = false;
  }
});

async function regenerateReport(reportId) {
  if (!confirm('Пересоздать отчёт для этого периода?')) return;
  try {
    const reqId = crypto.randomUUID();
    const r = await api('/reports/' + reportId);
    if (r) {
      await api('/companies/' + state.activeCompanyId + '/reports', {
        method: 'POST',
        body: JSON.stringify({ request_id: reqId }),
      });
    }
    await loadReports();
    pollReportStatus();
  } catch (err) {
    alert('Ошибка: ' + (err.detail || err));
  }
}

/* Report detail */
let _pollTimer = null;

function pollReportStatus() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const reports = await api('/companies/' + state.activeCompanyId + '/reports');
      state.reports = reports || [];
      renderReports();
      // Check if any report is still being prepared
      const pending = state.reports.some(r => !r.version || !r.version.published_at);
      if (!pending) {
        clearInterval(_pollTimer);
        _pollTimer = null;
      }
    } catch (_) { clearInterval(_pollTimer); _pollTimer = null; }
  }, 3000);
  setTimeout(() => { clearInterval(_pollTimer); _pollTimer = null; }, 5 * 60 * 1000);
}

function startProfilePolling() {
  const check = async () => {
    try {
      const ctx = await api('/companies/' + state.activeCompanyId + '/context');
      if (ctx && ctx.status === 'ready') {
        $('#pendingProfile').style.display = 'none';
        return true;
      }
    } catch (_) {}
    return false;
  };
  const interval = setInterval(async () => {
    if (await check()) clearInterval(interval);
  }, 2000);
  setTimeout(() => clearInterval(interval), 3 * 60 * 1000);
}

async function openReport(reportId) {
  show('reportScreen');
  $('#backToDash').dataset.reportId = reportId;
  $('#reportTitle').textContent = 'Отчёт';
  $('#reportMeta').innerHTML = '';
  $('#reportCard').style.display = 'none';
  $('#reportBody').innerHTML = '';

  try {
    const data = await api('/reports/' + reportId);
    renderReportDetail(data);
  } catch (err) {
    $('#reportBody').innerHTML = '<div class="card error-msg">Ошибка загрузки: ' + esc(err.detail || err) + '</div>';
  }
}

function renderReportDetail(data) {
  if (!data) return;
  const { report, version, cards, status } = data;

  $('#reportTitle').textContent = 'Еженедельный обзор';
  $('#reportMeta').innerHTML =
    '<span>Период: до ' + formatDate(report.window_end) + '</span>' +
    (version && version.published_at ? ' · <span>Опубликовано: ' + formatDate(version.published_at) + '</span>' : '');

  if (status === 'preparing' || (!version)) {
    $('#reportCard').style.display = 'block';
    $('#reportCard').innerHTML = '<div class="spinner"></div><p>Формирую отчёт…</p>';
    pollReportStatus();
    return;
  }

  if (data.availability_notice) {
    const notice = el('div', 'availability-notice', '⚠️ <span>Отчёт сформирован на основе заполнителей — фактические данные ещё не поступили.</span>');
    $('#reportBody').appendChild(notice);
  }

  if (!cards || cards.length === 0) {
    $('#reportBody').innerHTML = '<div class="card"><p>Карточек пока нет.</p></div>';
    return;
  }

  cards.forEach(card => {
    const insight = el('div', 'insight-card', '');
    const claimCount = card.claims ? card.claims.length : 0;

    // Header
    const header = el('div', 'insight-header');
    header.innerHTML = `
      <span class="insight-title"><span class="icon">📌</span> ${esc(card.title)}</span>
      <span class="insight-chevron">▼</span>
    `;

    // Body
    const body = el('div', 'insight-body');

    // Event summary
    if (card.event_summary) {
      const section = el('div', 'insight-section');
      section.innerHTML = '<h4>Событие</h4><p>' + esc(card.event_summary) + '</p>';
      body.appendChild(section);
    }

    // Business impact
    if (card.business_impact) {
      const section = el('div', 'insight-section');
      section.innerHTML = '<h4>Влияние на бизнес</h4><p>' + esc(card.business_impact) + '</p>';
      body.appendChild(section);
    }

    // Claims
    if (card.claims && card.claims.length) {
      const section = el('div', 'insight-section');
      section.innerHTML = '<h4>Утверждения</h4>';
      card.claims.forEach(claim => {
        const cEl = el('div', 'claim-item ' + (claim.kind === 'risk' ? 'risk' : claim.kind === 'opportunity' ? 'opportunity' : claim.kind === 'recommendation' ? 'recommendation' : claim.kind === 'verified_fact' ? '' : ''), '');
        const kindLabel = { risk: '⚠ Риск', opportunity: '💡 Возможность', recommendation: '➜ Рекомендация', verified_fact: '📄 Факт', ai_inference: '🤖 AI-вывод' };
        cEl.innerHTML = '<span class="claim-label">' + (kindLabel[claim.kind] || claim.kind) + '</span> ' + esc(claim.statement);
        section.appendChild(cEl);

        if (claim.sources && claim.sources.length) {
          claim.sources.forEach(src => {
            if (src.excerpt) {
              const cit = el('div', 'citation', '');
              cit.innerHTML = '<span class="citation-title">' + esc(src.source_type) + ':</span> ' + esc(src.excerpt.substring(0, 200));
              cEl.appendChild(cit);
            }
          });
        }
        if (claim.company_facts && claim.company_facts.length) {
          claim.company_facts.forEach(fact => {
            const cEl2 = el('div', 'citation', '');
            cEl2.textContent = '📋 Факт: ' + (fact.field || '') + ' = ' + esc(fact.value || '');
            cEl.appendChild(cEl2);
          });
        }
      });
      body.appendChild(section);
    }

    // Role action
    if (card.details && card.details.role_action) {
      const section = el('div', 'insight-section');
      section.innerHTML = '<h4>Действия для вашей роли</h4><p>' + esc(card.details.role_action) + '</p>';
      body.appendChild(section);
    }

    // Mechanism
    if (card.details && card.details.mechanism) {
      const section = el('div', 'insight-section');
      section.innerHTML = '<h4>Механизм влияния</h4><p>' + esc(card.details.mechanism) + '</p>';
      body.appendChild(section);
    }

    // Limitations
    if (version && version.limitations && version.limitations.length) {
      const section = el('div', 'limitations', '');
      section.innerHTML = '<strong>Ограничения:</strong>';
      version.limitations.forEach(l => {
        section.innerHTML += '<p style="margin-top:4px">• ' + esc(l) + '</p>';
      });
      body.appendChild(section);
    }

    // Ask question section
    const askSection = el('div', 'ask-question-section', '');
    askSection.innerHTML = '<h3>❓ Задать вопрос по отчёту</h3>';
    const qForm = el('form', 'question-form', '');
    qForm.innerHTML = '<input type="text" placeholder="Ваш вопрос…" required/><button type="submit" class="btn-primary" style="padding:8px 16px;font-size:13px;">Спросить</button>';
    askSection.appendChild(qForm);

    const discussionContainer = el('div', 'discussion-messages', { id: 'discussion-' + report.report_id });
    askSection.appendChild(discussionContainer);
    body.appendChild(askSection);

    qForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const qInput = qForm.querySelector('input');
      const q = qInput.value.trim();
      if (!q) return;
      qInput.disabled = true;
      qForm.querySelector('button').disabled = true;

      const userMsg = el('div', 'disc-msg user', '');
      userMsg.innerHTML = '<div class="msg-role">Вы</div>' + esc(q);
      discussionContainer.appendChild(userMsg);

      try {
        const reqId = crypto.randomUUID();
        const result = await api('/reports/' + report.report_id + '/questions', {
          method: 'POST',
          body: JSON.stringify({ request_id: reqId, question: q }),
        });

        const loadingMsg = el('div', 'disc-msg assistant', '');
        loadingMsg.innerHTML = '<div class="msg-role">AI-аналитик</div>' + '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div>';
        discussionContainer.appendChild(loadingMsg);

        // Poll for answer
        const pollAnswer = async () => {
          try {
            const jobData = await api('/jobs/' + result.job_id);
            if (jobData && jobData.status === 'completed' && jobData.result) {
              loadingMsg.innerHTML = '<div class="msg-role">AI-аналитик</div><p>' + esc(jobData.result.answer || 'Ответ готов.') + '</p>';
              qInput.disabled = false;
              qForm.querySelector('button').disabled = false;
              qInput.value = '';
              return;
            } else if (jobData && jobData.status === 'failed') {
              loadingMsg.innerHTML = '<div class="msg-role">AI-аналитик</div><p style="color:var(--red)">Ошибка: ' + esc(jobData.last_error || 'неизвестно') + '</p>';
              qInput.disabled = false;
              qForm.querySelector('button').disabled = false;
              return;
            }
            setTimeout(pollAnswer, 1500);
          } catch (_) { setTimeout(pollAnswer, 1500); }
        };
        pollAnswer();

      } catch (err) {
        loadingMsg.innerHTML = '<div class="msg-role">AI-аналитик</div><p style="color:var(--red)">Ошибка: ' + esc(err.detail || err) + '</p>';
        qInput.disabled = false;
        qForm.querySelector('button').disabled = false;
      }
    });

    header.addEventListener('click', () => {
      insight.classList.toggle('open');
    });

    insight.appendChild(header);
    insight.appendChild(body);
    $('#reportBody').appendChild(insight);
  });
}

$('#backToDash').addEventListener('click', () => {
  loadDashboard();
});

/* --- Onboarding --- */
async function checkOnboarding() {
  try {
    const ctx = await api('/companies/' + state.activeCompanyId + '/context');
    if (ctx && ctx.status === 'ready') {
      $('#onboardingPrompt').style.display = 'none';
      $('#pendingProfile').style.display = 'none';
    } else if (ctx && ctx.status === 'preparing' && (ctx.profile || ctx.user_context)) {
      $('#onboardingPrompt').style.display = 'none';
      $('#pendingProfile').style.display = 'block';
    } else {
      $('#onboardingPrompt').style.display = 'block';
      $('#pendingProfile').style.display = 'none';
    }
  } catch (_) {
    $('#onboardingPrompt').style.display = 'none';
    $('#pendingProfile').style.display = 'block';
  }
}

$('#startOnboardingBtn').addEventListener('click', () => {
  navigateTo('onboardingScreen');
});

$('#backFromOnboarding').addEventListener('click', () => {
  navigateTo('dashboardScreen');
});

/* Tags */
function createTag(text, containerId, inputId, stateKey) {
  const container = $('#' + containerId);
  // Check if tag exists
  if ($('.' + stateKey + '-tag-' + stateKey, container)) return;
  const tag = document.createElement('span');
  tag.className = stateKey + '-tag-' + stateKey + ' tag';
  tag.innerHTML = esc(text) + ' <button type="button">×</button>';
  tag.querySelector('button').addEventListener('click', (e) => {
    e.stopPropagation();
    tag.remove();
    state[stateKey] = state[stateKey].filter(t => t !== text);
  });
  container.appendChild(tag);
  state[stateKey].push(text);
}

$('#tagInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const v = e.target.value.trim();
    if (v) { createTag(v, 'tags', 'tagInput', 'onboardingTags'); e.target.value = ''; }
  }
});

$('#goalInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const v = e.target.value.trim();
    if (v) { createTag(v, 'goals', 'goalInput', 'onboardingGoals'); e.target.value = ''; }
  }
});

/* File upload */
const fileInput = $('#fileInput');
fileInput.addEventListener('change', (e) => {
  Array.from(e.target.files).forEach(f => {
    state.pendingFiles.push(f);
  });
  renderFileList();
  e.target.value = '';
});

function renderFileList() {
  const list = $('#fileList');
  list.innerHTML = '';
  state.pendingFiles.forEach((f, i) => {
    const item = el('div', 'file-item', '');
    const size = f.size < 1024 * 1024 ? (f.size / 1024).toFixed(0) + ' КБ' : (f.size / (1024 * 1024)).toFixed(1) + ' МБ';
    item.innerHTML = `
      <span class="file-name">📄 ${esc(f.name)}</span>
      <span class="file-size">${size}</span>
      <button class="file-remove" type="button">×</button>
    `;
    item.querySelector('.file-remove').addEventListener('click', () => {
      state.pendingFiles.splice(i, 1);
      renderFileList();
    });
    list.appendChild(item);
  });
}

/* Submit onboarding */
$('#onboardingForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const role = $('#obRole').value.trim();
  if (!role) { $('#obRole').focus(); return; }

  const submitBtn = $('#obSubmit');
  const status = $('#obSubmitStatus');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Отправка…';
  status.className = 'submit-status loading';
  status.style.display = 'block';
  status.textContent = 'Сохраняю анкету и документы…';

  try {
    // 1. Upload files first
    const documentIds = [];
    for (const file of state.pendingFiles) {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('title', file.name);
      const result = await api('/companies/' + state.activeCompanyId + '/documents', {
        method: 'POST',
        headers: {}, // let browser set Content-Type with boundary
        body: formData,
      });
      if (result && result.document_id) documentIds.push(result.document_id);
    }

    // 2. Submit onboarding answers
    const answers = {
      role: role,
      responsibilities: state.onboardingTags,
      goals: state.onboardingGoals,
      note: $('#obNote').value.trim(),
    };
    const result = await api('/companies/' + state.activeCompanyId + '/onboarding', {
      method: 'POST',
      body: JSON.stringify(answers),
    });

    status.className = 'submit-status success';
    status.textContent = '✅ Анкета сохранена. Контекст компании формируется — вернитесь к обзору через 1–2 минуты.';

    // Clear state
    state.onboardingTags = [];
    state.onboardingGoals = [];
    state.pendingFiles = [];
    $('#tags').innerHTML = '';
    $('#goals').innerHTML = '';
    $('#fileList').innerHTML = '';
    $('#onboardingForm').reset();

    // Go back and reload
    setTimeout(() => {
      navigateTo('dashboardScreen');
      loadDashboard();
    }, 2000);

  } catch (err) {
    status.className = 'submit-status error';
    status.textContent = '❌ Ошибка: ' + (err.detail || err);
    submitBtn.disabled = false;
    submitBtn.textContent = 'Сохранить и сформировать профиль';
  }
});

/* --- Init: local demo needs no manual token provisioning. --- */
async function initialize() {
  loadToken();
  let demo = null;
  try {
    demo = await api('/demo/session', { method: 'POST' });
    setToken(demo.token);
  } catch (err) {
    if (err.status !== 404) {
      navigateTo('loginScreen');
      showLoginError('Не удалось открыть демо: ' + (err.detail || 'backend недоступен'));
      return;
    }
  }
  if (!getToken()) { navigateTo('loginScreen'); return; }
  try {
    const data = await api('/companies');
    if (!data || !data.length) throw new Error('Нет доступных компаний');
    state.token = getToken();
    state.companies = data;
    state.activeCompanyId = data[0].company_id;
    state.activeCompany = data[0];
    $('#userName').textContent = demo ? 'Демо-пользователь · тестовый режим' : '';
    $('#logoutBtn').hidden = Boolean(demo);
    await loadDashboard();
  } catch (err) {
    if (err.status === 401) setToken('');
    navigateTo('loginScreen');
    showLoginError('Не удалось загрузить компании: ' + (err.detail || err.message || 'ошибка API'));
  }
}
initialize();
