'use strict';
const $ = id => document.getElementById(id);
const all = sel => [...document.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let role = 'employer';
let toastTimer;
let lastExtract = null;

function notify(msg) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts });
  let data = null;
  try { data = await r.json(); } catch { /* ignore */ }
  if (r.status === 401 && path !== '/api/session') showLogin((data && data.detail) || '請先登入');
  if (!r.ok) throw new Error((data && data.detail) || `伺服器錯誤（${r.status}）`);
  return data;
}

/* ---------------- login (S01) ---------------- */
let me = null;
function showLogin(msg) {
  me = null;
  document.body.classList.add('logged-out');
  $('login').hidden = false;
  $('login-error').hidden = !msg; $('login-error').textContent = msg || '';
  $('login-error').classList.toggle('info', msg === '已登出。');
  $('login-code').value = '';
  setTimeout(() => $('login-account').focus(), 0);
}
function applySession(data) {
  me = data;
  document.body.classList.remove('logged-out');
  $('login').hidden = true;
  const c = (data.cases || [])[0];
  $('who').textContent = data.account.display_name + (c ? `・案件 ${c.display_code}` : '');
  applyRole(data.account.role);
  refreshCount();
}
$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = $('login-submit'); btn.disabled = true;
  try {
    const data = await api('/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account: $('login-account').value, code: $('login-code').value }) });
    applySession(data);
  } catch (err) {
    $('login-error').hidden = false; $('login-error').classList.remove('info'); $('login-error').textContent = err.message;
    $('login-code').focus();
  } finally { btn.disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try { await api('/api/session', { method: 'DELETE' }); } catch { /* ignore */ }
  showLogin('已登出。');
});

/* ---------------- navigation ---------------- */
function showView(name) {
  const permitted = role === 'employer' ? ['upload', 'tasks', 'chat', 'line'] : ['review', 'chat'];
  if (!permitted.includes(name)) return;
  all('.view').forEach(v => { v.hidden = v.id !== 'view-' + name; });
  all('.nav-item').forEach(b => {
    const on = b.dataset.view === name;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  window.scrollTo({ top: 0, behavior: 'instant' });
  if (name === 'tasks') loadTodos();
  if (name === 'line') loadLine();
}
function applyRole(r) {
  role = r;
  document.body.classList.toggle('officer', role === 'officer');
  all('[data-role]').forEach(b => { const on = b.dataset.role === role; b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
  all('[data-access]').forEach(el => { el.hidden = el.dataset.access !== 'both' && el.dataset.access !== role; });
  const label = role === 'employer' ? '雇主專區' : '市府承辦人專區';
  $('role-label').textContent = label; $('chat-role').textContent = label;
  showView(role === 'employer' ? 'upload' : 'review');
}
all('[data-view]').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
all('[data-goto]').forEach(b => b.addEventListener('click', () => showView(b.dataset.goto)));
document.querySelector('.brand').addEventListener('click', e => { e.preventDefault(); showView(role === 'employer' ? 'upload' : 'review'); });

/* ---------------- health ---------------- */
(async () => {
  const b = $('health-badge');
  try {
    const h = await api('/api/health');
    b.classList.add('health-ok');
    b.innerHTML = `<span></span>系統連線正常・法條 ${h.articles} 條・辦公日曆 ${h.calendar_years.join('、')}`;
    $('cal-years').textContent = h.calendar_years.map(y => `${y - 1911} 年`).join('、');
  } catch {
    b.classList.add('health-bad'); b.innerHTML = '<span></span>後端未連線';
  }
  try { applySession(await api('/api/session')); } catch { showLogin(''); }
})();

/* ---------------- upload & extract ---------------- */
const SAMPLES = [
  ['聘僱許可函_掃描.png', '聘僱許可函（掃描圖片）', '圖片改用較強模型判讀'],
  ['聘僱許可函_國字日期.txt', '聘僱許可函（國字日期）', '例：一一五年…'],
  ['入國通報證明_國字日期.txt', '入國通報受理證明書（國字日期）', ''],
  ['入國通報證明_春節連假.txt', '入國通報受理證明書（遇春節）', '期限遇連假自動順延'],
  ['住宿地點變更_掃描.png', '住宿地點變更表（掃描，2 位移工）', ''],
];
/* samples dated relative to today, so LINE reminders fire immediately during the demo */
const rocDate = d => `${d.getFullYear() - 1911}年${d.getMonth() + 1}月${d.getDate()}日`;
const daysAgo = n => { const d = new Date(); d.setDate(d.getDate() - n); return d; };
const DYNAMIC = {
  'live:entry': () => ['入國通報證明_昨天入國.txt', [
    '【合成測試文件・非真實公文・僅供竹聘安 AI 驗證使用】', '新竹市政府', '受理雇主聘僱外國人入國通報證明書',
    '證明書字號：府勞外字第1159990001號', '雇主名稱：風城精密工業股份有限公司', '雇主地址：新竹市東區光復路一段100號',
    '外國人姓名：PUTRI AYU LESTARI', '國籍：印尼', '護照號碼：C7788123',
    `入國日期：${rocDate(daysAgo(1))}`, `通報日期：${rocDate(daysAgo(0))}`, '住宿地點：新竹市北區中正路200號',
    '', '茲證明上開雇主已依雇主聘僱外國人許可及管理辦法第34條規定辦理入國通報。', '', `中華民國${rocDate(daysAgo(0))}`].join('\n')],
  'live:dorm': () => ['住宿地點變更_前天搬遷.txt', [
    '【合成測試文件・非真實公文・僅供竹聘安 AI 驗證使用】',
    '外國人生活照顧服務計畫書　變更事項表（第五款：住宿地點及生活照顧服務人員）',
    '雇主名稱：竹塹電子材料股份有限公司', `填表日期：${rocDate(daysAgo(1))}`, '',
    '原住宿地點：新竹市香山區西濱路一段50號', '新住宿地點：新竹市東區經國路一段300號',
    `新宿舍租賃契約起租日：${rocDate(daysAgo(15))}`, `外國人遷入新住宿地點日期（變更日）：${rocDate(daysAgo(2))}`,
    '', '異動外國人：', '（1）NGUYEN VAN AN　越南　護照號碼 B5566778', '（2）TRAN THI HONG　越南　護照號碼 B5566779'].join('\n')],
};
SAMPLES.unshift(['live:entry', '★ 昨天剛入國的移工（現場展示用）', '依今天日期產生，期限就在這幾天'],
                ['live:dorm', '★ 前天搬宿舍（現場展示用）', '7 日內須通報，可立即測 LINE 推播']);
$('sample-buttons').innerHTML = SAMPLES.map(([f, t, s]) => `<button type="button" data-sample="${esc(f)}">${esc(t)}${s ? `<small>${esc(s)}</small>` : ''}</button>`).join('');
all('[data-sample]').forEach(b => b.addEventListener('click', async () => {
  const name = b.dataset.sample;
  if (DYNAMIC[name]) {
    const [fname, text] = DYNAMIC[name]();
    uploadDocument(new File([text], fname, { type: 'text/plain' }));
    return;
  }
  const blob = await (await fetch('samples/' + encodeURIComponent(name))).blob();
  uploadDocument(new File([blob], name, { type: blob.type }));
}));
$('document-input').addEventListener('change', e => uploadDocument(e.target.files[0]));
const dz = $('dropzone');
['dragenter', 'dragover'].forEach(n => dz.addEventListener(n, e => { e.preventDefault(); dz.classList.add('dragging'); }));
['dragleave', 'drop'].forEach(n => dz.addEventListener(n, e => {
  e.preventDefault(); dz.classList.remove('dragging');
  if (n === 'drop') uploadDocument(e.dataTransfer.files[0]);
}));

function setStep(n) {
  all('#stepper li').forEach((li, i) => { li.classList.toggle('current', i === n - 1); li.classList.toggle('done', i < n - 1); });
}

async function uploadDocument(file) {
  if (!file) return;
  if (!/\.(jpe?g|png|pdf|txt)$/i.test(file.name)) { notify('請選擇 JPG、PNG、PDF 或 TXT 檔案。'); return; }
  $('uploaded-file').hidden = false;
  $('uploaded-name').textContent = file.name;
  $('upload-description').textContent = `${(file.size / 1024).toFixed(0)} KB`;
  $('upload-status').className = 'status warning'; $('upload-status').textContent = '辨識中';
  $('extract-empty').hidden = true; $('worker-form').hidden = true; $('result-card').hidden = true;
  $('extract-loading').hidden = false;
  const isImg = /\.(jpe?g|png)$/i.test(file.name);
  $('loading-text').textContent = isImg ? 'AI 正在閱讀掃描圖片…（約 20～40 秒）' : 'AI 正在閱讀公文…（約 10～20 秒）';
  $('flag-count').className = 'status neutral'; $('flag-count').textContent = '辨識中';
  setStep(1);
  const fd = new FormData(); fd.append('file', file);
  try {
    const data = await api('/api/extract', { method: 'POST', body: fd });
    lastExtract = data;
    renderExtract(data);
    $('upload-status').className = 'status ok'; $('upload-status').textContent = '已完成辨識';
    setStep(2);
  } catch (err) {
    $('extract-loading').hidden = true; $('extract-empty').hidden = false;
    $('upload-status').className = 'status danger'; $('upload-status').textContent = '辨識失敗';
    $('flag-count').textContent = '辨識失敗';
    notify(err.message);
  }
}

const FIELDS = {
  '聘僱許可函': [['entry_date', '入國日'], ['permit_start', '許可起日'], ['permit_end', '許可迄日']],
  '期滿續聘許可函': [['permit_start', '許可起日'], ['permit_end', '許可迄日']],
  '入國通報受理證明書': [['entry_date', '入國日']],
  '再檢查診斷證明書': [['event_date', '雇主收文日']],
  '住宿地點變更': [['event_date', '遷入新住處日（變更日）']],
  '聘僱關係終止': [['event_date', '聘僱關係終止日']],
  '移工失聯': [['event_date', '連續曠職滿 3 日之日'], ['notify_date', '實際書面通報日（選填）'], ['permit_end', '聘僱許可迄日（選填）']],
  '家事移工預定入國': [['entry_date', '預定入國日']],
};

/* 一站式服務：入國前尚無公文，直接開啟空白表單 */
$('onestop-btn').addEventListener('click', () => {
  lastExtract = { doc_type: '家事移工預定入國', is_image: false, model: '', workers: [{ name: '', entry_date: '', flags: {}, evidence: {} }] };
  $('uploaded-file').hidden = true;
  $('extract-empty').hidden = true; $('extract-loading').hidden = true; $('result-card').hidden = true;
  $('worker-form').hidden = false;
  $('doc-type').value = '家事移工預定入國';
  renderWorkerTable();
  $('flag-count').className = 'status warning'; $('flag-count').textContent = '入國前期限';
  $('model-note').textContent = '適用自國外新聘之家庭看護工／家庭幫傭；國內接續聘僱、期滿續聘不適用';
  setStep(2);
  document.querySelector('#worker-rows input[data-k=name]').focus();
  $('worker-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
});

/* 失聯不是公文：直接開啟空白表單讓雇主填寫 */
$('abscond-btn').addEventListener('click', () => {
  lastExtract = { doc_type: '移工失聯', is_image: false, model: '', workers: [{ name: '', event_date: '', job: '製造', flags: {}, evidence: {} }] };
  $('uploaded-file').hidden = true;
  $('extract-empty').hidden = true; $('extract-loading').hidden = true; $('result-card').hidden = true;
  $('worker-form').hidden = false;
  $('doc-type').value = '移工失聯';
  renderWorkerTable();
  $('flag-count').className = 'status warning'; $('flag-count').textContent = '失聯應變';
  $('model-note').textContent = '失聯日＝連續曠職滿 3 日之日；通報須屬實';
  setStep(2);
  document.querySelector('#worker-rows input[data-k=name]').focus();
  $('worker-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
});

function renderExtract(data) {
  $('extract-loading').hidden = true;
  $('worker-form').hidden = false;
  const sel = $('doc-type');
  if ([...sel.options].some(o => o.value === data.doc_type)) sel.value = data.doc_type;
  renderWorkerTable();
  const flags = (data.workers || []).reduce((n, w) => n + Object.keys(w.flags || {}).length, 0);
  $('flag-count').className = 'status ' + (flags ? 'warning' : 'ok');
  $('flag-count').textContent = flags ? `${flags} 項請再確認` : '辨識完成';
  $('model-note').textContent = `${data.is_image ? '掃描圖片' : '文字公文'}・使用 Claude ${data.model === 'sonnet' ? 'Sonnet' : 'Haiku'} 判讀・請以原件核對`
    + (data.is_image ? '・⚠ 掃描圖片無法先在本機遮蔽個資，建議改傳 PDF 或文字檔' : '・🔒 姓名與證號已在本機遮蔽後才送給 AI');
  const btn = document.querySelector('#worker-form button[type=submit]');
  btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
  btn.classList.add('pulse');
  setTimeout(() => btn.classList.remove('pulse'), 4000);
  notify('AI 已讀完公文。請核對日期，再按右下角「確認無誤，產生待辦」。');
}

function renderWorkerTable() {
  const type = $('doc-type').value;
  const fields = FIELDS[type] || [];
  const isAbscond = type === '移工失聯';
  const isManual = isAbscond || type === '家事移工預定入國';  /* 手動填寫，非 AI 判讀 */
  $('worker-head').innerHTML = `<tr><th>移工姓名</th>${isAbscond ? '<th>工作類別</th>' : ''}${fields.map(([, l]) => `<th>${esc(l)}</th>`).join('')}</tr>`;
  const workers = (lastExtract && lastExtract.workers) || [];
  $('worker-rows').innerHTML = workers.map((w, i) => `<tr data-i="${i}">
      <td><input type="text" data-k="name" value="${esc(w.name)}" required placeholder="移工姓名"><span class="worker-meta">移工 ${String(i + 1).padStart(2, '0')}</span></td>
      ${isAbscond ? `<td><select data-k="job"><option value="製造" ${w.job !== '家庭看護' ? 'selected' : ''}>製造／營造等</option><option value="家庭看護" ${w.job === '家庭看護' ? 'selected' : ''}>家庭看護</option></select><small>家庭看護工遞補時程較短</small></td>` : ''}
      ${fields.map(([k]) => {
        const flag = (w.flags || {})[k];
        const src = (w.evidence || {})[k];
        return `<td class="${flag ? 'flagged' : ''}"><input type="date" data-k="${k}" value="${esc(w[k] || '')}">
          ${src ? `<small>原文：「${esc(src)}」</small>` : (w[k] || isManual ? '' : '<small>文件中未找到</small>')}
          ${flag ? `<span class="field-warning">${esc(flag)}</span>` : ''}</td>`;
      }).join('')}</tr>`).join('');
}
$('doc-type').addEventListener('change', renderWorkerTable);

$('worker-form').addEventListener('submit', async e => {
  e.preventDefault();
  const type = $('doc-type').value;
  const workers = all('#worker-rows tr').map(tr => {
    const w = { name: '', entry_date: null, permit_start: null, permit_end: null, event_date: null };
    tr.querySelectorAll('input[data-k], select[data-k]').forEach(inp => { w[inp.dataset.k] = inp.value || (inp.dataset.k === 'name' ? '' : null); });
    return w;
  });
  if (type === '移工失聯' && workers.some(w => !w.name || !w.event_date)) { notify('請填寫移工姓名與失聯日（連續曠職滿 3 日之日）。'); return; }
  if (workers.some(w => w.permit_start && w.permit_end && w.permit_start > w.permit_end)) { notify('聘僱許可迄日不得早於起日，請再次核對。'); return; }
  if (workers.some(w => (FIELDS[type] || []).every(([k]) => !w[k]))) { notify('請至少填入一個日期。'); return; }
  const btn = e.submitter || document.querySelector('#worker-form button[type=submit]');
  const btnHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner small-spinner"></span>產生中…';
  try {
    const r = await api('/api/obligations', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ doc_type: type, workers, save: true }) });
    const total = r.obligations.length;
    const msg = r.added === total ? `已加入 ${total} 項待辦`
      : r.added === 0 ? `這 ${total} 項待辦先前已加入過，未重複新增` : `已新增 ${r.added} 項待辦（${total - r.added} 項先前已加入過）`;
    renderObligations(r.obligations, workers, msg, r.added > 0);
    setStep(3);
    refreshCount(true);
    btn.classList.add('done');
    btn.innerHTML = `✓ ${r.added > 0 ? `已加入 ${r.added} 項待辦` : '先前已加入過'}`;
    notify(msg + '，可到「我的待辦」或「LINE 提醒」查看。');
    setTimeout(() => { btn.classList.remove('done'); btn.innerHTML = btnHtml; btn.disabled = false; }, 3000);
  } catch (err) {
    btn.innerHTML = btnHtml; btn.disabled = false;
    notify('產生待辦失敗：' + err.message);
  }
});

function statusChip(o) {
  const startLeft = o.start ? daysBetween(today(), o.start) : null;
  if (o.days_left < 0) return ['danger', `已逾期 ${-o.days_left} 天`];
  if (startLeft !== null && startLeft > 0) return [startLeft <= 30 ? 'warning' : 'neutral', `${startLeft} 天後開始`];
  if (o.days_left === 0) return ['danger', '今天截止'];
  if (o.days_left <= 7) return ['danger', `剩 ${o.days_left} 天`];
  if (o.days_left <= 30) return ['warning', `剩 ${o.days_left} 天`];
  return ['neutral', `剩 ${o.days_left} 天`];
}
const today = () => new Date().toISOString().slice(0, 10);
const daysBetween = (a, b) => Math.round((new Date(b) - new Date(a)) / 86400000);

/* 辦理窗口（許願卡 1：知道要做什麼、去哪裡辦） */
function officeLine(o) {
  if (!o.office) return '';
  const link = o.online && /^https:\/\//.test(o.online.url)
    ? ` ・<a href="${esc(o.online.url)}" target="_blank" rel="noopener">${esc(o.online.label)}</a>` : '';
  return `<span class="office">辦理窗口：${esc(o.office)}${link}</span>`;
}
function renderObligations(list, workers, msg = '', added = true) {
  $('result-card').hidden = false;
  $('result-sub').textContent = workers.map(w => w.name).join('、');
  const banner = msg ? `<div class="success-banner ${added ? '' : 'info'}"><span class="success-icon">${added ? '✓' : 'i'}</span>
      <div><strong>${esc(msg)}</strong><small>${added ? '已存入「我的待辦」，期限快到時會透過 LINE 提醒' : '「我的待辦」中已有這些項目'}</small></div>
      <div class="banner-actions"><button type="button" class="button secondary" data-goto="tasks">前往我的待辦</button><button type="button" class="button primary" data-goto="line">推播到 LINE</button></div></div>` : '';
  $('obligation-list').innerHTML = banner + (list.length ? list.map(o => {
    const [cls, txt] = statusChip(o);
    const when = o.start ? `${o.start} ～ ${o.due}` : `${o.due} 前`;
    return `<div class="obligation"><div class="task-icon"><svg><use href="#i-${o.key.startsWith('health') ? 'shield' : 'file'}"/></svg></div>
      <div class="obligation-name"><strong>${esc(o.title)}</strong><small>${esc(o.worker)}・${esc(o.basis)}</small>${officeLine(o)}${o.note ? `<span class="note">${esc(o.note)}</span>` : ''}</div>
      <div class="deadline"><strong>${esc(when)}</strong><small>${o.start ? '辦理期間' : '最後期限'}</small></div>
      <span class="status ${cls}">${esc(txt)}</span></div>`;
  }).join('') : '<p class="empty-state">這份文件沒有觸發需要追蹤的法定期限。</p>');
  all('#obligation-list [data-goto]').forEach(b => b.addEventListener('click', () => showView(b.dataset.goto)));
  $('abscond-guide').hidden = !list.some(o => o.key === 'abscond_notice');
  $('result-card').classList.remove('flash'); void $('result-card').offsetWidth; $('result-card').classList.add('flash');
  $('result-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ---------------- todos ---------------- */
let todoItems = [];
async function refreshCount(bump = false) {
  try {
    const t = await api('/api/todos');
    const el = $('nav-count');
    el.textContent = t.items.length;
    el.title = '待辦總數';
    if (bump) { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); }
  } catch { /* ignore */ }
}
async function loadTodos() {
  try {
    const t = await api('/api/todos');
    todoItems = t.items;
    $('sum-workers').innerHTML = `${t.workers.length} <small>位</small>`;
    const soon = t.items.filter(o => !o.done && o.days_left >= 0 && o.days_left <= 30).length;
    const over = t.items.filter(o => !o.done && o.days_left < 0).length;
    $('sum-soon').innerHTML = `${soon} <small>項</small>`;
    $('sum-overdue').innerHTML = `${over} <small>項</small>`;
    $('sum-overdue').className = over ? 'red' : 'green';
    $('sum-overdue-note').textContent = over ? '請儘速辦理或確認是否已完成' : '目前沒有逾期待辦';
    renderTodoRows(currentFilter);
    $('nav-count').textContent = t.items.length;
  } catch (err) { notify(err.message); }
}
let currentFilter = 'all';
const VERDICT_CLS = { on_time: 'ok', late: 'danger', early: 'warning', mismatch: 'warning', unknown: 'warning', name_mismatch: 'warning' };
function proofCell(o) {
  const p = o.proof;
  const assignBtn = `<button type="button" class="chip-btn ${o.assigned === '仲介' ? 'on' : ''}" data-assign="${o.id}">${o.assigned === '仲介' ? '✓ 已委任仲介' : '委任仲介'}</button>`;
  if (o.key === 'permit_expiry') return `<span class="muted small">期滿出國或續聘後自動結案</span>`;
  if (p) {
    return `<div class="proof-box"><span class="status ${VERDICT_CLS[p.verdict] || 'neutral'}">${p.verdict === 'on_time' ? '✓ ' : ''}${esc(p.verdict_text)}</span>
      <small>${esc(p.doc_kind || '證明文件')}・${esc(p.date_label || '完成日')} ${esc(p.completed_date || '—')}</small>
      ${p.evidence ? `<small>原文：「${esc(p.evidence)}」</small>` : ''}
      <small>${esc(p.uploaded_by)}上傳 ${esc((p.uploaded_at || '').replace('T', ' '))}</small>
      <div class="proof-actions"><a href="/api/todos/${o.id}/proof/file" target="_blank" rel="noopener">查看證明</a><button type="button" class="text-button" data-unproof="${o.id}">移除</button></div></div>`;
  }
  return `<div class="proof-box"><span class="status neutral">尚未上傳</span>${assignBtn}
    <div class="proof-actions"><button type="button" class="text-button" data-proof="${o.id}">上傳證明</button>
    <button type="button" class="text-button muted" data-sample-proof="${o.id}" data-late="0">範例：準時</button>
    <button type="button" class="text-button muted" data-sample-proof="${o.id}" data-late="1">範例：逾期</button></div></div>`;
}
function renderTodoRows(filter) {
  currentFilter = filter;
  const rows = todoItems.filter(o => filter === 'all' || (filter === 'open' && !o.done)
    || (filter === 'soon' && !o.done && o.days_left >= 0 && o.days_left <= 30)
    || (filter === 'overdue' && !o.done && o.days_left < 0) || (filter === 'done' && o.done));
  $('task-rows').innerHTML = rows.map(o => {
    const [cls, txt] = o.done ? (o.proof.verdict === 'late' ? ['danger', '逾期完成'] : ['ok', '已完成']) : statusChip(o);
    return `<tr class="${o.done ? 'row-done' : ''}"><td><strong>${esc(o.title)}</strong><small>${esc(o.basis)}</small>${officeLine(o)}</td><td>${esc(o.worker)}</td><td>${esc(o.start ? `${o.start} ～ ${o.due}` : o.due)}</td><td><span class="status ${cls}">${esc(txt)}</span></td><td>${proofCell(o)}</td></tr>`;
  }).join('');
  $('empty-tasks').hidden = rows.length > 0;
  all('[data-assign]').forEach(b => b.addEventListener('click', () => toggleAssign(b.dataset.assign)));
  all('[data-proof]').forEach(b => b.addEventListener('click', () => { proofTarget = b.dataset.proof; $('proof-input').value = ''; $('proof-input').click(); }));
  all('[data-unproof]').forEach(b => b.addEventListener('click', () => removeProof(b.dataset.unproof)));
  all('[data-sample-proof]').forEach(b => b.addEventListener('click', () => sampleProof(b.dataset.sampleProof, b.dataset.late === '1')));
}
let proofTarget = null;
async function toggleAssign(id) {
  const o = todoItems.find(x => x.id === id);
  await api(`/api/todos/${id}/assign`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ assignee: o.assigned === '仲介' ? null : '仲介' }) });
  notify(o.assigned === '仲介' ? '已取消委任。' : '已標記為委任仲介辦理；仲介上傳證明後即可在此看到結果。');
  loadTodos();
}
async function removeProof(id) {
  await api(`/api/todos/${id}/proof`, { method: 'DELETE' });
  loadTodos();
}
async function submitProof(id, file) {
  const o = todoItems.find(x => x.id === id);
  const fd = new FormData(); fd.append('file', file); fd.append('by', o.assigned === '仲介' ? '仲介' : '雇主');
  const cell = document.querySelector(`[data-proof="${id}"]`)?.closest('td');
  if (cell) cell.innerHTML = '<div class="proof-box"><span class="spinner small-spinner dark"></span><small>AI 正在讀取證明…</small></div>';
  try {
    const r = await api(`/api/todos/${id}/proof`, { method: 'POST', body: fd });
    notify(`證明判定：${r.verdict_text}（${r.date_label || '完成日'} ${r.completed_date || '讀不到'}）`);
  } catch (err) { notify('證明上傳失敗：' + err.message); }
  loadTodos();
}
$('proof-input').addEventListener('change', e => { if (proofTarget && e.target.files[0]) submitProof(proofTarget, e.target.files[0]); });

/* 範例證明：依待辦事項產生一份合成的完成證明（展示用） */
const PROOF_KIND = {
  health_entry: ['受聘僱外國人健康檢查報告', '檢查日期'], health_6m: ['受聘僱外國人定期健康檢查報告', '檢查日期'],
  health_18m: ['受聘僱外國人定期健康檢查報告', '檢查日期'], health_30m: ['受聘僱外國人定期健康檢查報告', '檢查日期'],
  apply_permit: ['勞動部聘僱許可申請收件回執', '收件日期'], renewal_window: ['勞動部期滿續聘許可申請收件回執', '收件日期'],
  recheck_report: ['衛生局備查收件回執', '收件日期'], dorm_change: ['住宿地點變更通報收執聯', '收件日期'],
  termination_notice: ['聘僱關係終止通報收執聯', '通報日期'], abscond_notice: ['外國人連續曠職三日失去聯繫通報收執聯', '通報日期'],
  replacement_window: ['勞動部遞補申請收件回執', '收件日期'],
  onestop_register: ['移工一站式服務網入國講習申請完成畫面', '申請日期'],
};
function sampleProof(id, late) {
  const o = todoItems.find(x => x.id === id);
  const [kind, label] = PROOF_KIND[o.key] || ['辦理收件回執', '收件日期'];
  const due = new Date(o.due);
  let d;
  if (late) { d = new Date(due); d.setDate(d.getDate() + 5); }
  else { const start = o.start ? new Date(o.start) : null; d = new Date(Math.min(Date.now(), due.getTime())); if (start && d < start) d = start; }
  const text = ['【合成測試文件・非真實公文・僅供竹聘安 AI 驗證使用】', kind, '',
    `外國人姓名：${o.worker}`, '雇主：（展示用）', `${label}：${rocDate(d)}`,
    `列印日期：${rocDate(new Date())}`, '', '上述事項已受理完成。'].join('\n');
  submitProof(id, new File([text], `${kind}.txt`, { type: 'text/plain' }));
}
all('[data-filter]').forEach(b => b.addEventListener('click', () => {
  all('[data-filter]').forEach(x => { const on = x === b; x.classList.toggle('active', on); x.setAttribute('aria-pressed', String(on)); });
  renderTodoRows(b.dataset.filter);
}));
$('clear-todos').addEventListener('click', async () => {
  if (!confirm('確定清除所有展示用待辦資料？')) return;
  await api('/api/todos', { method: 'DELETE' }); loadTodos(); notify('已清除展示資料。');
});

/* ---------------- chat ---------------- */
function mdLite(s) {
  return esc(s).replace(/^#+\s*(.*)$/gm, '<strong>$1</strong>').replace(/\*\*(.+?)\*\*/g, '<strong class="answer-highlight">$1</strong>').replace(/^---$/gm, '');
}
all('[data-question]').forEach(b => b.addEventListener('click', () => { $('question').value = b.dataset.question; $('chat-form').requestSubmit(); }));
$('chat-form').addEventListener('submit', async e => {
  e.preventDefault();
  const q = $('question').value.trim(); if (!q) return;
  const box = $('messages');
  box.insertAdjacentHTML('beforeend', `<div class="message user"><span class="message-label">您</span><div>${esc(q)}</div></div>`);
  const pending = document.createElement('div');
  pending.className = 'message assistant';
  pending.innerHTML = '<span class="message-label">竹聘安 AI</span><div class="assistant-bubble"><span class="spinner" style="width:20px;height:20px"></span> 正在查找相關條文…</div>';
  box.append(pending); box.scrollTop = box.scrollHeight;
  $('question').value = ''; $('ask-btn').disabled = true;
  try {
    const r = await api('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q }) });
    const cites = r.citations.map((c, i) => `<details class="citation" ${i === 0 ? 'open' : ''}><summary>引用條文 <span>${esc(c.law)}${esc(c.no)}</span></summary><div><strong>${esc(c.law)} ${esc(c.no)}</strong><blockquote>${esc(c.text)}</blockquote></div></details>`).join('');
    pending.querySelector('.assistant-bubble').innerHTML = `<div class="answer-text">${mdLite(r.answer)}</div>${cites}<small class="disclaimer">本回答依法條原文產生，非正式法律意見</small>`;
  } catch (err) {
    pending.querySelector('.assistant-bubble').textContent = '抱歉，查詢失敗：' + err.message;
  }
  $('ask-btn').disabled = false; box.scrollTop = box.scrollHeight;
});

/* ---------------- LINE ---------------- */
const lineDays = () => Number((document.querySelector('input[name=days]:checked') || {}).value || 30);
const lineLang = () => (document.querySelector('input[name=language]:checked') || {}).value || 'zh';
async function loadLine() {
  try {
    const s = await api('/api/line/status');
    $('line-status-tag').textContent = s.configured ? `已連接 LINE 官方帳號・好友 ${s.followers} 人` : '尚未連接 LINE（預覽模式）';
    $('line-send').textContent = s.configured ? '產生提醒並推播到 LINE' : '產生提醒預覽';
    $('phone-note').textContent = s.configured ? '實際推播內容預覽' : 'LINE 對話預覽・設定 Messaging API 後即可實際推播';
  } catch { /* ignore */ }
  renderPhone();
}
async function renderPhone() {
  try {
    const p = await api(`/api/line/preview?days=${lineDays()}&lang=${lineLang()}`);
    const d = new Date();
    $('phone-chat').innerHTML = `<div class="phone-date">${d.getMonth() + 1} 月 ${d.getDate()} 日</div>` + (p.count ? `<div class="line-message"><span class="line-avatar">竹</span><div class="line-bubble"><p>${esc(p.text)}</p><div class="line-buttons"><button type="button" class="mark-done">已讀</button><button type="button" data-goto-tasks>查看全部</button></div></div></div>` : '<p class="line-detail">目前範圍內沒有需要提醒的待辦。請先到「上傳公文」產生待辦，或放寬提醒範圍。</p>');
    all('[data-goto-tasks]').forEach(b => b.addEventListener('click', () => showView('tasks')));
    all('.mark-done').forEach(b => b.addEventListener('click', () => { b.textContent = '已標記完成'; b.disabled = true; }));
  } catch (err) { notify(err.message); }
}
all('input[name=days], input[name=language]').forEach(i => i.addEventListener('change', renderPhone));
$('settings-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    const r = await api('/api/line/send', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days: lineDays(), lang: lineLang() }) });
    $('line-result').textContent = r.note || '';
    loadLine();
    renderPhone();
  } catch (err) { notify(err.message); }
});

/* ---------------- penalty review ---------------- */
$('load-draft').addEventListener('click', async () => {
  const t = await (await fetch('samples/' + encodeURIComponent('裁處書草稿_範例.txt'))).text();
  $('draft').value = t; notify('已載入範例草稿（合成資料）。');
});
const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦'];
$('check-draft').addEventListener('click', async () => {
  const text = $('draft').value.trim();
  if (text.length < 30) { notify('請先貼上裁處書草稿，或載入範例草稿。'); return; }
  $('check-loading').hidden = false; $('checklist').innerHTML = ''; $('suggestion-card').hidden = true;
  $('check-summary').className = 'status neutral'; $('check-summary').textContent = '檢核中'; $('check-draft').disabled = true;
  try {
    const r = await api('/api/check-penalty', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    const fails = r.items.filter(i => i.ok === false);
    if (r.masked_preview) {
      $('masked-card').hidden = false;
      $('masked-text').textContent = r.masked_preview;
    }
    $('checklist').innerHTML = r.items.map((it, i) => `<li class="check-item ${it.ok === false ? 'check-fail' : ''}">
      <div class="check-row"><span>${CIRCLED[i]}</span><strong>${esc(it.name)}</strong>
      ${it.ok === true ? '<span class="check-state check-pass">✓ 符合</span>' : it.ok === false ? '<span class="check-state">✕ 需修正</span>' : '<span class="check-state check-unknown">？未判定</span>'}</div>
      <p class="reason">${esc(it.reason || '')}</p></li>`).join('');
    $('check-progress').style.width = `${Math.round(100 * (7 - fails.length) / 7)}%`;
    $('check-summary').className = 'status ' + (fails.length ? 'danger' : 'ok');
    $('check-summary').textContent = fails.length ? `${fails.length} 項需修正` : '7 項皆符合';
    const fixes = fails.filter(f => f.fix);
    if (fixes.length) {
      $('suggestion-card').hidden = false;
      $('suggestions').innerHTML = fixes.map(f => `<div class="fix-item"><strong>${esc(f.name)}</strong><p>${esc(f.fix)}</p><button class="text-button" type="button" data-copy="${esc(f.fix)}">複製文字</button></div>`).join('');
      all('[data-copy]').forEach(b => b.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(b.dataset.copy); notify('已複製，請依個案確認後採用。'); } catch { notify('無法自動複製，請手動選取文字。'); }
      }));
    }
  } catch (err) {
    $('check-summary').className = 'status danger'; $('check-summary').textContent = '檢核失敗'; notify(err.message);
  }
  $('check-loading').hidden = true; $('check-draft').disabled = false;
});
