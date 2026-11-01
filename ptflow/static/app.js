(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const apiBase = new URL('./api/', location.href.split('#')[0]);
  const pages = {overview:'总览',torrents:'托管种子',candidates:'候选池',logs:'运行日志',settings:'设置'};
  const kinds = {full:'完整运行',query:'查询候选',brush:'执行刷流',cleanup:'清理检查',collect:'统计采样'};
  const settingsAnchors = ['connections','schedule-settings','strategy-settings','security-settings','qb-url','qb-username','qb-password','qb-api-key','mt-api-key'];
  const statuses = {succeeded:'成功',partial:'部分完成',skipped:'已跳过',failed:'失败',pending:'待处理',submitted:'已确认',uncertain:'待核实',removed:'已移除',running:'运行中'};
  const knownStages = new Set([...$('log-stages').options].map(option => option.value));
  const state = {csrf:'',authenticated:false,setup:false,page:'overview',config:null,secretFields:[],dirty:false,revision:0,status:null,clockOffset:0,torrents:[],candidates:[],runs:[],history:[],cursor:0,logGeneration:0,logLoading:false,logPaused:false,logCount:0,polling:false,timer:null,lastData:0,authLoading:false,toastTimer:null,busyRun:false,manualRun:null};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const now = () => Date.now() + state.clockOffset;
  const epoch = value => typeof value === 'number' ? value * 1000 : Date.parse(value);
  const number = (value, digits=0) => Number.isFinite(Number(value)) && value !== null && value !== '' ? Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits}) : '—';
  const date = (value, full=false) => {
    if (!value || !Number.isFinite(epoch(value))) return '—';
    return new Intl.DateTimeFormat('zh-CN',{timeZone:state.status?.timezone || state.config?.schedule.timezone || 'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(full ? {year:'numeric',second:'2-digit',hour12:false} : {hour12:false})}).format(new Date(epoch(value)));
  };
  const timeOnly = value => new Intl.DateTimeFormat('zh-CN',{timeZone:state.status?.timezone || 'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
  function bytes(value, speed=false) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return {value:'—',unit:''};
    let size = Math.max(0, Number(value));
    const units = speed ? ['B/s','KiB/s','MiB/s','GiB/s'] : ['B','KiB','MiB','GiB','TiB','PiB'];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
    return {value:number(size, unit > 0 ? 2 : 0),unit:units[unit]};
  }
  const sizeText = (value, speed=false) => { const size = bytes(value,speed); return `${size.value}${size.unit ? ' '+size.unit : ''}`; };
  const metric = (id,value,speed=false) => { const size = bytes(value,speed); $(id).innerHTML = `${escape(size.value)}<small>${escape(size.unit)}</small>`; };
  const badge = value => `<span class="badge ${['failed','ERROR'].includes(value) ? 'danger' : ['partial','uncertain','WARNING'].includes(value) ? 'warning' : ['skipped','removed','DEBUG'].includes(value) ? 'neutral' : ''}">${escape(statuses[value] || {INFO:'信息',WARNING:'警告',ERROR:'错误',DEBUG:'调试'}[value] || value || '未知')}</span>`;
  function showMessage(id,message) { const element = $(id); element.textContent = message || ''; element.hidden = !message; }
  function toast(message,error=false) {
    clearTimeout(state.toastTimer);
    $('toast').textContent = message;
    $('toast').classList.toggle('error',error);
    $('toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 7000 : 4500);
  }
  async function request(path,{method='GET',body}={}) {
    const headers = {'Accept':'application/json'};
    if (method !== 'GET') { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = state.csrf; }
    let response;
    try { response = await fetch(new URL(path,apiBase),{method,headers,credentials:'same-origin',cache:'no-store',...(body !== undefined ? {body:JSON.stringify(body)} : {})}); }
    catch { throw new Error('无法连接 PTFlow 服务。请检查应用是否运行及网络连接。'); }
    let result;
    try { result = await response.json(); }
    catch { throw new Error(`服务返回了非 JSON 响应（HTTP ${response.status}）。请检查 fnOS 网关与服务状态。`); }
    if (!response.ok) {
      const error = new Error(result.error || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      if (response.status === 401 && state.authenticated && !['login','password'].includes(path)) {
        state.authenticated = false;
        clearTimeout(state.timer);
        $('app').hidden = true;
        $('auth-screen').hidden = false;
        initAuth('登录已过期，请重新输入工作台密码。');
      }
      throw error;
    }
    return result;
  }
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('theme-toggle').setAttribute('aria-label',theme === 'dark' ? '切换浅色模式' : '切换深色模式');
    $('theme-toggle').title = theme === 'dark' ? '切换浅色模式' : '切换深色模式';
    try { localStorage.setItem('ptflow-theme',theme); } catch { /* Private browsers may disable local storage. */ }
  }
  let savedTheme;
  try { savedTheme = localStorage.getItem('ptflow-theme'); } catch { /* System theme remains available. */ }
  setTheme(['light','dark'].includes(savedTheme) ? savedTheme : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  $('theme-toggle').addEventListener('click',() => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  function renderAuth(message='') {
    $('app').hidden = true;
    $('auth-screen').hidden = false;
    $('auth-loading').hidden = true;
    $('auth-retry').hidden = true;
    $('auth-form').hidden = false;
    $('auth-title').textContent = state.setup ? '开启你的 PTFlow' : '欢迎回来';
    $('auth-eyebrow').textContent = state.setup ? 'A LITTLE SETUP. A LOT OF FLOW.' : 'WELCOME BACK';
    $('auth-description').textContent = state.setup ? '首次使用，请设置至少 10 个字符的工作台密码。下一步连接你的下载器与 M-Team。' : '输入工作台密码，继续你的下载计划。';
    $('auth-submit').textContent = state.setup ? '创建工作台 →' : '进入工作台 →';
    $('auth-password').autocomplete = state.setup ? 'new-password' : 'current-password';
    $('auth-password').minLength = state.setup ? 10 : 1;
    $('auth-confirm-wrap').hidden = !state.setup;
    $('auth-confirm').required = state.setup;
    showMessage('auth-error',message);
  }
  async function initAuth(message='') {
    if (state.authLoading) return;
    state.authLoading = true;
    try {
      const auth = await request('auth');
      state.csrf = auth.csrf_token || '';
      state.setup = auth.setup_required;
      state.authenticated = auth.authenticated;
      if (state.authenticated) await openApp(); else renderAuth(message);
    } catch (error) {
      $('auth-form').hidden = true;
      $('auth-loading').hidden = false;
      $('auth-loading').textContent = error.message;
      $('auth-retry').hidden = false;
    } finally { state.authLoading = false; }
  }
  $('auth-retry').addEventListener('click',() => initAuth());
  $('auth-confirm').addEventListener('input',() => $('auth-confirm').setCustomValidity(''));
  $('auth-form').addEventListener('submit',async event => {
    event.preventDefault();
    if (state.setup && $('auth-password').value !== $('auth-confirm').value) { showMessage('auth-error','两次输入的密码不一致。'); $('auth-confirm').focus(); return; }
    const button = $('auth-submit');
    button.disabled = true;
    showMessage('auth-error','');
    try {
      await request(state.setup ? 'setup' : 'login',{method:'POST',body:{password:$('auth-password').value}});
      $('auth-form').reset();
      const auth = await request('auth');
      state.csrf = auth.csrf_token || '';
      state.authenticated = auth.authenticated;
      state.setup = auth.setup_required;
      if (!state.authenticated) throw new Error('登录会话未建立，请检查浏览器 Cookie 设置。');
      await openApp();
    } catch (error) { showMessage('auth-error',error.message); }
    finally { button.disabled = false; }
  });
  $('logout').addEventListener('click',async () => {
    if (state.dirty && !confirm('尚有未保存的设置。退出后将丢失这些更改，确定退出吗？')) return;
    $('logout').disabled = true;
    try {
      await request('logout',{method:'POST',body:{}});
      state.authenticated = false;
      state.dirty = false;
      state.config = null;
      clearTimeout(state.timer);
      $('settings-form').reset();
      $('password-form').reset();
      $('app').hidden = true;
      $('auth-screen').hidden = false;
      await initAuth();
    } catch (error) { toast(error.message,true); }
    finally { $('logout').disabled = false; }
  });
  async function openApp() {
    $('auth-screen').hidden = true;
    $('app').hidden = false;
    state.status = null;
    state.config = null;
    state.torrents = [];
    state.candidates = [];
    state.runs = [];
    state.manualRun = null;
    $('run-feedback').hidden = true;
    state.history = [];
    state.dirty = false;
    state.lastData = 0;
    resetLogs(false);
    $('settings-fields').disabled = true;
    $('save-config').disabled = true;
    state.page = '';
    navigate(false);
    await Promise.all([loadConfig(),loadStatus(),loadRuns()]);
    if (!state.authenticated) return;
    await loadPageData();
    clearTimeout(state.timer);
    state.timer = setTimeout(poll,2000);
  }
  function navigate(fetchData=true) {
    const hash = location.hash.slice(1);
    const settingsAnchor = settingsAnchors.includes(hash);
    const page = settingsAnchor ? 'settings' : Object.hasOwn(pages,hash) ? hash : 'overview';
    const changed = page !== state.page;
    state.page = page;
    for (const key of Object.keys(pages)) $('page-'+key).hidden = key !== page;
    document.querySelectorAll('[data-page]').forEach(link => {
      link.classList.toggle('active',link.dataset.page === page);
      if (link.dataset.page === page) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
    });
    $('page-label').textContent = pages[page];
    document.title = `${pages[page]} · PTFlow`;
    if (changed && !settingsAnchor) window.scrollTo({top:0,behavior:'instant'});
    if (settingsAnchor) requestAnimationFrame(() => { $(hash).scrollIntoView({block:'center'}); if ($(hash).matches('input')) $(hash).focus({preventScroll:true}); });
    if (fetchData && state.authenticated && changed) loadPageData();
  }
  window.addEventListener('hashchange',() => navigate());
  document.querySelectorAll('.settings-jump a').forEach(link => link.addEventListener('click',() => { if (location.hash === link.hash) $(link.hash.slice(1)).scrollIntoView({block:'start'}); }));
  async function loadConfig() {
    try {
      const config = await request('config');
      if (!state.authenticated) return;
      acceptConfig(config,true);
      showMessage('config-error','');
    } catch (error) { showMessage('config-error',error.message); $('save-state').textContent = '配置读取失败'; }
  }
  function acceptConfig(result,fill=false) {
    state.secretFields = result.secret_fields || [];
    state.config = Object.fromEntries(Object.entries(result).filter(([key]) => key !== 'secret_fields'));
    if (fill) {
      for (const input of $('settings-form').querySelectorAll('[name]')) {
        const [section,field] = input.name.split('.');
        const value = state.config[section]?.[field];
        if (input.name === 'mteam.modes') input.checked = (value || []).includes(input.value);
        else if (input.type === 'checkbox') input.checked = Boolean(value);
        else input.value = input.hasAttribute('data-secret') ? '' : value ?? '';
      }
      state.dirty = false;
      updateDirty();
    }
    updateSecretHints();
    $('settings-fields').disabled = false;
    $('save-config').disabled = false;
    renderStatus();
  }
  function updateSecretHints() {
    document.querySelectorAll('[data-secret-for]').forEach(hint => {
      const saved = state.secretFields.includes(hint.dataset.secretFor);
      hint.textContent = saved ? '已安全保存 · 留空保留；填写新值可替换。' : '尚未保存 · 请填写当前认证方式所需的凭据。';
    });
    document.querySelectorAll('[data-secret]').forEach(input => { input.placeholder = state.secretFields.includes(input.name) ? '已保存，留空保留' : '尚未设置'; });
    const keyAuth = $('qb-auth').value === 'api_key';
    $('qb-username').closest('.field').hidden = keyAuth;
    $('qb-password').closest('.field').hidden = keyAuth;
    $('qb-api-key').closest('.field').hidden = !keyAuth;
  }
  function readDraft() {
    const config = {downloader:{},mteam:{modes:[]},schedule:{},strategy:{}};
    for (const input of $('settings-form').querySelectorAll('[name]')) {
      const [section,field] = input.name.split('.');
      if (input.name === 'mteam.modes') { if (input.checked) config.mteam.modes.push(input.value); }
      else config[section][field] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.hasAttribute('data-secret') ? input.value : input.value.trim();
    }
    return config;
  }
  function updateDirty() {
    $('dirty-dot').hidden = !state.dirty;
    $('save-state').classList.toggle('dirty',state.dirty);
    $('save-state').textContent = state.dirty ? '有未保存的更改' : '配置已保存';
    $('savebar-label').textContent = state.dirty ? '有未保存的更改' : '更改只在保存后生效';
  }
  $('settings-form').addEventListener('input',event => {
    state.revision++; state.dirty = true; updateDirty();
    const target = event.target.name?.split('.')[0];
    if (['downloader','mteam'].includes(target)) showMessage('test-'+target,'');
  });
  $('settings-form').addEventListener('change',updateSecretHints);
  window.addEventListener('beforeunload',event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
  $('settings-form').addEventListener('submit',async event => {
    event.preventDefault();
    const revision = state.revision;
    const draft = readDraft();
    if (!draft.mteam.modes.length) { $('save-message').textContent = '请至少选择一个 M-Team 查询分类。'; $('save-message').classList.add('error'); return; }
    $('save-config').disabled = true;
    $('save-config').textContent = '正在保存…';
    $('save-message').classList.remove('error');
    try {
      const result = await request('config',{method:'PUT',body:draft});
      acceptConfig(result,state.revision === revision);
      $('save-message').textContent = state.revision === revision ? '已保存 · 运行计划已立即更新。' : '已保存提交内容；新编辑的更改仍未保存。';
      showMessage('config-error','');
      toast('设置已保存，计划已更新。');
      await loadStatus();
    } catch (error) { $('save-message').textContent = error.message; $('save-message').classList.add('error'); }
    finally { $('save-config').disabled = !state.config; $('save-config').textContent = '保存设置'; }
  });
  document.querySelectorAll('[data-test]').forEach(button => button.addEventListener('click',async () => {
    const target = button.dataset.test;
    const output = $('test-'+target);
    const text = button.textContent;
    button.disabled = true;
    button.textContent = '正在测试…';
    output.hidden = false;
    output.classList.remove('failed');
    output.textContent = '正在使用当前表单内容测试；不会保存更改。';
    try {
      const draft = readDraft();
      const tested = JSON.stringify(draft[target]);
      const result = await request('test',{method:'POST',body:{target,config:draft}});
      const changed = tested !== JSON.stringify(readDraft()[target]);
      output.classList.toggle('failed',!result.ok || changed);
      output.textContent = changed ? '测试期间连接内容已更改，请重新测试当前表单。' : `${result.ok ? '连接成功' : '连接失败'} · ${result.message || '服务未返回说明'}（使用测试开始时的表单；不会保存）`;
    } catch (error) { output.classList.add('failed'); output.textContent = error.message; }
    finally { output.hidden = false; button.disabled = false; button.textContent = text; }
  }));
  $('automation-toggle').addEventListener('click',async () => {
    if (!state.config || !state.status) return;
    const enabled = !state.status.enabled;
    $('automation-toggle').disabled = true;
    try {
      const result = await request('config',{method:'PUT',body:{schedule:{enabled}}});
      acceptConfig(result,false);
      if (!state.dirty) $('settings-form').elements.namedItem('schedule.enabled').checked = enabled;
      toast(`${enabled ? '已启用自动运行' : '已暂停自动查询、添加与闲置清理；统计采样与 Free 安全保护继续运行'}${state.dirty ? '；设置页未保存的草稿仍保留。' : '。'}`);
      await loadStatus();
    } catch (error) { toast(error.message,true); }
    finally { $('automation-toggle').disabled = !state.config; }
  });
  $('password-form').addEventListener('submit',async event => {
    event.preventDefault();
    const output = $('password-message');
    output.hidden = false;
    output.classList.remove('error');
    if ($('new-password').value !== $('confirm-password').value) { output.textContent = '两次输入的新密码不一致。'; output.classList.add('error'); $('confirm-password').focus(); return; }
    const button = $('password-form').querySelector('button');
    button.disabled = true;
    try {
      await request('password',{method:'POST',body:{current_password:$('current-password').value,password:$('new-password').value}});
      const auth = await request('auth');
      state.csrf = auth.csrf_token || '';
      $('password-form').reset();
      output.textContent = '工作台密码已更新。';
    } catch (error) { output.textContent = error.message; output.classList.add('error'); }
    finally { button.disabled = false; }
  });
  async function loadStatus() {
    try {
      const status = await request('status');
      if (!state.authenticated) return;
      const finished = Boolean(state.status?.current_run && !status.current_run);
      const changedRun = Boolean(status.last_run && status.last_run.id !== state.status?.last_run?.id);
      state.status = status;
      const serverTime = Date.parse(status.now);
      if (Number.isFinite(serverTime)) state.clockOffset = serverTime - Date.now();
      renderStatus();
      showMessage('global-error','');
      $('connection-state').innerHTML = '<span class="status-dot"></span>服务在线';
      $('connection-state').classList.remove('offline');
      if (finished || changedRun) { state.lastData = 0; loadRuns(); }
    } catch (error) {
      $('connection-state').innerHTML = '<span class="status-dot"></span>连接中断';
      $('connection-state').classList.add('offline');
      showMessage('global-error',`${error.message} 已显示的数据为最后成功读取的快照，不代表当前状态。`);
    }
  }
  function missingConfig() {
    if (!state.config) return [];
    const missing = [];
    const downloader = state.config.downloader;
    if (!downloader.url) missing.push(['qb-url','qBittorrent 服务地址']);
    if (downloader.auth_type === 'api_key') {
      if (!state.secretFields.includes('downloader.api_key')) missing.push(['qb-api-key','qBittorrent API Key']);
    } else {
      if (!downloader.username) missing.push(['qb-username','qBittorrent 用户名']);
      if (!state.secretFields.includes('downloader.password')) missing.push(['qb-password','qBittorrent 密码']);
    }
    if (!state.secretFields.includes('mteam.api_key')) missing.push(['mt-api-key','M-Team API Key']);
    return missing;
  }
  function renderRunFeedback() {
    const tracked = state.manualRun;
    const current = state.status?.current_run;
    const run = tracked ? (current?.id === tracked.id ? current : state.runs.find(run => run.id === tracked.id) || (state.status?.last_run?.id === tracked.id ? state.status.last_run : tracked)) : current;
    const output = $('run-feedback');
    output.hidden = !run && !state.busyRun;
    if (output.hidden) return;
    const status = run?.status || (run ? 'running' : 'pending');
    const finished = Boolean(run?.finished_at);
    if (tracked && finished) state.manualRun = run;
    output.classList.toggle('danger',status === 'failed');
    output.classList.toggle('warning',['partial','skipped'].includes(status));
    const markup = `<div class="run-feedback-copy"><strong>${run ? escape(kinds[run.kind] || run.kind)+' · '+escape(statuses[status] || status) : '正在提交任务…'}</strong><p>${escape(run?.message || '等待服务确认，请勿重复点击。')}</p>${run ? `<p>${escape(finished ? date(run.finished_at,true)+' 结束' : run.stage || '等待执行')} · 本次运行 ${escape(run.id)}</p>` : ''}</div>${run ? `<div class="run-feedback-links"><a class="text-link run-log-link" href="#logs" data-run-id="${escape(run.id)}">${finished ? '查看本次日志' : '跟踪本次日志'} →</a>${finished && ['full','query','brush'].includes(run.kind) ? `<a class="text-link" href="#candidates">候选结果 →</a>${run.kind !== 'query' ? '<a class="text-link" href="#torrents">托管种子 →</a>' : ''}` : ''}</div>` : ''}`;
    if (output.innerHTML !== markup) output.innerHTML = markup;
  }
  function renderStatus() {
    const status = state.status;
    if (!status) return;
    $('version-label').textContent = 'v'+status.version;
    const missing = missingConfig();
    $('setup-notice').hidden = state.config ? !missing.length : status.configured;
    $('setup-missing').innerHTML = missing.map(([id,label]) => `<li><a href="#${id}">${escape(label)} →</a></li>`).join('');
    $('automation-title').textContent = status.enabled ? missing.length || !status.configured ? '自动运行已启用，但连接配置尚未补齐。' : status.working_time ? '自动运行已启用，一切按计划进行。' : '自动运行已启用，当前在工作时段之外。' : '自动查询、添加与闲置清理已暂停。';
    $('automation-description').textContent = status.current_run ? `${kinds[status.current_run.kind] || status.current_run.kind}进行中 · ${status.current_run.message || status.current_run.stage || '正在执行'}` : missing.length ? '请先补齐上方已保存配置并测试连接；启用开关不代表服务已连接。' : status.enabled ? status.working_time ? '等待下方计划时间；手动运行同样遵守磁盘、Free 与速度保护。' : '自动查询与添加会跳过当前时段；统计采样与安全维护仍按计划执行。' : '统计采样与 Free 安全保护继续运行；手动运行仍可使用。';
    $('automation-toggle').textContent = status.enabled ? '暂停自动运行' : '启用自动运行';
    $('automation-toggle').disabled = !state.config;
    metric('upload-speed',status.sample?.upspeed,true);
    metric('download-speed',status.sample?.dlspeed,true);
    metric('disk-free',status.sample?.free_space);
    $('upload-total').textContent = '累计 '+sizeText(status.sample?.uploaded);
    $('download-total').textContent = '累计 '+sizeText(status.sample?.downloaded);
    $('disk-protection').textContent = state.config ? `低于 ${number(state.config.strategy.min_free_gib,1)} GiB 时停止添加` : '尚未读取保护阈值';
    const summary = status.summary || {};
    $('managed-count').textContent = number(summary.submitted);
    $('nav-managed').textContent = number(summary.submitted);
    $('managed-summary').textContent = `待处理 ${number(summary.pending)} · 待核实 ${number(summary.uncertain)} · 失败 ${number(summary.failed)}`;
    $('schedule-timezone').textContent = `${status.timezone} · ${state.config ? state.config.schedule.work_start === state.config.schedule.work_end ? '全天运行' : state.config.schedule.work_start+'–'+state.config.schedule.work_end : '读取时段中'}`;
    const symbols = {query:'⌕',brush:'↓',cleanup:'◷',collect:'∿'};
    $('schedule-list').innerHTML = (status.schedules || []).map(schedule => `<div class="schedule-item"><div class="schedule-item-label"><span aria-hidden="true">${symbols[schedule.kind] || '·'}</span><div>${escape(schedule.label || kinds[schedule.kind] || schedule.kind)}<small>每 ${schedule.interval_seconds >= 60 ? number(schedule.interval_seconds / 60,1)+' 分钟' : number(schedule.interval_seconds)+' 秒'}</small></div></div><div class="schedule-next" data-next="${escape(schedule.next_at || '')}" data-kind="${escape(schedule.kind)}"></div></div>`).join('') || '<p class="muted">暂无计划</p>';
    $('current-run').hidden = !status.current_run;
    if (status.current_run) {
      const run = status.current_run;
      $('current-run').innerHTML = `<strong><span class="running-dot" aria-hidden="true"></span>${escape(kinds[run.kind] || run.kind)} · ${escape(run.stage || '准备中')}</strong><p>${escape(run.message || '任务正在执行…')}</p><p>${escape(date(run.started_at,true))} 开始 · ${escape(run.source === 'manual' ? '手动' : '自动')} · <a class="text-link run-log-link" href="#logs" data-run-id="${escape(run.id)}">跟踪本次日志 →</a></p>`;
    }
    updateRunButtons();
    updateCountdowns();
  }
  function countdown(value) {
    const seconds = Math.ceil((epoch(value) - now()) / 1000);
    if (!Number.isFinite(seconds)) return '等待调度';
    if (seconds <= 0) return state.status?.current_run ? '等待当前任务' : '即将运行';
    if (seconds >= 86400) return `${Math.floor(seconds/86400)} 天 ${Math.floor(seconds%86400/3600)} 小时`;
    if (seconds >= 3600) return `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
    return `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
  }
  function updateCountdowns() {
    document.querySelectorAll('[data-next]').forEach(element => {
      const next = element.dataset.next;
      const disabledCleanup = element.dataset.kind === 'cleanup' && state.config && !state.config.strategy.cleanup_enabled;
      const label = state.config && !state.config.downloader.url ? '未配置下载器' : disabledCleanup ? '仅 Free 保护' : ['query','brush'].includes(element.dataset.kind) && state.status?.enabled && !state.status.working_time ? '工作时段外' : next ? countdown(next) : state.status?.enabled || element.dataset.kind === 'collect' ? '等待调度' : '已暂停';
      element.innerHTML = `${escape(label)}${next ? `<small title="${escape(date(next,true))}">${escape(date(next))}</small>` : ''}`;
    });
    if (state.status?.sample) {
      const age = Math.max(0,(now() - epoch(state.status.sample.time)) / 1000);
      const stale = age > Math.max(90,(state.config?.schedule.stats_seconds || 30)*3);
      $('sample-note').classList.toggle('stale',stale);
      $('sample-note').textContent = `${stale ? '采样已过期 · ' : '最后真实采样 · '}${date(state.status.sample.time,true)} · ${age < 60 ? Math.floor(age)+' 秒前' : Math.floor(age/60)+' 分钟前'}。速度与累计传输均为下载器全局数据，不仅是 PTFlow。`;
    } else { $('sample-note').classList.remove('stale'); $('sample-note').textContent = state.config && !state.config.downloader.url ? '尚无下载器采样 · 请先保存 qBittorrent 连接配置。' : '等待第一次下载器采样 · 按下方采样计划自动读取；连接失败原因可在运行记录与日志中查看。'; }
  }
  setInterval(() => { if (state.authenticated && !document.hidden) updateCountdowns(); },1000);
  setInterval(() => { if (state.authenticated && state.page === 'logs' && !document.hidden && !state.logPaused) loadLogs(); },2000);
  function updateRunButtons() {
    const busy = state.busyRun || Boolean(state.status?.current_run);
    document.querySelectorAll('[data-run]').forEach(button => { button.disabled = busy; button.title = busy ? '已有任务运行中，请等待完成' : '手动运行仍遵守所有保护条件'; });
    renderRunFeedback();
  }
  document.querySelectorAll('[data-run]').forEach(button => button.addEventListener('click',async () => {
    if (state.busyRun || state.status?.current_run) return;
    state.busyRun = true;
    state.manualRun = null;
    updateRunButtons();
    try {
      const result = await request('run',{method:'POST',body:{kind:button.dataset.run}});
      state.manualRun = {id:result.id,kind:button.dataset.run,status:'pending',message:result.message || '任务已接收，等待执行'};
      renderRunFeedback();
      toast(result.message || '任务已启动，运行结果请查看日志。');
      await Promise.all([loadStatus(),loadRuns()]);
      state.lastData = 0;
    } catch (error) { toast(error.message,true); if (error.status === 409) await loadStatus(); }
    finally { state.busyRun = false; updateRunButtons(); }
  }));
  async function loadRuns() {
    try {
      const result = await request('runs');
      if (!state.authenticated) return;
      state.runs = result.items || [];
      renderRuns();
      renderRunFeedback();
      showMessage('runs-error','');
    } catch (error) { showMessage('runs-error',error.message); }
  }
  function renderRuns() {
    const runs = state.runs.length ? state.runs : state.status?.last_run ? [state.status.last_run] : [];
    $('recent-runs').innerHTML = runs.length ? runs.slice(0,4).map(run => `<div class="run-item"><div class="run-item-icon ${escape(run.status)}" aria-hidden="true">${run.status === 'failed' ? '!' : run.status === 'partial' ? '·' : run.status === 'skipped' ? '−' : '✓'}</div><div class="run-item-copy"><a href="#logs" class="run-log-link" data-run-id="${escape(run.id)}"><strong>${escape(kinds[run.kind] || run.kind)}</strong> ${badge(run.status)}</a><p>${escape(run.message)}</p>${run.counts && Object.keys(run.counts).length ? `<p class="counts">${escape(Object.entries(run.counts).map(([key,value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value) : value}`).join(' · '))}</p>` : ''}</div><div class="run-item-time">${escape(date(run.finished_at || run.started_at))}<br>${run.source === 'manual' ? '手动触发' : '计划触发'}</div></div>`).join('') : '<div class="empty-state compact"><strong>还没有运行记录</strong><p>点击「立即完整运行」开始第一次任务。</p></div>';
    const selected = $('log-run').value;
    const logRuns = [...runs];
    if (state.status?.current_run && !logRuns.some(run => run.id === state.status.current_run.id)) logRuns.unshift(state.status.current_run);
    if (selected && !logRuns.some(run => run.id === selected)) logRuns.unshift({id:selected,kind:'当前筛选'});
    $('log-run').innerHTML = '<option value="">全部运行</option>'+logRuns.map(run => `<option value="${escape(run.id)}">${escape(kinds[run.kind] || run.kind)} · ${escape(date(run.started_at))} · ${escape(String(run.id).slice(0,8))}</option>`).join('');
    $('log-run').value = selected;
  }
  $('main').addEventListener('click',event => {
    const link = event.target.closest('.run-log-link');
    if (!link) return;
    const id = link.dataset.runId;
    if (![...$('log-run').options].some(option => option.value === id)) $('log-run').add(new Option('本次运行 · '+id.slice(0,8),id));
    $('log-run').value = id;
    resetLogs();
  });
  async function loadHistory() {
    try {
      const minutes = $('history-period').value;
      const result = await request('history?minutes='+minutes);
      if (!state.authenticated || $('history-period').value !== minutes) return;
      state.history = result.items || [];
      renderChart();
      showMessage('history-error','');
    } catch (error) { showMessage('history-error',error.message+' 现有曲线保留为历史数据。'); }
  }
  function renderChart() {
    const items = state.history.filter(item => Number.isFinite(Number(item.time)) && Number.isFinite(Number(item.upspeed)) && Number.isFinite(Number(item.dlspeed)));
    $('chart-sample-count').textContent = items.length ? items.length+' 个真实采样' : '';
    if (!items.length) { $('chart-container').innerHTML = '<div class="empty-state"><span class="empty-symbol">∿</span><strong>这个时段还没有真实采样</strong><p>连接下载器后运行任务，或等待统计采样。历史不足时不会补造曲线。</p></div>'; return; }
    const width = 660, height = 220, left = 46, right = 16, top = 12, bottom = 30;
    const plotWidth = width-left-right, plotHeight = height-top-bottom;
    const first = Number(items[0].time), last = Number(items.at(-1).time);
    const rawMax = Math.max(...items.map(item => Math.max(Number(item.upspeed),Number(item.dlspeed))/1048576));
    const max = rawMax > 0 ? Math.ceil(rawMax * 1.12 * 10) / 10 : 1;
    const x = item => items.length === 1 || last === first ? left+plotWidth/2 : left+(Number(item.time)-first)/(last-first)*plotWidth;
    const y = (item,key) => top+plotHeight-Math.max(0,Number(item[key]))/1048576/max*plotHeight;
    let grid = '';
    for (let i=0;i<=4;i++) { const py = top+plotHeight*i/4; grid += `<line class="chart-grid" x1="${left}" y1="${py}" x2="${width-right}" y2="${py}"/><text class="chart-label" x="${left-9}" y="${py+3}" text-anchor="end">${number(max*(4-i)/4,2)}</text>`; }
    let lines = '';
    for (const [key,cls,color] of [['upspeed','upload','var(--primary)'],['dlspeed','download','var(--teal)']]) {
      const points = items.map(item => `${x(item).toFixed(2)},${y(item,key).toFixed(2)}`).join(' ');
      if (items.length > 1) {
        if (key === 'upspeed') lines += `<defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--primary)" stop-opacity=".15"/><stop offset="100%" stop-color="var(--primary)" stop-opacity="0"/></linearGradient></defs><polygon points="${x(items[0])},${top+plotHeight} ${points} ${x(items.at(-1))},${top+plotHeight}" fill="url(#chart-fill)"/>`;
        lines += `<polyline class="chart-${cls}" points="${points}"/>`;
      }
      lines += items.length === 1 ? `<circle cx="${x(items[0])}" cy="${y(items[0],key)}" r="4" fill="${color}"><title>${escape(date(items[0].time,true))} · ${escape(sizeText(items[0][key],true))}</title></circle>` : '';
    }
    let labels = '';
    const ticks = items.length === 1 ? [first] : [first,first+(last-first)/2,last];
    ticks.forEach((value,index) => { const px = items.length === 1 ? left+plotWidth/2 : left+plotWidth*index/2; labels += `<text class="chart-label" x="${px}" y="${height-7}" text-anchor="${index === 0 && ticks.length > 1 ? 'start' : index === 2 ? 'end' : 'middle'}">${escape(timeOnly(value*1000))}</text>`; });
    const latest = items.at(-1);
    $('chart-container').innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escape(`${items.length} 个真实采样的全局传输趋势；最新上传 ${sizeText(latest.upspeed,true)}，下载 ${sizeText(latest.dlspeed,true)}`)}"><title>全局上传与下载速度趋势，${items.length} 个真实采样</title>${grid}${lines}${labels}</svg>`;
  }
  $('history-period').addEventListener('change',loadHistory);
  async function loadTorrents() {
    try {
      const result = await request('torrents');
      if (!state.authenticated) return;
      if (result.error) { showMessage('torrents-error',result.error+' 上次成功列表已保留，不代表下载器当前状态。'); return; }
      state.torrents = result.items || [];
      renderTorrents();
      showMessage('torrents-error','');
      $('torrents-updated').textContent = `最后更新 ${date(new Date().toISOString(),true)} · 托管任务速率不等于下载器全局速率。`;
    } catch (error) { showMessage('torrents-error',error.message+' 上次成功列表已保留。'); }
  }
  function freeText(value) {
    if (!value || !Number.isFinite(epoch(value))) return '<span class="muted">未知</span>';
    const remaining = (epoch(value)-now())/3600000;
    return `<span class="${remaining <= 0 ? 'danger-text' : remaining < (state.config?.strategy.min_free_hours || 3) ? 'warning-text' : ''}">${escape(date(value))}</span><div class="table-secondary">${remaining <= 0 ? '已到期' : '剩余 '+number(remaining,1)+' 小时'}</div>`;
  }
  const torrentState = value => ({downloading:'下载中',stalledDL:'等待下载',metaDL:'获取元数据',forcedDL:'强制下载',uploading:'做种中',stalledUP:'等待上传',forcedUP:'强制上传',pausedDL:'下载已暂停',stoppedDL:'下载已停止',pausedUP:'做种已暂停',stoppedUP:'做种已停止',queuedDL:'下载排队',queuedUP:'做种排队',checkingDL:'检查下载',checkingUP:'检查做种',checkingResumeData:'恢复检查',error:'错误',missingFiles:'文件缺失',moving:'移动文件',allocating:'分配空间'}[value] || value || '未知');
  function renderTorrents() {
    const query = $('torrent-search').value.trim().toLowerCase();
    const items = state.torrents.filter(item => `${item.name} ${item.id} ${item.hash}`.toLowerCase().includes(query));
    $('torrent-count').textContent = `${items.length} / ${state.torrents.length} 个托管任务`;
    $('torrent-rows').innerHTML = items.map(item => {
      const progress = Math.max(0,Math.min(1,Number(item.progress)||0));
      return `<tr><td><div class="torrent-name">${escape(item.name)}</div><div class="table-secondary">#${escape(item.id || '—')} · ${escape(sizeText(item.size))}</div></td><td><div class="progress-value"><span>${number(progress*100,1)}%</span></div><div class="progress-track" role="progressbar" aria-label="${escape(item.name)} 下载进度" aria-valuenow="${(progress*100).toFixed(1)}" aria-valuemin="0" aria-valuemax="100"><span style="width:${progress*100}%"></span></div></td><td><span class="badge ${['error','missingFiles'].includes(item.state) ? 'danger' : progress >= 1 ? '' : 'neutral'}">${escape(torrentState(item.state))}</span></td><td><div class="speed-up">↑ ${escape(sizeText(item.upspeed,true))}</div><div class="speed-down">↓ ${escape(sizeText(item.dlspeed,true))}</div></td><td>${escape(sizeText(item.uploaded))}<div class="table-secondary">分享率 ${number(item.ratio,2)} · 已下载 ${escape(sizeText(item.downloaded))}</div></td><td>${freeText(item.free_until)}</td></tr>`;
    }).join('') || `<tr><td colspan="6" class="empty-cell">${query ? '没有匹配的托管种子。试试其他关键词。' : '还没有已确认托管的种子。查询并运行刷流后，下载器确认的任务会显示在这里。'}</td></tr>`;
  }
  $('torrent-search').addEventListener('input',renderTorrents);
  $('refresh-torrents').addEventListener('click',async () => { $('refresh-torrents').disabled = true; try { await loadTorrents(); } finally { $('refresh-torrents').disabled = false; } });
  async function loadCandidates() {
    try {
      const result = await request('candidates');
      if (!state.authenticated) return;
      state.candidates = result.items || [];
      renderCandidates();
      showMessage('candidates-error','');
    } catch (error) { showMessage('candidates-error',error.message+' 上次成功读取的候选列表已保留。'); }
  }
  function renderCandidates() {
    const query = $('candidate-search').value.trim().toLowerCase();
    const selected = $('candidate-state').value;
    const items = state.candidates.filter(item => (!selected || item.state === selected) && `${item.name} ${item.id} ${item.reason}`.toLowerCase().includes(query));
    $('candidate-count').textContent = `${items.length} / ${state.candidates.length} 个候选`;
    $('candidate-rows').innerHTML = items.map(item => `<tr><td><div class="torrent-name">${escape(item.name)}</div><div class="table-secondary">#${escape(item.id)} · 更新 ${escape(date(item.updated_at))}</div></td><td>${escape(sizeText(item.size))}<div class="table-secondary">${escape({movie:'电影',tvshow:'剧集',normal:'综合',adult:'成人',rankings:'排行'}[item.mode] || item.mode)}</div></td><td>${number(item.seeders)} / ${number(item.leechers)}</td><td>${number(item.score,2)}</td><td>${freeText(item.free_until)}</td><td>${badge(item.state)}<div class="table-reason">${escape(item.reason || (item.state === 'pending' ? '等待下一次刷流评估' : item.state === 'submitted' ? '已核实下载器中的对应任务' : '无额外说明'))}</div></td></tr>`).join('') || `<tr><td colspan="6" class="empty-cell">${query || selected ? '没有匹配的候选。请调整搜索或状态筛选。' : '候选池还是空的。连接 M-Team 后点击「立即查询」，仅合格的 Free 候选会被记录。'}</td></tr>`;
  }
  $('candidate-search').addEventListener('input',renderCandidates);
  $('candidate-state').addEventListener('change',renderCandidates);
  function updateLogContext(loading=false) {
    const filters = [$('log-level').value,$('log-stage').value.trim(),$('log-search').value.trim()].filter(Boolean);
    $('log-filter-notice').hidden = !filters.length;
    $('log-filter-description').textContent = `仍按级别、阶段或搜索筛选：${filters.join(' · ')}。可能隐藏本次运行的其他记录。`;
    $('log-empty-title').textContent = state.logPaused ? '日志更新已暂停' : loading ? '正在读取日志…' : filters.length || $('log-run').value ? '暂无匹配日志' : '尚无运行日志';
    $('log-empty-description').textContent = state.logPaused ? '点击「继续」读取记录；当前运行与筛选条件会保留。' : loading ? '正在读取服务端真实记录，请稍候。' : filters.length ? '可以清除级别、阶段与搜索，保留当前运行筛选查看完整过程。' : '新记录每 2 秒自动读取；任务开始后可在这里查看过程。';
  }
  function resetLogs(fetchData=true) {
    state.cursor = 0;
    state.logGeneration++;
    state.logCount = 0;
    $('log-rows').replaceChildren();
    $('log-empty').hidden = false;
    $('log-count').textContent = '0 条记录';
    showMessage('log-reset-notice','');
    showMessage('logs-error','');
    updateLogContext(fetchData);
    if (fetchData && state.authenticated && !state.logPaused) loadLogs();
  }
  let logDebounce;
  for (const id of ['log-level','log-run']) $(id).addEventListener('change',() => resetLogs());
  for (const id of ['log-stage','log-search']) $(id).addEventListener('input',() => { clearTimeout(logDebounce); logDebounce = setTimeout(() => resetLogs(),350); });
  $('log-clear-filters').addEventListener('click',() => {
    clearTimeout(logDebounce);
    for (const id of ['log-level','log-stage','log-search']) $(id).value = '';
    resetLogs();
  });
  $('log-pause').addEventListener('click',() => {
    state.logPaused = !state.logPaused;
    $('log-pause').textContent = state.logPaused ? '继续' : '暂停';
    $('log-live').innerHTML = `<span class="status-dot"></span>${state.logPaused ? '更新已暂停' : '每 2 秒更新'}`;
    $('log-live').classList.toggle('paused',state.logPaused);
    updateLogContext();
    if (!state.logPaused) loadLogs();
  });
  async function loadLogs() {
    if (state.logLoading || state.logPaused) return;
    state.logLoading = true;
    updateLogContext(true);
    const generation = state.logGeneration;
    const params = new URLSearchParams({after:String(state.cursor),limit:'200',level:$('log-level').value,stage:$('log-stage').value.trim(),q:$('log-search').value.trim(),run_id:$('log-run').value});
    try {
      const result = await request('logs?'+params);
      if (generation !== state.logGeneration || !state.authenticated || state.logPaused) return;
      if (result.reset) {
        $('log-rows').replaceChildren();
        state.logCount = 0;
        showMessage('log-reset-notice',`较早日志已超出服务端保留范围。已从当前可用历史继续读取（最早 ID ${result.oldest_id ?? '—'}）；中间记录无法恢复。`);
      }
      const fragment = document.createDocumentFragment();
      for (const item of result.items || []) {
        if (item.stage && !knownStages.has(item.stage)) { knownStages.add(item.stage); $('log-stages').append(new Option(item.stage,item.stage)); }
        const row = document.createElement('article');
        row.className = 'log-row';
        const detail = item.details !== null && item.details !== undefined ? typeof item.details === 'string' ? item.details : JSON.stringify(item.details,null,2) : '';
        row.innerHTML = `<div class="log-line"><time class="log-time" datetime="${escape(item.time)}" title="${escape(item.time)}">${escape(date(item.time,true))}</time>${badge(item.level)}<span class="log-stage">${escape(item.stage)}</span>${item.run_id ? `<span class="log-run-id" title="${escape(item.run_id)}">run ${escape(String(item.run_id).slice(0,12))}</span>` : ''}</div><p class="log-message">${escape(item.message)}</p>${detail ? `<details><summary>查看详情 · #${escape(item.id)}</summary><pre>${escape(detail)}</pre></details>` : ''}`;
        fragment.append(row);
        state.logCount++;
      }
      $('log-rows').append(fragment);
      state.cursor = result.cursor ?? state.cursor;
      // ponytail: retain 2,000 visible rows; export provides the server's full retained history.
      while ($('log-rows').childElementCount > 2000) { $('log-rows').firstElementChild.remove(); showMessage('log-reset-notice','浏览器仅展示最近 2,000 条已读取记录，以保持界面流畅。完整保留历史可通过「导出日志」获取。'); }
      $('log-empty').hidden = $('log-rows').childElementCount > 0;
      updateLogContext();
      $('log-count').textContent = `${$('log-rows').childElementCount} 条显示 · 已读取 ${state.logCount} 条 · 游标 ${state.cursor}`;
      showMessage('logs-error','');
      if ($('log-autoscroll').checked && (result.items || []).length) $('log-view').scrollTop = $('log-view').scrollHeight;
    } catch (error) {
      if (generation === state.logGeneration) {
        showMessage('logs-error',error.message+' 保留现有记录，连接恢复后继续读取。');
        if (!$('log-rows').childElementCount) { $('log-empty-title').textContent = '日志读取失败'; $('log-empty-description').textContent = '连接恢复后将自动重试，当前筛选条件会保留。'; }
      }
    }
    finally { state.logLoading = false; }
  }
  $('export-logs').addEventListener('click',async () => {
    const button = $('export-logs');
    button.disabled = true;
    try {
      const response = await fetch(new URL('logs/export',apiBase),{credentials:'same-origin',cache:'no-store'});
      if (!response.ok) { let message = `日志导出失败（HTTP ${response.status}）`; try { message = (await response.json()).error || message; } catch { /* Display HTTP error when no JSON is available. */ } throw new Error(message); }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = `ptflow-logs-${new Date().toISOString().slice(0,10)}.txt`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url),1000);
      toast('已导出服务端全部保留日志（不受当前筛选限制）。');
    } catch (error) { toast(error.message || '无法连接服务，日志导出失败。',true); }
    finally { button.disabled = false; }
  });
  async function loadPageData() {
    if (state.page === 'overview') await Promise.all([loadHistory(),loadRuns()]);
    else if (state.page === 'torrents') { renderTorrents(); await loadTorrents(); }
    else if (state.page === 'candidates') { renderCandidates(); await loadCandidates(); }
    else if (state.page === 'logs') await Promise.all([loadLogs(),loadRuns()]);
    else if (state.page === 'settings' && !state.config) await loadConfig();
  }
  async function poll() {
    if (!state.authenticated || state.polling) return;
    state.polling = true;
    try {
      if (!document.hidden) {
        await loadStatus();
        if (Date.now()-state.lastData >= 12000 && state.authenticated) { state.lastData = Date.now(); await loadPageData(); }
      }
    } finally {
      state.polling = false;
      clearTimeout(state.timer);
      if (state.authenticated) state.timer = setTimeout(poll,2000);
    }
  }
  document.addEventListener('visibilitychange',() => { if (!document.hidden && state.authenticated) { clearTimeout(state.timer); poll(); } });
  initAuth();
})();
