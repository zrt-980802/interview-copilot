const create = (tag, text, className = '') => {
  const element = document.createElement(tag); element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};
const time = ms => {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
};
const states = {not_started:'尚未转写', awaiting_configuration:'本地转写环境未就绪', awaiting_model:'等待本地模型权重', queued:'等待本地转写',
  running:'正在本地精转写', complete:'本地精转写已完成', failed:'转写失败，可重试', interrupted:'转写中断，可继续'};
async function request(url, data) {
  const response = await fetch(url, {cache:'no-store', method:data ? 'POST' : 'GET',
    headers:data ? {'Content-Type':'application/json'} : {}, body:data ? JSON.stringify(data) : undefined});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '录音读取失败');
  return value;
}
function segmentView(sid, chunk, segment) {
  const section = create('details', undefined, 'recording-segment'); section.dataset.recordingFile = chunk.file;
  const role = chunk.source === 'mic' ? '麦克风' : '系统声音';
  section.append(create('summary', `${time(chunk.start_ms)}–${time(chunk.end_ms)} · ${role}`));
  const audio = create('audio'); audio.controls = true; audio.preload = 'none';
  audio.src = `/api/recordings/${sid}/audio/${encodeURIComponent(chunk.file)}`;
  audio.setAttribute('aria-label', `${role} ${time(chunk.start_ms)}`);
  section.append(audio);
  if (segment) {
    const checked = segment.reviewed_text !== undefined;
    const text = create('pre', checked ? segment.reviewed_text : segment.text, 'recording-text');
    const state = create('span', checked ? '已人工核对' : '模型转写 · 未核对', 'recording-review-state');
    const edit = create('button', '核对文本', 'command'); edit.type = 'button';
    const textarea = create('textarea'); textarea.value = text.textContent; textarea.hidden = true;
    textarea.maxLength = 64000; textarea.setAttribute('aria-label', `${role}核对文本`);
    const save = create('button', '保存核对', 'command'); save.hidden = true; save.type = 'button';
    edit.onclick = () => { textarea.hidden = save.hidden = !textarea.hidden; if (!textarea.hidden) textarea.focus(); };
    save.onclick = async () => {
      save.disabled = true;
      try {
        await request(`/api/recordings/${sid}/review`, {file:chunk.file, text:textarea.value});
        text.textContent = textarea.value; state.textContent = '已人工核对'; textarea.hidden = save.hidden = true;
      } catch (error) { state.textContent = error.message; }
      finally { save.disabled = false; }
    };
    section.append(state, text, edit, textarea, save);
  } else section.append(create('p', '尚无精转写文本', 'recording-review-state'));
  return section;
}
export function recordingReview(sid) {
  const root = create('section', undefined, 'recording-review');
  const heading = create('div', undefined, 'recording-heading');
  heading.append(create('h3', '会话录音与精转写'));
  const status = create('span', '读取录音…', 'recording-review-state'); status.setAttribute('role', 'status');
  const actions = create('div', undefined, 'recording-actions');
  const transcribe = create('button', '本地精转写', 'command'); transcribe.type = 'button'; transcribe.hidden = true;
  const exported = create('a', '导出转写与对照', 'command'); exported.href = `/api/recordings/${sid}/export`; exported.hidden = true;
  const content = create('div'); actions.append(transcribe, exported); root.append(heading, status, actions, content);
  let timer, signature;
  async function load() {
    clearTimeout(timer);
    try {
      const value = await request('/api/recordings/' + sid);
      if (!root.isConnected) return;
      const task = value.transcription, busy = ['queued','running'].includes(task.state);
      status.textContent = value.recovery_error ? '该会话的录音清单损坏，原始文件已保留' : !value.chunks.length ? '本次会话没有录音' :
        `${value.chunks.length} 个本地音频片段 · ${states[task.state] || task.state}` +
        (task.total ? ` · ${task.completed || 0}/${task.total}` : '') +
        (value.dropped_packets || value.writer_error ? ' · 录音存在缺口' : '');
      transcribe.hidden = !value.chunks.length;
      transcribe.disabled = busy || !value.ended_at;
      transcribe.textContent = task.state === 'failed' || task.state === 'interrupted' ? '继续本地转写' : '本地精转写';
      exported.hidden = !value.chunks.length;
      const next = JSON.stringify([value.chunks, value.transcript]);
      if (next !== signature && !root.contains(document.activeElement)) {
        const segments = new Map((value.transcript?.segments || []).map(s => [s.file, s]));
        content.replaceChildren(...[...value.chunks].sort((a,b) => a.start_ms - b.start_ms).map(chunk => segmentView(sid,chunk,segments.get(chunk.file))));
        signature = next;
      }
      if (busy || !value.ended_at && value.chunks.length) timer = setTimeout(load, 3000);
    } catch (error) { if (root.isConnected) status.textContent = error.message; }
  }
  transcribe.onclick = async () => {
    transcribe.disabled = true;
    try { await request(`/api/recordings/${sid}/transcribe`, {}); await load(); }
    catch (error) { status.textContent = error.message; transcribe.disabled = false; }
  };
  queueMicrotask(load);
  return root;
}
