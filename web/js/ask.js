/** 全局「问 AI」入口：任何页面都能呼出。
 *
 * 设计取舍：
 *   · 用浮在右下角的圆钮 + Ctrl+K，不占导航栏位置
 *   · 打开时自动带上"你在看哪一页"和"选中的文字"，用户不用解释上下文
 *   · 没配置端点时不假装能用，直接把用户送去设置页
 */
import { api } from './api.js';
import { h, mount, clear, toast, button } from './ui.js';
import { icon } from './icons.js';

let host = null;

function ensureHost() {
  if (host && document.body.contains(host)) return host;
  host = h('div', { class: 'ask-fab-host' });
  document.body.append(host);
  return host;
}

/** 右下角那颗圆钮 */
export function mountAskButton() {
  const box = ensureHost();
  const btn = h('button', {
    class: 'ask-fab', title: '问 AI（Ctrl+K）',
    onclick: () => openAsk(),
  }, h('span', { html: icon('auto_awesome', 'outlined', 22) }));
  mount(clear(box), btn);
}

/** 当前页面能给出的上下文：页面名 + 选中的文字 */
function gatherContext() {
  const route = (location.hash || '#projects').slice(1);
  const labels = {
    projects: '项目', doc: '文档', papers: '文献', tasks: '历史记录',
    settings: '设置', home: '解析', music: '音乐', player: '播放', clip: '剪辑', image: '图片',
  };
  const selected = String(window.getSelection ? window.getSelection().toString() : '').trim();
  return { route, label: labels[route] || route, selected };
}

export function openAsk(preset) {
  if (document.getElementById('askModal')) return;
  const ctx = gatherContext();
  const shell = h('div', { class: 'ask-modal', id: 'askModal' });
  document.body.append(shell);        // 忘了这句就直接白屏 —— 元素建好了但没挂上去
  const answer = h('div', { class: 'ask-answer' });
  const sources = h('div', { class: 'ask-sources' });
  const input = h('textarea', {
    class: 'input ask-input',
    placeholder: '问点什么…例如：这一段能怎么改写得更学术一点？',
  });
  if (preset) input.value = preset;

  const status = h('div', { class: 'ask-status' }, '正在检查 AI 配置…');
  const sendBtn = button('提问', { onClick: () => submit() });

  const close = () => shell.remove();
  const submit = async () => {
    const question = input.value.trim();
    if (!question) { input.focus(); return; }
    sendBtn.disabled = true;
    clear(answer).append(h('div', { class: 'ask-thinking' }, '正在本地检索并提问…'));
    clear(sources);
    try {
      const r = await api.aiAsk(question, true);
      clear(answer).append(h('div', { class: 'ask-text' }, r.answer || '(没有返回内容)'));
      if ((r.hits || []).length) {
        mount(sources, h('div', { class: 'ask-sources-title' }, '参考了 ' + r.hits.length + ' 处资料'),
          ...r.hits.map((x) => h('div', { class: 'ask-source' }, '· ' + x.project + ' / ' + x.node)));
      } else {
        mount(sources, h('div', { class: 'ask-source' }, '（没有检索到相关资料，回答只基于你的问题）'));
      }
    } catch (err) {
      clear(answer).append(h('div', { class: 'ask-error' }, '失败：' + err.message));
    }
    sendBtn.disabled = false;
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    if (e.key === 'Escape') close();
  });

  mount(shell,
    h('div', { class: 'ask-box' },
      h('div', { class: 'ask-head' },
        h('span', { html: icon('auto_awesome', 'outlined', 20) }),
        h('b', {}, '问 AI'),
        h('span', { class: 'ask-hint' },
          '检索在本地做，只有命中的片段会发到你配置的端点'),
        h('div', { class: 'spacer' }),
        h('button', { class: 'ask-close', title: '关闭', onclick: close }, '×')),
      ctx.selected
        ? h('div', { class: 'ask-context' }, '会带上你选中的 ' + ctx.selected.length + ' 个字作为上下文')
        : h('div', { class: 'ask-context' }, '当前在「' + ctx.label + '」页，会自动带上相关资料'),
      input,
      h('div', { class: 'ask-row' }, status, h('div', { class: 'spacer' }), sendBtn),
      answer, sources));

  shell.addEventListener('click', (e) => { if (e.target === shell) close(); });
  setTimeout(() => input.focus(), 30);

  // 状态：没配端点就说清楚，并给一个"去设置"的按钮
  api.aiStatus().then((st) => {
    clear(status);
    if (st.ready) {
      mount(status, h('span', { class: 'ask-ok' }, '已连接 ' + (st.model || '') + ' · 本地索引 ' + (st.chunks || 0) + ' 段'));
    } else {
      mount(status,
        h('span', { class: 'ask-off' }, '还没配置 AI 端点　'),
        button('去设置', { variant: 'text', onClick: () => { close(); location.hash = '#settings'; } }));
    }
  }).catch(() => { clear(status); mount(status, h('span', { class: 'ask-off' }, '无法读取 AI 状态')); });
}

/** 全局快捷键 */
export function bindAskShortcut() {
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      openAsk();
    }
  });
}
