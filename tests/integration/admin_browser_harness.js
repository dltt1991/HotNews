// Development-only DOM/HTTP boundary: runs the shipped script, never duplicates its logic.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let focused = null;
class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = [];
    this.listeners = {}; this.value = attrs.value || ''; this._text = '';
    this.hidden = Object.hasOwn(attrs, 'hidden'); this.disabled = Object.hasOwn(attrs, 'disabled');
    this.checked = Object.hasOwn(attrs, 'checked');
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Unsafe innerHTML write: ' + value); }
  insertAdjacentHTML() { throw new Error('Unsafe HTML insertion'); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  removeAttribute(key) { delete this.attrs[key]; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  focus() { if (!this.disabled) focused = this; }
}
const nodes = input.nodes.map(node => new Element(node.tag, node.attrs));
const byId = Object.fromEntries(nodes.filter(node => node.attrs.id).map(node => [node.attrs.id, node]));
const document = {
  getElementById(id) { assert.ok(byId[id], 'missing HTML element ' + id); return byId[id]; },
  createElement(tag) { return new Element(tag); }
};
let current = {...input.subscription};
let rows = [current];
const requests = [], confirmations = [];
let answer = true, failure = null;
const context = vm.createContext({document, URLSearchParams, Intl, Date, console,
  window: {confirm(message) { confirmations.push(message); return answer; }},
  fetch: async (url, options = {}) => {
    const request = {url, ...options};
    requests.push(request);
    assert.ok(url.startsWith('/api/'), 'requests must stay same-origin');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.mode, 'same-origin');
    assert.ok(!Object.keys(options.headers || {}).some(key => key.toLowerCase() === 'origin'),
              'Origin is set by the browser, never forged');
    if (failure) {
      const once = failure; failure = null;
      if (once === 'network') throw new Error('secret-network-detail');
      return {ok: false, status: once, json: async () => ({error: 'version_conflict'})};
    }
    const method = options.method || 'GET';
    if (method !== 'GET') {
      assert.equal(options.headers['X-Hotnews-CSRF'], 'browser-token');
      assert.equal(options.headers['Content-Type'], 'application/json');
      const value = JSON.parse(options.body);
      assert.equal(value.version, current.version, 'mutation must use displayed version');
      if (method === 'PATCH') current = {...current, ...value};
      if (url.endsWith('/pause')) current.state = 'paused';
      if (url.endsWith('/resume')) current.state = 'ready';
      if (method === 'DELETE') { current.state = 'cancelled'; rows = []; }
      current = {...current, version: current.version + 1};
      rows = rows.map(row => row.id === current.id ? current : row);
    }
    const value = url === '/api/session' ? {csrf_token: 'browser-token'} :
      url === '/api/connection' ? {connection: {state: 'reconnecting', connected_at: '2026-10-02T01:00:00Z',
        last_event_at: null, reconnect_attempts: 2, last_error: '<img src=x onerror=alert(1)>'}} :
      url.startsWith('/api/subscriptions?') || url === '/api/subscriptions' ?
        {subscriptions: rows.map(row => ({...row}))} : {subscription: {...current}, run_id: 'run-1'};
    return {ok: true, status: 200, json: async () => value};
  }
});
function walk(element) { return [element, ...element.children.flatMap(walk)]; }
function button(label) {
  const found = walk(byId.subscriptions).find(element => element.tagName === 'BUTTON' && element.textContent === label);
  assert.ok(found, 'missing row action ' + label); return found;
}
async function settle() { for (let count = 0; count < 4; count++) await new Promise(resolve => setImmediate(resolve)); }
async function fire(element, event = 'click') {
  assert.ok(!element.disabled, 'control should be enabled');
  const callbacks = element.listeners[event] || [];
  assert.ok(callbacks.length, 'missing event handler ' + event);
  for (const callback of callbacks) await callback({preventDefault() {}, target: element});
  await settle();
}
async function edit() { await fire(button('编辑')); assert.equal(byId.editor.hidden, false); }
function mutations() { return requests.filter(request => request.method && request.method !== 'GET'); }

(async () => {
  vm.runInContext(input.javascript, context); await settle();
  switch (input.case) {
    case 'render': {
      const rendered = byId.subscriptions.textContent;
      assert.ok(rendered.includes('<img src=x onerror=alert(1)>'));
      assert.ok(rendered.includes('<script>bad()</script>'));
      assert.ok(rendered.includes('2026/10/02 09:00'));
      assert.ok(rendered.includes('从未成功'));
      assert.ok(!walk(byId.subscriptions).some(element => ['SCRIPT', 'IMG', 'A'].includes(element.tagName)));
      break;
    }
    case 'filters': {
      byId['filter-chat'].value = 'chat-a'; byId['filter-state'].value = 'paused';
      byId['filter-keyword'].value = ' AI & 国内 '; byId['include-history'].checked = true;
      await fire(byId.filters, 'submit');
      const query = new URL(requests.at(-1).url, 'http://localhost').searchParams;
      assert.equal(query.get('chat_id'), 'chat-a'); assert.equal(query.get('status'), 'paused');
      assert.equal(query.get('keyword'), 'AI & 国内'); assert.equal(query.get('include_cancelled'), 'true');
      byId['include-history'].checked = false; await fire(byId['include-history'], 'change');
      assert.equal(new URL(requests.at(-1).url, 'http://localhost').searchParams.has('include_cancelled'), false);
      break;
    }
    case 'casefold': {
      // The real API casefolds STRASSE and straße to the same value.
      rows = [{...current, topic: 'STRASSE', keywords: ['German']}];
      byId['filter-keyword'].value = 'straße'; await fire(byId.filters, 'submit');
      assert.ok(byId.subscriptions.textContent.includes('STRASSE'), 'server-approved Unicode match must remain visible');
      break;
    }
    case 'edit': {
      await edit();
      byId['edit-topic'].value = ''; await fire(byId['edit-form'], 'submit');
      assert.equal(mutations().length, 0); assert.ok(byId['topic-error'].textContent);
      assert.equal(focused, byId['edit-topic']);
      byId['edit-topic'].value = '新主题'; byId['edit-keywords'].value = 'a\n'.repeat(21);
      await fire(byId['edit-form'], 'submit'); assert.equal(mutations().length, 0);
      assert.ok(byId['keywords-error'].textContent);
      byId['edit-keywords'].value = '国内\n海外'; byId['edit-kind'].value = 'interval';
      await fire(byId['edit-kind'], 'change');
      byId['edit-interval'].value = '4'; byId['edit-unit'].value = 'minutes';
      await fire(byId['edit-form'], 'submit'); assert.equal(mutations().length, 0);
      assert.ok(byId['schedule-error'].textContent);
      byId['edit-interval'].value = '2'; byId['edit-unit'].value = 'hours';
      await fire(byId['edit-form'], 'submit');
      const request = mutations()[0]; assert.equal(request.method, 'PATCH');
      const body = JSON.parse(request.body); assert.equal(body.topic, '新主题');
      assert.deepEqual(body.keywords, ['国内', '海外']);
      assert.deepEqual(body.schedule, {kind: 'interval', interval_minutes: 120});
      assert.equal(byId.editor.hidden, true); assert.ok(byId.status.textContent.includes('不会立即推送'));
      break;
    }
    case 'actions': {
      await fire(button('暂停')); assert.equal(mutations()[0].url.split('/').at(-1), 'pause');
      assert.equal(focused, byId.refresh, 'completed action must restore focus to a live control');
      await fire(button('恢复')); assert.equal(mutations()[1].url.split('/').at(-1), 'resume');
      await fire(button('立即推送')); assert.equal(mutations()[2].url.split('/').at(-1), 'run-now');
      assert.equal(current.state, 'ready'); assert.ok(byId.status.textContent.includes('已排队'));
      assert.deepEqual(mutations().map(request => JSON.parse(request.body).version), [1, 2, 3]);
      break;
    }
    case 'cancel': {
      answer = false; await fire(button('取消订阅')); assert.equal(mutations().length, 0);
      assert.ok(confirmations[0].includes(current.topic));
      answer = true; await fire(button('取消订阅')); assert.equal(mutations()[0].method, 'DELETE');
      assert.ok(byId.subscriptions.textContent.includes('没有匹配的订阅'));
      break;
    }
    case 'conflict': {
      await edit(); failure = 409;
      byId['edit-topic'].value = 'stale'; await fire(byId['edit-form'], 'submit');
      assert.equal(byId['conflict-refresh'].hidden, false); assert.ok(byId.status.textContent.includes('刷新'));
      assert.equal(byId['save-edit'].disabled, true);
      assert.equal(focused, byId['conflict-refresh'], 'conflict refresh must be keyboard discoverable');
      current = {...current, topic: '新的远程修改', version: 9}; rows = [current];
      await fire(byId['conflict-refresh']);
      assert.equal(byId.editor.hidden, true); assert.equal(byId['conflict-refresh'].hidden, true);
      assert.ok(byId.subscriptions.textContent.includes('新的远程修改'));
      await fire(button('暂停')); assert.equal(JSON.parse(mutations().at(-1).body).version, 9);
      break;
    }
    case 'errors': {
      failure = 'network'; await fire(button('暂停'));
      assert.ok(byId.status.textContent.includes('连接')); assert.ok(!byId.status.textContent.includes('secret'));
      failure = 403; await fire(button('暂停')); assert.ok(byId.status.textContent.includes('刷新'));
      failure = 500; await fire(button('暂停')); assert.ok(byId.status.textContent.includes('服务'));
      await fire(button('暂停')); assert.equal(current.state, 'paused');
      break;
    }
    case 'connection': {
      assert.ok(byId['connection-status'].textContent.includes('正在重连'));
      assert.ok(byId['connection-status'].textContent.includes('2'));
      assert.ok(byId['connection-error'].textContent.includes('<img src=x onerror=alert(1)>'));
      assert.ok(!walk(byId['connection-error']).some(element => element.tagName === 'IMG'));
      assert.ok(requests.some(request => request.url === '/api/connection'));
      break;
    }
    case 'states': {
      rows = [{...current, id: 'pending', state: 'paused', search_terms: [], search_terms_status: 'pending'},
              {...current, id: 'history', state: 'cancelled'}];
      byId['include-history'].checked = true;
      await fire(byId.refresh);
      const tableRows = byId.subscriptions.children;
      assert.ok(tableRows[0].textContent.includes('等待 Codex 更新搜索词'));
      const run = walk(tableRows[0]).find(element => element.textContent === '立即推送');
      assert.ok(run.disabled);
      assert.ok(!walk(tableRows[1]).some(element => element.tagName === 'BUTTON'));
      break;
    }
    default: throw new Error('unknown scenario');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
