(() => {
  const root = document.getElementById('speechDrafts');
  const items = new Map();
  const labels = {collecting:'正在听', evaluating:'判断是否说完', awaiting_transcript:'等待完整转写',
    ready:'准备提交', needs_review:'待确认'};
  const reasons = {audio_gap:'音频存在缺口', capture_stalled:'采集暂时中断', speech_queue_full:'识别队列已满',
    recognition_failed:'转写失败', uncertain_recognition:'转写可信度较低', repetition:'疑似重复识别',
    suspected_hallucination:'疑似无关识别内容',
    waiting_for_completion:'尚未确认说完', invalid_speech_result:'识别结果无效', unfinished:'等待后续内容',
    awaiting_question:'等待具体问题', editing:'编辑中'};

  function reset() { items.clear(); root.replaceChildren(); root.hidden = true; }
  function update(event, sessionId) {
    const speaker = event.speaker || 'interviewer';
    let item = items.get(speaker);
    if (!event.draft_id || ['empty','committed'].includes(event.state)) {
      item?.el.remove(); items.delete(speaker); root.hidden = !items.size; return;
    }
    if (item && item.latest.draft_id !== event.draft_id) {
      item.el.remove(); items.delete(speaker); item = null;
    }
    if (!item) {
      const el = document.createElement('div'); el.className = 'speech-draft';
      el.innerHTML = '<div class="draft-heading"><strong></strong><span role="status"></span></div>' +
        '<textarea maxlength="12000" rows="2"></textarea><div class="draft-actions">' +
        '<span class="draft-error" role="alert"></span>' +
        '<button data-action="edit" title="编辑草稿" aria-label="编辑草稿"><i data-lucide="pencil"></i></button>' +
        '<button data-action="refresh" title="载入最新转写" aria-label="载入最新转写" hidden><i data-lucide="refresh-cw"></i></button>' +
        '<button data-action="discard" title="放弃草稿" aria-label="放弃草稿"><i data-lucide="trash-2"></i></button>' +
        '<button data-action="submit"><i data-lucide="check"></i>确认' + (speaker === 'self' ? '记录' : '提问') + '</button></div>';
      const input = el.querySelector('textarea'); input.readOnly = true;
      input.setAttribute('aria-label', speaker === 'self' ? '本人发言草稿' : '面试官问题草稿');
      el.querySelector('strong').textContent = speaker === 'self' ? '本人发言' : '问题草稿';
      item = {el,input,dirty:false,base:event,latest:event,sessionId};
      items.set(speaker,item); root.appendChild(el);
      input.addEventListener('input', () => {item.dirty = true;});
      el.querySelector('[data-action="refresh"]').onclick = () => {
        item.dirty = false; item.base = item.latest; input.value = item.latest.text;
        el.querySelector('[data-action="refresh"]').hidden = true;
        el.querySelector('.draft-error').textContent = '';
      };
      for (const action of ['edit','submit','discard']) el.querySelector(`[data-action="${action}"]`).onclick = async () => {
        const buttons = [...el.querySelectorAll('button')]; buttons.forEach(button => button.disabled = true);
        try {
          const response = await fetch('/api/speech/draft', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({...item.base, session_id:item.sessionId, speaker, action:action === 'edit' ? 'hold' : action, text:input.value})});
          const result = await response.json();
          if (!response.ok || !result.ok) throw new Error(result.error || '提交失败');
          if (action === 'edit') {
            item.dirty = true; item.base = result.draft; input.readOnly = false; input.focus(); return;
          }
          if (items.get(speaker) === item) {el.remove(); items.delete(speaker); root.hidden = !items.size;}
        } catch (error) {el.querySelector('.draft-error').textContent = error.message;}
        finally {buttons.forEach(button => button.disabled = false);}
      };
      window.lucide?.createIcons();
    }
    item.latest = event; item.sessionId = sessionId;
    item.el.querySelector('[role="status"]').textContent = (labels[event.state] || event.state) +
      (event.reason ? ' · ' + (reasons[event.reason] || '请核对草稿') : '');
    if (!item.dirty) {item.input.value = event.text; item.base = event;}
    item.el.querySelector('[data-action="refresh"]').hidden = !item.dirty || item.base.revision === event.revision;
    root.hidden = false;
  }
  window.SpeechDrafts = {reset,update};
})();

let speechModeBusy = false;
document.getElementById('speechMode').onchange = async event => {
  speechModeBusy = true; event.target.disabled = true;
  try {
    const response = await fetch('/api/speech/mode', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:event.target.value})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '切换失败');
    window.SpeechDrafts.reset();
  } catch (error) {
    const notice = document.getElementById('captureNotice');
    notice.hidden = false; notice.className = 'error'; notice.textContent = error.message;
  } finally {speechModeBusy = false; await refreshState();}
};
