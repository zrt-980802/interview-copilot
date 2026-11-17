const $ = id => document.getElementById(id);
const names = {reference:'参考资料',resume:'简历',candidate:'本人项目资料',paper:'论文',jd:'岗位说明'};
const statuses = {queued:'等待初始化',parsing:'解析正文',indexing:'建立索引',ready:'已就绪',error:'处理失败'};
const processing = file => ['queued','parsing','indexing'].includes(file.status);
let files = [], selected, current, view = 'all', contentView = 'overview', legacy = [], resumeMode;
let listSequence = 0, detailSequence = 0, detailController, dirty = false, saving = false;
const node = (tag, cls, text) => {
  const element = document.createElement(tag); element.className = cls || '';
  if (text !== undefined) element.textContent = text;
  return element;
};
const symbol = name => { const value = node('i'); value.dataset.lucide = name; return value; };
const icons = () => window.lucide?.createIcons();
const size = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
function notice(message, error = false) { $('notice').textContent = message; $('notice').hidden = !message; $('notice').classList.toggle('error',error); }
async function api(url, options = {}) {
  const response = await fetch(url, {...options,cache:'no-store'});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || '资料操作失败，请稍后重试');
  return value;
}
function renderRows() {
  const query = $('search').value.trim().toLowerCase();
  const visible = files.filter(file => (view === 'archived' ? file.archived : !file.archived)
    && (view !== 'processing' || processing(file)) && (view !== 'error' || file.status === 'error')
    && (!query || `${file.name} ${file.title} ${file.overview}`.toLowerCase().includes(query)));
  $('fileRows').replaceChildren(...visible.map(file => {
    const row = node('tr'); row.dataset.fileId = file.id; row.dataset.archived = file.archived;
    row.setAttribute('aria-selected',String(file.id === selected));
    const cell = node('td'), label = node('div','file-name'), description = node('div');
    const select = node('button','file-select',file.title); select.type = 'button'; select.onclick = () => selectFile(file.id);
    description.append(select,node('div','file-original',file.name),node('div','file-size',size(file.bytes)));
    label.append(symbol(file.source_url ? 'github' : 'file-text'),description); cell.append(label);
    const state = node('td','status-' + file.status,file.archived ? '已归档' : statuses[file.status] || file.status);
    const enabled = node('td','enable-cell'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = file.enabled;
    checkbox.setAttribute('aria-label',`启用 ${file.name}`); checkbox.disabled = file.archived || file.status !== 'ready' || saving;
    checkbox.onchange = () => mutate(file.id,{enabled:checkbox.checked}); enabled.append(checkbox);
    row.append(cell,node('td','kind-col',names[file.kind]),state,enabled); return row;
  }));
  const active = files.filter(file => !file.archived);
  $('totals').textContent = `${active.length} 份 · ${active.filter(file => file.enabled && file.status === 'ready').length} 份启用`;
  $('listState').classList.remove('error');
  $('listState').textContent = visible.length ? `${visible.length} 份资料${active.some(processing) ? ' · 初始化中' : ''}` : query ? '未找到资料' : view === 'archived' ? '暂无归档资料' : '暂无资料';
  icons();
}
function renderLegacy() {
  $('legacyPanel').hidden = !legacy.length; $('legacyHeading').textContent = `原有资料 · ${legacy.length}`;
  $('legacyFiles').replaceChildren(...legacy.map(file => {
    const row = node('div'); row.append(symbol('file-text'),node('span','',`${file.name} · ${names[file.kind]}`));
    if (file.kind === 'resume') {
      const button = node('button','command',resumeMode === 'legacy' ? '当前简历' : '使用原有简历');
      button.disabled = resumeMode === 'legacy'; button.prepend(symbol('file-user'));
      button.onclick = async () => { try { await api('/api/files/resume-source/legacy',{method:'POST'}); await load(); notice('已切换到原有简历'); } catch(error) { notice(error.message,true); } };
      row.append(button);
    }
    return row;
  })); icons();
}
async function load({quiet = false} = {}) {
  const request = ++listSequence;
  if (!quiet) $('listState').textContent = '读取中…';
  try {
    const value = await api('/api/files'); if (request !== listSequence) return;
    const previous = files.find(file => file.id === selected);
    const changed = JSON.stringify(files) !== JSON.stringify(value.files) || JSON.stringify(legacy) !== JSON.stringify(value.legacy || []) || resumeMode !== value.resume_mode;
    files = value.files; legacy = value.legacy || []; resumeMode = value.resume_mode;
    if (changed || !quiet) { renderRows(); renderLegacy(); }
    const next = files.find(file => file.id === selected);
    if (next && (!current || JSON.stringify(previous) !== JSON.stringify(next))) await loadDetail(selected,{quiet:true});
  } catch(error) {
    if (request === listSequence) { $('listState').textContent = error.message; $('listState').classList.add('error'); }
  }
}
function preview() {
  if (!current) return;
  $('overviewTab').setAttribute('aria-pressed',String(contentView === 'overview'));
  $('textTab').setAttribute('aria-pressed',String(contentView === 'text'));
  $('preview').textContent = contentView === 'text' ? current.preview || '暂无正文' : current.overview || '等待正文解析';
  $('textCount').textContent = `${current.chars} 字 · ${current.chunks} 段${contentView === 'text' && current.preview_truncated ? ' · 正文节选' : ''}`;
}
function renderDetail(file) {
  current = file; $('fileDetail').hidden = false; $('detailEmpty').hidden = true; $('detailState').textContent = '';
  $('detailName').textContent = file.name;
  $('fileMeta').textContent = `${size(file.bytes)} · ${file.created_at} · ${file.archived ? '已归档' : statuses[file.status]}`;
  $('download').href = `/api/files/${file.id}/download`;
  $('archive').title = $('archive').ariaLabel = file.archived ? '恢复文件' : '归档文件';
  $('archive').replaceChildren(symbol(file.archived ? 'archive-restore' : 'archive'));
  $('sourceLink').hidden = !file.source_url;
  if (file.source_url) { $('sourceLink').href = file.source_url; $('sourceLink').textContent = file.source_url; }
  const completed = file.status === 'ready';
  const steps = [['原文件已保存',true,false],['正文解析',completed || file.chars > 0,file.status === 'parsing'],['分块索引',completed,file.status === 'indexing'],['可以参考',completed,false]];
  $('initialization').replaceChildren(...steps.map(([label,done,active],index) => {
    const failed = file.status === 'error' && index === (file.chars > 0 ? 2 : 1);
    const item = node('li',failed ? 'failed' : done ? 'done' : active ? 'active' : '',label);
    item.prepend(symbol(failed ? 'circle-alert' : done ? 'circle-check' : active ? 'loader-circle' : 'circle')); return item;
  }));
  $('fileError').hidden = !file.error; $('fileError').textContent = file.error;
  $('retry').hidden = file.status !== 'error' || file.archived;
  if (!dirty) { $('fileTitle').value = file.title; $('fileKind').value = file.kind; }
  for (const input of $('fileProperties').elements) input.disabled = file.archived || saving;
  preview(); icons();
}
async function loadDetail(id,{quiet = false} = {}) {
  detailController?.abort(); detailController = new AbortController(); const request = ++detailSequence;
  if (!quiet) { $('detailState').textContent = '读取中…'; $('fileDetail').hidden = true; $('detailEmpty').hidden = true; }
  try {
    const value = await api('/api/files/' + id,{signal:detailController.signal});
    if (request === detailSequence && selected === id) { $('detailState').classList.remove('error'); renderDetail(value.file); }
  } catch(error) {
    if (error.name !== 'AbortError' && request === detailSequence) { $('detailState').textContent = error.message; $('detailState').classList.add('error'); }
  }
}
async function selectFile(id) {
  selected = id; current = undefined; dirty = false; contentView = 'overview'; renderRows();
  document.body.classList.add('inspecting');
  const url = new URL(location.href); url.searchParams.set('file',id); history.replaceState(null,'',url);
  await loadDetail(id);
}
async function mutate(id,changes) {
  if (saving) return; saving = true; renderRows();
  try {
    await api('/api/files/' + id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)});
    if (id === selected) dirty = false;
    notice(changes.archived === true ? '文件已归档' : changes.archived === false ? '文件已恢复，暂未启用' : '资料设置已保存');
    await load(); if (id === selected) await loadDetail(id,{quiet:true});
  } catch(error) { notice(error.message,true); }
  finally { saving = false; renderRows(); if (current) renderDetail(current); }
}
$('upload').onclick = () => { $('fileInput').multiple = $('uploadKind').value !== 'resume'; $('fileInput').click(); };
$('fileInput').onchange = async () => {
  const chosen = [...$('fileInput').files]; $('fileInput').value = ''; if (!chosen.length) return;
  if (chosen.length > 20) { notice('每批最多 20 份参考资料',true); return; }
  const body = new FormData(); for (const file of chosen) body.append('files',file); body.append('kind',$('uploadKind').value);
  $('upload').disabled = true; $('uploadProgress').hidden = false; $('uploadProgress').textContent = `上传 ${chosen.length} 份文件…`;
  try {
    const value = await api('/api/files/upload',{method:'POST',body});
    const errors = value.results.filter(item => !item.ok), duplicates = value.results.filter(item => item.duplicate).length;
    notice([`${value.results.length - errors.length - duplicates} 份已接收`,duplicates ? `${duplicates} 份已存在` : '',...errors.map(item => `${item.name}：${item.error}`)].filter(Boolean).join(' · '),!!errors.length);
    await load(); const accepted = value.results.find(item => item.ok); if (accepted) await selectFile(accepted.file.id);
  } catch(error) { notice(error.message,true); }
  finally { $('upload').disabled = false; $('uploadProgress').hidden = true; }
};
$('search').oninput = renderRows;
for (const button of $('viewFilters').children) button.onclick = () => {
  view = button.dataset.view;
  for (const item of $('viewFilters').children) item.setAttribute('aria-pressed',String(item === button));
  renderRows();
};
$('refresh').onclick = () => { load(); if (selected) loadDetail(selected,{quiet:true}); };
$('fileTitle').oninput = $('fileKind').onchange = () => { dirty = true; };
$('fileProperties').onsubmit = event => { event.preventDefault(); if (selected) mutate(selected,{title:$('fileTitle').value.trim(),kind:$('fileKind').value}); };
$('archive').onclick = () => current && mutate(current.id,{archived:!current.archived});
$('retry').onclick = async () => {
  if (!current) return; $('retry').disabled = true;
  try { await api(`/api/files/${current.id}/retry`,{method:'POST'}); notice('已重新加入初始化队列'); await load(); }
  catch(error) { notice(error.message,true); } finally { $('retry').disabled = false; }
};
$('overviewTab').onclick = () => { contentView = 'overview'; preview(); };
$('textTab').onclick = () => { contentView = 'text'; preview(); };
$('backToFiles').onclick = () => {
  detailController?.abort(); ++detailSequence; selected = undefined; current = undefined; dirty = false;
  document.body.classList.remove('inspecting'); renderRows();
  const url = new URL(location.href); url.searchParams.delete('file'); history.replaceState(null,'',url);
};
$('openGithub').onclick = () => { $('githubError').textContent = ''; $('githubDialog').showModal(); };
$('closeGithub').onclick = () => $('githubDialog').close();
$('githubForm').onsubmit = async event => {
  event.preventDefault(); $('importGithub').disabled = true; $('githubError').textContent = '读取中…';
  try {
    const value = await api('/api/files/import-github',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:$('githubUrl').value})});
    $('githubDialog').close(); notice(value.duplicate ? '相同内容已存在于资料库' : 'GitHub 文件已接收，正在初始化');
    await load(); await selectFile(value.file.id);
  } catch(error) { $('githubError').textContent = error.message; }
  finally { $('importGithub').disabled = false; }
};
icons(); await load();
const initialKind = new URLSearchParams(location.search).get('kind'); if (names[initialKind]) $('uploadKind').value = initialKind;
const initial = new URLSearchParams(location.search).get('file'); if (/^[a-f0-9]{32}$/.test(initial || '')) await selectFile(initial);
setInterval(() => { if (!document.hidden) load({quiet:true}); },2000);
document.addEventListener('visibilitychange',() => { if (!document.hidden) load({quiet:true}); });
