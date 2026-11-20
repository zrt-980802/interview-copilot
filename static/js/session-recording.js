const status = document.getElementById('recordingState');
let requestId = 0;
async function refresh() {
  const id = ++requestId;
  try {
    const response = await fetch('/api/recordings/current', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const value = await response.json();
    if (id !== requestId) return;
    const tracks = [...new Set(value.chunks.map(chunk => chunk.source))];
    status.textContent = !value.enabled ? '录音归档已关闭' : value.writer_error || value.dropped_packets ? '录音存在缺口' :
      value.capturing.length ? '正在归档 · ' + value.capturing.map(s => s === 'mic' ? '麦克风' : '系统声音').join(' / ') :
      tracks.length ? '录音已暂停' : '录音待开始';
  } catch {
    if (id !== requestId) return;
    status.textContent = '录音状态不可用';
  }
  window.WorkspaceView?.updateRecording(status.textContent === '录音待开始'
    ? '已开启 · 本地归档 · 待开始' : status.textContent);
}
document.getElementById('btnEnd').onclick = async event => {
  const button = event.currentTarget; button.disabled = true;
  try {
    const response = await fetch('/api/session/end', {method: 'POST'});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || '结束面试失败');
    location.href = '/history?session=' + encodeURIComponent(value.archived_session_id);
  } catch (error) { status.textContent = error.message; button.disabled = false; }
};
window.addEventListener('workspace-recording-changed', refresh);
await refresh();
setInterval(refresh, 3000);
