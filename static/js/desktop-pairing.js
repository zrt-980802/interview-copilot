const $ = id => document.getElementById(id);
const dialog = $('phoneDialog');
let timer, code, enabled = false, busy = false;
async function request(path, method = 'GET') {
  const response = await fetch(path, {method, cache: 'no-store'});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}
function show(value) {
  enabled = value.enabled;
  $('phoneState').textContent = enabled ? `${value.local_only ? '仅本机预览' : '手机端口已开启'} · ${value.port} · 已配对 ${value.devices} 台` : '手机端口未开启';
  $('phoneNewCode').disabled = !enabled; $('phoneRevoke').disabled = !enabled;
  const selected = $('phoneAddress').selectedIndex;
  $('phoneAddress').replaceChildren(...value.urls.map(url => {
    const option = document.createElement('option'); option.value = url; option.textContent = url; return option;
  }));
  $('phoneAddress').selectedIndex = Math.max(0, Math.min(selected, value.urls.length - 1));
  $('phoneAddress').hidden = !value.urls.length;
  $('phoneCode').textContent = value.code || (enabled ? '配对码已失效' : '');
  $('phoneExpiry').textContent = value.code ? `${value.expires_in} 秒后失效 · 仅限一次配对` : '';
  if (code !== value.code) {
    code = value.code; updateQR();
  }
  $('phoneHint').textContent = enabled
    ? value.local_only ? '此端口仅供电脑浏览器预览，手机无法通过局域网连接' : value.urls.length ? '手机与电脑需连接同一可互通的局域网' : '未检测到局域网地址，请检查网络连接'
    : '使用“后台手机模式.bat”启动，或运行 python server.py --mobile';
}
function updateQR() {
  $('phoneQr').hidden = !code || $('phoneAddress').selectedIndex < 0;
  if (!$('phoneQr').hidden) $('phoneQr').src = '/api/mobile/qr?address=' + $('phoneAddress').selectedIndex + '&v=' + Date.now();
}
async function refresh() {
  if (busy || !dialog.open) return;
  busy = true;
  try { show(await request('/api/mobile')); }
  catch (error) { $('phoneError').textContent = error.message; }
  finally { busy = false; }
}
$('btnPhone').onclick = async () => {
  dialog.showModal(); $('phoneError').textContent = '';
  await refresh(); timer = setInterval(refresh, 1000);
};
$('phoneClose').onclick = () => dialog.close();
dialog.addEventListener('close', () => { clearInterval(timer); timer = null; });
$('phoneAddress').onchange = updateQR;
$('phoneNewCode').onclick = async () => {
  $('phoneError').textContent = ''; $('phoneNewCode').disabled = true;
  try { show(await request('/api/mobile/pairing', 'POST')); }
  catch (error) { $('phoneError').textContent = error.message; }
  finally { $('phoneNewCode').disabled = !enabled; }
};
$('phoneRevoke').onclick = async () => {
  $('phoneError').textContent = '';
  try { await request('/api/mobile/revoke', 'POST'); await refresh(); }
  catch (error) { $('phoneError').textContent = error.message; }
};
window.lucide?.createIcons();
