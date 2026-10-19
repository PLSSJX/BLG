/** 应用引导：导航栏、路由、任务轮询。 */
import { api, setToken } from './api.js';
import { icon } from './icons.js';
import { h, clear, toast, dialog, button } from './ui.js';
import { applyTheme, prefersDark } from './theme.js';
import { initPlayer } from './player.js';
import {
  store, renderHome, renderMusic, renderPlayer, renderImage, renderDoc, renderClip,
  renderTasks, renderSettings,
  refreshTasks,
} from './pages.js';

// 通道体检已经并进解析页，所以这里不再有独立的「通道」入口
const ROUTES = [
  { id: 'home', label: '解析', icon: 'search', render: renderHome },
  // 图标名必须是 icons.js 里真实存在的：queue_music 没有，之前整条入口渲染成空白
  { id: 'music', label: '音乐', icon: 'music_note', render: renderMusic },
  { id: 'player', label: '播放', icon: 'headphones', render: renderPlayer },
  { id: 'clip', label: '剪辑', icon: 'videocam', render: renderClip },
  { id: 'doc', label: '文档', icon: 'description', render: renderDoc },
  { id: 'image', label: '图片', icon: 'image', render: renderImage },
  { id: 'tasks', label: '历史记录', icon: 'history', render: renderTasks },
  { id: 'settings', label: '设置', icon: 'settings', render: renderSettings },
];

let current = 'home';
let pollTimer = null;
let lastSignature = '';

/* ------------------------------------------------------------ 导航栏 */
function buildNavigation() {
  const railHost = document.getElementById('railDestinations');
  const bottomHost = document.getElementById('bottomBar');
  clear(railHost); clear(bottomHost);

  for (const route of ROUTES) {
    const make = (forBottom) => {
      const node = h('button', {
        class: 'destination' + (current === route.id ? ' active' : ''),
        dataset: { route: route.id },
        onclick: () => navigate(route.id),
      },
        h('span', { class: 'indicator', html: icon(route.icon, current === route.id ? 'filled' : 'outlined', 24) }),
        h('span', { class: 'label' }, route.label));
      if (forBottom) node.dataset.bottom = '1';
      return node;
    };
    railHost.append(make(false));
    bottomHost.append(make(true));
  }

  // 左上角的主按钮 —— Kazumi 的 NavigationRail.leading 同样是一枚 elevation 0 的按钮。
  // 图标用加号：这一栏下面全是"看内容"的入口，左上角留给"新建一次解析"。
  const fab = document.getElementById('railSearch');
  fab.innerHTML = icon('add', 'filled', 24);
  fab.title = '新建解析';
  fab.setAttribute('aria-label', '新建解析');
  fab.onclick = openQuickSearch;
}

function markActive() {
  for (const node of document.querySelectorAll('.destination')) {
    node.classList.toggle('active', node.dataset.route === current);
  }
  for (const route of ROUTES) {
    const node = document.querySelector('.rail .destination[data-route="' + route.id + '"] .indicator');
    if (node) node.innerHTML = icon(route.icon, current === route.id ? 'filled' : 'outlined', 24);
  }
}

/* ------------------------------------------------------------ 路由 */
/** 页面渲染前先把该页需要的数据准备好，避免"先画空列表再补数据"的闪动。 */
async function ensureData() {
  if (current === 'tasks' || !store.tasks) {
    await refreshTasks();
  }
}

async function navigate(id) {
  if (!ROUTES.some((r) => r.id === id)) id = 'home';
  current = id;
  if (location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
  markActive();
  await ensureData();
  paint();
  setPolling();
}

/** 只重画 DOM，不发请求。轮询时保住滚动位置，否则页面会一直跳回顶部。 */
function paint() {
  const view = document.getElementById('view');
  const scroll = view.scrollTop;
  const route = ROUTES.find((r) => r.id === current) || ROUTES[0];
  route.render(view);
  view.scrollTop = scroll;
}

/* ------------------------------------------------------------ 任务轮询 */
function setPolling() {
  const active = (store.tasks || []).some(
    (t) => t.status === 'running' || t.status === 'merging' || t.status === 'queued');
  const wanted = current === 'tasks' || active;
  if (wanted && !pollTimer) {
    pollTimer = setInterval(pollTasks, 800);
  } else if (!wanted && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollTasks() {
  await refreshTasks();
  const signature = JSON.stringify((store.tasks || []).map(
    (t) => [t.id, t.status, Math.round(t.percent || 0), (t.files || []).length, (t.log || []).length]));
  if (current === 'tasks' && signature !== lastSignature) {
    lastSignature = signature;
    paint();
  }
  setPolling();
}

/* ------------------------------------------------------------ 快速解析 */
async function openQuickSearch() {
  const input = h('input', {
    class: 'input',
    placeholder: 'BV 号 / 视频链接 / App 分享文案',
    value: store.input || '',
  });
  const platformNames = (store.config?.sites || []).map((s) => s.name).join(' / ');
  const body = h('div', {},
    h('p', {}, '粘贴视频链接或 App 分享文案，回车即可解析。'),
    h('p', { class: 'hint' }, platformNames ? '支持：' + platformNames : '正在加载支持的平台…'),
    input);
  const host = document.getElementById('dialogHost');
  const proceed = () => {
    store.input = input.value.trim();
    host.classList.remove('open');
    clear(host);
    if (store.input) {
      navigate('home');
      setTimeout(() => document.querySelector('.search-field .btn')?.click(), 60);
    }
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') proceed(); });

  const node = h('div', { class: 'dialog' },
    h('h3', {}, '新建解析'),
    body,
    h('div', { class: 'actions' },
      button('取消', { variant: 'text', onClick: () => { host.classList.remove('open'); clear(host); } }),
      button('解析', { onClick: proceed })));
  clear(host).append(node);
  host.classList.add('open');
  host.onclick = (event) => { if (event.target === host) { host.classList.remove('open'); clear(host); } };
  setTimeout(() => input.focus(), 50);
}

/* ------------------------------------------------------------ 启动 */
async function boot() {
  buildNavigation();
  initPlayer();     // 底部播放条，先建好，之后任何页面都能直接播

  try {
    store.config = await api.state();
  } catch (error) {
    document.getElementById('view').append(
      h('div', { class: 'page' },
        h('div', { class: 'hint-box' },
          h('span', { html: icon('error_outline', 'outlined', 22) }),
          h('div', {}, '无法连接后端：' + error.message + '　请确认服务仍在运行。'))));
    return;
  }

  setToken(store.config.api_token);

  // 自绘标题栏：后端已经把原生标题栏整个去掉了，这里补一条 HTML 的。
  // 尺寸必须和 videograb/titlebar.py 里的常量对齐，否则右侧按钮点不动。
  if ((store.config.titlebar || 'native') === 'custom') buildTitlebar();
  // 双保险：万一 state 里没这个字段（旧后端），只要检测到原生标题栏被拿掉了
  // 就补画一条 —— 否则窗口会没有任何按钮，连关都关不掉。
  else if (store.config.window_mode === 'webview'
           && window.innerHeight === document.documentElement.clientHeight
           && (store.config.titlebar === 'custom')) buildTitlebar();

  applyTheme({
    seed: store.config.settings.theme_seed,
    mode: store.config.settings.theme_mode,
    dynamic: store.config.settings.dynamic_color,
  });

  // 跟随系统模式下，系统切换深浅色要实时反映
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      applyTheme({
        seed: store.config.settings.theme_seed,
        mode: store.config.settings.theme_mode,
        dynamic: store.config.settings.dynamic_color,
      });
    });
  }

  current = (location.hash || '#home').slice(1);
  if (!ROUTES.some((r) => r.id === current)) current = 'home';
  markActive();
  await refreshTasks();
  paint();
  setPolling();

  // 深链：/?input=BV1xx411c7mD#home 打开即自动解析，方便把链接当书签或分享出去
  const params = new URLSearchParams(location.search);
  const deep = params.get('input') || params.get('url') || params.get('bv');
  if (deep) {
    store.input = deep.trim();
    navigate('home');
    setTimeout(() => document.querySelector('.search-field .btn')?.click(), 120);
  }
}

document.addEventListener('navigate', (event) => { navigate(event.detail); });
document.addEventListener('tasks-changed', () => {
  refreshTasks().then(() => { if (current === 'tasks') paint(); setPolling(); });
});
window.addEventListener('hashchange', () => {
  const id = (location.hash || '#home').slice(1);
  if (id !== current) navigate(id);
});

boot();

/* ------------------------------------------------------------ 自绘标题栏 */

const TITLEBAR_HEIGHT = 44;
const TITLEBAR_BUTTON = 52;

function buildTitlebar() {
  if (document.getElementById('titlebar')) return;
  const bar = h('div', { id: 'titlebar' },
    h('div', { class: 'tb-title' }, 'MakiseToolkit'),
    h('div', { class: 'tb-buttons' },
      tbButton('minimize', '最小化', '—'),
      tbButton('maximize', '最大化', '□'),
      tbButton('close', '关闭', '✕')));
  document.body.prepend(bar);
  document.body.classList.add('has-titlebar');
}

function tbButton(action, title, glyph) {
  return h('button', {
    class: 'tb-btn tb-' + action,
    title,
    onclick: async () => {
      try {
        const fn = { minimize: api.windowMinimize, maximize: api.windowMaximize,
                     close: api.windowClose }[action];
        if (fn) await fn();
      } catch (err) {
        // 不是窗口模式（用浏览器打开）时这里会报错，不影响使用
        toast('窗口操作失败：' + err.message);
      }
    },
  }, h('span', { class: 'tb-glyph' }, glyph));
}
