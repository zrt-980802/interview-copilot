const $ = id => document.getElementById(id);
let revision;
function show(value) {
  revision = value.revision;
  $('recordingEnabled').checked = value.enabled;
  $('recordingAuto').checked = value.auto_transcribe;
  $('recordingModel').value = value.model;
  $('recordingDevice').value = value.device;
  $('recordingConfigState').textContent = !value.configured ? '本地语音运行环境不可用' :
    !value.model_ready ? '本地模型权重尚未准备' : '本机处理 · 录音不上传';
}
async function request(body) {
  const response = await fetch('/api/recordings/settings', {method: body ? 'POST' : 'GET', cache: 'no-store',
    headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '录音配置读取失败');
  return value;
}
$('recordingSave').onclick = async () => {
  $('recordingSave').disabled = true;
  try {
    const value = await request({revision, enabled: $('recordingEnabled').checked, auto_transcribe: $('recordingAuto').checked,
      model: $('recordingModel').value, device: $('recordingDevice').value});
    show(value); $('recordingConfigState').textContent = '已保存 · ' + $('recordingConfigState').textContent;
  } catch (error) { $('recordingConfigState').textContent = error.message; }
  finally { $('recordingSave').disabled = false; }
};
try { show(await request()); } catch (error) { $('recordingConfigState').textContent = error.message; }
