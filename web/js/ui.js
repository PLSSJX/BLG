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

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** 带图标的按钮。
 *
 *  class 里的额外类会被合并进来 —— 播放条要用 'active' 高亮乱序/循环状态，
 *  以前这个字段被无声丢掉，导致按钮永远是默认色。
 */
export function button(label, { variant = '', iconName = '', onClick, title, disabled, class: extra = '' } = {}) {
  const classes = [...new Set(('btn ' + variant + ' ' + extra).split(/\s+/).filter(Boolean))].join(' ');
  return h('button', {
    class: classes,
    title,
    disabled: disabled || false,
    onclick: onClick,
    html: (iconName ? icon(iconName, variant === 'text' ? 'outlined' : 'filled', 18) : '') +
          (label ? '<span>' + escapeHtml(label) + '</span>' : ''),
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
  return h('div', { class: 'field' }, h('label', {}, label), control);
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
export function toast(message, ms = 2600) {
  const host = document.getElementById('toastHost');
  const node = h('div', { class: 'toast' }, message);
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
    const close = (value) => {
      host.classList.remove('open');
      clear(host);
      resolve(value);
    };
    const node = h('div', { class: 'dialog' },
      h('h3', {}, title),
      typeof body === 'string' ? h('p', {}, body) : body,
      h('div', { class: 'actions' },
        cancel ? button(cancel, { variant: 'text', onClick: () => close(false) }) : null,
        button(confirm, { variant: danger ? 'danger' : '', onClick: () => close(true) })));
    clear(host).append(node);
    host.classList.add('open');
    host.onclick = (event) => { if (event.target === host) close(false); };
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
