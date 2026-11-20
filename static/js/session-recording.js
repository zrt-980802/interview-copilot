const status = document.getElementById('recordingState');
async function refresh() {
  try {
    const response = await fetch('/api/recordings/current', {cache: 'no-store'});
    if (!response.ok) throw new Error();
    const value = await response.json();
    const tracks = [...new Set(value.chunks.map(chunk => chunk.source))];
    status.textContent = value.writer_error || value.dropped_packets ? '录音存在缺口' : !value.enabled ? '录音归档已关闭' :
      value.capturing.length ? '正在归档 · ' + value.capturing.map(s => s === 'mic' ? '麦克风' : '系统声音').join(' / ') :
      tracks.length ? '录音已暂停' : '录音待开始';
  } catch { status.textContent = '录音状态不可用'; }
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
await refresh();
setInterval(refresh, 3000);
