/* Local subscription manager. Untrusted values only enter text or form values. */
(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const controls = ['filter-chat', 'filter-state', 'filter-keyword', 'include-history',
    'refresh', 'conflict-refresh', 'edit-topic', 'edit-keywords', 'edit-kind',
    'edit-daily', 'edit-interval', 'edit-unit', 'save-edit', 'close-editor'];
  const stateNames = {ready: '已启用', search_terms_pending: '等待搜索词', paused: '已暂停', cancelled: '已取消'};
  const chats = new Map();
  let items = [], csrf = '', editing = null, returnFocus = null;
  let busy = false, stale = false, actionButtons = [];

  function message(text) { get('status').textContent = text; }
  function renderConnection(connection) {
    const names = {starting: '正在连接', connected: '已连接', reconnecting: '正在重连',
      stopped: '已停止', fatal: '连接失败'};
    let text = `飞书长连接：${names[connection.state] || connection.state}`;
    if (connection.state === 'reconnecting') text += `（第 ${connection.reconnect_attempts} 次）`;
    get('connection-status').textContent = text;
    get('connection-error').textContent = connection.last_error || '';
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.setAttribute('class', className);
    return node;
  }
  function setBusy(value) {
    busy = value;
    controls.forEach(id => { get(id).disabled = busy || (stale && id === 'save-edit'); });
    actionButtons.forEach(({node, blocked}) => { node.disabled = busy || stale || blocked; });
    get('subscriptions').setAttribute('aria-busy', String(busy));
  }
  function showError(error) {
    const messages = {
      400: '输入未被接受，请检查主题、关键词和时间设置。',
      403: '管理会话已失效，请刷新页面后重试。',
      404: '订阅已不存在，请刷新列表。',
      409: '订阅已被其他操作修改。请刷新最新数据后重新编辑，当前修改未保存。',
      500: '管理服务暂时不可用，请稍后重试。'
    };
    const text = messages[error.status] || '无法连接本机管理服务，请检查服务是否运行后重试。';
    message(text);
    if (editing) get('edit-error').textContent = text;
    if (error.status === 409) {
      stale = true;
      get('conflict-refresh').hidden = false;
    }
  }
  async function api(path, method = 'GET', value) {
    const options = {method, mode: 'same-origin', credentials: 'same-origin', cache: 'no-store'};
    if (method !== 'GET') {
      options.headers = {'Content-Type': 'application/json', 'X-Hotnews-CSRF': csrf};
      options.body = JSON.stringify(value);
    }
    // Fetch supplies the browser Origin on mutations. No cookies or forged Origin headers.
    const response = await fetch(path, options);
    if (!response.ok) throw {status: response.status};
    return response.json();
  }
  function filters() {
    const params = new URLSearchParams();
    if (get('filter-chat').value) params.set('chat_id', get('filter-chat').value);
    if (get('filter-state').value) params.set('status', get('filter-state').value);
    const keyword = get('filter-keyword').value.trim();
    if (keyword) params.set('keyword', keyword);
    if (get('include-history').checked) params.set('include_cancelled', 'true');
    return params;
  }
  function date(value) {
    if (!value) return '—';
    const instant = new Date(value);
    if (Number.isNaN(instant.getTime())) return '—';
    const parts = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric',
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
      .formatToParts(instant);
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return `${values.year}/${values.month}/${values.day} ${values.hour}:${values.minute}`;
  }
  function scheduleText(schedule) {
    if (schedule.kind === 'daily') return `每天 ${schedule.daily_at}`;
    if (schedule.kind === 'interval') return `每 ${schedule.interval_minutes} 分钟`;
    return '手动执行';
  }
  function cell(row, label, children) {
    const td = element('td'); td.setAttribute('data-label', label);
    const content = element('div');
    children.forEach(child => content.appendChild(child));
    td.appendChild(content); row.appendChild(td);
  }
  function action(label, handler, blocked = false, danger = false) {
    const node = element('button', label, danger ? 'danger' : 'secondary');
    node.setAttribute('type', 'button');
    node.addEventListener('click', handler);
    actionButtons.push({node, blocked});
    return node;
  }
  function matches(sub) {
    if (sub.state === 'cancelled' && !get('include-history').checked) return false;
    if (get('filter-state').value && get('filter-state').value !== sub.state) return false;
    // Keyword matching belongs to the API's Unicode casefold implementation.
    return true;
  }
  function render() {
    get('subscriptions').replaceChildren(); actionButtons = [];
    const visible = items.filter(matches);
    get('count').textContent = `· ${visible.length} 条`;
    visible.forEach(sub => {
      const row = element('tr');
      cell(row, '群 / 编号', [element('div', sub.chat_name), element('p', `#${sub.display_number}`, 'muted')]);
      const details = [element('div', sub.topic, 'topic'), element('p', sub.keywords.join(' / '), 'muted')];
      if (sub.search_terms_status === 'pending') details.push(element('p', '等待 Codex 更新搜索词', 'badge pending'));
      cell(row, '主题与关键词', details);
      cell(row, '调度', [element('div', scheduleText(sub.schedule)), element('p', '北京时间', 'muted')]);
      cell(row, '下一次执行', [element('div', date(sub.next_run_at))]);
      cell(row, '状态', [element('span', stateNames[sub.state] || sub.state, 'badge')]);
      cell(row, '最近运行', [element('div', sub.last_success_at ? date(sub.last_success_at) : '从未成功'),
        element('p', `连续失败 ${sub.consecutive_failures} 次`, 'muted')]);
      const actions = element('div', undefined, 'actions');
      if (sub.state !== 'cancelled') {
        const editButton = action('编辑', () => openEditor(sub, editButton));
        actions.appendChild(editButton);
        const paused = sub.state === 'paused';
        actions.appendChild(action(paused ? '恢复' : '暂停', () => mutate(sub, 'POST',
          paused ? 'resume' : 'pause', {}, paused ? '订阅已恢复。' : '订阅已暂停。')));
        actions.appendChild(action('立即推送', () => mutate(sub, 'POST', 'run-now', {},
          '立即推送已排队，等待下一轮 Codex 处理。'), sub.search_terms_status === 'pending'));
        actions.appendChild(action('取消订阅', () => {
          if (window.confirm(`确认取消 ${sub.chat_name} 的 #${sub.display_number}「${sub.topic}」订阅？历史推送记录会保留。`)) {
            return mutate(sub, 'DELETE', '', {}, '订阅已取消，历史记录已保留。');
          }
        }, false, true));
      } else {
        actions.appendChild(element('span', '已取消，保留历史', 'muted'));
      }
      cell(row, '操作', [actions]); get('subscriptions').appendChild(row);
    });
    if (!visible.length) {
      const row = element('tr'), td = element('td', '没有匹配的订阅。新增订阅请在飞书群内 @机器人。');
      td.setAttribute('colspan', '7'); row.appendChild(td); get('subscriptions').appendChild(row);
    }
    setBusy(busy);
  }
  function updateChats() {
    items.forEach(sub => chats.set(sub.chat_id, sub.chat_name));
    const selected = get('filter-chat').value;
    get('filter-chat').replaceChildren();
    const all = element('option', '全部群'); all.value = ''; get('filter-chat').appendChild(all);
    chats.forEach((name, id) => {
      const option = element('option', name); option.value = id; get('filter-chat').appendChild(option);
    });
    get('filter-chat').value = selected;
  }
  function clearErrors() {
    ['topic-error', 'keywords-error', 'schedule-error', 'edit-error'].forEach(id => { get(id).textContent = ''; });
    ['edit-topic', 'edit-keywords', 'edit-daily', 'edit-interval'].forEach(id => get(id).removeAttribute('aria-invalid'));
  }
  function closeEditor(focus = true) {
    get('editor').hidden = true; editing = null; clearErrors();
    if (focus && returnFocus) returnFocus.focus();
    returnFocus = null;
  }
  function showSchedule() {
    const daily = get('edit-kind').value === 'daily';
    get('daily-fields').hidden = !daily; get('interval-fields').hidden = daily;
    get('edit-interval').setAttribute('min', get('edit-unit').value === 'hours' ? '1' : '5');
  }
  function openEditor(sub, button) {
    if (busy || stale) return;
    editing = sub; returnFocus = button; clearErrors();
    get('editor-title').textContent = `编辑 ${sub.chat_name} · #${sub.display_number}`;
    get('edit-topic').value = sub.topic; get('edit-keywords').value = sub.keywords.join('\n');
    get('edit-kind').value = sub.schedule.kind;
    get('edit-daily').value = sub.schedule.daily_at || '09:00';
    get('edit-interval').value = sub.schedule.interval_minutes || 60;
    get('edit-unit').value = 'minutes'; showSchedule();
    get('editor').hidden = false; get('editor-title').focus();
  }
  async function load(bootstrap = false) {
    if (busy) return;
    closeEditor(false); setBusy(true); message('正在加载订阅…');
    try {
      if (bootstrap) csrf = (await api('/api/session')).csrf_token;
      renderConnection((await api('/api/connection')).connection);
      await readList();
      stale = false; get('conflict-refresh').hidden = true;
      updateChats(); render(); message(`已加载 ${items.length} 条订阅。`);
    } catch (error) { showError(error); }
    finally {
      setBusy(false);
      if (stale) get('conflict-refresh').focus();
    }
  }
  async function readList() {
    const params = filters().toString();
    items = (await api('/api/subscriptions' + (params ? '?' + params : ''))).subscriptions;
  }
  async function mutate(sub, method, actionName, changes, success) {
    if (busy || stale) return;
    let completed = false;
    setBusy(true); message('正在保存操作…');
    try {
      const path = '/api/subscriptions/' + encodeURIComponent(sub.id) + (actionName ? '/' + actionName : '');
      const result = await api(path, method, {...changes, version: sub.version});
      items = items.map(item => item.id === sub.id ? result.subscription : item);
      if (editing && editing.id === sub.id) closeEditor(false);
      render(); message(success);
      completed = true;
      try {
        await readList(); updateChats(); render();
      } catch (error) {
        message(success + ' 列表未能刷新，请点击刷新查看最新数据。');
      }
    } catch (error) { showError(error); }
    finally {
      setBusy(false);
      if (stale) get('conflict-refresh').focus();
      else if (completed) get('refresh').focus();
    }
  }
  function validate() {
    clearErrors();
    const topic = get('edit-topic').value.trim();
    const keywords = get('edit-keywords').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    let first = null;
    const invalid = (id, errorId, text) => {
      get(id).setAttribute('aria-invalid', 'true'); get(errorId).textContent = text;
      if (!first) first = get(id);
    };
    if (!topic || [...topic].length > 200) invalid('edit-topic', 'topic-error', '请输入 1–200 个字符的主题。');
    if (!keywords.length || keywords.length > 20 || keywords.some(word => [...word].length > 80)) {
      invalid('edit-keywords', 'keywords-error', '请输入 1–20 个关键词，每个最多 80 个字符，每行一个。');
    }
    let schedule;
    if (get('edit-kind').value === 'daily') {
      const daily = get('edit-daily').value;
      if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(daily)) invalid('edit-daily', 'schedule-error', '请输入有效的 HH:MM 时间。');
      schedule = {kind: 'daily', daily_at: daily};
    } else {
      const value = get('edit-interval').value;
      const minutes = Number(value) * (get('edit-unit').value === 'hours' ? 60 : 1);
      if (!/^\d+$/.test(value) || !Number.isSafeInteger(minutes) || minutes < 5) {
        invalid('edit-interval', 'schedule-error', '请输入整数间隔，至少 5 分钟。');
      }
      schedule = {kind: 'interval', interval_minutes: minutes};
    }
    if (first) { first.focus(); return null; }
    return {topic, keywords, schedule};
  }

  get('filters').addEventListener('submit', event => {
    event.preventDefault();
    if (get('filter-state').value === 'cancelled') get('include-history').checked = true;
    return load();
  });
  get('include-history').addEventListener('change', () => {
    if (!get('include-history').checked && get('filter-state').value === 'cancelled') get('filter-state').value = '';
    return load();
  });
  get('refresh').addEventListener('click', () => load(true));
  get('conflict-refresh').addEventListener('click', () => load(true));
  get('edit-kind').addEventListener('change', showSchedule);
  get('edit-unit').addEventListener('change', showSchedule);
  get('close-editor').addEventListener('click', () => closeEditor());
  get('edit-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!editing || busy || stale) return;
    const changes = validate();
    if (changes) return mutate(editing, 'PATCH', '', changes, '修改已保存，正常调度已更新，不会立即推送。');
  });
  load(true);
})();
