/** 右侧「AI 助手」抽屉：和节点编辑栏一样贴着右边，带聊天记录。
 *
 * 设计：
 *   · 右下角圆钮 / Ctrl+K 打开，再点一次收起
 *   · 顶部可以切换用哪个助手（设置里配了多个）
 *   · 每条消息都能单独删，也能一次清空
 *   · 检索在本地做，只有命中的片段会发到你配置的端点
 */
import { api } from './api.js';
import { h, mount, clear, button } from './ui.js';
import { icon } from './icons.js';

let open = false;
let shell = null;
let listEl = null;
let state = { messages: [], assistants: [], active: '', ready: false };

function el(tag, props, ...kids) { return h(tag, props || {}, ...kids); }

function currentAssistant() {
  return state.assistants.find((a) => a.id === state.active) || state.assistants[0] || null;
}

function renderMessages() {
  if (!listEl) return;
  clear(listEl);
  if (!state.messages.length) {
    mount(listEl, el('div', { class: 'chat-empty' },
      '还没有对话。试试问：「我关于时钟门控的部分写了什么？」'));
    return;
  }
  for (const msg of state.messages) {
    const mine = msg.role === 'user';
    const bubble = el('div', { class: 'chat-row' + (mine ? ' mine' : '') },
      el('div', { class: 'chat-bubble' + (mine ? ' mine' : '') },
        el('div', { class: 'chat-text' }, msg.text || ''),
        msg.hits ? el('div', { class: 'chat-meta' }, '参考 ' + msg.hits + ' 处资料') : null),
      el('button', {
        class: 'chat-del', title: '删除这条',
        onclick: async () => {
          try { await api.aiChatDelete(msg.id); } catch (e) { /* 删不掉就留着 */ }
          state.messages = state.messages.filter((x) => x.id !== msg.id);
          renderMessages();
        },
      }, '×'));
    mount(listEl, bubble);
  }
  listEl.scrollTop = listEl.scrollHeight;
}

function renderAssistants() {
  const sel = shell.querySelector('.chat-model');
  if (!sel) return;
  sel.textContent = '';
  for (const a of state.assistants) {
    sel.append(el('option', { value: a.id }, a.name + (a.model ? '（' + a.model + '）' : '')));
  }
  if (state.active) sel.value = state.active;
}

async function refreshStatus() {
  try {
    const st = await api.aiStatus();
    state.assistants = st.assistants || [];
    state.active = st.active || (state.assistants[0] || {}).id || '';
    state.ready = !!st.ready;
  } catch (e) { state.assistants = []; state.ready = false; }
  renderAssistants();
  const hint = shell.querySelector('.chat-hint');
  if (hint) {
    hint.textContent = state.ready
      ? '已连接 · 本地索引 ' + (state.stats?.chunks || 0) + ' 段'
      : '还没配置 AI 助手，去设置里加一个';
  }
}

export function buildChatPanel() {
  if (shell) return shell;
  listEl = el('div', { class: 'chat-list' });
  const input = el('textarea', { class: 'chat-input', placeholder: '问点什么…（Ctrl+Enter 发送）' });
  const send = el('button', { class: 'btn', onclick: () => submit() }, '发送');

  const submit = async () => {
    const question = input.value.trim();
    if (!question) { input.focus(); return; }
    if (!state.ready) { shell.querySelector('.chat-hint').textContent = '还没配置 AI 助手，去设置里加一个'; return; }
    input.value = '';
    const assistant = currentAssistant();
    state.messages.push({ id: 'tmp' + Date.now(), role: 'user', text: question });
    renderMessages();
    const thinking = el('div', { class: 'chat-row' }, el('div', { class: 'chat-bubble thinking' }, '正在检索并提问…'));
    mount(listEl, thinking);
    listEl.scrollTop = listEl.scrollHeight;
    send.disabled = true;
    try {
      const r = await api.aiAsk(question, true, '', state.active);
      thinking.remove();
      await loadHistory();
    } catch (err) {
      thinking.remove();
      mount(listEl, el('div', { class: 'chat-row' }, el('div', { class: 'chat-bubble error' }, '失败：' + err.message)));
      listEl.scrollTop = listEl.scrollHeight;
    }
    send.disabled = false;
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
  });

  const sel = el('select', { class: 'chat-model', onchange: (e) => { state.active = e.target.value; } });

  shell = el('div', { class: 'chat-panel' },
    el('div', { class: 'chat-head' },
      el('span', { class: 'chat-icon', html: icon('auto_awesome', 'outlined', 20) }),
      el('b', {}, 'AI 助手'),
      el('div', { class: 'spacer' }),
      el('button', { class: 'chat-x', title: '收起', onclick: () => toggle(false) }, '×')),
    el('div', { class: 'chat-bar' },
      sel,
      el('button', {
        class: 'chat-clear', title: '清空对话',
        onclick: async () => {
          const yes = confirm('清空所有聊天记录？');
          if (!yes) return;
          try { await api.aiChatClear(); } catch (e) { /* 忽略 */ }
          state.messages = [];
          renderMessages();
        },
      }, '清空')),
    el('div', { class: 'chat-hint' }, '正在读取…'),
    listEl,
    el('div', { class: 'chat-foot' }, input, send));
  document.body.append(shell);
  return shell;
}

export async function loadHistory() {
  try {
    const r = await api.aiChatList();
    state.messages = r.messages || [];
  } catch (e) { state.messages = []; }
  renderMessages();
}

export async function toggle(force) {
  const panel = buildChatPanel();
  const next = force === undefined ? !open : !!force;
  open = next;
  panel.classList.toggle('open', open);
  document.body.classList.toggle('chat-open', open);
  if (open) {
    await refreshStatus();
    await loadHistory();
  }
}

export function mountAskButton() {
  const host = el('div', { class: 'ask-fab-host' });
  document.body.append(host);
  mount(host, el('button', {
    class: 'ask-fab', title: 'AI 助手（Ctrl+K）', onclick: () => toggle(),
  }, el('span', { html: icon('auto_awesome', 'outlined', 22) })));
}

export function bindAskShortcut() {
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      toggle();
    }
  });
}
