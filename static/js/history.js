import {draftDocument, isAnswerDocument, renderDocument} from '/static/js/answer-content.js';
import {recordingReview} from '/static/js/recording-review.js';

const $ = id => document.getElementById(id);
let sessions = [], selected, query = '', offset = 0, listRequest = 0, detailRequest = 0;
let listController, detailController, searchTimer;
const node = (tag, className, text) => {
  const element = document.createElement(tag); element.className = className || '';
  if (text !== undefined) element.textContent = text;
  return element;
};
const icon = (name, label) => {
  const button = node('button', 'command copy-command'); button.type = 'button'; button.title = label; button.setAttribute('aria-label', label);
  const symbol = node('i'); symbol.dataset.lucide = name; button.append(symbol,node('span','copy-label',label)); return button;
};
async function get(url, signal) {
  const response = await fetch(url, {signal, cache:'no-store'});
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(value.error === 'not found' ? '会话不存在' : value.error || '历史记录暂不可读');
  }
  return response.json();
}
function rows() {
  $('sessionList').replaceChildren(...sessions.map(session => {
    const row = node('button', 'session-row'); row.dataset.sessionId = session.id;
    row.setAttribute('aria-current', String(session.id === selected));
    const date = node('div', 'date'); date.append(node('span', '', session.started_at), node('span', '', session.end_at ? '已结束' : '未结束'));
    row.append(date, node('span', 'name', session.title || '未命名会话'), node('div', 'preview', session.preview || ''), node('span', 'count', `${session.turns} 题`));
    row.onclick = () => selectSession(session.id); return row;
  }));
}
async function loadSessions(append = false) {
  listController?.abort(); listController = new AbortController();
  const request = ++listRequest;
  $('listState').classList.remove('error'); $('listState').textContent = '读取中…'; $('loadMore').disabled = true;
  const nextOffset = append ? offset : 0;
  try {
    const value = await get('/api/history/sessions?' + new URLSearchParams({limit:30, offset:nextOffset, q:query}), listController.signal);
    if (request !== listRequest) return;
    sessions = append ? [...sessions, ...value.sessions.filter(item => !sessions.some(previous => previous.id === item.id))] : value.sessions;
    offset = nextOffset + value.sessions.length; rows();
    $('listState').textContent = sessions.length ? `${sessions.length} 个会话` : query ? '未找到会话' : '暂无会话';
    $('loadMore').hidden = !value.has_more;
  } catch (error) {
    if (error.name !== 'AbortError' && request === listRequest) { $('listState').textContent = error.message; $('listState').classList.add('error'); }
  } finally { if (request === listRequest) $('loadMore').disabled = false; }
}
function answerView(content, level, document, status, meta) {
  const root = node('section', 'history-answer'); root.dataset.documentStatus = status || 'legacy';
  const body = node('div', 'history-answer-body');
  renderDocument(body, isAnswerDocument(document, level) ? document : draftDocument(content || '', level));
  const toolbar = node('div', 'answer-toolbar');
  const description = status === 'invalid' ? '来源记录不可用 · 保留原文' : status === 'stored' ? '来源快照已保存' : '原始记录';
  toolbar.append(node('span', '', [description, meta?.model, meta?.attempt > 1 ? `尝试 ${meta.attempt}` : ''].filter(Boolean).join(' · ')));
  const copy = icon('copy', '复制回答');
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(content); copy.title = '已复制'; copy.setAttribute('aria-label', '已复制'); copy.querySelector('.copy-label').textContent = '已复制'; }
    catch { copy.title = '复制失败'; copy.setAttribute('aria-label', '复制失败'); copy.querySelector('.copy-label').textContent = '复制失败'; }
  };
  toolbar.append(copy); root.append(body, toolbar); return root;
}
function renderSession(value) {
  $('sessionTitle').textContent = value.title || '未命名会话';
  $('sessionMeta').textContent = `${value.started_at} · ${value.turns.length} 题 · ${value.end_at ? '已结束' : '未结束'}`;
  $('export').href = `/api/history/export/${value.id}`; $('export').download = `interview-${value.id}.md`;
  const fragment = document.createDocumentFragment();
  fragment.append(recordingReview(value.id));
  for (const [index, turn] of value.turns.entries()) {
    const article = node('article', 'history-turn'); article.dataset.turnId = turn.id;
    article.append(node('div', 'turn-index', `问题 ${index + 1} · ${turn.ts}`), node('h3', '', turn.question));
    if (turn.quick_card) article.append(answerView(turn.quick_card, 'quick', turn.quick_document, turn.quick_document_status, turn.quick_meta));
    else article.append(node('div', 'unanswered', '未保存完整回答'));
    if (turn.self_context) {
      const context = node('details', 'candidate-context');
      context.append(node('summary', '', '当时的发言记录'), node('pre', '', turn.self_context)); article.append(context);
    }
    for (const detail of turn.details) {
      const section = node('details', 'history-detail'); section.dataset.detailId = detail.id;
      section.append(node('summary', '', '深入 · ' + detail.direction), answerView(detail.content, 'detail', detail.document, detail.document_status, detail.meta));
      article.append(section);
    }
    fragment.append(article);
  }
  if (!value.turns.length) fragment.append(node('div', 'unanswered', '本次会话没有问题'));
  $('reviewContent').replaceChildren(fragment); $('reviewContent').scrollTop = 0;
  $('reviewEmpty').hidden = true; $('sessionToolbar').hidden = false;
  window.lucide?.createIcons();
}
async function selectSession(id) {
  selected = Number(id); rows(); document.body.classList.add('reviewing');
  const url = new URL(location.href); url.searchParams.set('session', selected); history.replaceState(null, '', url);
  detailController?.abort(); detailController = new AbortController(); const request = ++detailRequest;
  $('reviewState').classList.remove('error'); $('reviewState').textContent = '读取中…';
  $('reviewContent').replaceChildren(); $('sessionToolbar').hidden = true; $('reviewEmpty').hidden = true;
  try {
    const value = await get('/api/history/session/' + selected, detailController.signal);
    if (request !== detailRequest) return;
    renderSession(value.session); $('reviewState').textContent = '';
  } catch (error) {
    if (error.name !== 'AbortError' && request === detailRequest) { $('reviewState').textContent = error.message; $('reviewState').classList.add('error'); }
  }
}
$('searchForm').onsubmit = event => { event.preventDefault(); clearTimeout(searchTimer); query = $('search').value.trim(); loadSessions(); };
$('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { query = $('search').value.trim(); loadSessions(); }, 250); };
$('loadMore').onclick = () => loadSessions(true);
$('refresh').onclick = () => { loadSessions(); if (selected) selectSession(selected); };
$('backToList').onclick = () => {
  detailController?.abort(); ++detailRequest;
  selected = undefined; rows();
  document.body.classList.remove('reviewing');
  const url = new URL(location.href); url.searchParams.delete('session'); history.replaceState(null, '', url);
};
window.lucide?.createIcons();
await loadSessions();
const initial = new URLSearchParams(location.search).get('session');
if (/^[1-9]\d*$/.test(initial || '')) await selectSession(Number(initial));
