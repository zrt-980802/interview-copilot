const $ = id => document.getElementById(id);
const fields = ['base_url','quick_model','detail_model','allow_anonymous','fallback_enabled',
  'fallback_base_url','fallback_model','fallback_allow_anonymous'];
const presets = {
  openai:{base_url:'https://api.openai.com/v1',quick_model:'gpt-4.1-mini',detail_model:'gpt-4.1'},
  deepseek:{base_url:'https://api.deepseek.com/v1',quick_model:'deepseek-chat',detail_model:'deepseek-chat'},
  zhipu:{base_url:'https://open.bigmodel.cn/api/paas/v4',quick_model:'glm-5.3-flash',detail_model:'glm-5.3'}
};
let revision, busy = false, loaded = false;
const cleared = {api_key:false,fallback_api_key:false};
function presetFor(url) {
  return Object.keys(presets).find(key=>presets[key].base_url===(url||'').replace(/\/+$/,''))||'custom';
}
function providerName(url) {
  const name={openai:'OpenAI',deepseek:'DeepSeek',zhipu:'智谱 GLM'}[presetFor(url)];
  if(name)return name;
  try{return new URL(url).hostname;}catch{return '未设置地址';}
}
function showInventory(value) {
  const summary=value.provider_summary;
  if(!summary){$('providerCount').textContent='';$('providerInventory').hidden=true;return;}
  $('providerCount').textContent=`已配置 ${summary.configured_count} / ${summary.capacity} 个 Provider · 已启用 ${summary.enabled_count} 个`;
  $('providerInventory').replaceChildren(...summary.slots.map(slot=>{
    const prefix=slot.channel==='primary'?'':'fallback_',item=document.createElement('li');
    const role=document.createElement('span'),name=document.createElement('strong'),state=document.createElement('span');
    role.className='provider-role';role.textContent=slot.channel==='primary'?'主用':'备用';
    name.textContent=providerName(value[prefix+'base_url']);
    state.className='provider-state'+(slot.configured&&slot.enabled?' enabled':'');
    state.textContent=!slot.configured?'配置未完成':slot.enabled?'已启用':'未启用';
    item.append(role,name,state);return item;
  }));
  $('providerInventory').hidden=false;
}
function notice(text,error=false) { $('notice').textContent=text; $('notice').classList.toggle('error',error); }
async function request(path, body) {
  const response = await fetch('/api/providers'+path,{method:body ? 'POST':'GET',cache:'no-store',
    headers:body ? {'Content-Type':'application/json'} : {},body:body ? JSON.stringify(body) : undefined});
  const value=await response.json();
  if(!response.ok)throw new Error(value.error||'请求失败');
  return value;
}
function fallback() {
  $('fallbackFields').hidden=!$('fallback_enabled').checked;
  for(const id of ['fallback_base_url','fallback_model'])$(id).required=$('fallback_enabled').checked;
}
function show(value) {
  revision=value.revision;
  for(const id of fields) {
    if($(id).type==='checkbox')$(id).checked=!!value[id];
    else $(id).value=value[id]||'';
  }
  const names={saved:'密钥已在本机加密保存',session:'密钥仅本次进程有效',environment:'使用环境变量密钥',none:'未配置密钥',unavailable:'保存的密钥无法解密'};
  for(const [key,prefix,status] of [['api_key','','primaryStatus'],['fallback_api_key','fallback_','fallbackStatus']]) {
    $(key).value='';$(key).placeholder=value[prefix+'key_set']?'已配置，留空保留':'未配置';
    $(status).textContent=names[value[prefix+'key_source']]||'未配置密钥';
    cleared[key]=false;$('clear_'+key).setAttribute('aria-pressed','false');
  }
  $('remember').checked=value.key_source!=='session';
  $('preset').value=presetFor(value.base_url);
  $('fallbackPreset').value=presetFor(value.fallback_base_url);
  showInventory(value);
  $('mode').hidden=!value.preview;fallback();
}
function payload() {
  return {...Object.fromEntries(fields.map(id=>[id,$(id).type==='checkbox'?$(id).checked:$(id).value.trim()])),
    revision,api_key:$('api_key').value,fallback_api_key:$('fallback_api_key').value,
    remember:$('remember').checked,clear_api_key:cleared.api_key,clear_fallback_api_key:cleared.fallback_api_key};
}
async function run(work) {
  if(busy)return;
  busy=true;$('fields').disabled=true;$('reload').disabled=true;
  try { await work(); } catch(error) { notice(error.message,true); }
  finally { busy=false;$('fields').disabled=!loaded;$('reload').disabled=false; }
}
async function load() {
  await run(async()=>{const value=await request('');show(value);loaded=true;notice('配置已加载');});
}
$('reload').onclick=load;
$('fallback_enabled').onchange=fallback;
$('preset').onchange=()=>{const value=presets[$('preset').value];if(value)for(const [id,text] of Object.entries(value))$(id).value=text;};
$('fallbackPreset').onchange=()=>{
  const value=presets[$('fallbackPreset').value];
  if(value){$('fallback_base_url').value=value.base_url;$('fallback_model').value=value.detail_model;}
};
for(const key of ['api_key','fallback_api_key']) {
  $('clear_'+key).onclick=()=>{
    cleared[key]=!cleared[key];$('clear_'+key).setAttribute('aria-pressed',String(cleared[key]));
    if(cleared[key])$(key).value='';
    $(key).placeholder=cleared[key]?'将在保存时清除':'留空保留已有密钥';
  };
  $(key).oninput=()=>{if($(key).value){cleared[key]=false;$('clear_'+key).setAttribute('aria-pressed','false');}};
}
for(const button of document.querySelectorAll('[data-action]'))button.onclick=()=>run(async()=>{
  const channel=button.dataset.channel, action=button.dataset.action;
  const result=$(channel+'Result');result.classList.remove('error');result.textContent='连接中…';
  try {
    const value=await request('/'+action,{settings:payload(),channel});
    if(action==='models') {
      $(channel+'Models').replaceChildren(...value.models.map(id=>new Option(id,id)));
      result.textContent=(value.preview?'模拟列表 · ':'')+`已获取 ${value.models.length} 个模型`;
    } else result.textContent=(value.preview?'模拟连接通过':'连接通过')+` · ${value.elapsed_ms} ms`;
  } catch(error) { result.textContent=error.message;result.classList.add('error'); }
});
$('providerForm').onsubmit=event=>{
  event.preventDefault();run(async()=>{
    const value=await request('',payload());show(value);
    notice(value.preview?'演示配置已保存至临时目录':'配置已保存，后续回答使用新配置');
  });
};
window.lucide?.createIcons();load();
