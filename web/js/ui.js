/** 极简 hyperscript + 通用组件，避免引入任何前端框架。 */
import { icon } from './icons.js';

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'html') el.innerHTML = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === 'value') el.value = value;
      else if (value === true) el.setAttribute(key, '');
      else el.setAttribute(key, value);
    }
  }
  append(el, children);
  return el;
}

export function append(parent, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** 往元素里塞子节点，**过滤 null/undefined/false**。
 *
 *  原生 el.append(null) 会插入文本 "null" —— 界面上真的会多出一个 null 字样。
 *  所以凡是用 ternaries 拼子节点的地方都必须走这里，不能直接用 .append()。
 */
export function mount(parent, ...kids) {
  return append(parent, kids);
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 带图标的按钮。
 *
 *  class 里的额外类会被合并进来 —— 播放条要用 'active' 高亮乱序/循环状态，
 *  以前这个字段被无声丢掉，导致按钮永远是默认色。
 */
export function button(label, { variant = '', iconName = '', iconRotate = 0, onClick,
                          title, disabled, class: extra = '' } = {}) {
  const classes = [...new Set(('btn ' + variant + ' ' + extra).split(/\s+/).filter(Boolean))].join(' ');
  // 图标旋转：展开/收起用同一个 expand_more，靠旋转 180° 区分。
  // 之前换图标（expand_less / expand_more）在实践中容易看错方向，
  // 旋转的视觉信号明确得多。
  const ico = iconName
    ? icon(iconName, variant === 'text' ? 'outlined' : 'filled', 18)
    : '';
  const wrapped = ico && iconRotate
    ? '<span style="display:inline-flex;transform:rotate(' + iconRotate + 'deg);transition:transform .18s ease">'
      + ico + '</span>'
    : ico;
  return h('button', {
    class: classes,
    title,
    disabled: disabled || false,
    onclick: onClick,
    html: wrapped + (label ? '<span>' + escapeHtml(label) + '</span>' : ''),
  });
}

export function chip(label, { active, locked, phantom, tag, onClick, title } = {}) {
  return h('button', {
    class: ['chip', active && 'active', locked && 'locked', phantom && 'phantom']
      .filter(Boolean).join(' '),
    title,
    onclick: onClick,
    html: escapeHtml(label) + (tag ? '<span class="tag">' + escapeHtml(tag) + '</span>' : ''),
  });
}

export function switchRow(name, hint, on, onChange) {
  const control = h('button', {
    class: 'switch' + (on ? ' on' : ''),
    role: 'switch',
    'aria-checked': String(!!on),
    'aria-label': name,
    onclick: () => {
      const next = !control.classList.contains('on');
      control.classList.toggle('on', next);
      control.setAttribute('aria-checked', String(next));
      onChange(next);
    },
  });
  return h('div', { class: 'switch-row' },
    h('div', { class: 'grow' },
      h('div', { class: 'name' }, name),
      hint ? h('div', { class: 'hint' }, hint) : null),
    control);
}

export function field(label, control) {
  // 点标签要能聚焦到控件：给控件补一个 id，label 用 for 指过去
  const id = 'f' + Math.random().toString(36).slice(2, 9);
  if (control && control.tagName && !control.id) control.id = id;
  return h('div', { class: 'field' },
    h('label', { for: (control && control.id) || id }, label),
    control);
}

export function card(...children) {
  return h('div', { class: 'card' }, ...children);
}

export function pill(text, variant = '') {
  return h('span', { class: 'pill ' + variant }, text);
}

export function iconSpan(name, variant = 'filled', size = 20) {
  return h('span', { class: 'md-icon-wrap', html: icon(name, variant, size) });
}

/** 可感知的轻提示，替代 Flutter 的 SnackBar。 */
export function toast(message, ms) {
  // 长错误（转换失败、接口报错）默认只停 2.6 秒根本看不完。
  // 按文字长度自动延长时间，上限 12 秒，别让提示跑得比眼睛快。
  const text = String(message == null ? '' : message);
  if (ms == null) {
    ms = Math.min(12000, 2600 + Math.max(0, text.length - 40) * 55);
  }
  const host = document.getElementById('toastHost');
  // role=status + aria-live：读屏软件能听到提示，不只是"看得见"
  const node = h('div', { class: 'toast', role: 'status', 'aria-live': 'polite' }, text);
  host.append(node);
  setTimeout(() => {
    node.style.transition = 'opacity 200ms, transform 200ms';
    node.style.opacity = '0';
    node.style.transform = 'translateY(8px)';
    setTimeout(() => node.remove(), 220);
  }, ms);
}

/** 简易对话框，返回 Promise。 */
export function dialog({ title, body, confirm = '确定', cancel = '取消', danger = false }) {
  return new Promise((resolve) => {
    const host = document.getElementById('dialogHost');
    // 记住打开前的焦点，关闭时还回去 —— 键盘用户不会"掉"到页面背后
    const previous = document.activeElement;
    const onKey = (event) => {
      if (event.key === 'Escape') { event.preventDefault(); close(false); return; }
      if (event.key !== 'Tab') return;
      // 焦点陷阱：Tab 在对话框里循环
      const items = [...node.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    const close = (value) => {
      host.classList.remove('open');
      document.removeEventListener('keydown', onKey, true);
      clear(host);
      try { if (previous && previous.focus) previous.focus(); } catch (e) { /* 忽略 */ }
      resolve(value);
    };
    const node = h('div', {
      class: 'dialog',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': title || '对话框',
    },
      h('h3', {}, title),
      typeof body === 'string' ? h('p', {}, body) : body,
      h('div', { class: 'actions' },
        cancel ? button(cancel, { variant: 'text', onClick: () => close(false) }) : null,
        button(confirm, { variant: danger ? 'danger' : '', onClick: () => close(true) })));
    clear(host).append(node);
    host.classList.add('open');
    host.onclick = (event) => { if (event.target === host) close(false); };
    document.addEventListener('keydown', onKey, true);
    // 打开就把焦点放在"确认"上，回车可以直接按
    try {
      const ok = node.querySelector('.actions .btn:last-child') || node;
      if (ok && ok.focus) ok.focus();
    } catch (e) { /* 忽略 */ }
  });
}

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function fmtSize(bytes) {
  if (!bytes) return '0 B';
  let value = Number(bytes);
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (value < 1024 || unit === 'TB') {
      return unit === 'B' ? Math.round(value) + ' B' : value.toFixed(2) + ' ' + unit;
    }
    value /= 1024;
  }
}

export function fmtDuration(seconds) {
  if (!seconds) return '00:00';
  const total = Math.floor(seconds);
  const hh = Math.floor(total / 3600);
  const mm = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return hh ? hh + ':' + mm + ':' + ss : mm + ':' + ss;
}

export function fmtCount(num) {
  const n = Number(num) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(1) + ' 亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
  return String(n);
}

export function fmtDate(ts) {
  if (!ts) return '-';
  const date = new Date(ts * 1000);
  return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') +
         '-' + String(date.getDate()).padStart(2, '0');
}

export function busy(text) {
  return h('span', { class: 'busy' }, h('span', { class: 'spinner' }), text);
}

export function emptyState(iconName, title, text) {
  return h('div', { class: 'empty' },
    h('div', { html: icon(iconName, 'outlined', 56) }),
    h('h3', {}, title),
    h('p', {}, text));
}

export function skeleton(height = 20, width = '100%', radius = 8) {
  return h('div', { class: 'skeleton', style: { height: height + 'px', width, borderRadius: radius + 'px' } });
}


/** 复制文本到剪贴板。
 *
 *  WebView2 里 navigator.clipboard 有可能被拒绝（没有用户手势 / 权限），
 *  所以退回到 execCommand，两条都不行才算失败 —— 调用方再弹个可手动复制的框。
 */
export async function copyText(text) {
  const value = String(text == null ? '' : text);
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch (e) { /* 走下面的兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0';
    document.body.append(ta);
    ta.select();
    ta.setSelectionRange(0, value.length);
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}
