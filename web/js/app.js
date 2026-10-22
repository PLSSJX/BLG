/** 应用引导：导航栏、路由、任务轮询。 */
import { api, setToken } from './api.js';
import { icon } from './icons.js';
import { h, clear, toast, dialog, button } from './ui.js';
import { applyTheme, prefersDark } from './theme.js';
import { initPlayer, bindPlayerShortcuts } from './player.js';
import { mountAskButton, bindAskShortcut } from './ask.js';
import {
  store, renderHome, renderMusic, renderPlayer, renderImage, renderDoc, renderClip,
  renderTasks, renderPapers, renderSettings, renderProjects,
  refreshTasks,
  renderResources,
} from './pages.js';

// 通道体检已经并进解析页，所以这里不再有独立的「通道」入口
// extras: true 表示"附加工具箱"里的页面 —— 设置里没打开时不显示、也不可进
const ROUTES = [
  { id: 'projects', label: '项目', icon: 'folder_open', render: renderProjects },
  { id: 'doc', label: '文档', icon: 'description', render: renderDoc },
  { id: 'papers', label: '参考文献', icon: 'science', render: renderPapers },
  { id: 'tasks', label: '记录', icon: 'history', render: renderTasks },
  { id: 'home', label: '解析', icon: 'search', render: renderHome, extras: true },
  // 图标名必须是 icons.js 里真实存在的：queue_music 没有，之前整条入口渲染成空白
  { id: 'music', label: '音乐', icon: 'music_note', render: renderMusic, extras: true },
  { id: 'player', label: '播放', icon: 'headphones', render: renderPlayer, extras: true },
  { id: 'clip', label: '剪辑', icon: 'videocam', render: renderClip, extras: true },
  { id: 'image', label: '图片', icon: 'image', render: renderImage, extras: true },
  { id: 'resources', label: '找资源', icon: 'travel_explore', render: renderResources, extras: true },
  { id: 'settings', label: '设置', icon: 'settings', render: renderSettings },
];

/** 附加工具箱开着吗 */
function extrasOn() {
  return store.config?.settings?.extras_enabled === true;
}

/** 当前可见的路由 */
function visibleRoutes() {
  return ROUTES.filter((r) => !r.extras || extrasOn());
}

let current = 'projects';
let pollTimer = null;
let lastSignature = '';

/* ------------------------------------------------------------ 导航栏 */
/** 左上角"新建解析"加号：暂时隐藏，功能保留。改天要用改成 true。 */
const SHOW_RAIL_SEARCH = false;

function buildNavigation() {
  const railHost = document.getElementById('railDestinations');
  const bottomHost = document.getElementById('bottomBar');
  clear(railHost); clear(bottomHost);

  for (const route of visibleRoutes()) {
    const make = (forBottom) => {
      // 工具箱关着时这一页只剩文献，导航上就别再叫「历史记录」了
      const label = (route.id === 'tasks' && !extrasOn()) ? '下载过的论文' : route.label;
      const node = h('button', {
        class: 'destination' + (current === route.id ? ' active' : ''),
        dataset: { route: route.id },
        onclick: () => {
          // 点导航的「项目」是想看列表；左上角那颗返回箭头才是"回到刚才的项目"
          if (route.id === 'projects') openProject = null;
          navigate(route.id);
        },
      },
        h('span', { class: 'indicator', html: icon(route.icon, current === route.id ? 'filled' : 'outlined', 24) }),
        h('span', { class: 'label' }, label));
      if (forBottom) node.dataset.bottom = '1';
      return node;
    };
    railHost.append(make(false));
    bottomHost.append(make(true));
  }

  // 一枚圆球负责画"选中"的底色，切换时从旧位置滑到新位置
  let blob = railHost.querySelector('.rail-blob');
  if (!blob) {
    blob = h('div', { class: 'rail-blob' });
    // 位移结束立刻摊回胶囊：比定时器可靠，动画被打断时也不会卡在圆形
    blob.addEventListener('transitionend', (event) => {
      if (event.propertyName === 'top') blob.classList.remove('moving');
    });
    railHost.prepend(blob);
  }
  requestAnimationFrame(() => positionBlob(false));

  // 左上角的主按钮 —— Kazumi 的 NavigationRail.leading 同样是一枚 elevation 0 的按钮。
  // 图标用加号：这一栏下面全是"看内容"的入口，左上角留给"新建一次解析"。
  // 左上角按钮：其余页面按它回到「项目」；项目页里由页面内部的箭头负责保存退出
  const fab = document.getElementById('railSearch');
  fab.hidden = false;
  fab.onclick = () => {
    const mode = fab.dataset.mode || 'back';
    if (mode === 'back') { navigate('projects'); return; }
    const frame = document.querySelector('.projects-frame');
    const win = frame && frame.contentWindow;
    if (mode === 'exit') {
      // 保存并退出：交给画布页自己走保存检查
      try { win && win.postMessage({ type: 'mt-back' }, location.origin); } catch (e) { /* 忽略 */ }
      return;
    }
    // 列表页：让内嵌页面弹出「新建项目」
    try { win && win.postMessage({ type: 'mt-new' }, location.origin); } catch (e) { /* 忽略 */ }
  };
  syncRailFab();
}

/** 左上角按钮的三种状态：
 *  别的页面      → 返回箭头，回到项目页
 *  项目页·画布里  → 返回箭头，保存并退出当前项目
 *  项目页·列表里  → 加号，新建项目
 */
let projectView = 'list';   // iframe 里现在是列表还是画布
let openProject = null;     // 正在编辑的项目 {id,title}，离开页面后用来恢复

/** 供 pages.js 决定 iframe 该打开列表还是某个项目 */
export function getOpenProject() {
  return openProject;
}
// pages.js 和 app.js 是两个模块，不共享作用域，用 window 传一下
window.__mtOpenProject = () => openProject;

function syncRailFab() {
  const fab = document.getElementById('railSearch');
  if (!fab) return;
  const onProjects = current === 'projects';
  const inCanvas = onProjects && projectView === 'canvas';
  const showPlus = onProjects && !inCanvas;

  fab.innerHTML = icon(showPlus ? 'add' : 'arrow_back', 'filled', 24);
  fab.title = showPlus ? '新建项目' : (inCanvas ? '保存并退出项目' : '回到项目');
  fab.setAttribute('aria-label', fab.title);
  fab.dataset.mode = showPlus ? 'new' : (inCanvas ? 'exit' : 'back');
}

// 内嵌页面会告诉我们它在哪一屏
window.addEventListener('message', (event) => {
  if (!event || !event.data || event.data.type !== 'mt-state') return;
  const data = event.data;
  if (data.view === 'canvas' && data.id) {
    openProject = { id: data.id, title: data.title || '' };
  } else if (data.view === 'list') {
    openProject = null;         // 用户自己退出了项目
  }
  if (data.view && data.view !== projectView) {
    projectView = data.view;
    syncRailFab();
  } else if (current === 'projects') {
    syncRailFab();
  }
});

/** 把粘球移到当前选中项的位置。首次定位不做动画，之后每次都滑过去。 */
let blobReady = false;
function positionBlob(animate = true, routeId = current) {
  const rail = document.getElementById('railDestinations');
  if (!rail) return;
  const blob = rail.querySelector('.rail-blob');
  if (!blob) return;
  const target = rail.querySelector('.destination[data-route="' + routeId + '"]');
  if (!target) { blob.style.opacity = '0'; return; }
  blob.style.opacity = '1';

  const centreOf = () => {
    const box = target.querySelector('.indicator') || target;
    const railRect = rail.getBoundingClientRect();
    const boxRect = box.getBoundingClientRect();
    return boxRect.top - railRect.top + boxRect.height / 2;
  };

  // 逐帧动画：每一帧都重新读目标的实时中心，
  // 这样目标在飞行途中因为文字展开/收起而上下移动时，球始终追着它。
  cancelAnimationFrame(positionBlob._raf);
  const from = blob.getBoundingClientRect();
  const railTop = rail.getBoundingClientRect().top;
  const startY = from.top - railTop + from.height / 2;

  if (!animate || !blobReady) {
    blob.style.top = centreOf() + 'px';
    blobReady = true;
    return;
  }

  blob.classList.add('moving');
  const t0 = performance.now();
  const dur = 520;
  const ease = (t) => 1 - Math.pow(1 - t, 3);   // 缓出，不过冲
  const step = () => {
    const p = Math.min(1, (performance.now() - t0) / dur);
    const goal = centreOf();
    // 目标是"活"的：起点固定，终点每帧重算
    const y = startY + (goal - startY) * ease(p);
    blob.style.top = y + 'px';
    if (p < 1) {
      positionBlob._raf = requestAnimationFrame(step);
      return;
    }
    // 主行程结束，但文字可能还在展开（0→300ms 的过渡），目标会继续移动。
    // 这里用一段"跟随沉降"慢慢贴上去，既不跳也不冲。
    const settleUntil = performance.now() + 420;
    const settle = () => {
      const goal = centreOf();
      const rect = blob.getBoundingClientRect();
      const cur = rect.top - rail.getBoundingClientRect().top + rect.height / 2;
      const gap = goal - cur;
      if (Math.abs(gap) < 0.5 || performance.now() > settleUntil) {
        blob.style.top = centreOf() + 'px';
        blob.classList.remove('moving');
        return;
      }
      blob.style.top = (cur + gap * 0.25) + 'px';
      positionBlob._raf = requestAnimationFrame(settle);
    };
    settle();
  };
  positionBlob._raf = requestAnimationFrame(step);
}
function markActive() {
  const rail = document.getElementById('railDestinations');
  const prev = rail ? rail.querySelector('.destination.active') : null;
  const prevRoute = prev ? prev.dataset.route : '';

  // 已经在目标项上：直接对齐，不重放动画
  if (prevRoute === current) {
    // 点的就是当前页：不做任何位移，只保证高亮是对的
    refreshActiveClasses();
    positionBlob(false, current);
    return;
  }

  // ① 旧项立刻失去高亮 —— 它的图标先缩回去
  if (prev) prev.classList.remove('active');
  // ② 球滑向目标项
  positionBlob(true, current);
  // ③ 球到位后再点亮新项，新图标这时才放大。
  // 这里**不能** clearTimeout：轮询/重绘会重复调 markActive，
  // 一旦把定时器清掉，"点亮新项"永远等不到，图标就一直不放大（实测踩过）。
  // 改成不取消、只校验当前是否还是这个目标。
  markActive._pending = current;
  setTimeout(() => {
    if (markActive._pending !== current) return;
    refreshActiveClasses();
    // 位置交给 positionBlob 的跟随沉降，这里只点亮高亮
  }, 460);
}

/** 把 .active 同步到当前路由（含底部导航）。 */
function refreshActiveClasses() {
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
  if (id !== 'projects') projectView = 'list';
  const target = ROUTES.find((r) => r.id === id);
  // 附加工具箱关着的时候，那几个页面直接当不存在
  if (!target || (target.extras && !extrasOn())) id = 'projects';
  current = id;
  if (location.hash !== '#' + id) history.replaceState(null, '', '#' + id);
  markActive();
  await ensureData();
  paint();
  setPolling();
}

/** 当前路由 id（pages.js 用它判断异步结果回来时用户还在不在原页面）。 */
export function routeNow() { return current; }

// pages.js 需要"跳页"但直接 import navigate 会形成循环依赖，
// 这里用运行时钩子转一手（初始化时不存在也不影响，调用时已经挂好了）。
window.__mtNavigate = (id) => navigate(id);
window.__mtRepaint = (routeId) => { if (current === routeId) paint(); };

/** 异步操作回来后的安全重绘：用户已经切走就什么都不做。
 *  以前直接 renderXxx(view)，解析几秒后回来会把用户正在看的页面覆盖掉。 */
export function repaintIf(routeId) {
  if (current === routeId) paint();
}

/** 只重画 DOM，不发请求。轮询时保住滚动位置，否则页面会一直跳回顶部。 */
function paint() {
  const view = document.getElementById('view');
  const scroll = view.scrollTop;
  const route = ROUTES.find((r) => r.id === current) || ROUTES[0];
  const switched = current !== lastPaintedRoute;
  lastPaintedRoute = current;
  route.render(view);
  if (typeof syncRailFab === 'function') syncRailFab();
  view.scrollTop = scroll;
  applyMotion(view, switched);
}

let lastPaintedRoute = '';
let revealObserver = null;

/** 给刚渲染的内容挂上入场动效。
 *  只有"换页"才整页淡入；同一个页面内部重绘（轮询刷新）不重放整页动画，
 *  否则任务页每 0.8 秒闪一次。卡片显现只对还没显示过的元素做。 */
function applyMotion(view, switched) {
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (switched && !reduce) {
    view.classList.remove('view-enter');
    void view.offsetWidth;          // 强制回流，动画才会重放
    view.classList.add('view-enter');
  }
  if (reduce) return;
  if (!revealObserver) {
    revealObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add('in');
        revealObserver.unobserve(entry.target);
      }
    }, { root: view, rootMargin: '0px 0px -8% 0px', threshold: 0.01 });
  }
  // 只观察还没动过的卡片，避免每次重绘重复入队
  const cards = view.querySelectorAll('.card:not(.reveal)');
  cards.forEach((card, i) => {
    card.classList.add('reveal');
    card.style.setProperty('--i', String(Math.min(i, 8)));
    revealObserver.observe(card);
  });
}

/* ------------------------------------------------------------ 任务轮询 */
function setPolling() {
  const active = (store.tasks || []).some(
    (t) => t.status === 'running' || t.status === 'merging' || t.status === 'queued');
  const wanted = current === 'tasks' || active;
  // 窗口被最小化/切到后台时没必要每 0.8 秒拉一次全量
  const hidden = document.hidden;
  const gap = hidden ? 5000 : 800;
  if (wanted && !pollTimer) {
    pollTimer = setInterval(pollTasks, gap);
  } else if (!wanted && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (!pollTimer) return;
  clearInterval(pollTimer);
  pollTimer = null;
  setPolling();
});

let pollInFlight = false;

let lastStatuses = {};

async function pollTasks() {
  // 上一次还没回来就别叠请求：慢机器上会越叠越多，晚到的旧响应还会覆盖新状态
  if (pollInFlight) return;
  pollInFlight = true;
  try { await pollTasksOnce(); } finally { pollInFlight = false; }
}

async function pollTasksOnce() {
  await refreshTasks();
  // 任务从"在跑"变成"完成/失败"时，通知正在看歌单详情的页面刷一次。
  // 放在轮询里而不是渲染里 —— 渲染函数不能有副作用，否则会无限循环。
  let justFinished = false;
  for (const t of (store.tasks || [])) {
    const prev = lastStatuses[t.id];
    if (prev && prev !== t.status && (t.status === 'done' || t.status === 'error')) {
      justFinished = true;
    }
    lastStatuses[t.id] = t.status;
  }
  if (justFinished) document.dispatchEvent(new CustomEvent('task-finished'));
  // 签名只比"看起来会变的东西"：以前带上 log.length，
  // 日志多一行就整页重建 —— 用户展开的运行日志 0.8 秒后自己被合上。
  const signature = JSON.stringify((store.tasks || []).map(
    (t) => [t.id, t.status, Math.round(t.percent || 0), (t.files || []).length, t.speed_text || '']));
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
  initPlayer();
  bindPlayerShortcuts();     // 底部播放条，先建好，之后任何页面都能直接播
  mountAskButton(); // 右下角「问 AI」圆钮，任何页面都能呼出
  bindAskShortcut();

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
  // 导航在配置读回来之前就建好了，那时 extrasOn() 只能算 false。
  // 配置一到就重建一次，否则打开附加工具箱后要刷新两次才看得到那几个入口。
  buildNavigation();

  // 心跳：后端 8 秒内收不到就会回退原生标题栏（自绘模式下的逃生口）。
  // 前端报错也一并送进 data/error.log，别让问题只剩"没反应"。
  api.windowReady().catch(() => {});
  window.addEventListener('error', (e) => {
    api.frontendError(String(e.message || e.error || ''), String(e.filename || '') + ':' + (e.lineno || '')).catch(() => {});
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    api.frontendError(String((r && r.message) || r || ''), 'unhandledrejection').catch(() => {});
  });

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

  current = (location.hash || '#projects').slice(1);
  const startRoute = ROUTES.find((r) => r.id === current);
  if (!startRoute || (startRoute.extras && !extrasOn())) current = 'projects';
  if (location.hash !== '#' + current) history.replaceState(null, '', '#' + current);
  markActive();
  await refreshTasks();
  paint();
  setPolling();

  // 深链：/?input=BV1xx411c7mD#home 打开即自动解析，方便把链接当书签或分享出去
  const params = new URLSearchParams(location.search);
  const deep = params.get('input') || params.get('url') || params.get('bv');
  if (deep && extrasOn()) {
    store.input = deep.trim();
    navigate('home');
    setTimeout(() => document.querySelector('.search-field .btn')?.click(), 120);
  }
}

document.addEventListener('navigate', (event) => { navigate(event.detail); });
// 设置页里开了/关了附加工具箱：导航要立刻重建
window.addEventListener('toolbox-changed', () => {
  buildNavigation();
  const route = ROUTES.find((r) => r.id === current);
  if (route && route.extras && !extrasOn()) navigate('projects');
  else { markActive(); paint(); }
});
document.addEventListener('tasks-changed', () => {
  refreshTasks().then(() => { if (current === 'tasks') paint(); setPolling(); });
});
window.addEventListener('hashchange', () => {
  const id = (location.hash || '#projects').slice(1);
  if (id !== current) navigate(id);
});

boot();

/* ------------------------------------------------------------ 自绘标题栏 */

const TITLEBAR_HEIGHT = 44;
const TITLEBAR_BUTTON = 52;

function buildTitlebar() {
  if (document.getElementById('titlebar')) return;
  // 按下标题栏（按钮除外）就让系统开始拖动；双击切换最大化。
  //
  // 双击必须自己在这里判断：第一次 mousedown 就把系统拖拽循环启动了，
  // 第二次点击被那个模态循环吃掉，页面的 dblclick 根本不会触发（实测）。
  let lastDown = 0;
  const onDown = (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('button')) return;
    const now = Date.now();
    // 跟随系统设置（默认 500ms），写死会和用户的系统习惯对不上
    const gap = (store.config && store.config.dblclick_ms) || 500;
    if (now - lastDown < gap) {
      lastDown = 0;                       // 双击：切换最大化，不再发起拖动
      api.windowMaximize().catch(() => {});
      return;
    }
    lastDown = now;
    api.windowDrag().catch(() => {});
  };
  // 边缘缩放：WebView2 是跨进程窗口，边缘的命中测试到不了父窗口，
  // 只能由页面自己发现"按在边上"再让系统接管。
  installResizeHandles();

  const bar = h('div', { id: 'titlebar', onmousedown: onDown },
    h('div', { class: 'tb-title' }, 'MakiseToolkit'),
    h('div', { class: 'tb-buttons' },
      tbButton('minimize', '最小化'),
      tbButton('maximize', '最大化'),
      tbButton('close', '关闭')));
  document.body.prepend(bar);
  document.body.classList.add('has-titlebar');
}

function tbButton(action, title) {
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
  }, h('span', { class: 'tb-glyph g-' + action }));
}

/* ------------------------------------------------------------ 边缘缩放 */

// 边缘判定要按**物理像素**给，不能用固定 CSS 像素：
// 300% 缩放下 6 CSS px 等于 18 物理像素，抓取区宽得离谱；
// 反过来 100% 下又太窄。除以 devicePixelRatio 之后各种缩放手感一致。
function resizeEdgeWidth() {
  const dpr = window.devicePixelRatio || 1;
  return Math.min(10, Math.max(3, Math.round(8 / dpr)));
}

function edgeAt(e) {
  // 必须用 documentElement.clientWidth，不能用 window.innerWidth：
  // innerWidth 把竖直滚动条的宽度也算进去了，右侧那圈判定区正好压在滚动条上，
  // 鼠标事件被滚动条吃掉，页面收不到 mousedown —— 表现就是右边缘怎么拉都不动。
  const de = document.documentElement;
  const w = de.clientWidth || window.innerWidth;
  const h = de.clientHeight || window.innerHeight;
  const x = e.clientX, y = e.clientY;
  const edge = resizeEdgeWidth();
  // 最外那几像素可能被 WebView2 的悬浮滚动条占着，鼠标事件到不了页面，
  // 所以右/下边把判定带往里放宽一倍，用户抓的是"靠边一点点"的位置。
  const outer = edge * 2;
  const left = x <= edge, right = x >= w - outer;
  const top = y <= edge, bottom = y >= h - outer;
  if (top && left) return 'topleft';
  if (top && right) return 'topright';
  if (bottom && left) return 'bottomleft';
  if (bottom && right) return 'bottomright';
  if (left) return 'left';
  if (right) return 'right';
  if (top) return 'top';
  if (bottom) return 'bottom';
  return '';
}

const EDGE_CURSOR = {
  left: 'ew-resize', right: 'ew-resize', top: 'ns-resize', bottom: 'ns-resize',
  topleft: 'nwse-resize', bottomright: 'nwse-resize',
  topright: 'nesw-resize', bottomleft: 'nesw-resize',
};

function installResizeHandles() {
  if (window.__vgResizeReady) return;
  window.__vgResizeReady = true;
  let current = '';

  // 用捕获阶段，避免被页面里的元素吃掉
  // 一条贴在边上的细高光，告诉用户"这里可以拖"
  const hint = h('div', { id: 'edgeHint' });
  document.body.append(hint);

  document.addEventListener('mousemove', (e) => {
    const edge = edgeAt(e);
    if (edge !== current) {
      current = edge;
      document.body.style.cursor = edge ? EDGE_CURSOR[edge] : '';
      hint.dataset.edge = edge || '';
    }
  }, true);

  document.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const edge = edgeAt(e);
    if (!edge) return;
    e.preventDefault();
    e.stopPropagation();
    document.body.style.cursor = '';
    api.windowResize(edge).catch(() => {});
  }, true);
}
