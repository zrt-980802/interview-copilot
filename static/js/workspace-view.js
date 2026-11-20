(() => {
  const $ = id => document.getElementById(id);
  const storage = {
    read(key) { try { return localStorage.getItem(key); } catch { return null; } },
    write(key, value) { try { localStorage.setItem(key, value); } catch {} },
  };
  const narrow = matchMedia('(max-width:760px)');
  const sidebar = $('questionSidebar');
  const preparation = $('preparation');
  let capture = null, withQuestions = false, preparationOverride = null;
  let materials = null, providers = null, recording = null, checkedAt = 0, checking = false;
  let view = null, onLayout = change => change();
  let listPreference = storage.read('workspace-question-list');

  async function readStatus(path) {
    const response = await fetch(path, {cache:'no-store'});
    if (!response.ok) throw new Error('状态不可用');
    return response.json();
  }

  async function refreshPreparation(force = false) {
    if (checking || (!force && Date.now() - checkedAt < 10000)) return;
    checking = true;
    try {
      const [modelStatus, filesStatus] = await Promise.allSettled([
        readStatus('/api/providers'), readStatus('/api/files')
      ]);
      providers = modelStatus.status === 'fulfilled' ? modelStatus.value : null;
      materials = filesStatus.status === 'fulfilled' ? filesStatus.value : null;
      checkedAt = Date.now();
      renderPreparation();
    } finally {
      checking = false;
    }
  }

  function deviceState(source) {
    if (!capture) return '状态未知';
    const mic = source === 'mic';
    const select = $(mic ? 'micSel' : 'deviceSel');
    const devices = capture[mic ? 'input_devices' : 'devices'];
    const engine = capture[mic ? 'self_engine' : 'engine'];
    const listening = capture[mic ? 'self_listening' : 'listening'];
    if (capture.capture_failed?.includes(source)) return '采集失败，请检查设备';
    if (capture.audio_test_mode && !mic) return '提问测试中停用';
    if (mic && select.value === '' && !listening) return '已关闭';
    const device = select.selectedOptions[0]?.textContent;
    if (!listening && devices && !devices.length) return '未发现可用设备';
    if (!listening && select.value && select.value !== 'default' && devices &&
        !devices.some(item => String(item.index) === select.value)) return '所选设备未连接';
    const signal = listening ? engine?.speaking ? '检测到声音' : '监听中，当前未检测到发言' : '尚未检测声音';
    return [device, signal].filter(Boolean).join(' · ');
  }

  function materialState() {
    if (!materials || !capture || !Array.isArray(materials.files)) return '状态未知';
    const visible = materials.files.filter(file => !file.archived);
    const ready = visible.filter(file => file.enabled && file.kind !== 'resume' && file.status === 'ready').length;
    const waiting = visible.filter(file => ['queued','parsing','indexing'].includes(file.status)).length;
    const failed = visible.filter(file => file.status === 'failed').length;
    const text = [capture.resume?.loaded ? '简历已加载' : '简历未加载'];
    if (ready) text.push(ready + ' 份资料已启用');
    const legacyCount = (materials.legacy || []).filter(file => file.kind !== 'resume').length;
    if (legacyCount) text.push(legacyCount + ' 份原有资料');
    if (capture.jd_loaded) text.push('已加载岗位说明');
    if (waiting) text.push(waiting + ' 份等待解析');
    if (failed) text.push(failed + ' 份解析失败');
    return text.join(' · ');
  }

  function renderPreparation() {
    const idle = !withQuestions && !capture?.listening && !capture?.self_listening;
    const open = preparationOverride ?? (idle && view !== 'focus');
    preparation.hidden = !open;
    $('togglePreparation').setAttribute('aria-expanded', String(open));
    if (!open) return;
    $('prepOutput').textContent = deviceState('loopback');
    $('prepMic').textContent = deviceState('mic');
    const slot = providers?.provider_summary?.slots?.find(item => item.channel === 'primary');
    const model = providers?.quick_model;
    $('prepModel').textContent = !slot ? checkedAt ? '状态未知' : '未检查'
      : !slot.configured ? '未配置' : [model, '连接未检查'].filter(Boolean).join(' · ');
    $('prepMaterials').textContent = materialState();
    $('prepRecording').textContent = recording ?? '状态未知';
  }

  function renderCaptureIssue() {
    const failed = capture?.capture_failed || [];
    const channels = [failed.includes('loopback') && '系统声音', failed.includes('mic') && '麦克风'].filter(Boolean);
    $('captureIssue').hidden = !channels.length;
    $('captureIssueText').textContent = channels.length ? channels.join('、') + '采集失败' : '';
  }

  function setSidebar() {
    const open = listPreference === null ? !narrow.matches : listPreference === 'open';
    sidebar.hidden = !open;
    $('toggleQuestions').setAttribute('aria-expanded', String(open));
    $('toggleQuestions').title = open ? '收起问题列表' : '展开问题列表';
    $('toggleQuestions').setAttribute('aria-label', $('toggleQuestions').title);
  }

  function setView(next) {
    if (next === view) return;
    onLayout(() => {
      view = next;
      document.body.dataset.workspaceView = next;
      $('viewWorkbench').setAttribute('aria-pressed', String(next === 'workbench'));
      $('viewFocus').setAttribute('aria-pressed', String(next === 'focus'));
      preparationOverride = null;
      api.captureLayout?.(next === 'focus');
      renderPreparation();
    });
    storage.write('workspace-view', next);
  }

  const api = {
    init(layout) {
      onLayout = layout;
      $('viewWorkbench').onclick = () => setView('workbench');
      $('viewFocus').onclick = () => setView('focus');
      $('toggleQuestions').onclick = () => onLayout(() => {
        listPreference = sidebar.hidden ? 'open' : 'closed';
        storage.write('workspace-question-list', listPreference);
        setSidebar();
      });
      $('togglePreparation').onclick = () => {
        preparationOverride = preparation.hidden;
        renderPreparation();
        if (preparationOverride) {
          refreshPreparation(true);
          preparation.scrollIntoView({block:'nearest'});
        }
      };
      for (const id of ['deviceSel','micSel']) $(id).addEventListener('change', renderPreparation);
      narrow.addEventListener('change', () => onLayout(setSidebar));
      setSidebar();
      setView(storage.read('workspace-view') === 'focus' ? 'focus' : 'workbench');
    },
    hasQuestions(value) {
      withQuestions = value;
      if (value) preparationOverride = null;
      renderPreparation();
    },
    updateCapture(value) {
      const wasActive = capture?.listening || capture?.self_listening;
      capture = value;
      if (!wasActive && (value?.listening || value?.self_listening)) preparationOverride = null;
      renderCaptureIssue();
      renderPreparation();
      if (!preparation.hidden) refreshPreparation();
    },
    updateRecording(label) {
      recording = label;
      renderPreparation();
    },
  };
  window.WorkspaceView = api;
})();
