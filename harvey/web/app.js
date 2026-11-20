let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = mode;
}

function cycleTheme() {
  const now = localStorage.getItem('harvey-theme') || 'auto';
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  try { localStorage.setItem('harvey-theme', next); } catch { /* private mode */ }
  applyTheme(next);
}

(function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('harvey-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  return (data && (data.message || data.error || data.detail)) || fallback;
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function safeHref(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? escHtml(s) : '';
}

// ── One status vocabulary ──
//
// Harvey's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new:            ['New', 'active'],
  contacted:      ['Contacted', 'active'],
  replied:        ['Replied', 'good'],
  interested:     ['Interested', 'good'],
  meeting:        ['Meeting booked', 'note'],
  not_interested: ['Not interested', 'bad'],
  bounced:        ['Bounced', 'bad'],
  write_failed:   ["Couldn't write", 'bad'],
  queued:         ['Queued', 'active'],
  opted_out:      ['Opted out', 'bad'],
  // campaigns
  draft:          ['Draft', 'waiting'],
  active:         ['Active', 'good'],
  completed:      ['Completed', 'idle'],
  paused:         ['Paused', 'bad'],
  // conversations
  open:           ['Open', 'active'],
  closed:         ['Closed', 'idle'],
  closed_won:     ['Won', 'good'],
  closed_lost:    ['Lost', 'bad'],
  objection:      ['Objection', 'waiting'],
  question:       ['Question', 'active'],
  ooo:            ['Out of office', 'idle'],
  wrong_person:   ['Wrong person', 'waiting'],
  unsubscribe:    ['Unsubscribed', 'bad'],
  escalate:       ['Needs a human', 'bad'],
  needs_human:    ['Needs you', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved:       ['Approved', 'good'],
  sending:        ['Sending now', 'active'],
  scheduled:      ['Scheduled', 'active'],
  sent:           ['Sent', 'good'],
  awaiting_reply: ['Awaiting reply', 'active'],
  failed:         ['Failed', 'bad'],
  rejected:       ['Rejected', 'idle'],
  cancelled:      ['Cancelled', 'idle'],
  // signals
  proposed:       ['Waiting on you', 'waiting'],
  confirmed:      ['Confirmed', 'good'],
  // email deliverability
  verified:       ['Verified', 'good'],
  risky:          ['Catch-all', 'waiting'],
  guess:          ['Unverified', 'idle'],
  invalid:        ['Invalid', 'bad'],
  // runs
  running:        ['Running', 'active'],
  stale:          ['Stale', 'bad'],
  stalled:        ['Stopped', 'bad'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = own(STATUS, key);
  if (hit) return { label: hit[0], tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let t = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(t) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';  // stored UTC
    const dt = new Date(t);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let t = String(d || '').trim().replace(' ', 'T');
  if (t && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';
  return new Date(t);
}

// "3m ago", "5h ago", "Yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  if (mins < -1 && mins > -60) return 'in ' + -mins + 'm';
  if (mins < -1 && mins > -60 * 24) return 'in ' + Math.round(-mins / 60) + 'h';
  if (mins >= -1 && mins < 1) return 'just now';
  if (mins >= 1 && mins < 60) return mins + 'm ago';
  if (mins >= 60 && mins < 60 * 24) return Math.round(mins / 60) + 'h ago';
  if (mins >= 60 * 24 && mins < 60 * 48) return 'Yesterday';
  const opts = {month: 'short', day: 'numeric'};
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return dt.toLocaleDateString('en-US', opts);
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return dt.toLocaleDateString('en-US', {weekday: 'long', month: 'short', day: 'numeric'});
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || 'trend') + '">' +
    vals.map((v, i) => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({length: n}, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'Team page', known_company_backfill: 'Team page',
  scraped_mailto: 'Published address', hunter_domain_search: 'Hunter', linkedin_search: 'LinkedIn',
  job_board: 'Job board', web_search: 'Web search', company_inbox: 'Company inbox',
  osm: 'OpenStreetMap', dataforseo_listings: 'DataForSEO', dataforseo_serp: 'DataForSEO SERP', dataforseo_maps: 'DataForSEO Maps',
  serper: 'Serper', tinyfish: 'TinyFish', scavio_maps: 'Scavio Maps', scavio_search: 'Scavio Search',
};
function sourceLabel(s) {
  if (!s) return '';
  return escHtml(Object.prototype.hasOwnProperty.call(SOURCE_LABELS, s) ? SOURCE_LABELS[s]
    : String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh'};
// One plain sentence per logged action, from the details each agent records.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = (v, one, many) => (Number(v) || 0) + ' ' + ((Number(v) || 0) === 1 ? one : many);
  // Harvey's own check-ins log what it decided to do that cycle, no details.
  if (a.agent === 'main') return ({
    idle: 'Checked in, nothing to do', prospect: 'Checked in and looked for new leads',
    write_campaign: 'Checked in and wrote emails', send_campaign: 'Checked in and sent emails',
  })[a.action_type] || 'Checked in: ' + escHtml(String(a.action_type || '').replace(/_/g, ' '));
  switch (a.action_type) {
    case 'prospect': return Number(d.prospects_found) ? 'Found ' + n(d.prospects_found, 'new contact', 'new contacts')
      : 'Searched for leads, nothing new this time';
    case 'manual_reply': return 'You replied to ' + (who || 'a contact');
    case 'conversation_reply_mode': return d.mode === 'auto'
      ? 'You let AI answer a conversation on its own'
      : d.mode === 'manual' ? 'You took over a conversation (AI won’t reply)'
      : 'You set a conversation to “AI drafts, you approve”';
    case 'lead_approval': return 'You ' + ({approved: 'approved', rejected: 'rejected', pending: 'moved back to waiting'}[d.approval] || escHtml(d.approval || 'changed')) +
      ' ' + n(d.count, 'lead', 'leads');
    case 'write_campaign': return 'Wrote “' + camp + '” for ' + n(d.prospect_count, 'contact', 'contacts');
    case 'write_failed': return 'Couldn’t write for some contacts' + (camp ? ' in “' + camp + '”' : '');
    case 'stage_campaign': return 'Queued ' + n(d.staged, 'email', 'emails') + ' from “' + camp + '”' +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return 'Sent “' + camp + '” to Instantly · ' + n(d.leads_added, 'lead', 'leads');
    case 'email_sent': return d.kind === 'reply' ? 'Sent a reply to ' + who
      : 'Sent email ' + escHtml(String(d.step || 1)) + ' to ' + who;
    case 'reply': return 'Answered ' + who + intent;
    case 'reply_queued': return 'Drafted a reply to ' + who + intent;
    case 'opt_out': return who + ' opted out' + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return 'You undid ' + (who || 'a contact') + '’s opt-out';
    case 'reply_after_opt_out': return who + ' wrote again after opting out · no reply sent';
    case 'escalation': return 'Flagged ' + who + ' for you' + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return 'Email to ' + who + ' bounced';
    case 'manual_send': return 'You marked a contact as emailed by hand';
    case 'manual_send_undone': return 'You undid a hand-sent mark';
    case 'outbox_edited': return 'You edited a draft';
    case 'conversation_status':
      return d.to === 'closed' ? 'You closed a conversation'
        : d.from === 'needs_human' ? 'You handed a conversation back to Harvey' : 'You reopened a conversation';
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? 'Heartbeat: ' + text : text;
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>harvey dashboard</b> and refresh this page.');
}

async function api(path, opts) {
  try {
    const r = await authFetch(path, opts);
    if (await lockIfRefused(r)) return null;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? {success: false, ...body} : null;
    }
    return await r.json();
  } catch {
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? 'The dashboard server can\'t use its token file, so it is refusing every request. ' + why
    : authToken()
      ? 'Your session ended. Please sign in again.'
      : '');
  return true;
}

function showLock(reason) {
  const wasLocked = _locked;
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  if (!wasLocked) initAuthMode();
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = 'Locked';
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

// ── Sign in (the first visit creates the one account) ──

let _authMode = 'login';

async function initAuthMode() {
  try {
    const r = await authFetch('/auth/status');
    const s = await r.json();
    setAuthMode(s.signup_open ? 'signup' : 'login');
  } catch {
    setAuthMode('login');
  }
}

function setAuthMode(mode) {
  _authMode = mode;
  const signup = mode === 'signup';
  document.getElementById('auth-title').textContent = signup ? 'Create your account' : 'Sign in';
  document.getElementById('auth-sub').textContent = signup
    ? 'Set up a username and password for this dashboard.'
    : 'Welcome back. Sign in to your sales dashboard.';
  document.getElementById('auth-submit').textContent = signup ? 'Create account' : 'Sign in';
  document.getElementById('auth-confirm-group').style.display = signup ? '' : 'none';
  document.getElementById('auth-confirm').required = signup;
  document.getElementById('auth-password').autocomplete = signup ? 'new-password' : 'current-password';
  document.getElementById('auth-username').focus();
}

function switchAuthMode(mode) {
  document.getElementById('lock-reason').textContent = '';
  setAuthMode(mode);
}

async function submitAuth(event) {
  event.preventDefault();
  const err = document.getElementById('lock-reason');
  const btn = document.getElementById('auth-submit');
  const username = document.getElementById('auth-username').value.trim();
  const pw = document.getElementById('auth-password');
  const confirm = document.getElementById('auth-confirm');
  err.textContent = '';
  if (_authMode === 'signup' && pw.value !== confirm.value) {
    err.textContent = 'The passwords don\'t match.';
    return;
  }
  btn.disabled = true;
  try {
    const r = await authFetch('/auth/' + (_authMode === 'signup' ? 'signup' : 'login'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password: pw.value}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || !body.token || !setAuthToken(body.token)) {
      err.textContent = body.message || 'Something went wrong. Try again.';
      return;
    }
    pw.value = ''; confirm.value = '';
    hideLock();
    loadCurrentTab();
    loadHarveyStatus();
  } catch {
    err.textContent = 'Can\'t reach the dashboard server. Is it running?';
  } finally {
    btn.disabled = false;
  }
}

function signOut() {
  clearAuthToken();
  showLock('');
  switchAuthMode('login');
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast('Export failed.', 'error'); return; }
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast('Export failed — is the dashboard still running?', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

// Lead settings has tabs: the search form, then the old Settings, API
// Integration and Controls pages.
const LEADS_PANES = ['find', 'integrations', 'controls', 'settings'];
let _leadsPane = 'find';

function setLeadsPane(pane) {
  _leadsPane = LEADS_PANES.includes(pane) ? pane : 'find';
  LEADS_PANES.forEach(p => {
    document.getElementById('leads-pane-' + p).hidden = p !== _leadsPane;
    const b = document.getElementById('leads-tab-' + p);
    b.classList.toggle('on', p === _leadsPane);
    b.setAttribute('aria-selected', p === _leadsPane);
  });
  if (_leadsPane === 'settings') { loadSettings(); loadSendingModeCard(); }
  else if (_leadsPane === 'integrations') loadIntegrations();
  else if (_leadsPane === 'controls') { loadHarveyStatus(); loadLogs(); }
  else loadLeadRequest();
}

function showTab(id, btn) {
  // Settings and API Integration now live in Lead settings; old links still work.
  if (id === 'settings' || id === 'integrations' || id === 'controls') {
    _leadsPane = id;
    id = 'leads';
    btn = btn || [...document.querySelectorAll('nav button')].find(b => (b.getAttribute('onclick') || '').includes("'leads'"));
  } else if (id === 'leads' && btn) {
    _leadsPane = 'find';
  }
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'today': loadLiveStatus(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadRuns(); loadTodayActivity(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'leads': setLeadsPane(_leadsPane); break;
    case 'found': loadFoundLeads(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'sitewatch': loadSitewatch(true); break;
    case 'templates': loadTemplate(); break;
  }
}

// ── Today: what needs a human ──

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), 'Couldn\'t check what needs you',
      'The dashboard hit an error reading Harvey\'s data: ' + escHtml(data.error));
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-found', (data.stats || {}).leads_pending || 0);
  navCount('nav-conversations', convoBadge(data.stats));
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">Nothing needs you</div>' +
      '<div class="qdetail">Harvey has everything it needs. Anything that requires a ' +
      'decision — an email to approve, a signal to confirm — shows up here.</div>' +
      '</div></div>';
    return;
  }

  const glyph = {bad: 'alert', warn: 'flag', good: 'check'};
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
        '<div class="qtitle">' + escHtml(it.title) + '</div>' +
        '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
        escHtml(it.action) + icon('arrow') + '</button>' +
    '</div>';
  }).join('') + '</div>';
}

// ── Live status: the "what's happening?" answer, kept on screen ──

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = "Can't reach Harvey right now — numbers below may be old.";
    else el.innerHTML = '<p class="muted live-flag">Can\'t reach Harvey right now. ' +
      'Is the dashboard window still open?</p>';
    return;
  }
  const glyph = {done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: ''};
  const fill = {done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)'};
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(s.label) + '</span>' +
      '<span class="step-detail">' + escHtml(s.detail) + '</span>' + bar +
    '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? 'Reading websites'
    : r.stage === 'discover' ? 'Searching for businesses (' + (r.provider || '') + ')'
    : String(r.stage || 'Job');
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
        ' · ' + r.elapsed_min + ' min' + (r.eta_text ? ' · ' + escHtml(r.eta_text) : '') + '</span>').join('')
    : '<span class="live-now">Nothing running right now</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
      (d.stopped || []).map(r => escHtml(jobName(r))).join(', ') + ' stopped before finishing.</p></div>'
    : '';
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
      '<span class="setup-pct">Cost so far $' + Number(d.cost_usd || 0).toFixed(2) + '</span></div>' +
    stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (d.next ? '<div class="live-next">' + icon('arrow') + '<span><b>Next</b> · ' + escHtml(d.next) + '</span>' +
      (d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">Go</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 10 seconds</div>';
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const el = document.getElementById('today-kpis');
  if (!el) return;
  const t = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note) =>
    '<div class="kpi"><div class="kpi-label">' + label + '</div>' +
    '<div class="kpi-main"><span class="kpi-value' + (value ? '' : ' zero') + '">' + value + '</span>' + spark + '</div>' +
    '<span class="kpi-note">' + note + '</span></div>';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const sent = stats.email_provider === 'instantly'
    ? card('Emails sent', '&mdash;', '', 'Instantly tracks sends')
    : card('Emails sent', sum(t.sent), sparkBars(t.sent, 'Emails sent per day, last 14 days'),
           'last 14 days · <b>' + (stats.outbox_approved || 0) + '</b> scheduled');
  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? stats.outbox_pending + ' to approve' : '',
    stats.needs_human ? stats.needs_human + ' handed to you' : '',
  ].filter(Boolean).join(' · ') || 'nothing to approve';
  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span><b>' + (stats.companies || 0) + '</b> companies</span>' +
    '<span><b>' + (stats.unprofiled || 0) + '</b> sites not read yet</span>' +
    '<span><b>' + (stats.signals_confirmed || 0) + '</b> signals on</span>';

  el.innerHTML = '<div class="kpis">' +
    card('Contacts found', stats.prospects || 0, sparkBars(t.found, 'Contacts found per day, last 14 days'),
         '<b>+' + last7(t.found) + '</b> this week') +
    sent +
    card('Conversations', stats.open_conversations || 0, sparkBars(t.replies, 'New replies per day, last 14 days'),
         '<b>' + sum(t.replies) + '</b> new in 14 days') +
    card('Waiting on you', waiting, '', waitNote) +
  '</div>';
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const t = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (t && Array.isArray(t.days)) { _trend = t; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) return;  // offline: keep the last numbers; Today's queue says so
  if (data.error) { el.innerHTML = '<p class="muted" style="font-size:13px">Couldn\'t read the pipeline.</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    ['To write', pipe.to_write || 0, 'c-accent-soft'],
    ['Contacted', pipe.contacted || 0, 'c-accent'],
    ['Replied', pipe.replied || 0, 'c-active'],
    ['Interested', pipe.interested || 0, 'c-good'],
    ['Ready to meet', pipe.ready_to_meet || 0, 'c-note'],
    ['Closed out', pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted" style="font-size:13px">No contacts yet. Scout adds them as it finds people.</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="Contacts by stage">' +
      stages.filter(s => s[1] > 0).map(s =>
        '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + s[0] + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + s[0] + '</span>' +
      '<span class="legend-val">' + s[1] + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data)) return;  // offline: Today's queue says so
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Nothing yet. Every action Harvey takes shows up here.</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
      '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) {
  const btns = [...document.querySelectorAll('nav button')];
  const btn = btns.find(b => (b.getAttribute('onclick') || '').includes("'" + id + "'"));
  showTab(id, btn);
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
      '<span class="name">' + escHtml(r.stage) + '</span>' + badge(r.status) +
      '<span class="when">' + relTime(r.started_at) + '</span>' +
      '<span class="stats">' + (r.records || 0) + ' records · ' +
        (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : 'free') + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  // The first unfinished required step is the one to do now.
  const next = data.checks.find(c => c.required && !c.done);
  const renderCheck = (c, optional) =>
    '<div class="check-item' + (c === next ? ' current' : '') + '">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? '<span class="optional-tag">optional</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
        (!c.done && c.id === 'warmup_service' ? warmupServiceActions(c) : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);
  const done = required.filter(c => c.done).length;

  el.innerHTML =
    '<div class="card setup-card">' +
      '<div class="setup-head"><div><h3>Finish setting up Harvey</h3>' +
        '<p class="muted">Harvey starts finding prospects and sending once these are done.</p></div>' +
        '<span class="setup-pct">' + done + ' of ' + required.length + ' done</span></div>' +
      '<div class="bar thick" style="margin-bottom:8px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
      required.map(c => renderCheck(c, false)).join('') +
      (optional.length
        ? '<details style="margin-top:10px"><summary class="muted" ' +
          'style="font-size:12px">' + optional.length + ' optional</summary>' +
          '<div style="margin-top:6px">' +
          optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
        : '') +
    '</div>';
}

// The warmup step: get TrulyInbox, paste its key, or say you use another service.
function warmupServiceActions(c) {
  return '<div class="btn-group warmup-actions">' +
      '<a class="btn btn-primary btn-sm" href="' + safeHref(c.link) + '" target="_blank" ' +
        'rel="noopener sponsored">Get TrulyInbox</a>' +
      '<input class="form-input" id="trulyinbox-key" type="password" autocomplete="off" ' +
        'placeholder="Paste your TrulyInbox API key" aria-label="TrulyInbox API key" ' +
        'onkeydown="if (event.key === \'Enter\') connectTrulyInbox()">' +
      '<button class="btn btn-secondary btn-sm" id="trulyinbox-go" onclick="connectTrulyInbox()">Connect</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="useOtherWarmup()">I use another service</button>' +
    '</div>';
}

async function connectTrulyInbox() {
  const input = document.getElementById('trulyinbox-key');
  const go = document.getElementById('trulyinbox-go');
  if (!input || !input.value.trim()) return;
  if (go) go.disabled = true;
  const data = await api('/api/trulyinbox/connect', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: input.value.trim()}),
  });
  if (go) go.disabled = false;
  if (data && data.success) {
    input.value = '';
    showToast(data.message || 'TrulyInbox connected', 'success');
    loadSetupStatus();
  } else {
    showToast((data && data.message) || 'Could not connect TrulyInbox', 'error');
  }
}

async function useOtherWarmup() {
  const data = await api('/api/trulyinbox/other', {method: 'POST'});
  if (data && data.success) loadSetupStatus();
}

// ── Signals: Harvey proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [['Confirmed', s.confirmed || 0, 'c-good'], ['Waiting on you', s.proposed || 0, 'c-wait'],
                 ['Turned off', s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + p[0] + '</span>' +
      '<span class="legend-val">' + p[1] + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
            '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
              ', \'confirmed\')">Confirm all ' + undecided + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
              ', \'rejected\')">Skip all ' + undecided + '</button>' +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">seen on ' + sig.companies +
      (sig.companies === 1 ? ' company' : ' companies') + ' so far</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">only recorded above ' +
      Math.round(sig.confidence_floor * 100) + '% confidence</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
        '],\'rejected\')">Turn off</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Turn on</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Confirm</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'rejected\')">Skip</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sig.label || sig.code) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || 'cost unknown') + '</span>' +
        floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? 'Confirmed ' + data.changed + ' signal' + (data.changed === 1 ? '' : 's') +
          ' — Harvey will collect ' + (data.changed === 1 ? 'it' : 'them') + ' from now on.'
        : data.changed + ' signal' + (data.changed === 1 ? '' : 's') + ' turned off.',
      'success');
  } else {
    showToast(failMsg(data, 'Could not update that signal.'), 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  _provider = _provider || data.selected || data.default;
  el.innerHTML = '<div class="prov-grid">' + data.providers.map(p => {
    const ready = p.configured
      ? '<span class="badge t-good">ready</span>'
      : '<span class="badge t-waiting">needs a key</span>';
    return '<div class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" onclick="pickProvider(' + jsq(p.key) + ')">' +
      '<div class="prov-head"><h3>' + escHtml(p.label) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      '<div class="row"><span class="k">Cost</span><span class="v">' +
        escHtml(p.cost_note) + '</span></div>' +
      '<div class="row"><span class="k">Free</span><span class="v">' +
        escHtml(p.free_tier) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">Setup</span><span class="v">' +
          escHtml(p.env_keys.join(', ')) + ' in .env &middot; ' +
          '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer">get a key</a>' +
          '</span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(p.caveat) + '</div>' : '') +
    '</div>';
  }).join('') + '</div>';

  const running = data.running;
  // Reading sites can't be stopped part-way; only a discovery run can.
  document.getElementById('disc-stop').style.display = running && data.running_kind !== 'profile' ? '' : 'none';
  renderProfileCard(data.unprofiled || 0, running);
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    btn.disabled = true;
    btn.textContent = 'Estimate first';
    showToast(data.running_kind === 'profile' ? 'Finished reading websites.' : 'Discovery finished.', 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = 'Estimate first';
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  return {
    provider: _provider,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth').value) || 30,
    limit: Number(document.getElementById('disc-limit').value) || 100,
    max_spend: Number(document.getElementById('disc-cap').value) || 1,
    preset: (document.getElementById('disc-preset') || {}).value || '',
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted" style="font-size:13px">Pricing it…</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml((data && data.error) || 'Could not estimate.') + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
      (data.free ? 'Free' : '$' + data.estimated_cost.toFixed(4)) + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
      data.query_count + ' quer' + (data.query_count === 1 ? 'y' : 'ies') +
      (data.free ? '' : ' &middot; cap is $' + cap.toFixed(2)) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' +
      'Over your cap. Raise the cap or narrow the search.</div>' : '') +
    '<div class="query-list">' +
      data.queries.slice(0, 40).map(escHtml).join('<br>') +
      (data.queries.length > 40 ? '<br>… and ' + (data.queries.length - 40) + ' more' : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? 'Over cap'
    : (data.free ? 'Run — free' : 'Run — spend up to $' + data.estimated_cost.toFixed(2));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  btn.disabled = true;
  btn.textContent = 'Running…';
  const data = await api('/api/discover/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(discoverBody()),
  });
  if (data && data.success) {
    showToast('Discovery started — ' + data.queries + ' queries.', 'success');
  } else {
    showToast((data && data.message) || 'Could not start.', 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', {method: 'POST'});
  showToast(data && data.success ? 'Stopping after the current query.'
                                 : failMsg(data, 'Could not stop.'), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

// Companies found but not looked at: reading their sites is free and
// model-free, and it is what makes an email specific.
function renderProfileCard(pending, running) {
  const el = document.getElementById('discover-profile');
  if (!el) return;
  if (!pending) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">Read websites</div><div class="card"><div class="queue-item good" style="padding:0;border:0;box-shadow:none">' +
    '<div class="qtext"><div class="qtitle">' + pending + ' compan' + (pending === 1 ? 'y' : 'ies') +
    ' not looked at yet</div><div class="qdetail">Free, no account needed. Harvey reads each site for ' +
    'who their agency is, what they are missing, and whether they run ads. Up to 200 per run.</div></div>' +
    '<button class="btn btn-primary btn-sm" onclick="runProfile(this)"' + (running ? ' disabled' : '') + '>' +
    (running ? 'Busy…' : 'Read their sites') + '</button></div></div>';
}

async function runProfile(btn) {
  btn.disabled = true;
  btn.textContent = 'Starting…';
  const data = await api('/api/profile/run', {method: 'POST'});
  if (data && data.success) {
    showToast(data.pending ? 'Reading ' + Math.min(data.pending, 200) + ' websites.' : 'Every site is already read.', 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, 'Could not start.'), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>Running…</h2>' +
      '<p class="muted" style="font-size:13px">Harvey is working through the ' +
      'queries. Results land in Companies, and the run log is on Today.</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  if (report.profiled_companies !== undefined && report.new_companies === undefined) {
    el.innerHTML = '<div class="subhead">Last run</div><div class="stats-grid">' +
      stat('Websites read', report.profiled_companies || 0, 'accent') +
      stat('Facts recorded', report.profile_observations || 0) +
      stat('Cost', 'free') + '</div>' +
      (report.stopped ? '<div class="test-result error" style="margin-top:14px">Stopped early: ' + escHtml(report.stopped) + '</div>' : '');
    return;
  }
  el.innerHTML = '<div class="subhead">Last run</div>' +
    '<div class="stats-grid">' +
      stat('New companies', report.new_companies || 0, 'accent') +
      stat('Already known', report.known_companies || 0) +
      stat('Observations', report.observations || 0) +
      stat('Filtered as junk', report.junk || 0) +
      (report.profiled_companies ? stat('Websites read', report.profiled_companies) : '') +
      stat('Actual cost', report.actual_cost ? '$' + report.actual_cost.toFixed(4) : 'free') +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      'Stopped early: ' + escHtml(report.stopped) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>Problems</h2>' +
        (report.errors || []).slice(0, 8).map(e =>
          '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set(), ranges: {} };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Confirm some signals above and they become the building blocks here.</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(\'' + key + '\',' + jsq(s.code) + ',this.checked)">' +
        '<span>' + escHtml(s.label || s.code) + '</span>' +
        '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + side + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div style="height:18px"></div><div><div class="subhead" style="margin-top:0">In range ' +
      '<span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      '&mdash; e.g. organic rank 11 to 30; leave a side blank for no limit</span></div>' +
      '<div class="cohort-grid">' +
      numeric.map(s =>
        '<div class="cohort-pick cohort-range"><span>' + escHtml(s.label || s.code) + '</span>' +
          bound(s.code, 'min') + '<span class="muted">to</span>' + bound(s.code, 'max') + '</div>'
      ).join('') + '</div></div>' : '';

  el.innerHTML = col('Must have', 'require', '&mdash; every company in the cohort carries all of these') +
    '<div style="height:18px"></div>' +
    col('Must not have', 'exclude', '&mdash; disqualifiers') + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || {min: null, max: null};
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  if (!require.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">Loading cohort…</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude, ranges: ranges,
                          offset: offset, limit: COHORT_PAGE}),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' + escHtml(data.error) + '</div>' : '';
    return;
  }

  let html = '<div style="border-top:1px solid var(--border);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">compan' +
      (data.size === 1 ? 'y matches' : 'ies match') + ' this cohort right now</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>Company</th><th>Domain</th><th>Industry</th><th>Location</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < data.size;
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:10px">' +
      '<span class="muted" style="font-size:12px">Showing ' + first + '&ndash;' + last +
        ' of ' + data.size + '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
        (hasPrev ? '' : ' disabled') + '>Prev</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
        (hasNext ? '' : ' disabled') + '>Next</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">Export CSV</button>' +
      '</div>';
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      'No company carries all of those yet. Either loosen the cohort, or run ' +
      'prospecting to collect more.</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({code: code, min: r.min, max: r.max}));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({require: [..._cohort.require].join(','),
                                  exclude: [..._cohort.exclude].join(',')});
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = 'Exporting…';
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'cohort.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = 'Export CSV';
  } catch (error) {
    button.textContent = 'Export failed — retry';
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

// ── Email template ──
// One email per lead (writer.follow_ups: 0). The server turns it into the
// rules file the Writer reads; Advanced edits that file raw.

const TPL_SAMPLE = {first: 'Jane', last: 'Cooper', agency: 'Acme Digital', email: 'jane@acme.example'};
const TPL_MODE_HINT = {
  exact: 'Sent word for word. Only the placeholders change.',
  adapt: 'Same message and length; a few words tuned to each agency.',
};
const tplFocus = {};
let _tpl = null;          // {mode, emails: [{subject, body}]} as loaded
let _tplMode = 'exact';
let _tplSavedJson = '';   // what was last loaded/saved, to spot unsaved edits
let _tplRawSaved = '';
let _tplLimit = 20000;
let _tplPlaceholders = ['{first name}', '{agency name}', '{sender name}'];
let _tplSender = {name: 'Sam', company: ''};
let _tplAiDraft = null;   // the last "Write with AI" result, while it's shown

function tplRead() {
  return {mode: _tplMode, emails: [{
    subject: document.getElementById('tpl-subject-0').value,
    body: document.getElementById('tpl-body-0').value}]};
}

function tplFill(text) {
  const fill = {'{first name}': TPL_SAMPLE.first, '{agency name}': TPL_SAMPLE.agency, '{company}': TPL_SAMPLE.agency,
                '{sender name}': (_tplSender.name || 'Sam').split(' ')[0],
                '{website issues}': 'hidden links to other websites on your homepage',
                '{report link}': 'https://sitewatch.example/report/sample'};
  let out = String(text || '');
  for (const [k, v] of Object.entries(fill)) out = out.split(k).join(v);
  return out.replace(/[ \t]+([,.!?])/g, '$1');
}

function tplMailHtml(to, subject, body, subjectIsAuto) {
  const from = escHtml(_tplSender.name || 'You') + (_tplSender.company ? ' <span class="muted">· ' + escHtml(_tplSender.company) + '</span>' : '');
  return '<div class="tpl-mail-row"><span>From</span><b>' + from + '</b></div>' +
    '<div class="tpl-mail-row"><span>To</span><b>' + to + '</b></div>' +
    '<div class="tpl-mail-row tpl-mail-subject"><span>Subject</span>' +
      (subjectIsAuto ? '<i class="muted">AI writes a short subject</i>' : '<b>' + escHtml(subject) + '</b>') + '</div>' +
    '<div class="tpl-mail-body">' + (body.trim() ? escHtml(body) : '<span class="muted">Your message will appear here.</span>') + '</div>';
}

function tplShowSample() {
  _tplAiDraft = null;
  const e = tplRead().emails[0];
  document.getElementById('tpl-preview-title').textContent = 'Preview';
  document.getElementById('tpl-preview-badge').textContent = 'Example lead';
  document.getElementById('tpl-preview-badge').className = 'tpl-badge';
  document.getElementById('tpl-ai-reset').hidden = true;
  document.getElementById('tpl-mail').innerHTML = tplMailHtml(
    escHtml(TPL_SAMPLE.first + ' ' + TPL_SAMPLE.last) + ' <span class="muted">· ' + escHtml(TPL_SAMPLE.agency) + '</span>',
    tplFill(e.subject), tplFill(e.body), !e.subject.trim());
}

function tplAutoGrow() {
  const box = document.getElementById('tpl-body-0');
  box.style.height = 'auto';
  box.style.height = Math.max(260, box.scrollHeight + 2) + 'px';
}

function tplEdited() {
  const e = tplRead().emails[0];
  tplAutoGrow();
  const words = (e.body.match(/\S+/g) || []).length;
  document.getElementById('tpl-words').textContent = words + ' words · ' + e.body.length.toLocaleString() + ' / 3,000';
  if (!_tplAiDraft) tplShowSample();
  const dirty = _tpl && JSON.stringify(tplRead()) !== _tplSavedJson;
  const dirtyEl = document.getElementById('tpl-dirty');
  dirtyEl.textContent = dirty ? 'Unsaved changes' : 'All changes saved';
  dirtyEl.className = dirty ? 'tpl-unsaved' : 'muted';
  document.getElementById('tpl-save').disabled = !dirty;
}

function tplSetMode(mode) {
  _tplMode = mode === 'adapt' ? 'adapt' : 'exact';
  document.querySelectorAll('#tpl-mode button').forEach(b => {
    const on = b.dataset.mode === _tplMode;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  });
  document.getElementById('tpl-mode-hint').textContent = TPL_MODE_HINT[_tplMode];
  if (_tpl) tplEdited();
}

function tplInsert(token) {
  const box = document.getElementById(tplFocus[0] === 'subject' ? 'tpl-subject-0' : 'tpl-body-0');
  const start = box.selectionStart ?? box.value.length, end = box.selectionEnd ?? box.value.length;
  box.value = box.value.slice(0, start) + token + box.value.slice(end);
  box.focus();
  box.selectionStart = box.selectionEnd = start + token.length;
  tplEdited();
}

function tplRenderChips() {
  const help = {'{first name}': 'Their first name. Left out for a team inbox.',
                '{agency name}': 'Their agency\'s name, or "your agency" if unknown.',
                '{company}': 'Their company\'s name, or "your business" if unknown.',
                '{sender name}': 'Your first name.',
                '{website issues}': 'Problems a SiteWatch scan found on their site. The sentence is dropped when there are none.',
                '{report link}': 'Link to the public SiteWatch report. The sentence is dropped when there is none.'};
  document.getElementById('tpl-chips').innerHTML = '<span class="tpl-label">Insert</span>' +
    _tplPlaceholders.map(p => '<button type="button" class="tpl-chip" title="' + escHtml(help[p] || '') + '" ' +
      'onmousedown="event.preventDefault()" onclick="tplInsert(' + jsq(p) + ')">' + escHtml(p) + '</button>').join('');
}

async function loadTemplate(discard) {
  // Coming back to the tab mustn't throw away an edit in progress.
  const dirty = _tpl && JSON.stringify(tplRead()) !== _tplSavedJson;
  if (!discard && dirty) { tplEdited(); return; }
  if (discard && dirty && !confirm('Throw away your unsaved changes?')) return;
  const d = await api('/api/email-template');
  if (!d) return;
  if (d.error) { showToast(d.error, 'error'); return; }
  _tplLimit = d.limit || _tplLimit;
  _tplPlaceholders = d.placeholders || _tplPlaceholders;
  _tplSender = d.sender || _tplSender;
  _tpl = d.editor;
  const first = (_tpl.emails || [])[0] || {subject: '', body: ''};
  document.getElementById('tpl-subject-0').value = first.subject || '';
  document.getElementById('tpl-body-0').value = first.body || '';
  tplSetMode(_tpl.mode);
  // Saved state is what the editor holds now; out of sync means a save is due.
  _tplSavedJson = d.editor_in_sync ? JSON.stringify(tplRead()) : '';
  document.getElementById('tpl-meta').innerHTML =
    '<span class="tpl-pill ' + (d.active ? 'on' : '') + '">' + (d.active ? 'In use' : 'Not in use yet') + '</span>' +
    '<span class="muted">One email per lead · no follow-ups</span>';
  const notice = document.getElementById('tpl-notice');
  notice.hidden = d.editor_in_sync || !d.content;
  notice.textContent = 'Harvey is using rules written in Advanced, not this editor. Save here to put the editor back in charge.';
  tplRenderChips();
  _tplAiDraft = null;
  tplEdited();
  _tplRawSaved = d.content || '';
  document.getElementById('tpl-content').value = _tplRawSaved;
  tplRawChanged();
  loadTemplateLeads();
}

function tplRawChanged() {
  const box = document.getElementById('tpl-content');
  const dirty = box.value !== _tplRawSaved;
  document.getElementById('tpl-count').textContent = box.value.length.toLocaleString() + ' / ' +
    _tplLimit.toLocaleString() + ' characters' + (dirty ? ' · unsaved changes' : '');
  document.getElementById('tpl-raw-save').disabled = !dirty;
}

async function loadTemplateLeads() {
  const sel = document.getElementById('tpl-lead');
  if (sel.options.length) return;
  const rows = await api('/api/prospects');
  const people = (rows || []).filter(p => p.company_id && !['opted_out', 'bounced'].includes(p.status))
    .sort((a, b) => (!!b.first_name - !!a.first_name) || ((b.score || 0) - (a.score || 0)));
  sel.innerHTML = '<option value="">Best lead (automatic)</option>' + people.slice(0, 60).map(p =>
    '<option value="' + escHtml(p.id) + '">' + escHtml(((p.first_name || '') + ' ' + (p.last_name || '')).trim() || 'General inbox') +
    ' — ' + escHtml(p.company || '') + '</option>').join('');
}

async function postTemplate(payload, btn, okMsg) {
  btn.disabled = true;
  const r = await api('/api/email-template', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)});
  if (r && r.success) {
    const risky = (r.spam_check || []).some(f => f.level !== 'low');
    showToast(okMsg + (r.spam_summary ? ' ' + r.spam_summary : '') + (risky ? ' Preview a draft to see what to change.' : ''),
      risky ? 'error' : 'success');
    _tpl = null;  // reload everything from what was saved
    await loadTemplate(true);
  } else {
    btn.disabled = false;
    showToast(failMsg(r, 'Could not save the template.'), 'error');
  }
}

function saveTemplate(btn) {
  if (!tplRead().emails[0].body.trim()) { showToast('Write the message first.', 'error'); return; }
  postTemplate({editor: tplRead()}, btn, 'Template saved. Harvey uses it for the next emails it writes.');
}

function saveRawRules(btn) {
  const content = document.getElementById('tpl-content').value;
  if (!content.trim()) { showToast('The rules are empty.', 'error'); return; }
  if (!confirm('Replace Harvey\'s rules with this text? The editor above stops matching until you save it again.')) return;
  postTemplate({content}, btn, 'Rules saved.');
}

async function previewTemplate(btn) {
  if (!tplRead().emails[0].body.trim()) { showToast('Write the message first.', 'error'); return; }
  const mail = document.getElementById('tpl-mail');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = 'Writing…';
  mail.classList.add('tpl-loading');
  const r = await api('/api/email-template/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({editor: tplRead(), prospect_id: document.getElementById('tpl-lead').value})});
  btn.disabled = false;
  btn.innerHTML = label;
  mail.classList.remove('tpl-loading');
  if (!r || !r.success) { showToast(failMsg(r, 'Could not write a preview.'), 'error'); return; }
  _tplAiDraft = r;
  const to = r.to || {};
  document.getElementById('tpl-preview-title').textContent = 'AI draft';
  const badge = document.getElementById('tpl-preview-badge');
  badge.textContent = 'Not saved · not sent';
  badge.className = 'tpl-badge ai';
  document.getElementById('tpl-ai-reset').hidden = false;
  mail.innerHTML = tplMailHtml(
    escHtml(to.name || '') + (to.company ? ' <span class="muted">· ' + escHtml(to.company) + '</span>' : '') +
      (to.email_status ? ' <span class="email-tag ' + escHtml(to.email_status) + '">' + escHtml(to.email_status) + '</span>' : ''),
    r.subject, r.body, false) + spamCheckHtml(r.spam_check);
}

// What in an email's wording looks like spam (harvey/spam_check.py).
function spamCheckHtml(findings) {
  if (!Array.isArray(findings)) return '';
  if (!findings.length) return '<div class="spam-check ok">' + icon('check') + 'Spam check: nothing in the wording looks like spam.</div>';
  const tone = {high: 'invalid', medium: 'risky', low: 'guess'};
  const label = {high: 'serious', medium: 'look at', low: 'minor'};
  return '<div class="spam-check"><b>Spam check</b><ul>' + findings.map(f =>
    '<li><span class="email-tag ' + (tone[f.level] || 'guess') + '">' + escHtml(label[f.level] || f.level) + '</span>' +
    '<span>' + escHtml(f.issue) + ' <span class="muted">' + escHtml(f.fix) + '</span></span></li>').join('') +
    '</ul><p class="muted">Wording is one part of it: a new sending address lands in spam until it has a history, whatever it says.</p></div>';
}

// ── API Integration ──

async function loadIntegrations() {
  const data = await api('/api/settings');
  if (!data) return;
  const isSet = !!data.tinyfish_api_key_set;
  const el = document.getElementById('tinyfish-key');
  el.value = '';
  el.placeholder = isSet ? 'Saved — enter new value to change' : 'Enter your TinyFish API key';
  const tag = document.getElementById('tinyfish-status');
  tag.className = 'email-tag ' + (isSet ? 'verified' : 'guess');
  tag.textContent = isSet ? 'set' : 'not set';
}

function saveTinyfish() {
  const key = document.getElementById('tinyfish-key').value;
  if (!key) { showToast('Enter your TinyFish API key first.', 'error'); return; }
  saveEnv({TINYFISH_API_KEY: key}, 'TinyFish key saved.').then(loadIntegrations);
}

// ── SiteWatch: website scans of prospects ──

let _swEnabled = false;
let _swPoll = null;
let _swTick = null;

function swTag(id, ok, okLabel, badLabel) {
  const el = document.getElementById(id);
  el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
  el.textContent = ok ? okLabel : badLabel;
}

function swStatusTag(status) {
  const cls = {clean: 'verified', review: 'risky', critical: 'invalid'}[status] || 'guess';
  return status ? '<span class="email-tag ' + cls + '">' + escHtml(status) + '</span>' : '';
}

function swKpi(id, text, tone, note) {
  const el = document.getElementById(id);
  el.className = 'kpi-value sw-kv ' + (tone || '');
  el.innerHTML = (tone === 'good' || tone === 'bad' ? '<span class="status-dot ' + (tone === 'good' ? 'running' : 'offline') + '"></span>' : '') + escHtml(text);
  document.getElementById(id + '-note').innerHTML = note || '';
}

function swLink(u, t) {
  return safeHref(u) ? '<a href="' + safeHref(u) + '" target="_blank" rel="noopener noreferrer">' + escHtml(t) + '</a>' : '';
}

// The scan's separate UI and SEO reports, from fields ui_issues / ui_text / ui_report_url (and seo_).
const SW_AUDITS = [['ui', 'Responsive & UI'], ['seo', 'SEO']];

function swAuditSummary(o) {
  return SW_AUDITS.map(([k, label]) => {
    const n = o[k + '_issues'];
    if (typeof n !== 'number') return '';
    return '<div class="sw-audit"><b>' + escHtml(label) + ':</b> ' +
      (n ? n + ' problem' + (n === 1 ? '' : 's') + (o[k + '_text'] ? '<div class="sw-issue-text">' + escHtml(o[k + '_text']) + '</div>' : '')
         : '<span class="muted">nothing worth mentioning</span>') + '</div>';
  }).join('');
}

// SiteWatch's two reports: Basic (scores and main problems, the one emails link) and
// Advanced (every check). Any report link of a scan starts with the Basic one.
function swBasicUrl(...urls) {
  for (const u of urls) {
    const s = String(u || '').trim();
    if (/^https?:\/\//i.test(s)) return s.split('#')[0].split('?')[0];
  }
  return '';
}

function swReportLinks(o, securityUrl) {
  const basic = swBasicUrl(securityUrl, o.ui_report_url, o.seo_report_url);
  return basic ? swLink(basic, 'Reports') : '';   // opens Basic; the page has the Basic / Advanced tabs
}

// Checks the connection itself (one quick ping) so the tile says what is true now.
async function swCheckConnection(d) {
  if (!(d.url && d.token_set)) { swKpi('sw-k-conn', 'Not set up', 'off', 'Add the address and token under Setup'); return; }
  swKpi('sw-k-conn', 'Checking…', 'off', escHtml(d.url));
  const r = await api('/api/sitewatch/test', {method: 'POST'});
  if (r && r.success) swKpi('sw-k-conn', 'Connected', 'good', escHtml(d.url));
  else swKpi('sw-k-conn', 'Not connected', 'bad', escHtml((r && r.message) || 'No answer'));
}

async function loadSitewatch(recheck) {
  const d = await api('/api/sitewatch');
  if (!d) return;
  const url = document.getElementById('sw-url');
  if (document.activeElement !== url) url.value = d.url || '';
  const tok = document.getElementById('sw-token');
  tok.value = '';
  tok.placeholder = d.token_set ? 'Saved — enter new value to change' : 'Paste the API token';
  swTag('sw-conn-status', d.url && d.token_set, 'set', 'not set');
  if (recheck !== false) swCheckConnection(d);

  _swEnabled = !!d.enabled;
  const allConfirmed = d.codes.every(c => d.confirmed.includes(c));
  const on = _swEnabled && allConfirmed;
  swTag('sw-enabled-status', on, 'on', _swEnabled ? 'signals not confirmed' : 'off');
  swKpi('sw-k-mode', on ? 'On' : 'Off', on ? 'good' : 'off',
    on ? 'Restart <code>harvey run</code> after a change' : 'Turn on under Setup');
  const toggle = document.getElementById('sw-toggle');
  toggle.textContent = _swEnabled ? 'Turn off' : 'Turn on';
  toggle.className = 'btn btn-sm ' + (_swEnabled ? 'btn-secondary' : 'btn-primary');
  document.getElementById('sw-enabled-detail').innerHTML = d.config_error
    ? '<span class="error-text">Couldn\'t read the config: ' + escHtml(d.config_error) + '</span>'
    : 'Saved in <code>' + escHtml(d.config_file || 'harvey.yaml') + '</code>';
  const nav = document.getElementById('nav-sitewatch');
  if (nav) nav.textContent = d.pending ? d.pending : '';

  const done = d.scans.filter(s => s.state === 'done');
  const withIssues = done.filter(s => s.issues > 0);
  const scanned = document.getElementById('sw-k-scanned');
  scanned.textContent = done.length;
  scanned.className = 'kpi-value' + (done.length ? '' : ' zero');
  document.getElementById('sw-k-scanned-note').innerHTML = d.pending ? '<b>' + d.pending + '</b> waiting for a result' : 'by Harvey, before writing';
  const issues = document.getElementById('sw-k-issues');
  issues.textContent = withIssues.length;
  issues.className = 'kpi-value' + (withIssues.length ? '' : ' zero');
  document.getElementById('sw-k-issues-note').innerHTML = done.length
    ? Math.round(100 * withIssues.length / done.length) + '% of scanned sites' : 'nothing scanned yet';

  document.getElementById('sw-scans-meta').textContent = d.scans.length ? d.scans.length + ' compan' + (d.scans.length === 1 ? 'y' : 'ies') : '';
  const el = document.getElementById('sw-scans');
  if (!d.scans.length) {
    el.innerHTML = emptyState(icon('shield'), 'No prospects scanned yet',
      'Harvey scans the website of each company it is about to email, once <b>Scan before writing</b> is on and Harvey is running ' +
      '(<code>harvey run</code>). Scans you run by hand above show their result there.');
    return;
  }
  el.innerHTML = '<table><thead><tr><th>Company</th><th>Result</th><th>What was found</th><th>Report</th><th>When</th></tr></thead><tbody>' +
    d.scans.map(s => {
      const result = s.state === 'pending'
        ? '<span class="email-tag guess"><span class="spinner" aria-hidden="true" style="width:10px;height:10px;margin-right:4px"></span>scanning</span>'
        : s.state === 'failed' ? '<span class="email-tag invalid" title="' + escHtml(s.error || '') + '">failed</span>'
        : swStatusTag(s.status) + (typeof s.score === 'number' ? '<div class="muted" style="margin-top:4px">risk ' + s.score + '/100</div>' : '');
      const found = s.state === 'done'
        ? (s.issues ? '<b>' + s.issues + ' security problem' + (s.issues === 1 ? '' : 's') + '</b><div class="sw-issue-text">' + escHtml(s.issue_text || '') + '</div>'
                    : '<span class="muted">No security problems worth mentioning</span>') + swAuditSummary(s)
        : s.state === 'failed' ? '<span class="muted">' + escHtml(s.error || '') + '</span>' : '';
      return '<tr><td><b>' + escHtml(s.company || '') + '</b><div class="muted">' + escHtml(s.domain || '') + '</div></td>' +
        '<td>' + result + '</td><td>' + found + '</td>' +
        '<td>' + swReportLinks(s, s.report_url) + '</td>' +
        '<td class="muted">' + escHtml(String(s.finished_at || s.requested_at || '').slice(0, 16)) + '</td></tr>';
    }).join('') + '</tbody></table>';
}

async function saveSitewatch() {
  const payload = {SITEWATCH_URL: document.getElementById('sw-url').value.trim()};
  const token = document.getElementById('sw-token').value.trim();
  if (token) payload.SITEWATCH_API_TOKEN = token;
  await saveEnv(payload, 'SiteWatch settings saved.');
  loadSitewatch(true);
}

async function testSitewatch() {
  const out = document.getElementById('sw-test-result');
  const btn = document.getElementById('sw-test-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>Testing…';
  out.innerHTML = '';
  const r = await api('/api/sitewatch/test', {method: 'POST'});
  btn.disabled = false;
  btn.textContent = 'Test connection';
  out.innerHTML = r && r.success ? '<span class="success-text">' + escHtml(r.message) + '</span>'
    : '<span class="error-text">' + escHtml((r && r.message) || 'No answer from the dashboard.') + '</span>';
  loadSitewatch(true);
}

async function toggleSitewatch() {
  const on = !_swEnabled;
  if (on && !confirm('Turn on website scans before writing? This also confirms the SiteWatch signals.')) return;
  const btn = document.getElementById('sw-toggle');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>Saving…';
  const r = await api('/api/sitewatch/enabled', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: on})});
  btn.disabled = false;
  showToast((r && r.message) || 'Couldn\'t change the setting.', r && r.success ? 'success' : 'error');
  loadSitewatch(false);
}

function swFindingsHtml(r, url) {
  const shown = r.findings.filter(f => f.severity !== 'info');
  const tone = {review: 'review', critical: 'critical'}[r.status] || '';
  const head = '<div class="sw-result-head"><div><div class="sw-site">' + escHtml(url || '') + '</div>' +
      '<div class="muted">' + (shown.length ? shown.length + ' problem' + (shown.length === 1 ? '' : 's') + ' found' : 'No problems found') + '</div></div>' +
    '<div class="sw-risk">' + swStatusTag(r.status) +
      '<span class="sw-meter ' + tone + '"><span style="width:' + Math.max(3, Math.min(100, r.score)) + '%"></span></span>' +
      'risk ' + r.score + '/100</div></div>';
  const sev = {critical: 'invalid', high: 'invalid', medium: 'risky'};
  const list = shown.length ? '<ul class="sw-findings">' + shown.map(f =>
      '<li><span class="email-tag ' + (sev[f.severity] || 'guess') + '">' + escHtml(f.severity) + '</span><div>' +
      '<div class="t">' + escHtml(f.title) + ' <span class="w">' + escHtml(f.where) + '</span></div>' +
      (f.plain && f.plain !== f.title ? '<div class="p">' + escHtml(f.plain) + '</div>' : '') + '</div></li>').join('') + '</ul>'
    : '<div class="sw-clean">' + icon('check') + ' The site looks clean. Harvey won\'t mention a scan in emails to it.</div>';
  const audits = SW_AUDITS.map(([k, label]) => swAuditHtml(r[k], label)).join('');
  const basic = swBasicUrl(r.report_url);
  const foot = basic
    ? '<div class="sw-result-foot"><span class="muted">Anyone with this link can open the reports (Basic and Advanced tabs), no login needed.</span>' +
      '<a class="btn btn-secondary btn-sm" href="' + safeHref(basic) + '" target="_blank" rel="noopener noreferrer">Reports ' + icon('arrow') + '</a></div>' : '';
  return '<div class="sw-result">' + head + list + audits + foot + '</div>';
}

// One of the scan's separate reports (UI or SEO): score, findings, its own link.
function swAuditHtml(a, label) {
  if (!a) return '';
  const tone = {good: 'verified', fair: 'risky', poor: 'invalid'}[a.grade] || 'guess';
  const shown = (a.findings || []).filter(f => f.severity !== 'info');
  const sev = {high: 'invalid', medium: 'risky'};
  return '<div class="sw-audit-block"><div class="sw-result-head"><div><div class="sw-site">' + escHtml(label) + ' report</div>' +
      '<div class="muted">' + (shown.length ? shown.length + ' improvement' + (shown.length === 1 ? '' : 's') : 'Nothing to fix') + '</div></div>' +
    '<div class="sw-risk"><span class="email-tag ' + tone + '">' + escHtml(a.grade || '') + '</span> ' + escHtml(String(a.score)) + '/100' +
      '</div></div>' +
    (shown.length ? '<ul class="sw-findings">' + shown.map(f =>
      '<li><span class="email-tag ' + (sev[f.severity] || 'guess') + '">' + escHtml(f.severity) + '</span><div>' +
      '<div class="t">' + escHtml(f.title) + ' <span class="w">' + escHtml(f.where) + '</span></div>' +
      (f.plain && f.plain !== f.title ? '<div class="p">' + escHtml(f.plain) + '</div>' : '') + '</div></li>').join('') + '</ul>' : '') +
    '</div>';
}

async function scanSitewatch() {
  const url = document.getElementById('sw-scan-url').value.trim();
  const out = document.getElementById('sw-scan-result');
  if (!url) { showToast('Enter a website address first.', 'error'); return; }
  if (_swPoll) clearTimeout(_swPoll);
  if (_swTick) clearInterval(_swTick);
  const btn = document.getElementById('sw-scan-btn');
  const input = document.getElementById('sw-scan-url');
  const busy = on => {
    btn.disabled = on; input.disabled = on;
    btn.innerHTML = on ? '<span class="spinner" aria-hidden="true"></span>Scanning…' : 'Scan';
    if (!on && _swTick) { clearInterval(_swTick); _swTick = null; }
  };
  busy(true);
  const began = Date.now();
  let state = 'starting';
  const label = {starting: 'Asking SiteWatch to scan', queued: 'Waiting in SiteWatch\'s queue', running: 'Scanning the website'};
  const draw = () => {
    const secs = Math.floor((Date.now() - began) / 1000);
    const clock = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
    const hint = state === 'queued' && secs >= 60
      ? '<div class="sw-hint">Still waiting to start. Is SiteWatch\'s queue worker running? (<code>php artisan queue:work</code>, or its cron URL on the live site)</div>' : '';
    out.innerHTML = '<div class="sw-loading" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><div>' +
      '<b>' + escHtml(label[state] || 'Scanning') + '…</b>' +
      '<span class="muted">' + escHtml(url) + ' · ' + clock + ' · a scan usually takes a minute or two</span>' + hint + '</div></div>';
  };
  draw();
  _swTick = setInterval(draw, 1000);
  const start = await api('/api/sitewatch/scan', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url})});
  if (!start || !start.success) {
    busy(false);
    out.innerHTML = '<p class="error-text">' + escHtml((start && start.message) || 'Couldn\'t start the scan.') + '</p>';
    return;
  }
  state = start.state;
  const poll = async () => {
    const r = await api('/api/sitewatch/scans/' + start.scan_id);
    if (r && r.success && r.done) {
      busy(false);
      out.innerHTML = r.state === 'completed' ? swFindingsHtml(r, url)
        : '<p class="error-text">The scan ' + escHtml(r.state) + (r.error ? ': ' + escHtml(r.error) : '') + '</p>';
      loadSitewatch(false);
      return;
    }
    if (r && !r.success) { busy(false); out.innerHTML = '<p class="error-text">' + escHtml(r.message) + '</p>'; return; }
    if (Date.now() - began >= 10 * 60000) {
      busy(false);
      out.innerHTML = '<p class="error-text">Still not finished after 10 minutes. SiteWatch needs its queue worker (php artisan queue:work) or cron URL running. ' +
        'The scan stays queued, so you can click Scan again later to check.</p>';
      return;
    }
    if (r && r.state) { state = r.state; draw(); }
    _swPoll = setTimeout(poll, 5000);
  };
  poll();
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Saved — enter new value to change' : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : 'not set';
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'Enter client secret');
  savedPh('smtp-pass', data.smtp_password_set, 'Enter password / app password');
  savedPh('imap-pass', data.imap_password_set, 'Same as SMTP password');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('instantly-key', data.instantly_api_key_set, 'Enter your Instantly API key');
  savedPh('reoon-key', data.reoon_api_key_set, '600 free/mo — reoon.com/email-verifier');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, '100 free/mo — best for M365/Workspace catch-alls');
  savedPh('hunter-key', data.hunter_api_key_set, '50 free/mo + email-pattern lookup');
  savedPh('linkedin-password', data.linkedin_password_set, 'Enter password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'Your Cloudflare API Token');

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = 'authorized'; }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = 'run: harvey gmail auth'; }
    else { g.className = 'email-tag guess'; g.textContent = 'not set up'; }
  }
  const m = document.getElementById('m365-status');
  if (m) {
    if (data.m365_authorized) { m.className = 'email-tag verified'; m.textContent = 'authorized'; }
    else if (data.m365_client_id) { m.className = 'email-tag risky'; m.textContent = 'run: harvey microsoft auth'; }
    else { m.className = 'email-tag guess'; m.textContent = 'not set up'; }
  }
  tag('reoon-status', data.reoon_api_key_set, 'set');
  tag('zerobounce-status', data.zerobounce_api_key_set, 'set');
  tag('hunter-status', data.hunter_api_key_set, 'set');
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, 'Gmail OAuth saved. Now run "harvey gmail auth" in your terminal.').then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  saveEnv(payload, 'SMTP settings saved.').then(loadSettings);
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast('Paste the Application (client) ID first.', 'error'); return; }
  saveEnv({M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim()},
          'Saved. Now run "harvey microsoft auth" in your terminal.').then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Verification keys saved.').then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast('Enter a new key first. The saved one is kept.', 'error'); return; }
  saveEnv({INSTANTLY_API_KEY: key}, 'Instantly API key saved.');
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.');
}

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, 'Cloudflare credentials saved.');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadHarveyStatus() {
  const data = await api('/api/harvey/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'Harvey is running' : 'Harvey is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'Harvey wakes every few minutes, does what needs doing, and sleeps.';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/harvey/start', {method: 'POST'});
  if (data && data.success) showToast('Harvey started.', 'success');
  else showToast((data && data.message) || 'Failed to start.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/harvey/stop', {method: 'POST'});
  if (data && data.success) showToast('Harvey stopped.', 'success');
  else showToast((data && data.message) || 'Failed to stop.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const data = await api('/api/harvey/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Harvey to see activity.';
  }
}

// ── Pipeline data ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + label + ' calls today <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
      escHtml(st.provider === 'codex' ? 'Codex' : 'Brain') + ' <b>paused</b>: ' +
      escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="Email ' + escHtml(m.label.toLowerCase()) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? 'Codex (ChatGPT plan) Quota' : 'Claude Subscription Quota';
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex'
      ? "Quota unavailable: Harvey couldn't read your ChatGPT plan's limits (check codex login, the Codex CLI version, and network)."
      : "Quota unavailable: Harvey couldn't read your Claude subscription's limits (check claude login and network).";
    return html + '<p class="muted">' + escHtml(why) + ' Until it can, budgets count Harvey\'s own calls.</p>';
  }
  const labels = {five_hour: '5-hour window', seven_day: 'Weekly', primary: 'Primary window',
                  secondary: 'Secondary window', limit_reached: 'Usage limit reached'};
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">calls</span>' +
        '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>Output tokens per day <span class="hint muted" style="font-weight:400">last ' +
      days.length + ' days · peak ' + fmtTokens(maxOut) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="Output tokens per day">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + fmtTokens(d.output_tokens) + ' out, ' +
          (d.calls || 0) + ' calls"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
        escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, no cost column
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    let h = '<div class="card flush"><div class="card-head"><h3>' + title + '</h3></div><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td class="cell-main">' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
          (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + (r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), 'No usage recorded yet',
      'Once Harvey starts making model calls (Claude or Codex), every one is logged here with exact calls and tokens by provider, agent and task.');
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table('By Provider (30 days)', providers, 'Provider') : '') +
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), 'Couldn\'t read the Outbox', escHtml(data.error));
    list.innerHTML = '';
    return;
  }

  const pending = data.pending || [];
  _desk.items = pending;
  _desk.footer = data.footer || '';
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  actions.innerHTML =
    (pending.length && !data.paused
      ? (pending.length > pending.filter(p => heldReason(p)).length
        ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" ' +
          'title="Emails Harvey held are left for you to approve one by one">Approve all ' +
          (pending.length - pending.filter(p => heldReason(p)).length) + '</button>' : '') : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' +
        'Resume sending</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' +
        'Pause all sending</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = sendingModeBanner(_sendingMode) + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>Sending is paused</b>' +
        '<p>' + escHtml(data.paused) + '. Approved mail stays queued until you resume.</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>Add your postal address</b>' +
        '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), 'Nothing to review',
      _sendingMode && _sendingMode.mode === 'autopilot'
        ? 'Autopilot is on, so Harvey sends on its own. Anything it holds for you shows up here.'
        : 'Every draft Harvey writes lands here first. Approve one and it sends on schedule.') +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + title + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  list.innerHTML =
    table('Approved &amp; scheduled', data.approved, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sends', r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    table('Recently sent', data.sent, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sent', r => formatDate(r.sent_at), true],
    ]) +
    table('Didn\'t send', data.failed, [
      ['To', r => r.to_email], ['Status', r => badge(r.status), false, true],
      ['Reason', r => r.error, true], ['Updated', r => formatDate(r.updated_at), true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'team page on their site',
  company_website: 'team page on their site',
  scraped_mailto: 'address published on their site',
  linkedin_search: 'LinkedIn search',
  hunter_domain_search: 'Hunter domain search',
  job_board: 'job board',
  web_search: 'web search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + k + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, ctx.contact_title].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? 'score ' + ctx.score : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  return '<div class="desk-context">' +
    row('Company', escHtml(ctx.company)) +
    row('Website', site) +
    row('About', escHtml(ctx.description)) +
    row('Contact', who) +
    row('Found via', escHtml(Object.prototype.hasOwnProperty.call(CONTACT_SOURCES, ctx.contact_source)
      ? CONTACT_SOURCES[ctx.contact_source] : ctx.contact_source)) +
    row('Email', email) +
    row('Signals', signals) +
    row('Campaign', ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">(how they were found, not a fact about them)</span>' : '') +
    row('Writer notes', escHtml(ctx.notes)) +
  '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): Harvey sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>Autopilot is on</b>' +
      '<p>Harvey sends email on its own. It holds anything that links to a site that isn’t yours or ' +
      'quotes its own instructions; those wait here' + (m.held ? ' (' + m.held + ' now)' : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>Approval mode</b>' +
    '<p>' + (instantly
      ? 'Instantly can’t hold single emails, so campaigns aren’t sent to Instantly until you turn on autopilot.'
      : 'Every email Harvey writes waits here until you approve it.') +
    ' <a href="#" onclick="goTab(\'settings\');return false">About autopilot</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">Couldn’t read the sending mode.</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || 'none yet';
  let html = '<div class="card mode-card"><h3>Sending mode: ' + (auto ? 'Autopilot' : 'Approval') + '</h3>' +
    '<p><b>Approval</b> (the default): every email Harvey writes, first emails, follow-ups and replies, ' +
      'waits in the Outbox until you approve it.' +
      (instantly ? ' On Instantly, which can’t hold single emails, that means campaigns aren’t deployed.' : '') + '</p>' +
    '<p><b>Autopilot</b>: Harvey sends on its own, on schedule, within your daily limit and quiet hours. ' +
      'Opt-outs, bounces and Pause all sending work the same. It still holds an email for you when it links ' +
      'to, or gives an address at, a site that isn’t yours, or quotes Harvey’s own instructions: text from ' +
      'websites and replies can try to steer what it writes. The first reply after you undo an opt-out waits too.</p>' +
    '<p class="muted">Counted as yours: ' + domains + (m.booking_page ? '; your booking page ' + escHtml(m.booking_page) : '') +
      '. Add others under <code>channels.email.link_domains</code> in <code>' + escHtml(m.file) + '</code>.</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">Turning it off works at once: emails autopilot ' +
      'approved that haven’t gone out come back to the Outbox.' +
      (instantly ? ' Campaigns already on Instantly keep running until you pause them there.' : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">Approve a batch or two by hand first ' +
      'and read what Harvey writes. Type <b>autopilot</b> to turn it on. It starts from Harvey’s next cycle.</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="Type autopilot to confirm" ' +
        'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
        'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">Turn on autopilot</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">Turn on autopilot…</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = {mode};
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? 'Autopilot is on from Harvey’s next cycle. Held emails still wait for you.'
        : 'Autopilot is off. Every email waits for you' +
          (res.requeued ? '; ' + res.requeued + ' queued by autopilot came back to the Outbox.' : '.') +
          (_sendingMode && _sendingMode.provider === 'instantly'
            ? ' Campaigns already on Instantly keep running: pause them in Instantly.' : ''), 'success');
    } else {
      showToast(failMsg(res, 'Couldn’t change the sending mode.'), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="Harvey held this instead of sending it">Held</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(it.subject || '(no subject)') + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="A reply keeps its subject so it stays in the thread"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">' +
      '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || '(no subject)') + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="Added when it sends (compliance in harvey.yaml)">' +
        escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">Save</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(null)">Cancel</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
        'Approve <kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
        'Edit <kbd>E</kbd></button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        'Reject <kbd>R</kbd></button>';

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">To <b>' + escHtml(cur.to_email) + '</b>' +
        '<span class="tag">' + (cur.kind === 'reply' ? 'Reply' : 'Email ' + escHtml(String(cur.step))) + '</span>' +
        '<span class="muted">sends ' + formatDate(cur.send_at) + '</span></div>' +
      renderDeskContext(cur.context) +
      (heldReason(cur)
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>Harvey held this instead of sending it</b>' +
          '<div>' + escHtml(heldReason(cur)) + '. Read it before approving: text from a website or an ' +
          'email can try to steer what Harvey writes.</div></div></div>'
        : '') +
      draft + footer +
      '<div class="desk-actions">' + actions +
        '<span class="muted" style="font-size:12px;margin-left:auto">' +
          (i + 1) + ' of ' + items.length + '</span>' +
      '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast('Save or cancel your edit first.', 'error');
  return true;
}

async function deskSave(id) {
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast('Subject and body are both required.', 'error'); return; }
  let data = null;
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: subject, body: body}),
    });
    if (await lockIfRefused(r)) return;
    data = await r.json();
  } catch {}
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast('Saved. Still waiting for your approval.', 'success');
    loadOutbox();
  } else {
    showToast((data && data.message) || 'Save failed.', 'error');
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function outboxAct(id, action) {
  if (deskBusy()) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'approve' ? 'Approved — will send on schedule.' : 'Rejected.', 'success');
  else showToast(failMsg(data, 'Action failed.'), 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  if (deskBusy()) return;
  const data = await api('/api/outbox/approve-all', {method: 'POST'});
  if (data && data.success) showToast('Approved ' + data.approved + ' email(s).' + (data.held
    ? ' ' + data.held + ' Harvey held ' + (data.held === 1 ? 'is' : 'are') + ' left for you to read and approve one by one.'
    : ''), 'success');
  else showToast(failMsg(data, 'Approve-all failed.'), 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'pause' ? 'Sending paused.' : 'Sending resumed.', 'success');
  else showToast(failMsg(data, action === 'pause' ? 'Could not pause sending.' : 'Could not resume sending.'), 'error');
  loadOutbox();
}

const COMPANIES_PER_PAGE = 50;
let _companiesPage = 0;

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('companies'), 'No companies yet',
      'Confirm your <b>Signals</b>, then find businesses on the <b>Discover</b> tab. ' +
      'OpenStreetMap is free and needs no account.');
    return;
  }
  if (!document.getElementById('company-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search">' + icon('discover') +
        '<input class="form-input" id="company-search" type="search" placeholder="Search company, website, location" ' +
        'aria-label="Search companies" oninput="_companyQuery=this.value;renderCompaniesPage(0)"></label>' +
      '<div class="segmented" id="company-filters" role="group" aria-label="Filter companies"></div>' +
      '</div><div id="company-rows"></div>';
    document.getElementById('company-search').value = _companyQuery;
  }
  renderCompaniesPage(_companiesPage);
}

// Leads = contacts Harvey found there; Contacted / Replied use the same
// statuses as the Account tab's filters (counted server-side).
const COMPANY_FILTERS = [
  ['all', 'All'],
  ['leads', 'With leads'],
  ['contacted', 'Contacted'],
  ['replied', 'Replied'],
  ['none', 'No leads'],
];
let _companyFilter = 'all';
let _companyQuery = '';

function companyMatches(c, f) {
  const leads = c.contact_count || 0;
  if (f === 'leads') return leads > 0;
  if (f === 'none') return leads === 0;
  if (f === 'contacted') return (c.contacted_count || 0) > 0;
  if (f === 'replied') return (c.replied_count || 0) > 0;
  return true;
}

function setCompanyFilter(f) { _companyFilter = f; renderCompaniesPage(0); }

function renderCompaniesPage(page) {
  const el = document.getElementById('company-rows');
  const filtersEl = document.getElementById('company-filters');
  if (!el) return;
  const q = _companyQuery.trim().toLowerCase();
  const found = c => !q || [c.name, c.domain, c.location, c.industry].join(' ').toLowerCase().includes(q);
  filtersEl.innerHTML = COMPANY_FILTERS.map(f =>
    '<button class="' + (f[0] === _companyFilter ? 'on' : '') + '" aria-pressed="' + (f[0] === _companyFilter) +
      '" onclick="setCompanyFilter(\'' + f[0] + '\')">' + f[1] + ' <span class="n">' +
      _companies.filter(c => companyMatches(c, f[0]) && found(c)).length + '</span></button>'
  ).join('');

  // Keep each company's index into _companies: showCompanyContacts uses it.
  const data = _companies.map((c, i) => [c, i]).filter(([c]) => companyMatches(c, _companyFilter) && found(c));
  if (!data.length) {
    el.innerHTML = '<div class="table-card">' + emptyState(icon('companies'), 'No matches',
      'No company matches that search and filter.') + '</div>';
    return;
  }
  const pages = Math.max(1, Math.ceil(data.length / COMPANIES_PER_PAGE));
  _companiesPage = Math.min(Math.max(0, page), pages - 1);
  const start = _companiesPage * COMPANIES_PER_PAGE;
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Company</th>' +
    '<th title="Company fit, 0-100: sector, decision maker, reachability, buying signal. Hover a score for why.">Fit</th>' +
    '<th title="Hover a status for why the company was or wasn\'t contacted">Status</th>' +
    '<th>Industry</th><th>Location</th><th class="num" title="Contacts Harvey found at this company">Leads</th>' +
    '<th class="num" title="Leads who replied (including interested and meeting booked)">Replied</th><th>Added</th></tr></thead><tbody>';
  data.slice(start, start + COMPANIES_PER_PAGE).forEach(([c, i]) => {
    const replied = c.replied_count || 0;
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const domain = safeHref(website)
      ? '<a href="' + safeHref(website) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
        escHtml(c.domain || website.replace(/^https?:\/\//i, '')) + '</a>'
      : escHtml(c.domain);
    html += '<tr class="clickable" onclick="showCompanyContacts(' + i + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(c.name) + '</span>' +
        '<span class="cell-sub">' + domain + '</span></div></td>' +
      '<td title="' + escHtml((c.fit_reasons || []).join(' · ')) + '">' +
        (typeof c.fit === 'number' ? meterHtml(c.fit) : '') + '</td>' +
      '<td title="' + escHtml(c.register_why || '') + '">' + escHtml(c.register_status || '') + '</td>' +
      '<td>' + (c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '') + '</td>' +
      '<td>' + escHtml(c.location) + '</td>' +
      '<td class="num">' + (c.contact_count || 0) + '</td>' +
      '<td class="num">' + (replied ? '<span class="badge t-good">' + replied + ' replied</span>' : '<span class="muted">—</span>') + '</td>' +
      '<td class="muted">' + relTime(c.created_at) + '</td></tr>';
  });
  const pager = pages > 1
    ? ' &nbsp; <button class="btn btn-ghost btn-sm" ' + (_companiesPage === 0 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage - 1) + ')">&larr; Prev</button> ' +
      'Page ' + (_companiesPage + 1) + ' of ' + pages +
      ' <button class="btn btn-ghost btn-sm" ' + (_companiesPage >= pages - 1 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage + 1) + ')">Next &rarr;</button>'
    : '';
  el.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + (start + 1) + '–' + Math.min(start + COMPANIES_PER_PAGE, data.length) +
    ' of ' + data.length + ' compan' + (data.length === 1 ? 'y' : 'ies') + pager + '</div>';
}

// Basic facts about one company, above its contacts: where it came from,
// why it fits, what's been done, and what Harvey saw on its own site.
function companyInfoCard(company, profile) {
  const c = (profile && profile.company) || company;
  const reg = (profile && profile.register) || {};
  const obs = (profile && profile.observations) || [];
  const link = (url, text) => safeHref(url)
    ? '<a href="' + safeHref(url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(text || url) + '</a>'
    : escHtml(text || url || '');
  const website = c.website || (c.domain ? 'https://' + c.domain : '');
  const row = (k, v) => v ? '<tr><td class="muted" style="width:170px;vertical-align:top">' + k +
    '</td><td style="white-space:normal">' + v + '</td></tr>' : '';
  const latest = {};
  for (const o of obs) {
    const key = o.signal_code + '|' + (o.value_text || o.value_num);
    if (!latest[key] && (o.value_text || o.value_num !== null)) latest[key] = o;
  }
  const by = code => Object.values(latest).filter(o => o.signal_code === code);
  const list = (items, max) => items.slice(0, max).map(o =>
    (o.evidence_url ? link(o.evidence_url, o.value_text || String(o.value_num)) : escHtml(o.value_text || String(o.value_num)))
  ).join('<br>');
  const people = by('DECISION_MAKER_TITLE').length ? by('DECISION_MAKER_TITLE') : by('CONTACT_FOUND');
  const form = by('CONTACT_FORM_URL')[0];
  // Latest SiteWatch scan (observations come newest first)
  const swIssues = obs.find(o => o.signal_code === 'SITEWATCH_ISSUES');
  const swStatus = obs.find(o => o.signal_code === 'SITEWATCH_STATUS');
  const swReport = (swIssues && swIssues.evidence_url) || (swStatus && swStatus.evidence_url) || '';
  const sitewatch = (swIssues || swStatus)
    ? (swStatus ? '<b>' + escHtml(swStatus.value_text || '') + '</b>' +
        (swStatus.value_num !== null ? ' (risk ' + escHtml(String(swStatus.value_num)) + '/100)' : '') : '') +
      (swIssues ? (swStatus ? ' · ' : '') + escHtml(String(swIssues.value_num || 0)) + ' issue(s)' +
        (swIssues.value_text ? '<br>' + escHtml(swIssues.value_text) : '') : '') +
      (swReport ? '<br>' + link(swBasicUrl(swReport), 'Reports') : '')
    : '';
  // The scan's separate UI and SEO reports
  const swAudit = (code, label) => {
    const o = obs.find(x => x.signal_code === code);
    if (!o) return '';
    return '<b>' + escHtml(String(o.value_num || 0)) + '</b> problem(s)' + (o.value_text ? '<br>' + escHtml(o.value_text) : '') +
      '';
  };
  const shown = new Set(['DECISION_MAKER_TITLE', 'CONTACT_FOUND', 'CONTACT_FORM_URL', 'HIRING_ROLE', 'FOUND_IN_SERP', 'TECH_STACK',
    'SITEWATCH_ISSUES', 'SITEWATCH_STATUS', 'SITEWATCH_UI_ISSUES', 'SITEWATCH_SEO_ISSUES']);
  const other = Object.values(latest).filter(o => !shown.has(o.signal_code));
  let html = '<div class="card" style="margin-bottom:16px"><div class="card-head"><h3>' + escHtml(c.name || c.domain) + '</h3>' +
    '<span class="meta">' + (typeof company.fit === 'number' ? 'Fit ' + company.fit + ' / 100' : '') + '</span></div>' +
    '<table><tbody>' +
    row('Website', link(website, c.domain || website)) +
    row('Sector', escHtml(c.industry)) +
    row('Area', escHtml(c.location)) +
    row('Why selected', escHtml(reg.why_selected || (company.fit_reasons || []).join(' · '))) +
    row('Status', '<b>' + escHtml(reg.status || company.register_status || '') + '</b>' +
      (reg.why_not_contacted || company.register_why ? ' — ' + escHtml(reg.why_not_contacted || company.register_why) : '')) +
    row('People named on site', list(people, 8)) +
    row('Contact form', form ? link(form.value_text, form.value_text) : '') +
    row('Hiring', list(by('HIRING_ROLE'), 6)) +
    row('Tech on website', escHtml(by('TECH_STACK').map(o => o.value_text).slice(0, 1).join(''))) +
    row('Website issues', sitewatch) +
    row('Responsive &amp; UI (SiteWatch)', swAudit('SITEWATCH_UI_ISSUES', 'responsive & UI')) +
    row('SEO (SiteWatch)', swAudit('SITEWATCH_SEO_ISSUES', 'SEO')) +
    row('Other signals', other.slice(0, 8).map(o => escHtml(o.label) + ': ' +
      escHtml(o.value_text || String(o.value_num))).join('<br>')) +
    row('Found via', c.source_url ? link(c.source_url, 'search result') + ' · ' + escHtml(c.source || '') : escHtml(c.source || '')) +
    row('First found', escHtml((c.created_at || '').slice(0, 10))) +
    '</tbody></table></div>';
  return html;
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const [data, profile] = await Promise.all([
    api('/api/companies/' + encodeURIComponent(company.id) + '/contacts'),
    api('/api/companies/' + encodeURIComponent(company.id) + '/profile'),
  ]);
  let html = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
    '&larr; All companies</button>' + companyInfoCard(company, profile) +
    '<div class="card flush"><div class="card-head"><h3>Contacts</h3>' +
    '<span class="meta">' + escHtml([company.industry, company.location].filter(Boolean).join(' · ')) + '</span></div>';
  if (!data || !data.length) {
    html += '<div class="empty"><div class="copy">No contacts found at this company yet.</div></div></div>';
  } else {
    html += '<table><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const phoneIcon = p.phone_verified ? ' <span class="muted">&#10003;</span>' : '';
      html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
          '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
          '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
        '<td>' + escHtml(p.email) + emailTag(p) + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (safeHref(p.linkedin_url) ? '<a href="' + safeHref(p.linkedin_url) + '" target="_blank" rel="noopener noreferrer">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + sourceLabel(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(entityType === 'campaign'
    ? 'Saved. Harvey follows it in the emails it writes from now on.'
    : 'Saved. Harvey uses it if it writes this person\'s first email (Gmail, SMTP or Microsoft 365).', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Anything Harvey should know before writing to this person?');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'What should Harvey do differently in the emails it writes next? (e.g. "shorter; don\'t mention reviews")');
}

// Search and status filter live outside the table, so the 15-second refresh
// re-renders rows without losing what you typed.
let _contactQuery = '', _contactFilter = 'all';
const CONTACT_FILTERS = [
  ['all', 'All', null],
  ['todo', 'To write', ['new', 'queued']],
  ['contacted', 'Contacted', ['contacted']],
  ['replied', 'Replied', ['replied', 'interested', 'meeting']],
  ['closed', 'Closed out', null],
];

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects');
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('contacts'), 'No contacts yet',
      'Harvey hasn\'t found any prospects. Once it\'s running, the Scout agent searches the web for people matching your ideal customer profile in <b>harvey.yaml</b>.');
    return;
  }
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search">' + icon('discover') +
        '<input class="form-input" id="contact-search" type="search" placeholder="Search name, company, email" ' +
        'aria-label="Search contacts" oninput="_contactQuery=this.value;renderContacts()"></label>' +
      '<div class="segmented" id="contact-filters" role="group" aria-label="Filter by status"></div>' +
      '</div><div id="contact-rows"></div>';
    // A rebuild after an offline blip must show the search still applied.
    document.getElementById('contact-search').value = _contactQuery;
  }
  renderContacts();
}

function contactMatches(p, f) {
  const known = ['new', 'queued', 'contacted', 'replied', 'interested', 'meeting'];
  if (f === 'all') return true;
  if (f === 'closed') return !known.includes(p.status);
  const hit = CONTACT_FILTERS.find(x => x[0] === f);
  return !!(hit && hit[2] && hit[2].includes(p.status));
}

function setContactFilter(f) { _contactFilter = f; renderContacts(); }

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  if (!rowsEl) return;
  const q = _contactQuery.trim().toLowerCase();
  const found = p => !q || [p.first_name, p.last_name, p.company, p.email, p.title].join(' ').toLowerCase().includes(q);
  filtersEl.innerHTML = CONTACT_FILTERS.map(f =>
    '<button class="' + (f[0] === _contactFilter ? 'on' : '') + '" aria-pressed="' + (f[0] === _contactFilter) + '" onclick="setContactFilter(\'' + f[0] + '\')">' +
      f[1] + ' <span class="n">' + _prospects.filter(p => contactMatches(p, f[0]) && found(p)).length + '</span></button>'
  ).join('');

  const rows = _prospects.map((p, i) => [p, i]).filter(([p]) => contactMatches(p, _contactFilter) && found(p));
  if (!rows.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), 'No matches',
      'Nothing matches that search and filter.') + '</div>';
    return;
  }
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Name</th><th>Company</th><th>Email</th>' +
    '<th>Score</th><th>Status</th><th>Source</th><th>Added</th><th></th></tr></thead><tbody>';
  for (const [p, i] of rows) {
    html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
        '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
      '<td>' + escHtml(p.company) + '</td>' +
      '<td><div class="cell-stack"><span>' + escHtml(p.email) + '</span>' +
        '<span class="cell-sub">' + [emailTag(p).trim(), p.phone ? escHtml(p.phone) +
          (p.phone_verified ? ' <span class="verified" title="Phone verified">&#10003;</span>' : '') : '']
          .filter(Boolean).join(' · ') + '</span>' +
        '</div></td>' +
      '<td>' + (p.score || p.score === 0 ? meterHtml(p.score) : '') + '</td>' +
      '<td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + sourceLabel(p.source) + '</td>' +
      '<td class="muted" style="white-space:nowrap">' + relTime(p.created_at) + '</td>' +
      '<td><div class="cell-row">' + (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id
        ? '<button class="btn btn-ghost btn-sm" onclick="toggleOptOut(' + i + ')" ' +
          'aria-expanded="' + (_optOut.id === p.id) + '" title="What made Harvey stop emailing them">' +
          (p.status === 'opted_out' ? 'Why?' : 'Opt-out history') + '</button>' : '') +
        '<button class="btn btn-ghost btn-sm row-action" onclick="fbProspect(' + i + ')" ' +
        'title="Leave feedback on this contact">Feedback</button></div></td></tr>';
    if (_optOut.id === p.id) html += '<tr class="detail-row"><td colspan="8">' + optOutPanel(p) + '</td></tr>';
  }
  rowsEl.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + rows.length + ' of ' + _prospects.length + ' contacts</div>';
}

// Why someone is opted out, and undoing it. Harvey never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = {id: null, data: null, armed: false, busy: false};
const OPT_OUT_RULES = {
  phrase: 'Matched an opt-out phrase',
  no_thanks: 'Matched “no thanks”',
  only_words: 'Their reply was only an opt-out word',
  model: 'The model’s judgement (no phrase matched)',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = {id: null, data: null, armed: false, busy: false}; renderContacts(); return; }
  _optOut = {id: p.id, data: null, armed: false, busy: false};
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || {error: true};
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">Loading…</div>';
  if (d.error) return '<div class="optout-panel muted">Couldn’t load the opt-out record. Try again.</div>';
  let html = '<div class="optout-panel"><h4>' + (d.opted_out ? 'Why Harvey stopped emailing ' : 'Opt-out history for ') +
    name + '</h4>';
  if (d.recorded) {
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(own(OPT_OUT_RULES, d.rule) || d.rule || '') + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>They opted out before Harvey kept a record of why.' +
      (d.message ? ' The last thing they wrote:' : ' There’s no reply from them on file.') + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || 'They') +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => 'You undid an opt-out ' + relTime(h.at)).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">Not opted out now (' +
      escHtml(statusMeta(d.status).label) + ').</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">Harvey will treat them as having replied. Nothing is ' +
      'sent automatically, emails the opt-out cancelled stay cancelled, and Harvey’s answer to their next ' +
      'reply waits for your approval, even on autopilot. Only undo it if they didn’t mean to opt out.</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
        '>Confirm undo</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">Undo opt-out</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true}),
  });
  if (res && res.success) showToast('Opt-out undone. They’re marked as replied; Harvey sends nothing on its own.', 'success');
  else showToast(failMsg(res, 'Could not undo the opt-out. Try again.'), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || {error: true};
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  goTab('prospects');
  _contactQuery = email || '';
  const search = document.getElementById('contact-search');
  if (search) search.value = _contactQuery;
  await loadProspects();
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? 'there' : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account Harvey sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast('Your browser blocked the Gmail tab. Allow pop-ups for this page and click again.', 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so Harvey can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast('This draft still has a {{placeholder}} in it. Fix it in Gmail before sending.', 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sent}),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch {}
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || 'Could not save that. Try again.', 'error');
  else if (!sent && res.sent) showToast('They already replied, so this stays marked as sent.');
  else showToast(sent ? 'Marked as sent by you.' : 'Unmarked.', 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    ['Contacted', count(['contacted']), 'c-accent'],
    ['Replied', count(['replied']), 'c-active'],
    ['Interested', count(['interested', 'meeting']), 'c-good'],
    ['Waiting', count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push(['Closed out', closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="Where this campaign\'s people stand">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + p[0] + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + p[0] + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), 'No campaigns yet',
      'The Writer agent hasn\'t drafted any sequences. It kicks in automatically once Harvey has scored prospects to write for.');
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when Harvey can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; Sent by you</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: Harvey never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            'Open in Gmail</button>' +
            '<span class="muted" style="font-size:12px">Harvey won\'t send follow-ups to people you emailed yourself.</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // Harvey sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="muted" style="display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">' +
              '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
              'I already emailed ' + escHtml(r.email) + ' myself</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">Catch-all, may bounce</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          'Open in Gmail</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> Mark as sent</label>'
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + (s === 0 ? 'First email' : 'Follow-up') +
        (step.delay_days ? ' &middot; ' + escHtml(String(step.delay_days)) + ' days after the last' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
        '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span>' +
        '<span>' + (c.sequence || []).length + ' emails</span><span>Created ' + relTime(c.created_at) + '</span></div>' +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted" style="font-size:13px;padding-top:16px">No email steps in this campaign.</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: Harvey's cold emails (from the Outbox), the
// replies, Harvey's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const STAGE_LABELS = {
  initial_outreach: 'Outreach', engaged: 'Engaged', qualifying: 'Qualifying', presenting: 'Presenting',
  negotiating: 'Negotiating', closing: 'Closing', closed_won: 'Won', closed_lost: 'Lost',
};
const CONVO_FILTERS = [
  ['needs', 'Needs you', c => !!c.draft || c.status === 'needs_human' || !!c.new],
  ['open', 'Open', c => c.status !== 'closed' && c.status !== 'awaiting_reply'],
  ['awaiting', 'Awaiting reply', c => c.status === 'awaiting_reply'],
  ['closed', 'Closed', c => c.status === 'closed'],
  ['all', 'All', () => true],
];

async function loadConversations(listOnly) {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations?awaiting=1');
  if (!data) { if (!listOnly) el.innerHTML = offlineState(); return; }
  _convos = data;
  if (listOnly && document.querySelector('.convo-list')) { renderConvoList(); return; }
  if (!data.length) {
    el.innerHTML = emptyState(icon('conversations'), 'No conversations yet',
      'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.');
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][2]))
    _convoFilter = data.some(CONVO_FILTERS[1][2]) ? 'open' : 'all';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

const convoKey = c => c.id || ('await-' + (c.prospect_email || ''));

function convoFilterBar(filter) {
  return '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter conversations">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(\'' + f[0] + '\')">' + f[1] + ' <span class="n">' +
      _convos.filter(f[2]).length + '</span></button>').join('') + '</div></div>';
}

// The conversations the current filter shows, in order.
function shownConvos(filter) {
  // Handed-to-you first, then unread, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : (c.new || c.unseen) ? 1 : c.draft ? 2 : 3;
  return _convos.filter(filter[2]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
}

function convoListHtml(shown) {
  return shown.map(c => {
    const last = convoLastMessage(c);
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.company || c.prospect_email || 'Unknown';
    const unread = c.new || c.unseen;
    return '<button class="convo-item' + (convoKey(c) === _convoSelected ? ' on' : '') + (unread ? ' unread' : '') +
      '" onclick="selectConvo(' + jsq(convoKey(c)) + ')">' +
      '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
      '<span class="ci-main"><span class="ci-top"><b>' + escHtml(name) + '</b>' +
        '<span class="ci-when">' + (last.ts ? relTime(last.ts) : '') + '</span></span>' +
      '<span class="ci-preview">' + escHtml(last.text) + '</span></span>' +
      (unread ? '<span class="ci-dot" title="New message"></span>' : c.draft ? '<span class="ci-draft">Draft</span>' : '') +
      '</button>';
  }).join('');
}

// Refresh just the list on the left (and the filter counts), leaving the open
// thread and anything typed in it alone.
function renderConvoList() {
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[CONVO_FILTERS.length - 1];
  const listEl = document.querySelector('.convo-list');
  const bar = document.querySelector('#conversations-list > .toolbar');
  if (!listEl) return renderConversations();
  const keep = listEl.scrollTop;
  listEl.innerHTML = convoListHtml(shownConvos(filter));
  listEl.scrollTop = keep;
  if (bar) bar.outerHTML = convoFilterBar(filter);
}

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[CONVO_FILTERS.length - 1];
  const bar = convoFilterBar(filter);
  const shown = shownConvos(filter);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nothing here',
      'No conversation matches this filter.') + '</div>';
    return;
  }
  if (!shown.some(c => convoKey(c) === _convoSelected)) _convoSelected = convoKey(shown[0]);
  const selected = shown.find(c => convoKey(c) === _convoSelected);
  // Keep the open thread's scroll across the 5-second refresh; follow new
  // messages only when you were already at the bottom.
  const oldPane = document.querySelector('.convo-pane');
  const wasAtBottom = !oldPane || oldPane.scrollHeight - oldPane.scrollTop - oldPane.clientHeight < 40;
  const keepScroll = oldPane && oldPane.dataset.key === _convoSelected ? oldPane.scrollTop : null;
  el.innerHTML = bar + '<div class="convo-layout"><div class="convo-list">' + convoListHtml(shown) + '</div>' +
    '<div class="convo-pane" data-key="' + escHtml(_convoSelected) + '">' + convoCard(selected) + '</div></div>';
  const pane = el.querySelector('.convo-pane');
  if (keepScroll !== null && !wasAtBottom) pane.scrollTop = keepScroll;
  else pane.scrollTop = pane.scrollHeight;
  markConvoSeen(selected);
}

// The open conversation's messages count as read once it's on screen.
async function markConvoSeen(c) {
  if (!c || !c.id || !c.unseen || document.hidden || currentTab !== 'conversations') return;
  c.unseen = false;
  const res = await api('/api/conversations/' + encodeURIComponent(c.id) + '/seen',
    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  if (res) refreshNavCounts();
  const item = document.querySelector('.convo-item.on');
  if (item && !c.new) {
    item.classList.remove('unread');
    const dot = item.querySelector('.ci-dot');
    if (dot) dot.remove();
  }
}

let _convoSelected = '';

function selectConvo(k) {
  if (replyInProgress() && !confirm('Discard the reply you were writing?')) return;
  _convoSelected = k;
  renderConversations();
}

// How Harvey answers this customer: its own choice, else what Settings → Sending
// does (autopilot replies on its own, approval mode drafts for you).
function convoReplyMode(c) {
  return c.reply_mode || c.reply_mode_default || 'approve';
}

// The newest thing in a thread, for the list preview.
function convoLastMessage(c) {
  const all = (c.thread || []).map(m => ({ts: m.timestamp, text: (m.sender === 'prospect' ? '' : 'You: ') + m.content}))
    .concat((c.emails || []).map(e => ({ts: e.sent_at, text: 'You: ' + (e.subject || e.body || '')})))
    .concat((c.outgoing || []).map(m => ({ts: m.timestamp, text: 'Sending: ' + m.content})));
  all.sort((a, b) => (+parseTs(a.ts) || 0) - (+parseTs(b.ts) || 0));
  const last = all[all.length - 1] || {ts: c.updated_at, text: ''};
  return {ts: last.ts, text: (last.text || '').replace(/\s+/g, ' ').slice(0, 90)};
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ts: e.sent_at, email: e}))
    .concat((c.thread || []).map(m => ({ts: m.timestamp, msg: m})))
    .map(it => ({...it, key: +parseTs(it.ts)}))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || 'Them';
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>Email ' + escHtml(String(it.email.step)) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.sender === 'harvey' || m.sender === 'you';
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      (m.sender === 'you' ? 'You' : sent ? 'Harvey' : escHtml(first)) + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const sending = (c.outgoing || []).map(m =>
    '<div class="thread-msg sent sending"><div class="sender">' + (m.sender === 'you' ? 'You' : 'Harvey') +
    ' &middot; sending…</div>' + escHtml(m.content) + '</div>').join('');
  const canReply = c.id && c.status !== 'awaiting_reply' && c.prospect_status !== 'opted_out';
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + 'Harvey drafted a reply · edit it if you like, then send</div>' +
      '<textarea class="form-input convo-reply" id="draft-' + escHtml(c.draft.id) + '" aria-label="Reply draft">' +
        escHtml(c.draft.body) + '</textarea>' +
      '<div class="convo-reply-actions">' +
        '<button class="btn btn-primary btn-sm" onclick="sendDraft(' + jsq(c.draft.id) + ',' + jsq(c.draft.subject || '') + ',this)">Approve &amp; send</button>' +
        '<button class="btn btn-ghost btn-sm" onclick="discardDraft(' + jsq(c.draft.id) + ',this)">Discard</button></div></div>'
    : canReply
      ? '<div class="thread-draft convo-compose"><div class="sender">' + icon('pen') + 'Reply to ' + escHtml(first) + '</div>' +
        '<textarea class="form-input convo-reply" id="reply-' + escHtml(c.id) + '" placeholder="Write your reply…" aria-label="Your reply"></textarea>' +
        '<div class="convo-reply-actions"><button class="btn btn-primary btn-sm" onclick="sendReply(' + jsq(c.id) + ',this)">Send reply</button></div></div>'
      : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.company || 'Unknown';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (c.id && c.prospect_status !== 'opted_out'
      ? '<select class="form-input reply-mode" title="How Harvey answers this customer" onchange="setReplyMode(' + jsq(c.id) + ',this.value)">' +
        '<option value="approve"' + (convoReplyMode(c) === 'approve' ? ' selected' : '') + '>AI drafts · I approve</option>' +
        '<option value="auto"' + (convoReplyMode(c) === 'auto' ? ' selected' : '') + '>AI replies automatically</option>' +
        '<option value="manual"' + (convoReplyMode(c) === 'manual' ? ' selected' : '') + '>I\'ll manage it myself</option></select>'
      : '') +
      // No intent, stage or status labels here: the dropdown is the only control.
      '</div></div>' +
    '<div class="thread">' + ((thread + sending) || '<p class="muted" style="font-size:13px">No messages in this thread yet.</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

// Harvey answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  if (c.status === 'needs_human' && c.prospect_status === 'opted_out') {
    // A complaint or legal threat from someone who opted out: handing it
    // back wouldn't help, Harvey never answers them.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Opted out, and Harvey handed this to you. Reply from your inbox if you need to, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Harvey handed this to you. Reply from your inbox, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">Hand back to Harvey</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button></div>';
  }
  if (c.status === 'open') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">Close conversation</button></div>';
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: Harvey never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + 'Opted out. Harvey won’t write to them.</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">Reopen</button></div>';
  }
  return '';
}

// ── Replying from the Conversations tab ──

// ── Find leads: plain-language requirement → targeting → search ──

let _leadPoll = null;

// Location chips: pick several places to search.
let _leadLocations = [];

function renderLocChips() {
  const el = document.getElementById('lead-loc-chips');
  if (!el) return;
  el.innerHTML = _leadLocations.map((l, i) => '<span class="loc-chip">' + escHtml(l) +
    '<button type="button" aria-label="Remove ' + escHtml(l) + '" onclick="event.stopPropagation(); locRemove(' + i + ')">×</button></span>').join('');
}

function locAdd(value) {
  for (const v of String(value || '').split(';').map(s => s.trim()).filter(Boolean)) {
    if (!_leadLocations.some(l => l.toLowerCase() === v.toLowerCase())) _leadLocations.push(v);
  }
  renderLocChips();
}

function locRemove(i) { _leadLocations.splice(i, 1); renderLocChips(); }

function locKey(e) {
  const input = e.target;
  if (e.key === 'Enter' || e.key === ';' || e.key === 'Tab' && input.value.trim()) {
    e.preventDefault(); locAdd(input.value); input.value = '';
  } else if (e.key === 'Backspace' && !input.value && _leadLocations.length) {
    locRemove(_leadLocations.length - 1);
  }
}

// Picking a suggestion from the list adds it straight away.
function locPicked(input) {
  const opts = [...document.querySelectorAll('#lead-location-list option')].map(o => o.value);
  if (opts.includes(input.value)) { locAdd(input.value); input.value = ''; }
}

async function findLeads(btn, run) {
  const text = document.getElementById('lead-text').value.trim();
  locAdd(document.getElementById('lead-locations').value);
  document.getElementById('lead-locations').value = '';
  const locations = _leadLocations.slice();
  const autoSend = (document.querySelector('input[name="lead-send"]:checked') || {}).value === 'auto';
  if (text.length < 5) { showToast('Describe who you want to reach first.', 'error'); return; }
  if (!locations.length) { showToast('Choose at least one location.', 'error'); return; }
  if (autoSend && run && !confirm('AI will write AND send cold emails on its own to the leads it finds. Continue?')) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Understanding your request…';
  const r = await api('/api/leads/request', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text, locations, auto_send: autoSend, run})});
  btn.disabled = false;
  btn.textContent = label;
  if (!r || !r.success) { showToast(failMsg(r, 'Could not save that.'), 'error'); return; }
  showToast(!run ? 'Requirement saved.'
    : r.provider === 'web_search'
      ? 'Saved. Harvey will search the web for ' + r.spec.industries.join(', ') + ' in ' + r.spec.cities.join(', ') + ' on its next cycle.'
      : 'Saved. Searching for ' + r.spec.industries.join(', ') + ' in ' + r.spec.cities.join(', ') + '.', 'success');
  loadLeadRequest();
}

async function loadLeadRequest() {
  const el = document.getElementById('lead-status');
  const d = await api('/api/leads/request');
  if (!el || !d) return;
  // The live targeting from Harvey's config wins over the saved request: it
  // is what Scout actually searches with, even after a hand edit.
  const req = d.request || {};
  const cur = d.current;
  if (!d.request && !cur) { el.innerHTML = ''; return; }
  const saved = req.spec || {};
  const s = cur || saved;
  const sameAsSaved = JSON.stringify(s.industries || []) === JSON.stringify(saved.industries || []) &&
    JSON.stringify(s.cities || []) === JSON.stringify(saved.cities || []);
  const autoSend = cur ? cur.auto_send : req.auto_send;
  const box = document.getElementById('lead-text');
  if (box && !box.value) box.value = req.text || '';
  if (!_leadLocations.length) { _leadLocations = (s.cities || req.locations || []).slice(); renderLocChips(); }
  const radio = document.querySelector('input[name="lead-send"][value="' + (autoSend ? 'auto' : 'approve') + '"]');
  if (radio) radio.checked = true;
  const rep = d.report || {};
  const sourceLabel = req.provider === 'web_search' ? 'Web search (Harvey finds them each cycle)' : req.provider;
  const status = req.provider === 'web_search'
    ? '<span class="tag">Web search</span> These businesses aren\'t on the free map, so Harvey searches the web for them ' +
      'every cycle while it\'s running, keeps only ones based in your locations\' countries, then finds the person to email.'
    : d.running
    ? '<span class="tag">Searching…</span> Finding businesses and reading their websites. This takes a few minutes.'
    : rep.errors && rep.errors.length
      ? '<span class="tag">Finished with problems</span> ' + escHtml(rep.errors.join('; '))
      : '<span class="tag tag-new">Done</span> ' + (rep.new_companies != null ? rep.new_companies + ' new businesses found, ' : '') +
        (rep.profiled_companies != null ? rep.profiled_companies + ' websites read. ' : '') +
        'Harvey now finds the person to email at each one (Contacts) on its next cycles.';
  const row = (k, v) => '<div class="lead-row"><span>' + k + '</span><b>' + escHtml(v || '—') + '</b></div>';
  el.innerHTML = '<div class="card"><div class="card-head"><h3>What Harvey is looking for</h3></div>' +
    (saved.summary && sameAsSaved ? '<p class="lede">' + escHtml(saved.summary) + '</p>' : '') +
    row('Businesses', (s.industries || []).join(', ')) + row('Where', (s.cities || []).join(', ')) +
    row('Person to email', (s.titles || []).join(', ')) + row('Company size', s.company_size) +
    row('Source', sourceLabel || 'not searched yet') +
    row('Cold emails', autoSend ? 'AI sends automatically' : 'AI writes, you approve') +
    '<p class="lead-status">' + (req.provider ? status : 'Press <b>Save &amp; find leads</b> to start the search.') + '</p>' +
    '<div class="convo-reply-actions"><button class="btn btn-secondary btn-sm" onclick="goTab(\'companies\')">See companies</button>' +
    '<button class="btn btn-secondary btn-sm" onclick="goTab(\'prospects\')">See contacts</button></div></div>';
  clearTimeout(_leadPoll);
  if (d.running && currentTab === 'leads') _leadPoll = setTimeout(loadLeadRequest, 4000);
}

// ── Leads: what Harvey found, approved before it writes ──

let _foundLeads = [];
let _foundCounts = {};
let _foundTab = 'pending';
let _foundType = '';
let _foundSort = 'type';
let _foundQuery = '';
const _foundSelected = new Set();
const FOUND_TABS = [['pending', 'Waiting for approval'], ['approved', 'Approved'], ['rejected', 'Rejected'], ['all', 'All']];

let _foundSyncTimer = null;
let _foundSyncing = false;

// Ask SiteWatch about approved leads (send new ones, read back finished
// scans), then redraw if anything changed. Repeats while scans are running.
async function syncFoundReports() {
  clearTimeout(_foundSyncTimer);
  if (_foundSyncing) return;
  _foundSyncing = true;
  const r = await api('/api/sitewatch/sync', {method: 'POST'});
  _foundSyncing = false;
  if (r && r.active && (r.requested || r.observed)) await loadFoundLeads(true);
  const scanning = _foundLeads.some(l => l.report && l.report.state === 'pending');
  if (r && r.active && scanning && currentTab === 'found') _foundSyncTimer = setTimeout(syncFoundReports, 10000);
}

async function loadFoundLeads(skipSync) {
  const el = document.getElementById('found-list');
  const d = await api('/api/leads/found');
  if (!d) { el.innerHTML = offlineState(); return; }
  _foundLeads = d.leads || [];
  _foundCounts = d.counts || {};
  const ids = new Set(_foundLeads.map(l => l.id));
  [..._foundSelected].forEach(id => { if (!ids.has(id)) _foundSelected.delete(id); });
  navCount('nav-found', _foundLeads.filter(l => l.approval === 'pending' && l.status === 'new').length);
  renderFoundLeads();
  if (!skipSync) syncFoundReports();
}

function foundReportCell(l) {
  const r = l.report;
  if (l.approval !== 'approved') return '<span class="muted">After approval</span>';
  if (!r) return '<span class="muted">Not sent yet</span>';
  const id = jsq(l.id);
  const fix = '<div class="found-report-actions"><button class="btn btn-ghost btn-sm" onclick="leadReport([' + id + '], \'rescan\')">Scan again</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="leadReport([' + id + '], \'skip\')">Write without report</button></div>';
  if (r.state === 'pending') return '<span class="email-tag guess"><span class="spinner" aria-hidden="true" style="width:10px;height:10px;margin-right:4px"></span>scanning</span>' + fix;
  if (r.state === 'failed') return '<span class="email-tag invalid" title="' + escHtml(r.error || '') + '">scan failed</span>' + fix;
  if (r.state === 'skipped') return '<span class="email-tag guess" title="You chose to write without a report">no report (your choice)</span>';
  const status = r.status || '';
  const tag = '<span class="email-tag ' + ({clean: 'verified', review: 'risky', critical: 'invalid'}[status] || 'guess') + '">' + escHtml(status || 'done') + '</span>';
  const issues = r.issues ? '<div class="muted">' + r.issues + ' problem' + (r.issues === 1 ? '' : 's') + '</div>' : '';
  const links = swReportLinks(r, r.report_url);
  return tag + issues + (links ? '<div>' + links + '</div>' : '');
}

function _foundSelectedIds() { return [..._foundSelected]; }

function _foundVisible() {
  const q = _foundQuery.trim().toLowerCase();
  let rows = _foundLeads.filter(l =>
    (_foundTab === 'all' || l.approval === _foundTab) &&
    (!_foundType || l.business_type === _foundType) &&
    (!q || [l.company, l.first_name, l.last_name, l.email, l.domain, l.business_type].join(' ').toLowerCase().includes(q)));
  const by = {
    type: (a, b) => a.business_type.localeCompare(b.business_type) || String(a.company || '').localeCompare(String(b.company || '')),
    company: (a, b) => String(a.company || '').localeCompare(String(b.company || '')),
    newest: (a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')),
  }[_foundSort];
  return rows.sort(by);
}

const FOUND_PER_PAGE = 15;
const _foundOpen = new Set();      // leads whose details are open
const _foundDetails = {};          // id -> details from the server
const _foundPreview = {};          // id -> previewed email
let _foundPage = 0;

// Any change to what is shown starts again at page 1.
function foundFiltersChanged() { _foundPage = 0; renderFoundLeads(); }

function foundGoPage(page) { _foundPage = page; renderFoundLeads(); window.scrollTo({top: 0}); }

function renderFoundLeads() {
  document.getElementById('found-tabs').innerHTML = FOUND_TABS.map(([k, label]) =>
    '<button class="' + (k === _foundTab ? 'on' : '') + '" aria-pressed="' + (k === _foundTab) + '" onclick="_foundTab=\'' + k +
    '\'; _foundSelected.clear(); _foundPage = 0; renderFoundLeads()">' + label + ' <span class="n">' +
    (k === 'all' ? _foundLeads.length : (_foundCounts[k] || 0)) + '</span></button>').join('');

  // Business types in this tab, with how many leads each has.
  const inTab = _foundLeads.filter(l => _foundTab === 'all' || l.approval === _foundTab);
  const types = {};
  inTab.forEach(l => { types[l.business_type] = (types[l.business_type] || 0) + 1; });
  const typeSel = document.getElementById('found-type');
  if (_foundType && !types[_foundType]) _foundType = '';
  typeSel.innerHTML = '<option value="">All business types (' + inTab.length + ')</option>' +
    Object.keys(types).sort().map(t => '<option value="' + escHtml(t) + '"' + (t === _foundType ? ' selected' : '') + '>' +
      escHtml(t) + ' (' + types[t] + ')</option>').join('');
  document.getElementById('found-sort').value = _foundSort;

  const rows = _foundVisible();
  const bulk = document.getElementById('found-bulk');
  bulk.hidden = !_foundSelected.size;
  document.getElementById('found-selected').textContent = _foundSelected.size + ' selected';

  const el = document.getElementById('found-list');
  if (!_foundLeads.length) {
    el.innerHTML = '<div class="table-card">' + emptyState(icon('check'), 'No leads yet',
      'Set up who you want to reach in <b>Lead settings</b>. The leads Harvey finds show up here for you to approve.') + '</div>';
    return;
  }
  if (!rows.length) {
    el.innerHTML = '<div class="table-card">' + emptyState(icon('check'),
      _foundTab === 'pending' && !_foundQuery && !_foundType ? 'Nothing waiting for approval' : 'No matches',
      _foundTab === 'pending' && !_foundQuery && !_foundType ? 'New leads Harvey finds will wait here for your yes.' : 'No lead matches that search and filter.') + '</div>';
    return;
  }

  const pages = Math.max(1, Math.ceil(rows.length / FOUND_PER_PAGE));
  _foundPage = Math.min(Math.max(0, _foundPage), pages - 1);
  const start = _foundPage * FOUND_PER_PAGE;
  const pageRows = rows.slice(start, start + FOUND_PER_PAGE);
  const allOn = pageRows.every(l => _foundSelected.has(l.id));
  // Unchecked ("guess") addresses carry no tag in this list; checked ones do.
  const emailTag = s => {
    const cls = {verified: 'verified', risky: 'risky', invalid: 'invalid'}[s];
    return s && cls ? '<span class="email-tag ' + cls + '">' + escHtml(s) + '</span>' : '';
  };
  const approvalTag = a => '<span class="email-tag ' + ({approved: 'verified', rejected: 'invalid', pending: 'risky'}[a] || 'guess') + '">' +
    ({approved: 'approved', rejected: 'rejected', pending: 'waiting'}[a] || escHtml(a)) + '</span>';
  const actions = l => {
    const id = jsq(l.id);
    if (l.approval === 'pending') return '<button class="btn btn-primary btn-sm" onclick="setFoundApproval([' + id + '], \'approved\')">Approve</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setFoundApproval([' + id + '], \'rejected\')">Reject</button>';
    if (l.approval === 'approved' && l.status !== 'new') return '<span class="muted">' + escHtml(l.status) + '</span>';
    return '<button class="btn btn-ghost btn-sm" onclick="setFoundApproval([' + id + '], \'pending\')">Undo</button>';
  };

  let html = '<div class="table-card"><table class="found-table"><thead><tr>' +
    '<th class="found-check"><input type="checkbox" aria-label="Select all shown" ' + (allOn ? 'checked' : '') + ' onchange="foundSelectAll(this.checked)"></th>' +
    '<th>Company</th><th>Contact</th><th>Email</th><th>Location</th><th>Status</th><th>Website report</th><th class="found-actions-h"></th></tr></thead><tbody>';
  let group = null;
  pageRows.forEach(l => {
    if (_foundSort === 'type' && l.business_type !== group) {
      group = l.business_type;
      const n = rows.filter(r => r.business_type === group).length;
      html += '<tr class="found-group"><td colspan="8">' + escHtml(group) + ' <span class="muted">' + n + '</span></td></tr>';
    }
    const site = l.website || (l.domain ? 'https://' + l.domain : '');
    const name = [l.first_name, l.last_name].filter(Boolean).join(' ');
    html += '<tr class="' + (_foundSelected.has(l.id) ? 'sel' : '') + '">' +
      '<td class="found-check"><input type="checkbox" aria-label="Select" ' + (_foundSelected.has(l.id) ? 'checked' : '') +
        ' onchange="foundSelect(' + jsq(l.id) + ', this.checked)"></td>' +
      '<td><b>' + escHtml(l.company || '—') + '</b>' +
        (safeHref(site) ? '<div><a class="muted" href="' + safeHref(site) + '" target="_blank" rel="noopener noreferrer">' + escHtml(l.domain || site) + '</a></div>' : '') +
        (_foundSort !== 'type' ? '<div class="muted">' + escHtml(l.business_type) + '</div>' : '') + '</td>' +
      '<td>' + (name ? escHtml(name) : '<span class="muted">Company inbox</span>') +
        (l.title ? '<div class="muted">' + escHtml(l.title) + '</div>' : '') + '</td>' +
      '<td>' + escHtml(l.email || '') + ' ' + emailTag(l.email_status) + '</td>' +
      '<td class="muted">' + escHtml(l.location || '') + '</td>' +
      '<td>' + approvalTag(l.approval) + '</td>' +
      '<td class="found-report">' + foundReportCell(l) + '</td>' +
      '<td class="found-actions">' + actions(l) +
        '<button class="btn btn-ghost btn-sm" onclick="toggleFoundDetails(' + jsq(l.id) + ')">' + (_foundOpen.has(l.id) ? 'Hide' : 'Details') + '</button></td></tr>';
    if (_foundOpen.has(l.id)) html += '<tr class="found-detail-row"><td colspan="8">' + foundDetailsHtml(l) + '</td></tr>';
  });
  const pager = pages > 1
    ? '<div class="found-pager"><button class="btn btn-ghost btn-sm" ' + (_foundPage === 0 ? 'disabled ' : '') +
        'onclick="foundGoPage(' + (_foundPage - 1) + ')">&larr; Prev</button>' +
      '<span>Page ' + (_foundPage + 1) + ' of ' + pages + '</span>' +
      '<button class="btn btn-ghost btn-sm" ' + (_foundPage >= pages - 1 ? 'disabled ' : '') +
        'onclick="foundGoPage(' + (_foundPage + 1) + ')">Next &rarr;</button></div>'
    : '';
  el.innerHTML = html + '</tbody></table></div><div class="table-foot found-foot"><span>' + (start + 1) + '–' + (start + pageRows.length) +
    ' of ' + rows.length + ' lead' + (rows.length === 1 ? '' : 's') + '</span>' + pager + '</div>';
}

async function toggleFoundDetails(id) {
  if (_foundOpen.has(id)) { _foundOpen.delete(id); renderFoundLeads(); return; }
  _foundOpen.add(id);
  renderFoundLeads();
  if (!_foundDetails[id]) {
    const d = await api('/api/leads/' + encodeURIComponent(id) + '/details');
    _foundDetails[id] = d && d.success ? d : {error: (d && d.message) || 'Could not load the details.'};
    renderFoundLeads();
  }
}

async function previewFoundMessage(id) {
  _foundPreview[id] = {loading: true};
  renderFoundLeads();
  const r = await api('/api/email-template/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prospect_id: id})});
  _foundPreview[id] = r && r.success ? {subject: r.subject, body: r.body} : {error: (r && r.message) || 'Could not write a preview.'};
  renderFoundLeads();
}

function foundDetailsHtml(l) {
  const d = _foundDetails[l.id];
  if (!d) return '<div class="found-detail muted"><span class="spinner" aria-hidden="true"></span> Loading…</div>';
  if (d.error) return '<div class="found-detail error-text">' + escHtml(d.error) + '</div>';
  const pu = d.purpose || {}, ne = d.needs || {};
  const li = items => items.filter(Boolean).map(x => '<li>' + x + '</li>').join('');
  const why = li([
    pu.request_summary || pu.your_request ? '<b>Your requirement:</b> ' + escHtml(pu.request_summary || pu.your_request) : '',
    pu.business_type ? '<b>Business type:</b> ' + escHtml(pu.business_type) : '',
    pu.found_via ? '<b>Found via:</b> ' + escHtml(pu.found_via) +
      (safeHref(pu.found_url) ? ' (<a href="' + safeHref(pu.found_url) + '" target="_blank" rel="noopener noreferrer">source</a>)' : '') : '',
    typeof pu.fit === 'number' ? '<b>Fit:</b> ' + pu.fit + '/100' + ((pu.fit_reasons || []).length ? ' · ' + escHtml(pu.fit_reasons.join(' · ')) : '') : '',
  ]) || '<li class="muted">Nothing recorded.</li>';
  const needs = li([
    ne.about ? '<b>About them:</b> ' + escHtml(ne.about) : '',
    ...(ne.website_problems || []).map(x => '<b>Website:</b> ' + escHtml(x)),
    ne.hiring ? '<b>Hiring:</b> ' + escHtml(ne.hiring) : '',
    ne.notes ? '<b>Notes:</b> ' + escHtml(ne.notes) : '',
    ...(ne.signals || []).slice(0, 6).map(s => '<b>' + escHtml(s.label) + ':</b> ' + escHtml(s.value)),
  ]) || '<li class="muted">Nothing known yet. Harvey learns more when it reads their website and SiteWatch scans it.</li>';
  let msg;
  if ((d.messages || []).length) {
    msg = d.messages.map(m => '<div class="found-msg"><div class="found-msg-head"><b>Email ' + escHtml(String(m.step || 1)) +
      (m.kind === 'reply' ? ' (reply)' : '') + '</b> <span class="email-tag ' +
      ({sent: 'verified', approved: 'verified', pending_review: 'risky', rejected: 'invalid', cancelled: 'guess'}[m.status] || 'guess') + '">' +
      escHtml(String(m.status || '').replace('_', ' ')) + '</span></div>' +
      '<div class="found-msg-subject">' + escHtml(m.subject || '') + '</div><pre class="found-msg-body">' + escHtml(m.body || '') + '</pre></div>').join('');
  } else {
    const pv = _foundPreview[l.id];
    msg = '<p class="muted">No email written yet. Harvey writes it after you approve the lead and its website report is ready.</p>' +
      (pv && pv.loading ? '<p class="muted"><span class="spinner" aria-hidden="true"></span> Writing a preview…</p>'
       : pv && pv.error ? '<p class="error-text">' + escHtml(pv.error) + '</p>'
       : pv ? '<div class="found-msg"><div class="found-msg-head"><b>Preview</b> <span class="email-tag guess">not saved, not sent</span></div>' +
              '<div class="found-msg-subject">' + escHtml(pv.subject) + '</div><pre class="found-msg-body">' + escHtml(pv.body) + '</pre></div>'
       : '') +
      (!(pv && pv.loading) ? '<button class="btn btn-secondary btn-sm" onclick="previewFoundMessage(' + jsq(l.id) + ')">' +
        (pv && !pv.error ? 'Preview again' : 'Preview message') + '</button>' : '');
  }
  return '<div class="found-detail"><div><h4>Why this lead</h4><ul>' + why + '</ul></div>' +
    '<div><h4>What they need</h4><ul>' + needs + '</ul></div>' +
    '<div><h4>Message</h4>' + msg + '</div></div>';
}

function foundSelect(id, on) { on ? _foundSelected.add(id) : _foundSelected.delete(id); renderFoundLeads(); }

function foundSelectAll(on) {
  _foundVisible().slice(_foundPage * FOUND_PER_PAGE, (_foundPage + 1) * FOUND_PER_PAGE)
    .forEach(l => on ? _foundSelected.add(l.id) : _foundSelected.delete(l.id));
  renderFoundLeads();
}

async function setFoundApproval(ids, approval) {
  if (!ids.length) return;
  if (approval === 'rejected' && ids.length > 1 && !confirm('Reject ' + ids.length + ' leads? Harvey will never email them.')) return;
  const r = await api('/api/leads/approval', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids, approval})});
  showToast((r && r.message) || 'Couldn\'t change that.', r && r.success ? 'success' : 'error');
  ids.forEach(id => _foundSelected.delete(id));
  loadFoundLeads();
}

async function leadReport(ids, action) {
  const r = await api('/api/leads/report', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids, action})});
  showToast((r && r.message) || 'Could not do that.', r && r.success ? 'success' : 'error');
  loadFoundLeads();
}

let _campaignPoll = null;

async function createCampaign(ids) {
  if (!ids.length) return;
  const btn = document.getElementById('found-create-btn');
  const r = await api('/api/campaigns/create', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids})});
  if (!r || !r.success) { showToast((r && r.message) || 'Could not start the campaign.', 'error'); return; }
  const skipped = Object.entries(r.skipped || {}).map(([k, v]) => v + ' ' + k).join(', ');
  showToast(r.message + (skipped ? ' Skipped: ' + skipped + '.' : ''), 'success');
  _foundSelected.clear(); renderFoundLeads();
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>Writing…'; }
  clearTimeout(_campaignPoll);
  const poll = async () => {
    const st = await api('/api/campaigns/create');
    if (st && st.running) { _campaignPoll = setTimeout(poll, 4000); return; }
    if (btn) { btn.disabled = false; btn.textContent = 'Create campaign'; }
    const rep = (st && st.report) || {};
    if (rep.error) showToast('Writing failed: ' + rep.error, 'error');
    else showToast('Campaign written for ' + (rep.written || 0) + ' of ' + (rep.requested || 0) + ' leads. ' +
      ((rep.written || 0) < (rep.requested || 0) ? 'Harvey writes up to 8 per business type at a time; run it again for the rest. ' : '') +
      'Drafts wait in the Outbox for your approval.', rep.written ? 'success' : 'error');
    loadFoundLeads();
  };
  _campaignPoll = setTimeout(poll, 4000);
}

// ── Per-conversation reply mode (dropdown in the chat header) ──

async function setReplyMode(convoId, mode) {
  const r = await api('/api/conversations/' + encodeURIComponent(convoId) + '/mode',
    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode})});
  if (r && r.success) showToast(mode === 'auto'
    ? 'AI will reply to this customer on its own.'
    : mode === 'manual'
      ? 'You manage this conversation: AI won\'t draft or send replies here.' +
        (r.withdrawn ? ' Its waiting reply was withdrawn.' : '')
      : 'AI will draft replies here; you approve before they go.', 'success');
  else showToast(failMsg(r, 'Could not change that.'), 'error');
  loadConversations();
}

// New customer messages nobody has answered, or threads handed to you.
// Conversations with a customer message you haven't opened yet (even if
// Harvey already answered it), or handed to you.
function convoBadge(stats) {
  const s = stats || {};
  return Math.max(s.conversations_unseen || 0, s.needs_human || 0);
}

// A reply being typed or edited must survive the tab's auto-refresh.
function replyInProgress() {
  return [...document.querySelectorAll('.convo-reply')]
    .some(t => t === document.activeElement || t.value !== t.defaultValue);
}

const SENDS_SOON = 'Harvey is sending it now (a few seconds, while Harvey is running).';

async function sendDraft(draftId, subject, btn) {
  const box = document.getElementById('draft-' + draftId);
  const text = (box && box.value || '').trim();
  if (!text) { showToast('The reply is empty.', 'error'); return; }
  btn.disabled = true;
  const json = {method: 'POST', headers: {'Content-Type': 'application/json'}};
  if (text !== box.defaultValue.trim()) {
    const edited = await api('/api/outbox/' + encodeURIComponent(draftId) + '/edit',
      {...json, body: JSON.stringify({subject: subject, body: text})});
    if (!edited || !edited.success) { btn.disabled = false; showToast(failMsg(edited, 'Could not save your edit.'), 'error'); return; }
  }
  const ok = await api('/api/outbox/' + encodeURIComponent(draftId) + '/approve', {...json, body: '{}'});
  if (ok && ok.success) showToast('Approved. ' + SENDS_SOON, 'success');
  else { btn.disabled = false; showToast(failMsg(ok, 'Could not approve that reply.'), 'error'); return; }
  loadConversations(); loadToday();
}

async function discardDraft(draftId, btn) {
  btn.disabled = true;
  const r = await api('/api/outbox/' + encodeURIComponent(draftId) + '/reject',
    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  if (r && r.success) showToast('Draft discarded. Write your own reply below.', 'success');
  else { btn.disabled = false; showToast(failMsg(r, 'Could not discard that draft.'), 'error'); return; }
  loadConversations(); loadToday();
}

async function sendReply(convoId, btn) {
  const box = document.getElementById('reply-' + convoId);
  const text = (box && box.value || '').trim();
  if (!text) { showToast('Write a reply first.', 'error'); return; }
  btn.disabled = true;
  const r = await api('/api/conversations/' + encodeURIComponent(convoId) + '/reply',
    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({body: text})});
  if (r && r.success) { box.value = ''; showToast('Reply queued. ' + SENDS_SOON, 'success'); }
  else { btn.disabled = false; showToast(failMsg(r, 'Could not send that reply.'), 'error'); return; }
  loadConversations(); loadToday();
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status}),
  });
  if (data && data.success) showToast(status === 'open' ? 'Open again. Harvey will answer the next reply.' : 'Closed. A new reply starts a new conversation.', 'success');
  else showToast(failMsg(data, 'Could not update that conversation.'), 'error');
  loadConversations();
  loadToday();
}

// ── Activity: what happened, filtered, with the noise folded away ──

let _activity = [];
let _activitySummary = {};
let _activityFilter = 'all';
let _activityQuery = '';
let _activityCheckins = false;

const ACTIVITY_FILTERS = [['all', 'All'], ['emails', 'Emails'], ['replies', 'Replies'], ['leads', 'Leads'],
                          ['you', 'By you'], ['problems', 'Problems']];
const AGENT_NAMES = {scout: 'Lead finder', writer: 'Writer', sender: 'Sender', handler: 'Replies',
                     analyst: 'Analyst', dashboard: 'You', user: 'You', main: 'Harvey'};

function activityKind(a) {
  const t = a.action_type;
  if (['bounce', 'write_failed', 'escalation', 'reply_after_opt_out'].includes(t)) return 'problems';
  if (['email_sent', 'stage_campaign', 'write_campaign', 'send_campaign', 'outbox_edited', 'manual_send', 'manual_send_undone'].includes(t)) return 'emails';
  if (['reply', 'reply_queued', 'manual_reply', 'conversation_status', 'conversation_reply_mode', 'opt_out', 'opt_out_undone'].includes(t)) return 'replies';
  if (['prospect', 'lead_approval'].includes(t)) return 'leads';
  return 'other';
}

// Check-ins and "searched, nothing new" are routine: shown dimmed, and a run
// of them folds into one line.
function activityQuiet(a) {
  return a.agent === 'main' || (a.action_type === 'prospect' && !Number((a.details || {}).prospects_found));
}

function activityMatches(a) {
  const byYou = a.agent === 'dashboard' || a.agent === 'user';
  if (_activityFilter === 'you' && !byYou) return false;
  if (!['all', 'you'].includes(_activityFilter) && activityKind(a) !== _activityFilter) return false;
  const q = _activityQuery.trim().toLowerCase();
  if (!q) return true;
  const d = a.details || {};
  return [actionText(a).replace(/<[^>]+>/g, ''), a.action_type, a.agent, d.prospect_email, d.prospect, d.campaign_name]
    .join(' ').toLowerCase().includes(q);
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const box = document.getElementById('activity-checkins');
  if (box) box.checked = _activityCheckins;
  const data = await api('/api/activity/feed?checkins=' + (_activityCheckins ? 'true' : 'false'));
  if (!data) { el.innerHTML = offlineState(); return; }
  _activity = data.items || [];
  _activitySummary = data.summary || {};
  renderActivity();
}

function renderActivity() {
  const s = _activitySummary;
  const tile = (label, value, note, tone) => '<div class="kpi"><div class="kpi-label">' + label + '</div>' +
    '<div class="kpi-value' + (value ? '' : ' zero') + (tone && value ? ' ' + tone : '') + '">' + (value || 0) + '</div>' +
    '<div class="kpi-note">' + note + '</div></div>';
  const last = s.last_checkin ? dayLabel(s.last_checkin) + ', ' + timeOfDay(s.last_checkin) : 'never';
  document.getElementById('activity-kpis').innerHTML =
    tile('New contacts found', s.leads_found, 'last 7 days') +
    tile('Emails sent', s.emails_sent, 'last 7 days') +
    tile('Replies handled', s.replies, 'last 7 days') +
    tile('Problems', s.problems, 'bounces, failed drafts, flagged · 7 days', 'act-bad') +
    '';
  document.getElementById('activity-filters').innerHTML = ACTIVITY_FILTERS.map(([k, label]) => {
    const count = k === 'all' ? _activity.length
      : _activity.filter(a => k === 'you' ? (a.agent === 'dashboard' || a.agent === 'user') : activityKind(a) === k).length;
    return '<button class="' + (k === _activityFilter ? 'on' : '') + '" aria-pressed="' + (k === _activityFilter) +
      '" onclick="_activityFilter=\'' + k + '\'; renderActivity()">' + label + ' <span class="n">' + count + '</span></button>';
  }).join('');

  const el = document.getElementById('activity-list');
  const rows = _activity.filter(activityMatches);
  const lastLine = '<div class="activity-last">' + icon('refresh') + ' Harvey last checked in: <b>' + escHtml(last) + '</b>' +
    (s.checkins_week ? ' · ' + s.checkins_week + ' check-ins in the last 7 days' : '') + '</div>';
  if (!rows.length) {
    el.innerHTML = lastLine + '<div class="card">' + emptyState(icon('activity'),
      _activity.length ? 'Nothing matches' : 'No activity yet',
      _activity.length ? 'Try another filter or search.' :
      'Leads found, emails written and sent, and replies show up here the moment they happen.') + '</div>';
    return;
  }

  let html = lastLine + '<div class="card"><div class="timeline">';
  let day = '';
  for (let i = 0; i < rows.length; i++) {
    const a = rows[i];
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    // Fold a run of routine entries on the same day into one line.
    if (activityQuiet(a)) {
      let j = i;
      while (j + 1 < rows.length && activityQuiet(rows[j + 1]) && dayLabel(rows[j + 1].created_at) === label) j++;
      if (j > i) {
        html += '<div class="tl-item quiet act-fold"><span class="tl-time">' + timeOfDay(rows[j].created_at) + '</span>' +
          '<span class="tl-icon">' + icon('refresh') + '</span>' +
          '<span class="tl-text">Routine checks ×' + (j - i + 1) + ' · ' + timeOfDay(rows[j].created_at) + '–' + timeOfDay(a.created_at) +
          ', nothing new</span><span class="tl-agent">Harvey</span></div>';
        i = j;
        continue;
      }
    }
    const kind = activityKind(a);
    html += '<div class="tl-item act-' + kind + (activityQuiet(a) ? ' quiet' : '') + '"><span class="tl-time">' + timeOfDay(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || (a.agent === 'dashboard' ? 'contacts' : 'activity')) + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(own(AGENT_NAMES, a.agent) || a.agent) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// ── Init & live refresh ──

if (!authToken()) showLock('');  // straight to the sign-in page, no flash of an empty dashboard
loadToday();
loadTrend();
loadFunnel();
loadSetupStatus();
loadRuns();
loadTodayActivity();
loadHarveyStatus();

loadLiveStatus();

// Agent status: quick poll (not while locked: it would only be refused)
setInterval(() => { if (!_locked) loadHarveyStatus(); }, 8000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'today') loadLiveStatus();
}, 10000);

// Nav counts stay live wherever you are, so "something needs me" is visible
// from any tab without polling that tab's contents.
async function refreshNavCounts() {
  if (_locked) return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-conversations', convoBadge(data.stats));
}
setInterval(() => {
  if (document.hidden || _locked || currentTab === 'today') return;
  refreshNavCounts();
}, 10000);

// The Conversations tab follows new messages and your replies as they land.
// While you're typing a reply only the list on the left refreshes, so a new
// message still shows up without touching what you're writing.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'conversations') loadConversations(replyInProgress());
}, 5000);

// Data tabs: auto-refresh live views without clobbering anything in progress.
// Outbox, settings and help are deliberately excluded — re-rendering the desk
// under someone mid-decision loses their place, and settings may be mid-edit.
setInterval(() => {
  if (document.hidden || _locked) return;
  switch (currentTab) {
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadTodayActivity(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': break;  // its own 5-second refresh above
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'leads': if (_leadsPane === 'controls') loadLogs(); break;
  }
}, 15000);

// ── Brand name ──
//
// HARVEY_BRAND_NAME in .env shows another name instead of "Harvey" on every
// page (title, sidebar, sign-in card and all text), for a company running
// Harvey under its own product name. Commands and file names (in <code>) are
// left alone. Without it nothing changes.

const BRAND_SKIP = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE', 'TEXTAREA', 'INPUT', 'SELECT', 'OPTION', 'KBD']);
let _brand = '';

function brandText(s) {
  return _brand && s && s.indexOf('Harvey') !== -1 ? s.replace(/\bHarvey\b/g, _brand) : s;
}

function applyBrand(root) {
  if (!_brand || !root) return;
  if (root.nodeType === Node.TEXT_NODE) {
    const p = root.parentNode;
    if (p && !BRAND_SKIP.has(p.nodeName) && !(p.closest && p.closest('code, pre, [contenteditable]'))) {
      const next = brandText(root.nodeValue);
      if (next !== root.nodeValue) root.nodeValue = next;
    }
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE || BRAND_SKIP.has(root.nodeName)) return;
  for (const attr of ['title', 'placeholder', 'aria-label', 'alt']) {
    const v = root.getAttribute && root.getAttribute(attr);
    if (v && v.indexOf('Harvey') !== -1) root.setAttribute(attr, brandText(v));
  }
  if (root.closest && root.closest('code, pre, [contenteditable]')) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n = walker.nextNode();
  while (n) {
    if (n.nodeType === Node.TEXT_NODE) applyBrand(n);
    else if (n.getAttribute) {
      for (const attr of ['title', 'placeholder', 'aria-label', 'alt']) {
        const v = n.getAttribute(attr);
        if (v && v.indexOf('Harvey') !== -1) n.setAttribute(attr, brandText(v));
      }
    }
    n = walker.nextNode();
  }
}

async function loadBrand() {
  try {
    const r = await authFetch('/auth/status');
    const s = await r.json();
    const name = String((s && s.brand) || '').trim();
    if (!name || name === 'Harvey') return;
    _brand = name;
  } catch { return; }
  document.title = brandText(document.title);
  applyBrand(document.body);
  // Pages render their text later (tabs, tables, toasts): brand those too.
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'characterData') applyBrand(m.target);
      else m.addedNodes.forEach(applyBrand);
    }
  }).observe(document.body, {childList: true, subtree: true, characterData: true});
}

loadBrand();
