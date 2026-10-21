const MTD = window.MTData;

const params = new URLSearchParams(location.search);
const projectIdRaw = params.get('id') || (MTD.loadProjects()[0] || {}).id;
const projects = MTD.loadProjects();
const project = projects.find((p) => p.id === projectIdRaw) || projects[0] || { name: '未命名项目', path: '' };

document.getElementById('pName').textContent = project.name;
document.getElementById('pPath').textContent = project.path || '（原型数据）';

if (project.external) {
  const host = document.getElementById('bannerHost');
  const b = document.createElement('div');
  b.className = 'banner';
  b.innerHTML = '<span>⚠</span><span>' + (project.externalNote || '这个项目被外部 git 修改过') +
                '</span><span style="flex:1"></span>';
  const btn = document.createElement('button');
  btn.className = 'btn text';
  btn.textContent = '重新载入';
  btn.onclick = () => b.remove();
  b.append(btn);
  host.append(b);
}

/* ---------------------------------------------------------- 数据
   每条边有两个方向：
     axis: 'h' —— 主线方向（时间轴往右 / 导图往外放射）
     axis: 'v' —— 从某个节点垂直拉出去的分支，节点挂在这条线上
*/
const graph = {
  trunk: { id: 't1', title: '低功耗芯片设计', note: '主干 · 引言.md' },
  nodes: [
    { id: 'n1', title: '研究背景', note: '3 条备注', parent: 't1', axis: 'h' },
    { id: 'n2', title: '相关工作', note: '已挂 2 个文件', parent: 't1', axis: 'h' },
    { id: 'n3', title: 'DVS 方案', parent: 'n1', axis: 'v' },
    { id: 'n4', title: '动态电压调节', parent: 'n1', axis: 'v' },
    { id: 'n5', title: '存内计算', parent: 'n2', axis: 'v' },
    { id: 'n6', title: '浮空的思考', note: '还没决定挂在哪', parent: null, axis: null, floating: true },
  ],
  staged: [
    { id: 's1', title: '参考文献.pdf' },
    { id: 's2', title: '实验数据.csv' },
  ],
};

let layout = 'timeline';
let selected = null;

const canvas = document.getElementById('canvas');
const edgesSvg = document.getElementById('edges');
const stageItems = document.getElementById('stageItems');
const saveBtn = document.getElementById('save');
const dot = document.getElementById('dot');

const byId = (id) => (id === graph.trunk.id ? graph.trunk : graph.nodes.find((n) => n.id === id));
const kidsOf = (id, axis) => graph.nodes.filter((n) => n.parent === id && n.axis === axis);

/* ---------------------------------------------------------- 布局 */
function computeLayout() {
  // 关掉自动排版：谁被拖到哪儿就待在哪儿
  if (graph.freeLayout) {
    const all = [graph.trunk, ...graph.nodes].filter(Boolean);
    // 数据里没坐标（比如旧项目或异常保存）→ 先用自动排版算出位置再冻结，
    // 否则所有节点会叠在同一个点上，看起来像"节点全没了"。
    const missing = all.some((n) => typeof n.x !== 'number' || typeof n.y !== 'number');
    if (missing && !window.__layoutFallback) {
      window.__layoutFallback = true;
      const keep = graph.freeLayout;
      graph.freeLayout = false;
      computeLayout();
      graph.freeLayout = keep;
      window.__layoutFallback = false;
      return;
    }
    for (const n of all) {
      if (typeof n.x !== 'number' || typeof n.y !== 'number') {
        n.x = (canvas.clientWidth || 900) / 2;
        n.y = (canvas.clientHeight || 520) / 2;
      }
    }
    return;
  }
  const w = canvas.clientWidth || 900;
  const h = canvas.clientHeight || 520;
  const cx = w / 2, cy = h / 2;

  if (layout === 'timeline') {
    // 递归往右下生长：
    //   h 子节点 = 同一行继续往右；v 子节点 = 同一列往下。
    // 因为永远不会往左走，所以分支不可能盖住最左边的主干。
    const STEP_X = 240, GAP_Y = 26;
    const used = new Map();   // 每一列已占用的最下沿，用来避免同列重叠

    // 记录每一列「已占到的最下沿」，用真实高度累加，避免父子重叠
    const bottom = new Map();
    const heightOf = (node) => (sizeOf(node).h || 76);
    const placeColumn = (x, startY) => {
      const key = Math.round(x);
      const cur = bottom.get(key);
      return cur === undefined ? startY : Math.max(startY, cur + GAP_Y);
    };

    const walk = (node, x, y, depth) => {
      node.x = x;
      node.y = y;
      bottom.set(Math.round(x), y + heightOf(node));

      // 主线：同行往右
      let hx = x + STEP_X;
      const hs = kidsOf(node.id, 'h');
      hs.forEach((child) => {
        walk(child, hx, y, depth + 1);
        hx += STEP_X;
      });

      // 分支：同列往下，子节点自己再递归（可以继续嵌套、继续往右）
      const vs = kidsOf(node.id, 'v');
      let vy = y + heightOf(node) + GAP_Y;
      vs.forEach((child) => {
        vy = placeColumn(x, vy);
        walk(child, x, vy, depth + 1);
        vy = child.y + heightOf(child) + GAP_Y;
      });
      return vy;
    };

    // 主干先放，其它从主干的下方开始排，保证不与主干平齐
    walk(graph.trunk, 110, cy, 0);
    kidsOf(graph.trunk.id, 'v').forEach((v) => {
      // 主干的直接分支放到整张图的下方，视觉上不压主干
      const below = Math.max(...[graph.trunk, ...graph.nodes]
        .filter((n) => !n.floating && n.y !== undefined)
        .map((n) => n.y)) + STEP_Y;
      if (v.y < below) v.y = below;
    });
  } else {
    // 思维导图：主干居中，主线往外放射，分支沿同一方向继续外延
    graph.trunk.x = cx;
    graph.trunk.y = cy;
    const mains = kidsOf(graph.trunk.id, 'h');
    const count = Math.max(1, mains.length);
    const ring = Math.min(w, h) * 0.25;
    const spread = count <= 2 ? Math.PI * 0.5 : count === 3 ? Math.PI * 0.72 : Math.PI * 1.7;
    const startA = count <= 3 ? -spread / 2 : -Math.PI / 2 - spread / 2;

    const fan = (node, angle, radius, depth) => {
      node.x = cx + Math.cos(angle) * radius;
      node.y = cy + Math.sin(angle) * radius;
      kidsOf(node.id, 'h').forEach((child, ci, arr) => {
        const a2 = angle + (ci - (arr.length - 1) / 2) * 0.34;
        fan(child, a2, radius + 128, depth + 1);
      });
      kidsOf(node.id, 'v').forEach((child, ci) => {
        fan(child, angle, radius + 118 + ci * 108, depth + 1);
      });
    };

    mains.forEach((m, i) => {
      const a = count === 1 ? 0 : startA + (spread * i) / (count - 1);
      fan(m, a, ring, 1);
    });
  }

  resolveCollisions();

  graph.nodes.filter((n) => n.floating).forEach((n) => {
    if (!n.x && !n.y) { n.x = 170; n.y = h - 120; }
  });
}

/** 自动排版的兜底：不同列之间也可能撞在一起，这里统一推开。
 *  做法很简单——按 y 排序，发现重叠就把靠下的那个往下挪，直到不再重叠。
 *  宁可多留白，也不要叠字看不清。 */
function resolveCollisions() {
  const items = [graph.trunk, ...graph.nodes]
    .filter((n) => n && !n.floating && typeof n.x === 'number' && typeof n.y === 'number')
    .map((n) => {
      const s = sizeOf(n);
      return { n, w: n._w || s.w, h: n._h || s.h };
    });
  if (items.length < 2) return;

  const GAP = 16;
  const hit = (a, b) =>
    Math.abs(a.n.x - b.n.x) * 2 < a.w + b.w - 6 &&
    Math.abs(a.n.y - b.n.y) * 2 < a.h + b.h - 6;

  for (let round = 0; round < 80; round += 1) {
    let moved = false;
    items.sort((p, q) => p.n.y - q.n.y || p.n.x - q.n.x);
    for (let i = 0; i < items.length; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const a = items[i];
        const b = items[j];
        if (!hit(a, b)) continue;
        const need = (a.h + b.h) / 2 - (b.n.y - a.n.y) + GAP;
        if (need > 0) { b.n.y += need; moved = true; }
      }
    }
    if (!moved) break;
  }
}

/* ---------------------------------------------------------- 渲染 */
function nodeEl(id) { return canvas.querySelector('.node[data-id="' + id + '"]'); }

function ensureNodeEl(data) {
  let el = nodeEl(data.id);
  if (!el) {
    el = document.createElement('div');
    el.className = 'node';
    el.dataset.id = data.id;
    // 注意：这里不能直接用闭包里的 data —— 页面从示例数据切到真实项目时，
    // graph.nodes 会整体换掉，旧对象上的修改不会反映到画布上。按 id 现查。
    el.addEventListener('mousedown', (e) => {
      const live = byId(el.dataset.id);
      if (live) startDrag(e, live);
    });
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      // 刚拖完的那一下不要再当成"点击"，否则会在拖完立刻弹出编辑栏。
      // 用时间戳而不是布尔标志：click 和 mouseup 的先后顺序在合成事件里不保证。
      if (Date.now() - lastDragMovedAt < 300) return;
      const id = el.dataset.id;          // 以元素身上的 id 为准，别用闭包里的旧对象
      if (linkModeFrom) { finishLink(id); return; }
      select(id);
      const live = byId(id);
      if (live) openEditor(live);        // 点一下就在右侧面板里打开它
    });
    el.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const live = byId(el.dataset.id);
      if (live) showCtxMenu(e.clientX, e.clientY, live);
    });
    el.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      const live = byId(el.dataset.id);
      if (live) openEditor(live);
    });
    canvas.append(el);
  }
  return el;
}

function render() {
  computeLayout();
  canvas.classList.toggle('round', layout === 'mindmap');

  // 数据可能被整体替换（例如从内置示例切到真实项目），
  // 不清理的话旧节点会留在画布上，看着像"加载没生效"。
  const alive = new Set([graph.trunk.id, ...graph.nodes.map((n) => n.id)]);
  for (const el of [...canvas.querySelectorAll('.node')]) {
    if (!alive.has(el.dataset.id)) el.remove();
  }

  for (const data of [graph.trunk, ...graph.nodes]) {
    const el = ensureNodeEl(data);
    el.className = 'node' +
      (data.id === graph.trunk.id ? ' trunk' : '') +
      (data.floating ? ' floating' : '') +
      (layout === 'timeline' ? ' card' : '') +
      (selected === data.id ? ' selected' : '');
    el.innerHTML = '<div>' + data.title + '</div>' +
      (data.summary ? '<div class="summary">' + data.summary + '</div>'
        : (data.note ? '<div class="note">' + data.note + '</div>' : '')) +
      (data.file ? '<div class="filerow">' + data.file + '</div>' : '') +
      ((data.assets && data.assets.length)
        ? '<div class="docrow" title="点一下用外部程序打开">' +
          (/\.[a-z0-9]+$/i.test(data.assets[0].name) && /\.(md|txt)$/i.test(data.assets[0].name)
            ? '📄 ' : '📎 ') + data.assets[0].name +
          (data.assets.length > 1 ? ' +' + (data.assets.length - 1) : '') + '</div>'
        : '') +
      ((data.assets && data.assets.length)
        ? '<div class="asset-badge" title="挂了 ' + data.assets.length + ' 个文件，点开查看列表">📎 ' + data.assets.length + '</div>'
        : '');
    const fileRow = el.querySelector('.filerow');
    if (fileRow && data.file) {
      fileRow.title = '点一下用系统程序打开这个文件';
      fileRow.style.cursor = 'pointer';
      fileRow.onclick = (e) => {
        e.stopPropagation();
        window.MTApi.openFile(projectId, data.file)
          .then(() => toastMsg('已用系统程序打开 ' + data.file))
          .catch((err) => mtAlert('打开失败：' + err.message));
      };
    }
    const docRow = el.querySelector('.docrow');
    if (docRow && data.assets && data.assets.length) {
      docRow.onclick = (e) => {
        e.stopPropagation();
        const a = data.assets[0];
        window.MTApi.openFile(projectId, a.path)
          .then(() => toastMsg('已打开 ' + a.name))
          .catch((err) => mtAlert('打开失败：' + err.message));
      };
    }
    const badge = el.querySelector('.asset-badge');
    if (badge) {
      badge.onclick = (e) => {
        e.stopPropagation();
        const live = byId(data.id);
        if (live) showAssetPopover(live, badge);
      };
    }

    if (!data.floating) {
      const down = document.createElement('div');
      down.className = 'branch-handle down';
      down.textContent = '＋';
      down.title = '新增分支节点（挂在下面）';
      down.onclick = (e) => { e.stopPropagation(); addChild(data.id, 'v'); };
      el.append(down);

      if (layout === 'timeline' && data.id !== graph.trunk.id) {
        const right = document.createElement('div');
        right.className = 'branch-handle right';
        right.textContent = '▸';
        right.title = '新增主干节点（接在右边）';
        right.onclick = (e) => { e.stopPropagation(); addChild(data.id, 'h'); };
        el.append(right);
      }
    }
    el.style.left = data.x + 'px';
    el.style.top = data.y + 'px';
  }
  requestAnimationFrame(() => {
    let changed = false;
    for (const el of canvas.querySelectorAll('.node')) {
      const d = byId(el.dataset.id);
      if (!d || d.floating) continue;
      const w = Math.round(el.offsetWidth);
      const h = Math.round(el.offsetHeight);
      if (w && h && (d._w !== w || d._h !== h)) { d._w = w; d._h = h; changed = true; }
    }
    if (changed && measurePass < 2) {
      measurePass += 1;
      render();
      return;
    }
    measurePass = 0;
    drawEdges();
    startEdgeFollow();
  });
  renderStage();
  autoFitSoon();      // 数据/布局变了就重新适配（用户手动平移/缩放过就不打扰）
  // 布局有 460ms 的过渡动画，渲染那一瞬间节点还在路上，
  // 这时画出来的连线会是错的（悬空、穿节点）。等动画结束再补画一次。
  clearTimeout(window.__edgeSettleTimer);
  window.__edgeSettleTimer = setTimeout(() => {
    if (dragState || panState) return;   // 正在拖就别抢
    try { drawEdges(); } catch (e) { /* 忽略 */ }
  }, 480);
}

/* 节点尺寸：先用估算值排版，渲染完拿真实 DOM 尺寸回填。
   估算值和实际差得远会让分支线被父子框盖住，所以必须有这一步回测。 */
/* ---------------------------------------------------------- 应用内对话框
   不用原生 confirm/alert：用户一旦勾了"阻止此页面创建更多对话框"，
   原生 confirm 会直接返回 false，删除等操作就"点了没反应"。 */
function mtDialog(opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'mt-modal';
    host.innerHTML =
      '<div class="mt-modal-box">' +
        '<div class="mt-modal-title"></div>' +
        '<div class="mt-modal-body"></div>' +
        '<div class="mt-modal-row">' +
          '<button class="btn text" data-act="cancel"></button>' +
          '<button class="btn" data-act="ok"></button>' +
        '</div>' +
      '</div>';
    host.querySelector('.mt-modal-title').textContent = o.title || '确认';
    host.querySelector('.mt-modal-body').textContent = o.body || '';
    const okBtn = host.querySelector('[data-act="ok"]');
    const cancelBtn = host.querySelector('[data-act="cancel"]');
    okBtn.textContent = o.okText || '确定';
    cancelBtn.textContent = o.cancelText || '取消';
    if (o.danger) okBtn.style.background = '#ffb4ab';
    document.body.append(host);
    const close = (val) => { host.remove(); resolve(val); };
    okBtn.onclick = () => close(true);
    cancelBtn.onclick = () => close(false);
    host.onclick = (e) => { if (e.target === host) close(false); };
    host.querySelector('.mt-modal-box').setAttribute('tabindex', '-1');
    setTimeout(() => host.querySelector('.mt-modal-box').focus(), 30);
  });
}

function mtAlert(title, body) {
  // 单参数时当作正文，标题用「提示」
  if (body === undefined) return mtDialog({ title: '提示', body: String(title), okText: '知道了', cancelText: '关闭' });
  return mtDialog({ title, body, okText: '知道了', cancelText: '关闭' });
}

/** 带输入框的对话框，返回输入值；取消返回 null */
function mtPrompt(title, defaultValue, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'mt-modal';
    host.innerHTML = '<div class="mt-modal-box"><div class="mt-modal-title"></div>' +
      '<div class="mt-modal-body"></div>' +
      '<input class="input mt-modal-input">' +
      '<div class="mt-modal-row"><button class="btn text" data-act="cancel"></button>' +
      '<button class="btn" data-act="ok"></button></div></div>';
    host.querySelector('.mt-modal-title').textContent = title || '请输入';
    const bodyEl = host.querySelector('.mt-modal-body');
    if (o.body) bodyEl.textContent = o.body; else bodyEl.style.display = 'none';
    const input = host.querySelector('.mt-modal-input');
    input.value = defaultValue === undefined ? '' : String(defaultValue);
    host.querySelector('[data-act="ok"]').textContent = o.okText || '确定';
    host.querySelector('[data-act="cancel"]').textContent = o.cancelText || '取消';
    document.body.append(host);
    const close = (v) => { host.remove(); resolve(v); };
    host.querySelector('[data-act="ok"]').onclick = () => close(input.value);
    host.querySelector('[data-act="cancel"]').onclick = () => close(null);
    host.onclick = (e) => { if (e.target === host) close(null); };
    setTimeout(() => { input.focus(); input.select(); }, 40);
  });
}

/* 编辑框里有没有未保存的输入：有的话 Ctrl+Z 交给浏览器的文本撤销，
   没有的话 Ctrl+Z 就是画布撤销（否则点了节点之后撤销键会像失灵一样）。 */
let editorDirty = false;

/* ---------------------------------------------------------- 撤销 / 重做
   每一步改动前把结构拍个快照；涉及删除文件的动作，还会把文件内容一起记下来，
   撤销时连同文件一起还原。 */
const undoStack = [];
const redoStack = [];
const UNDO_LIMIT = 40;

function cloneGraph() {
  return JSON.parse(JSON.stringify({
    trunk: graph.trunk,
    nodes: graph.nodes,
    staged: graph.staged,
    links: graph.links || [],
    freeLayout: graph.freeLayout,
  }));
}

/** 在改动前调用。extra.files = {路径: 内容}，撤销时会把这些文件写回去 */
function pushUndo(label, extra) {
  undoStack.push({
    label: label || '操作',
    graph: cloneGraph(),
    files: (extra && extra.files) || null,
  });
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack.length = 0;
  updateUndoButtons();
}

function updateUndoButtons() {
  const u = document.getElementById('undoBtn');
  const r = document.getElementById('redoBtn');
  // 不用 disabled：禁用状态点了完全没反应，用户会以为坏了。
  // 改成变淡 + 点了给一句提示。
  if (u) {
    u.disabled = false;      // HTML 里写了 disabled 属性，不清掉按钮永远是禁用的（点了没反应）
    u.classList.toggle('is-empty', !undoStack.length);
    u.title = undoStack.length ? '撤销：' + undoStack[undoStack.length - 1].label : '没有可撤销的操作';
  }
  if (r) {
    r.disabled = false;
    r.classList.toggle('is-empty', !redoStack.length);
    r.title = redoStack.length ? '恢复：' + redoStack[redoStack.length - 1].label : '没有可恢复的操作';
  }
}

function applySnapshot(snap) {
  const g = snap.graph;
  graph.trunk = JSON.parse(JSON.stringify(g.trunk));
  graph.nodes = JSON.parse(JSON.stringify(g.nodes));
  graph.staged = JSON.parse(JSON.stringify(g.staged));
  graph.links = JSON.parse(JSON.stringify(g.links || []));
  graph.freeLayout = g.freeLayout;
  markDirty();
  render();
}

/** 把快照里记录的文件内容写回去 */
async function restoreFiles(snap) {
  if (!snap || !snap.files || !window.MTApi) return 0;
  let n = 0;
  for (const [path, content] of Object.entries(snap.files)) {
    try { await window.MTApi.assetWrite(projectId, path, content); n += 1; } catch (e) { /* 忽略单个失败 */ }
  }
  return n;
}

async function doUndo() {
  if (!undoStack.length) { toastMsg('没有可撤销的操作'); return; }
  const snap = undoStack.pop();
  redoStack.push({ label: snap.label, graph: cloneGraph(), files: null });
  applySnapshot(snap);
  await saveGraphNow();
  const n = await restoreFiles(snap);
  if (n) { render(); toastMsg('已撤销「' + snap.label + '」，并还原了 ' + n + ' 个文件', 3000); }
  else toastMsg('已撤销「' + snap.label + '」');
  updateUndoButtons();
}

async function doRedo() {
  if (!redoStack.length) { toastMsg('没有可恢复的操作'); return; }
  const snap = redoStack.pop();
  undoStack.push({ label: snap.label, graph: cloneGraph(), files: null });
  applySnapshot(snap);
  await saveGraphNow();
  toastMsg('已恢复「' + snap.label + '」');
  updateUndoButtons();
}

window.addEventListener('keydown', (e) => {
  const tag = document.activeElement && document.activeElement.tagName;
  const inField = tag === 'INPUT' || tag === 'TEXTAREA';
  if (inField && editorDirty) return;   // 正在改正文：让浏览器做文本撤销
  const mod = e.ctrlKey || e.metaKey;
  if (!mod) return;
  if (e.key === 'z' || e.key === 'Z') {
    e.preventDefault();
    if (e.shiftKey) doRedo(); else doUndo();
  } else if (e.key === 'y' || e.key === 'Y') {
    e.preventDefault();
    doRedo();
  }
});

let measurePass = 0;

const sizeOf = (n) => {
  if (layout === 'mindmap') {
    return { w: 104, h: 104 };
  }
  if (n._w && n._h) return { w: n._w, h: n._h };
  const title = String(n.title || '');
  const summary = String(n.summary || '');
  return {
    w: Math.min(190, Math.max(120, 40 + title.length * 20, summary ? 170 : 0)),
    h: summary ? 92 : 44,
  };
};

const labelsSvg = document.getElementById('edgeLabels');

/** 取节点当前的屏幕内布局位置（受 CSS 过渡影响，能反映回弹过程） */
function nodeAnchor(n) {
  const el = nodeEl(n.id);
  if (!el) return { x: n.x, y: n.y, w: sizeOf(n).w, h: sizeOf(n).h };
  const w = el.offsetWidth || sizeOf(n).w;
  const h = el.offsetHeight || sizeOf(n).h;
  return { x: el.offsetLeft, y: el.offsetTop, w: w, h: h };
}

/** 节点位置变化有 460ms 过渡，这期间每帧重画连线，否则线会"甩"到终点 */
let edgeFollowUntil = 0;
function startEdgeFollow() {
  edgeFollowUntil = performance.now() + 520;
  if (window.__edgeFollowRunning) return;
  window.__edgeFollowRunning = true;
  const tick = () => {
    if (performance.now() > edgeFollowUntil) { window.__edgeFollowRunning = false; drawEdges(); return; }
    drawEdges();
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/** 给线加一条透明的粗路径，2px 的线用户根本点不中 */
function addEdgeHit(d, onContext, onClick, onHover) {
  const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  hit.setAttribute('d', d);
  hit.setAttribute('class', 'edge-hit');
  if (onContext) hit.addEventListener('contextmenu', onContext);
  if (onClick) hit.addEventListener('click', onClick);
  if (onHover) {
    hit.addEventListener('mousemove', onHover);
    hit.addEventListener('mouseleave', hideEdgeTip);
  }
  edgesSvg.append(hit);
  return hit;
}

/* 线的小工具条：鼠标移上去出现，点「说明」改文字，点「删除」断线 */
let edgeTip = null;
function hideEdgeTip() {
  if (edgeTip) { edgeTip.remove(); edgeTip = null; }
}
let edgeTipTimer = null;

function showEdgeTip(x, y, kind, target) {
  hideEdgeTip();
  const box = document.createElement('div');
  box.className = 'edge-tip';
  const mk = (label, cls, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = (ev) => { ev.stopPropagation(); hideEdgeTip(); fn(); };
    box.append(b);
  };
  mk('✎ 说明', '', () => editEdgeLabel(kind, target));
  mk('✕ 删除', 'danger', () => breakConnection(kind, target));
  document.body.append(box);
  box.style.left = Math.min(x, window.innerWidth - box.offsetWidth - 10) + 'px';
  box.style.top = Math.max(10, y - 40) + 'px';
  edgeTip = box;
  // 3 秒没动就自己收起来，避免长期赖在画面上
  clearTimeout(edgeTipTimer);
  edgeTipTimer = setTimeout(hideEdgeTip, 3000);
}

// 点别处、移出画布、滚轮缩放，都收起线的工具条
window.addEventListener('mousedown', (e) => {
  // e.target 可能不是 DOM 节点（比如直接在 window 上派发），要防一下
  const inside = e.target instanceof Node && edgeTip && edgeTip.contains(e.target);
  if (edgeTip && !inside) hideEdgeTip();
}, true);
window.addEventListener('wheel', () => hideEdgeTip(), { passive: true });
document.addEventListener('mouseleave', () => hideEdgeTip());

function drawEdges() {
  hideEdgeTip();          // 线重画了，旧的鼠标离开事件不会来，得主动收起
  edgesSvg.innerHTML = '';
  if (labelsSvg) labelsSvg.innerHTML = '';
  drawLinks();   // 关联线先画，压在结构线下面
  const useDom = window.__edgeFollowRunning;   // 回弹动画期间按真实位置画
  for (const n of graph.nodes) {
    const parent = n.parent ? byId(n.parent) : null;
    if (!parent) continue;
    const pa = useDom ? nodeAnchor(parent) : { x: parent.x, y: parent.y, w: sizeOf(parent).w, h: sizeOf(parent).h };
    const ca = useDom ? nodeAnchor(n) : { x: n.x, y: n.y, w: sizeOf(n).w, h: sizeOf(n).h };
    let d;
    if (n.axis === 'v' && layout === 'timeline') {
      // 垂直分支：从父节点下沿一直竖着拉，节点挂在这条线上
      d = 'M ' + pa.x + ' ' + (pa.y + pa.h / 2) + ' L ' + ca.x + ' ' + (ca.y - ca.h / 2);
    } else if (layout === 'timeline') {
      // 子节点在右边：父右沿 → 子左沿；在左边则反过来，
      // 否则曲线会从父节点身体里穿过去，看着像断线。
      const forward = ca.x >= pa.x;
      const x1 = forward ? pa.x + pa.w / 2 : pa.x - pa.w / 2;
      const y1 = pa.y;
      const x2 = forward ? ca.x - ca.w / 2 : ca.x + ca.w / 2;
      const y2 = ca.y;
      const k = Math.max(34, Math.abs(x2 - x1) * 0.45) * (forward ? 1 : -1);
      d = 'M ' + x1 + ' ' + y1 + ' C ' + (x1 + k) + ' ' + y1 + ', ' + (x2 - k) + ' ' + y2 + ', ' + x2 + ' ' + y2;
    } else {
      // 导图：沿两点连线方向，从圆周边缘出去
      const dx = ca.x - pa.x, dy = ca.y - pa.y;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len, uy = dy / len;
      const r1 = pa.w / 2, r2 = ca.w / 2;
      const x1 = pa.x + ux * r1, y1 = pa.y + uy * r1;
      const x2 = ca.x - ux * r2, y2 = ca.y - uy * r2;
      d = 'M ' + x1 + ' ' + y1 + ' L ' + x2 + ' ' + y2;
    }
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    const onCtx = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showEdgeMenu(ev.clientX, ev.clientY, 'structural', { node: n });
    };
    path.addEventListener('contextmenu', onCtx);

    const midX = (pa.x + ca.x) / 2;
    const midY = (pa.y + ca.y) / 2;
    const onEnter = (ev) => showEdgeTip(ev.clientX, ev.clientY, 'structural', { node: n });
    path.addEventListener('mouseenter', onEnter);
    path.addEventListener('click', (ev) => { ev.stopPropagation(); editEdgeLabel('structural', { node: n }); });
    let cls = 'edge ' + (n.axis === 'v' ? 'branch' : 'main');
    // 从分支再接出来的主线：用另一种线型，提示这段主线是从支线延伸的
    if (n.axis === 'h' && parent.axis === 'v') cls += ' main-from-branch';
    path.setAttribute('class', cls);
    edgesSvg.append(path);
    addEdgeHit(d, onCtx, (ev) => { ev.stopPropagation(); editEdgeLabel('structural', { node: n }); },
      (ev) => showEdgeTip(ev.clientX, ev.clientY, 'structural', { node: n }));

    // 结构线的说明：挂在父子连线中点
    if (n.edgeLabel) {
      const midX = (pa.x + ca.x) / 2;
      const midY = (pa.y + ca.y) / 2;
      const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      t.setAttribute('x', midX);
      t.setAttribute('y', midY);
      t.setAttribute('class', 'edge-label structural' + (n.axis === 'v' ? ' is-branch' : ''));
      t.textContent = n.edgeLabel;
      t.addEventListener('contextmenu', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        showEdgeMenu(ev.clientX, ev.clientY, 'structural', { node: n });
      });
      (labelsSvg || edgesSvg).append(t);
    }
  }
}

document.getElementById('stageAddText').addEventListener('click', async () => {
  const title = await mtPrompt('文本框标题', '新文本框');
  if (title === null) return;
  graph.staged.push({ id: 's' + Date.now(), title: (title || '').trim() || '新文本框' });
  markDirty();
  render();
});

document.getElementById('stageAddFile').addEventListener('click', async () => {
  const btn = document.getElementById('stageAddFile');
  const old = btn.textContent;
  try {
    const picked = await pickFilePath((busy) => {
      btn.textContent = busy ? '等待选择…' : old;
      btn.disabled = busy;
    });
    if (!picked) return;
    const r = await window.MTApi.stagedAddFile(projectId, picked);
    graph.staged.push({
      id: 's' + Date.now(), title: r.name, path: r.path, size: r.size,
    });
    markDirty();
    render();
    toastMsg('已复制到项目：' + r.path + '，拖到画布上即可挂到节点');
  } catch (e) {
    mtAlert('选择文件失败：' + e.message);
  } finally {
    btn.textContent = old;
    btn.disabled = false;
  }
});

function renderStage() {
  stageItems.innerHTML = '';
  document.getElementById('stageCount').textContent = graph.staged.length || '';
  if (!graph.staged.length) {
    const p = document.createElement('div');
    p.className = 'hint';
    p.textContent = '暂存区是空的';
    stageItems.append(p);
    return;
  }
  for (const item of graph.staged) {
    const el = document.createElement('div');
    el.className = 'item';
    el.textContent = item.title;
    if (item.size) {
      const s = item.size > 1048576 ? (item.size / 1048576).toFixed(1) + ' MB'
        : Math.max(1, Math.round(item.size / 1024)) + ' KB';
      const tag = document.createElement('span');
      tag.className = 'stage-size';
      tag.textContent = s;
      el.append(tag);
    }
    el.draggable = true;
    el.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', item.id));
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'stage-del';
    del.textContent = '×';
    del.title = '从暂存区移除';
    del.setAttribute('aria-label', '从暂存区移除 ' + item.title);
    del.onclick = async (ev) => {
      ev.stopPropagation();
      graph.staged = graph.staged.filter((s) => s.id !== item.id);
      if (online && window.MTApi) {
        try { await window.MTApi.stagedRemove(projectId, item.id); } catch (e) { /* 忽略 */ }
      }
      markDirty(true);
      render();
    };
    el.append(del);
    stageItems.append(el);
  }
}

/* ---------------------------------------------------------- 操作 */
/** 问一句这个新节点算主干还是分支；取消返回 null */
function askNodeKind() {
  // 不用 prompt 了：两个按钮点一下就行，比让用户敲 1/2 清楚
  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'mt-modal';
    host.innerHTML = '<div class="mt-modal-box"><div class="mt-modal-title">新节点算什么？</div>' +
      '<div class="mt-modal-body">主干节点：横着接在右边，延续主线\n分支节点：挂在下面，另起一段</div>' +
      '<div class="mt-modal-row"><button class="btn text" data-act="main">主干</button>' +
      '<button class="btn text" data-act="branch">分支</button>' +
      '<button class="btn" data-act="cancel">取消</button></div></div>';
    document.body.append(host);
    const close = (v) => { host.remove(); resolve(v); };
    host.querySelector('[data-act="main"]').onclick = () => close('h');
    host.querySelector('[data-act="branch"]').onclick = () => close('v');
    host.querySelector('[data-act="cancel"]').onclick = () => close(null);
    host.onclick = (e) => { if (e.target === host) close(null); };
  });
}

function addChild(parentId, axis) {
  pushUndo(axis === 'v' ? '新增分支节点' : '新增主干节点');
  const id = 'n' + Date.now();
  graph.nodes.push({ id, title: axis === 'v' ? '新分支' : '主线节点', parent: parentId, axis });
  markDirty();
  render();
  const fresh = graph.nodes.find((n) => n.id === id);
  // 等一帧，让布局算出位置再判断要不要挪视图
  requestAnimationFrame(() => { ensureNodeVisible(fresh); openEditor(fresh); });
}


/** 从主干到当前节点，把整条父链高亮出来 */
function highlightPath(id) {
  for (const el of canvas.querySelectorAll('.node')) el.classList.remove('on-path');
  for (const p of canvas.querySelectorAll('#edges path')) p.classList.remove('on-path');

  if (!id) return;
  const chain = new Set();
  let cur = byId(id);
  let guard = 0;
  while (cur && guard < 200) {
    chain.add(cur.id);
    cur = cur.parent ? byId(cur.parent) : null;
    guard += 1;
  }
  for (const el of canvas.querySelectorAll('.node')) {
    if (chain.has(el.dataset.id)) el.classList.add('on-path');
  }
  for (const n of graph.nodes) {
    if (!chain.has(n.id) || !n.parent || !chain.has(n.parent)) continue;
    const parentIdx = graph.nodes.indexOf(n);
    const paths = canvas.querySelectorAll('#edges path');
    // 依次对应 drawEdges 里的顺序：节点数组里每个有父节点的节点各一条
    let k = -1;
    for (const m of graph.nodes) {
      if (!m.parent) continue;
      k += 1;
      if (m.id === n.id) {
        const p = paths[k];
        if (p) p.classList.add('on-path');
        break;
      }
    }
  }
}

function select(id) {
  selected = id;
  for (const el of canvas.querySelectorAll('.node')) {
    el.classList.toggle('selected', el.dataset.id === id);
  }
  highlightPath(id);
}
canvas.addEventListener('click', () => {
  select(null);
  showEditorEmpty();
});

// 选中节点后按 Enter / F2 打开编辑面板
window.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== 'F2') return;
  if (document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (!selected) return;
  const node = byId(selected);
  if (!node) return;
  e.preventDefault();
  openEditor(node);
});

let dirty = false;
let saveTimer = null;

/** 把当前画布结构立刻写到服务端（只让结构落盘） */
async function saveGraphNow() {
  if (!(online && window.MTApi)) return;
  try {
    await window.MTApi.saveGraph(projectId, {
      trunk: graph.trunk,
      nodes: graph.nodes,
      staged: graph.staged,
      links: graph.links,
      freeLayout: graph.freeLayout,
    });
    dirty = false;
    saveBtn.disabled = true;
    dot.classList.remove('on');
  } catch (e) {
    console.warn('保存结构失败：' + e.message);
  }
}

/** 改动后自动落盘：很多服务端操作（并入/删除/挂文件）读的是磁盘上的结构，
 *  前端改完不写回去，服务端就会看到旧数据，报"没有父节点"这类莫名其妙的错。 */
function scheduleSave() {
  if (!(online && window.MTApi)) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveGraphNow, 1200);
}

function markDirty(on = true) {
  dirty = on;
  saveBtn.disabled = !on;
  dot.classList.toggle('on', on);
  if (on) scheduleSave();
}

saveBtn.addEventListener('click', async () => {
  if (online && window.MTApi) {
    try {
      const r = await window.MTApi.commitNow(projectId);
      toastMsg(r.committed ? '已保存版本' : ('没有需要保存的改动：' + (r.reason || '')));
      markDirty(false);
    } catch (e) { toastMsg('保存失败：' + e.message); }
    return;
  }
  const snapshot = { trunk: graph.trunk, nodes: graph.nodes, staged: graph.staged, links: graph.links, freeLayout: graph.freeLayout };
  if (online && window.MTApi) {
    try {
      const r = await window.MTApi.commit(projectId, snapshot, 'mt: 保存画布改动');
      markDirty(false);
      if (!r.committed) { toastMsg('没有改动，不用保存'); return; }
      toastMsg('已保存版本 ' + String(r.sha).slice(0, 7));
      return;
    } catch (e) {
      toastMsg('保存失败：' + e.message);
      return;
    }
  }
  markDirty(false);
  const t = document.createElement('div');
  t.textContent = '已保存';
  t.style.cssText = 'position:fixed;left:50%;bottom:26px;transform:translateX(-50%);' +
    'background:#1d2e29;color:#dfe9e4;padding:9px 16px;border-radius:999px;font-size:12.5px;z-index:200';
  document.body.append(t);
  setTimeout(() => t.remove(), 1600);
});

/* 双击画布空白处新建浮空节点（导图模式下也能加节点） */
// 双击空白处不再创建节点。
// 以前双击画布会凭空多一个「新节点」—— 想拖动背景时手一抖就多出来一个，
// 还得手动删掉。要加节点请用底部「＋ 添加节点」或节点上的＋号。
canvas.addEventListener('dblclick', (e) => {
  if (e.target.closest('.node')) return;
  e.preventDefault();
});

/* ---------------------------------------------------------- 拖拽 */
/* ---------------------------------------------------------- 线的右键菜单
   结构线（父子）和关联线都可以加说明、也可以直接断开。 */
async function editEdgeLabel(kind, target) {
  const current = kind === 'link' ? (target.link.label || '') : (target.node.edgeLabel || '');
  const next = await mtPrompt('给这条线加个说明', current, { body: '留空表示清除说明', okText: '保存' });
  if (next === null) return;
  pushUndo('修改线的说明');
  const text = next.trim().slice(0, 24);
  if (kind === 'link') {
    target.link.label = text;
  } else {
    if (text) target.node.edgeLabel = text; else delete target.node.edgeLabel;
  }
  markDirty();
  render();
}

async function breakConnection(kind, target) {
  if (kind === 'link') {
    if (!(await mtDialog({ title: '删除关联', body: '断开这个关联？', okText: '删除', danger: true }))) return;
    pushUndo('删除关联');
    graph.links = (graph.links || []).filter((l) => l.id !== target.link.id);
  } else {
    if (!(await mtDialog({ title: '断开连接', body: '断开「' + (target.node.title || '节点') + '」和父节点的连接？它会变成浮空节点。', okText: '断开' }))) return;
    pushUndo('断开连接');
    delete target.node.parent;
    delete target.node.axis;
    target.node.parent = null;
    target.node.floating = true;
    if (!graph.freeLayout) {
      graph.freeLayout = true;
      const btn = document.getElementById('layoutToggle');
      if (btn) btn.textContent = '自动排版：关';
    }
  }
  markDirty();
  render();
}

function showEdgeMenu(x, y, kind, target) {
  closeCtxMenu();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.id = 'ctxMenu';
  const at = (label, cls, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = () => { closeCtxMenu(); fn(); };
    menu.append(b);
  };
  at('编辑说明', '', () => editEdgeLabel(kind, target));
  at(kind === 'link' ? '删除这条关联' : '断开这条连接', 'danger', () => breakConnection(kind, target));
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
}

/* ---------------------------------------------------------- 右键菜单
   把手柄上那些「＋ / → / 关联」都收进菜单，避免节点上挂太多小按钮。 */
function closeCtxMenu() {
  const old = document.getElementById('ctxMenu');
  if (old) old.remove();
}

function showCtxMenu(x, y, node) {
  closeCtxMenu();
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.id = 'ctxMenu';
  const at = (label, cls, fn) => {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = () => { closeCtxMenu(); fn(); };
    menu.append(b);
  };
  const sep = () => {
    const s = document.createElement('div');
    s.className = 'sep';
    menu.append(s);
  };

  if (!node) {
    at('＋ 新建文本框', '', () => {
      const rect = canvas.getBoundingClientRect();
      const cx = (x - rect.left - view.x) / view.scale;
      const cy = (y - rect.top - view.y) / view.scale;
      const id = 'n' + Date.now();
      graph.nodes.push({ id, title: '新节点', note: '浮空', parent: null, axis: null,
                         floating: true, x: cx, y: cy });
      markDirty(); render();
      openEditor(graph.nodes.find((n) => n.id === id));
    });
    at('适应内容', '', () => fitView());
    at('视图复位', '', () => { view = { x: 0, y: 0, scale: 1 }; applyView(); });
  } else {
    at('编辑', '', () => openEditor(node));
    at('新增分支节点（挂在下方）', '', () => addChild(node.id, 'v'));
    if (node.id !== graph.trunk.id) at('新增主干节点（接在右侧）', '', () => addChild(node.id, 'h'));
    if ((node.assets || []).length) {
      at('查看挂件（' + node.assets.length + '）', '', () => {
        const el = nodeEl(node.id);
        showAssetPopover(node, el ? el.querySelector('.asset-badge') || el : canvas);
      });
    }
    if (node.file) {
      at('打开正文文件', '', () => {
        window.MTApi.openFile(projectId, node.file)
          .then(() => toastMsg('已打开 ' + node.file))
          .catch((e) => mtAlert('打开失败：' + e.message));
      });
    }
    at('关联到其它节点…', '', () => openLinkMode(node));
    if (node.id !== graph.trunk.id) {
      at('并入父节点（变成挂件）', '', () => mergeIntoParent(node));
    }
    if (node.floating) at('设为浮空', '', () => { /* 已经是浮空 */ });
    sep();
    if (node.id !== graph.trunk.id) {
      at('脱离父节点（变浮空）', '', () => {
        node.parent = null; node.axis = null; node.floating = true;
        markDirty(); render();
      });
      at('删除节点', 'danger', () => {
        editingId = node.id;
        document.getElementById('editorDelete').click();
      });
    }
  }
  document.body.append(menu);
  // 贴边时往回收一点，别跑出窗口
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - r.width - 8) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - r.height - 8) + 'px';
}

window.addEventListener('click', closeCtxMenu);
window.addEventListener('blur', closeCtxMenu);

/** 把视图缩放到刚好框住所有节点 */
function fitView(padding = 80) {
  // 编辑栏可以拖得很宽，画布就会变得很窄。
  // 这里必须用"能放下全图"的缩放，否则节点会被挤出可视区；
  // 只在放得下的前提下才尽量放大（最大 1.3 倍）。
  // 浮空节点也要算进来：它们同样占地方，漏掉就会出现
  // "适配之后还有节点在可视区外面"的情况。
  const list = [graph.trunk, ...graph.nodes]
    .filter((n) => n && typeof n.x === 'number' && typeof n.y === 'number');
  if (!list.length) { view = { x: 0, y: 0, scale: 1 }; applyView(); return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of list) {
    // 优先用真实 DOM 尺寸：估算值对长摘要/长标题会偏小，
    // 按估算算出来的缩放会把边缘节点挤出画布几个像素。
    const dom = nodeEl(n.id);
    const s = dom && dom.offsetWidth
      ? { w: dom.offsetWidth, h: dom.offsetHeight }
      : sizeOf(n);
    minX = Math.min(minX, n.x - s.w / 2);
    minY = Math.min(minY, n.y - s.h / 2);
    maxX = Math.max(maxX, n.x + s.w / 2);
    maxY = Math.max(maxY, n.y + s.h / 2);
  }
  // 注意：不能量 #canvas —— 它自己带 transform: scale，量出来的是缩放后的尺寸，
  // 每适配一次就小一圈（实测 0.53 → 0.22 一路缩下去）。要用外层未缩放的容器。
  const wrapEl = document.getElementById('wrap');
  const rect = (wrapEl || canvas).getBoundingClientRect();
  const bw = Math.max(1, maxX - minX);
  const bh = Math.max(1, maxY - minY);
  const need = Math.min((rect.width - padding * 2) / bw, (rect.height - padding * 2) / bh);
  const scale = Math.min(1.3, Math.max(0.2, need));
  let fitScale = scale;
  view.scale = fitScale;
  view.x = (rect.width - bw * fitScale) / 2 - minX * fitScale;
  view.y = (rect.height - bh * fitScale) / 2 - minY * fitScale;
  applyView();

  // 复核一遍：模型尺寸和真实 DOM 尺寸对不上时（比如摘要还没渲染出来），
  // 上面算的缩放会让边上的节点露在外面。溢出就缩一点再来。
  for (let tries = 0; tries < 6; tries++) {
    const box = (document.getElementById('wrap') || canvas).getBoundingClientRect();
    let overflow = false;
    for (const n of list) {
      const el = nodeEl(n.id);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.left < box.left + 2 || r.right > box.right - 2) { overflow = true; break; }
    }
    if (!overflow) break;
    fitScale = Math.max(0.2, fitScale * 0.9);
    view.scale = fitScale;
    view.x = (rect.width - bw * fitScale) / 2 - minX * fitScale;
    view.y = (rect.height - bh * fitScale) / 2 - minY * fitScale;
    applyView();
  }
}

/* ---------------------------------------------------------- 节点之间的关联线
   父子关系决定结构（主线/分支），关联线只表示「这两个节点有关系」，不影响结构。 */
let linkModeFrom = null;

function openLinkMode(node) {
  linkModeFrom = node.id;
  closeEditor();
  canvas.classList.add('linking');
  const src = nodeEl(node.id);
  if (src) src.classList.add('link-source');
  toastMsg('关联模式：连着点多个节点都能建立关联；再点一次自己或按 Esc 结束', 5000);
}

function endLinkMode(quiet) {
  if (!linkModeFrom) return;
  const src = nodeEl(linkModeFrom);
  if (src) src.classList.remove('link-source');
  linkModeFrom = null;
  canvas.classList.remove('linking');
  if (!quiet) toastMsg('已结束关联模式');
}

function finishLink(targetId) {
  const from = linkModeFrom;
  if (!from) return;
  // 再点一次自己 = 结束关联模式
  if (from === targetId) { endLinkMode(); return; }

  graph.links = graph.links || [];
  pushUndo('建立关联');
  const exists = graph.links.some((l) =>
    (l.from === from && l.to === targetId) || (l.from === targetId && l.to === from));
  if (exists) { toastMsg('这两个节点已经有关联了，可以接着点别的'); return; }

  graph.links.push({ id: 'l' + Date.now(), from: from, to: targetId, label: '' });
  markDirty();
  render();

  // 保持关联模式：可以继续连下一个
  const target = byId(targetId);
  const name = target ? (target.title || '节点') : '节点';
  toastMsg('已关联到「' + name + '」，继续点其它节点；点自己或 Esc 结束', 4000);
  const src = nodeEl(from);
  if (src) src.classList.add('link-source');
}

/** 从节点中心朝 (tx,ty) 方向射出，返回与节点矩形边框的交点。
 *  连线必须落在边框上，否则会从节点身上穿过去。 */
function edgeAnchor(node, tx, ty, size) {
  const dx = tx - node.x, dy = ty - node.y;
  if (!dx && !dy) return { x: node.x, y: node.y };
  const halfW = size.w / 2, halfH = size.h / 2;
  const scaleX = dx ? halfW / Math.abs(dx) : Infinity;
  const scaleY = dy ? halfH / Math.abs(dy) : Infinity;
  const t = Math.min(scaleX, scaleY);
  return { x: node.x + dx * t, y: node.y + dy * t };
}

function drawLinks() {
  if (!graph.links || !graph.links.length) return;
  for (const link of graph.links) {
    const a = byId(link.from);
    const b = byId(link.to);
    if (!a || !b) continue;
    const sa = sizeOf(a), sb = sizeOf(b);
    const ax = a.x, ay = a.y, bx = b.x, by = b.y;
    // 弧线往外拱，避免和直线关系叠在一起
    const mx = (ax + bx) / 2, my = (ay + by) / 2;
    const dx = bx - ax, dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    const bow = Math.min(90, len * 0.18);
    const cx = mx - (dy / len) * bow;
    const cy = my + (dx / len) * bow;
    // 两端收在节点边框上，别从节点身体里穿出来
    const startPoint = edgeAnchor(a, cx, cy, sa);
    const endPoint = edgeAnchor(b, cx, cy, sb);
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M' + startPoint.x + ',' + startPoint.y + ' Q' + cx + ',' + cy + ' ' + endPoint.x + ',' + endPoint.y);
    path.setAttribute('class', 'edge link');
    path.dataset.linkId = link.id;
    const onCtx = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showEdgeMenu(ev.clientX, ev.clientY, 'link', { link: link });
    };
    const onClick = (ev) => {
      ev.stopPropagation();
      editEdgeLabel('link', { link: link });
    };
    path.addEventListener('contextmenu', onCtx);
    path.addEventListener('click', onClick);
    edgesSvg.append(path);
    addEdgeHit(path.getAttribute('d'), onCtx, onClick,
      (ev) => showEdgeTip(ev.clientX, ev.clientY, 'link', { link: link }));

    // 标签：曲线中点，点一下可以改文字
    const qx = 0.25 * ax + 0.5 * cx + 0.25 * bx;
    const qy = 0.25 * ay + 0.5 * cy + 0.25 * by;
    // 没有说明文字就什么都不画（不显示占位提示）
    if (!link.label) { edgesSvg.append(path); return; }
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', qx);
    text.setAttribute('y', qy);
    text.setAttribute('class', 'edge-label');
    text.textContent = link.label;
    text.addEventListener('click', (ev) => {
      ev.stopPropagation();
      editEdgeLabel('link', { link: link });
    });
    text.addEventListener('contextmenu', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showEdgeMenu(ev.clientX, ev.clientY, 'link', { link: link });
    });
    (labelsSvg || edgesSvg).append(text);
  }
}

/* ---------------------------------------------------------- 附件角标弹层 */
function closeAssetPopover() {
  const old = document.getElementById('assetPop');
  if (old) old.remove();
}

function showAssetPopover(node, anchorEl) {
  closeAssetPopover();
  const box = document.createElement('div');
  box.id = 'assetPop';
  box.className = 'asset-pop';
  const title = document.createElement('div');
  title.className = 'hint';
  title.textContent = node.title + ' · ' + (node.assets || []).length + ' 个文件';
  box.append(title);
  for (const a of node.assets || []) {
    const row = document.createElement('div');
    row.className = 'asset-pop-row';
    const size = a.size > 1048576 ? (a.size / 1048576).toFixed(1) + ' MB'
      : Math.round(a.size / 1024) + ' KB';
    row.innerHTML = '<span class="asset-pop-name">' + a.name + '</span><span class="asset-pop-size">' + size + '</span>';
    row.title = '点一下用系统程序打开';
    row.onclick = () => window.MTApi.openFile(projectId, a.path)
      .then(() => toastMsg('已打开 ' + a.name))
      .catch((e) => mtAlert('打不开：' + e.message));
    if (/\.(md|txt|json|csv|ya?ml)$/i.test(a.name)) {
      const edit = document.createElement('button');
      edit.className = 'mini-btn';
      edit.textContent = '编辑';
      edit.onclick = (e) => { e.stopPropagation(); closeAssetPopover(); openAssetEditor(a.path, a.name); };
      row.append(edit);
    }
    box.append(row);
  }
  document.body.append(box);
  const r = anchorEl.getBoundingClientRect();
  box.style.left = Math.min(r.left, window.innerWidth - box.offsetWidth - 12) + 'px';
  box.style.top = Math.min(r.bottom + 6, window.innerHeight - box.offsetHeight - 12) + 'px';
  setTimeout(() => {
    document.addEventListener('click', function once(e) {
      if (!box.contains(e.target)) { closeAssetPopover(); document.removeEventListener('click', once); }
    });
  }, 0);
}

/** 新建节点后如果落在视口外，轻推视图把它带进来 */
function ensureNodeVisible(node, margin = 90) {
  if (!node || typeof node.x !== 'number') return;
  const rect = canvas.getBoundingClientRect();
  const s = sizeOf(node);
  const cx = node.x * view.scale + view.x;
  const cy = node.y * view.scale + view.y;
  let dx = 0;
  let dy = 0;
  if (cx - s.w / 2 * view.scale < margin) dx = margin - (cx - s.w / 2 * view.scale);
  if (cx + s.w / 2 * view.scale > rect.width - margin) dx = rect.width - margin - (cx + s.w / 2 * view.scale);
  if (cy - s.h / 2 * view.scale < margin) dy = margin - (cy - s.h / 2 * view.scale);
  if (cy + s.h / 2 * view.scale > rect.height - margin) dy = rect.height - margin - (cy + s.h / 2 * view.scale);
  if (!dx && !dy) return;
  view.x += dx;
  view.y += dy;
  applyView();
}

/* ---------------------------------------------------------- 附件编辑器 */
function closeAssetEditor() {
  const old = document.getElementById('assetEditor');
  if (old) old.remove();
}

async function openAssetEditor(assetPath, name) {
  closeAssetEditor();
  let text = '';
  try {
    const r = await window.MTApi.assetRead(projectId, assetPath);
    text = r.text;
  } catch (e) { mtAlert('打不开：' + e.message); return; }

  const host = document.createElement('div');
  host.id = 'assetEditor';
  host.className = 'editor-host open';
  host.innerHTML =
    '<div class="editor" style="width:min(720px,94vw)">' +
      '<div class="editor-head"><h3>编辑 ' + name + '</h3>' +
        '<span class="hint">保存后自动保存到 git</span></div>' +
      '<div class="md-body" style="flex:1"><textarea id="assetText"></textarea></div>' +
      '<div class="row"><button class="btn text" id="assetCancel">取消</button>' +
        '<button class="btn" id="assetSave">保存</button></div>' +
    '</div>';
  document.body.append(host);
  const ta = host.querySelector('#assetText');
  ta.value = text;
  ta.focus();
  host.querySelector('#assetCancel').onclick = closeAssetEditor;
  host.addEventListener('click', (e) => { if (e.target === host) closeAssetEditor(); });
  host.querySelector('#assetSave').onclick = async () => {
    try {
      await window.MTApi.assetWrite(projectId, assetPath, ta.value);
      closeAssetEditor();
      toastMsg('已保存 ' + name);
    } catch (e) { mtAlert('保存失败：' + e.message); }
  };
}

/* ---------------------------------------------------------- 视图：平移与缩放 */
let view = { x: 0, y: 0, scale: 1 };
let panState = null;

function applyView() {
  canvas.style.transformOrigin = '0 0';
  canvas.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.scale + ')';
  // 点阵背景也按同样的位移和缩放走，视觉上才是"同一块画布"
  const wrap = document.getElementById('wrap');
  if (wrap) {
    const size = 22 * view.scale;
    wrap.style.backgroundSize = size + 'px ' + size + 'px';
    wrap.style.backgroundPosition = view.x + 'px ' + view.y + 'px';
  }
}
applyView();

/** 屏幕坐标 → 画布逻辑坐标（考虑缩放与平移） */
function toCanvas(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (clientX - rect.left) / view.scale,
    y: (clientY - rect.top) / view.scale,
  };
}

// 空白处按住拖动 → 平移整个画布
canvas.addEventListener('mousedown', (e) => {
  // 在画布上按鼠标 = 离开输入框，否则 Ctrl+Z 会被输入框的文本撤销吃掉，
  // 用户会觉得"画布的撤销键没反应"。
  const active = document.activeElement;
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
    try { active.blur(); } catch (err) { /* 忽略 */ }
  }
  if (e.button !== 0) return;
  if (e.target.closest('.node')) return;
  panState = { sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
  userAdjustedView = true;
  canvas.style.cursor = 'grabbing';
});
window.addEventListener('mousemove', (e) => {
  if (!panState) return;
  view.x = panState.ox + (e.clientX - panState.sx);
  view.y = panState.oy + (e.clientY - panState.sy);
  applyView();
});
window.addEventListener('mouseup', () => {
  if (panState) { panState = null; canvas.style.cursor = ''; }
});

// 滚轮缩放（以指针为锚点）
canvas.addEventListener('mousemove', (e) => {
  if (!edgeTip) return;
  if (!e.target.classList || !String(e.target.getAttribute && e.target.getAttribute('class') || '').includes('edge-hit')) {
    // 指针已经不在线的命中层上了
    hideEdgeTip();
  }
}, true);

canvas.addEventListener('wheel', (e) => {
  if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 2) return;
  e.preventDefault();
  const before = toCanvas(e.clientX, e.clientY);
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  const next = Math.min(2.5, Math.max(0.35, view.scale * factor));
  if (next === view.scale) return;
  userAdjustedView = true;
  view.scale = next;
  const after = toCanvas(e.clientX, e.clientY);
  view.x += (after.x - before.x) * view.scale;
  view.y += (after.y - before.y) * view.scale;
  applyView();
}, { passive: false });

let dragState = null;
const lastModifiers = { alt: false, shift: false, ctrl: false };
window.addEventListener('keydown', (e) => {
  lastModifiers.alt = e.altKey; lastModifiers.shift = e.shiftKey; lastModifiers.ctrl = e.ctrlKey || e.metaKey;
});
window.addEventListener('keyup', (e) => {
  lastModifiers.alt = e.altKey; lastModifiers.shift = e.shiftKey; lastModifiers.ctrl = e.ctrlKey || e.metaKey;
});
let lastDragMovedAt = 0;

function startDrag(e, data) {
  if (e.button !== 0) return;
  data = byId(data.id) || data;
  if (e.target.classList && e.target.classList.contains('branch-handle')) return;
  // 先选中、再拖动：没选中的节点按下时只选中它，不跟着指针跑。
  // 这样"点一下看内容"和"按住拖位置"不会互相打架。
  const el0 = nodeEl(data.id);
  if (el0 && !el0.classList.contains('selected')) {
    select(data.id);
    const live0 = byId(data.id);
    if (live0) openEditor(live0);
    return;
  }
  e.preventDefault();
  select(data.id);
  const p0 = toCanvas(e.clientX, e.clientY);
  dragState = { data, ox: p0.x - data.x, oy: p0.y - data.y, moved: false, startX: data.x, startY: data.y };
  const el = nodeEl(data.id);
  if (el) el.classList.add('dragging');
  window.addEventListener('mousemove', onDrag);
  window.addEventListener('mouseup', endDrag, { once: true });
}
function onDrag(e) {
  if (!dragState) return;
  const p = toCanvas(e.clientX, e.clientY);
  const x = p.x - dragState.ox;
  const y = p.y - dragState.oy;
  dragState.data.x = x; dragState.data.y = y;
  const el = nodeEl(dragState.data.id);
  if (el) { el.style.left = x + 'px'; el.style.top = y + 'px'; }
  if (!dragState.moved && Math.hypot(x - dragState.startX, y - dragState.startY) > 4) {
    dragState.moved = true;
    lastDragMovedAt = Date.now();   // 这一下是拖动，不是点击
  }
  drawEdges();
  const live = byId(dragState.data.id);
  if (!dragState.moved) return;

  if (live && !live.parent) {
    // 浮空节点：靠近谁就接谁（带吸附虚线）
    updatePreview(live);
    hideReparentHint();
  } else {
    // 已有父节点：只有把指针压在别的节点上才算改接
    const over = nodeUnderPointer(e.clientX, e.clientY, dragState.data.id);
    dragState.reparentTo = over ? over.id : null;
    clearPreview();
    if (over) {
      showReparentHint(e.clientX, e.clientY, over.title || '节点');
      const a = nodeAnchor(over);
      const b = nodeAnchor(live);
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M' + a.x + ',' + a.y + ' L' + b.x + ',' + b.y);
      path.setAttribute('class', 'edge preview reparent');
      edgesSvg.append(path);
    } else {
      hideReparentHint();
    }
  }
}
/* ---------- 拖动时的吸附预判：谁会成为父节点、以哪种方式连 ---------- */
function descendantsOf(id) {
  const out = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    for (const n of graph.nodes) {
      if (n.parent === cur && !out.has(n.id)) { out.add(n.id); stack.push(n.id); }
    }
  }
  return out;
}

function nearestParentFor(node, threshold = 150) {
  if (!node) return null;
  // 不能连到自己的子孙上，否则会形成环
  const banned = descendantsOf(node.id);
  banned.add(node.id);
  let best = null;
  let bestDist = Infinity;
  for (const other of [graph.trunk, ...graph.nodes]) {
    if (!other || banned.has(other.id)) continue;
    const dx = (other.x || 0) - node.x;
    const dy = (other.y || 0) - node.y;
    // 距离按"到目标边框"算，而不是到中心：大节点旁边更该被选中
    const ow = (other._w || 160) / 2;
    const oh = (other._h || 80) / 2;
    const edgeDist = Math.hypot(Math.max(0, Math.abs(dx) - ow), Math.max(0, Math.abs(dy) - oh));
    if (edgeDist < bestDist) { bestDist = edgeDist; best = { other, dx, dy, dist: edgeDist }; }
  }
  if (!best || best.dist > threshold) return null;
  best.dist = bestDist;
  // 判定顺序：自己声明 > 落点相对位置 > 横向兜底
  if (node.preferredAxis) {
    best.axis = node.preferredAxis;
  } else {
    const targetH = (best.other._h || 80);
    const targetW = (best.other._w || 160);
    // 落在目标正下方（横向还压在目标宽度内）就是分支
    const insideWidth = Math.abs(best.dx) < targetW * 0.7;
    const below = best.dy > targetH * 0.45;
    if (below && insideWidth) best.axis = 'v';
    else if (best.dx > targetW * 0.35) best.axis = 'h';
    else best.axis = Math.abs(best.dy) > Math.abs(best.dx) ? 'v' : 'h';
  }
  return best;
}

let previewPath = null;
let previewTarget = null;

function clearPreview() {
  if (previewPath && previewPath.parentNode) previewPath.parentNode.removeChild(previewPath);
  previewPath = null;
  if (previewTarget) { previewTarget.classList.remove('link-target'); previewTarget = null; }
}

function updatePreview(node) {
  const hit = nearestParentFor(node);
  if (!hit) { clearPreview(); return; }
  const targetEl = nodeEl(hit.other.id);
  if (previewTarget && previewTarget !== targetEl) previewTarget.classList.remove('link-target');
  if (targetEl) { targetEl.classList.add('link-target'); previewTarget = targetEl; }

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  const a = nodeAnchor(hit.other);
  const b = nodeAnchor(node);
  let d;
  if (hit.axis === 'v') {
    d = 'M' + a.x + ',' + (a.y + a.h / 2) + ' L' + b.x + ',' + (b.y - b.h / 2);
  } else {
    d = 'M' + (a.x + a.w / 2) + ',' + a.y + ' C' + (a.x + a.w / 2 + 50) + ',' + a.y
      + ' ' + (b.x - b.w / 2 - 50) + ',' + b.y + ' ' + (b.x - b.w / 2) + ',' + b.y;
  }
  path.setAttribute('d', d);
  path.setAttribute('class', 'edge preview ' + (hit.axis === 'v' ? 'preview-v' : 'preview-h'));
  if (previewPath && previewPath.parentNode) previewPath.parentNode.removeChild(previewPath);
  edgesSvg.append(path);
  previewPath = path;
}

/** 指针正下方是哪个节点（排除自己和自己的子孙，避免成环） */
function nodeUnderPointer(clientX, clientY, selfId) {
  const el = document.elementFromPoint(clientX, clientY);
  const host = el && el.closest ? el.closest('.node') : null;
  if (!host) return null;
  const id = host.dataset.id;
  if (!id || id === selfId) return null;
  const banned = descendantsOf(selfId);
  if (banned.has(id)) return null;
  return byId(id);
}

/** 改接提示：指针旁边浮一个小标签，告诉用户"松手就接到这个节点" */
let reparentHint = null;
function showReparentHint(x, y, name) {
  if (!reparentHint) {
    reparentHint = document.createElement('div');
    reparentHint.className = 'reparent-hint';
    document.body.append(reparentHint);
  }
  reparentHint.textContent = '改接到「' + name + '」';
  reparentHint.style.left = (x + 18) + 'px';
  reparentHint.style.top = (y + 18) + 'px';
}
function hideReparentHint() {
  if (reparentHint) { reparentHint.remove(); reparentHint = null; }
}

function endDrag() {
  window.removeEventListener('mousemove', onDrag);

  const st = dragState;
  dragState = null;
  clearPreview();
  hideReparentHint();
  if (!st) return;
  const el = nodeEl(st.data.id);
  if (el) el.classList.remove('dragging');
  const node = byId(st.data.id) || st.data;
  if (!node) return;
  // 已经在结构里的节点被拖动，只当作"挪位置"：
  // 之前这里无脑吸附，结果用户只是想挪一下，链接就被改接到别处了。
  const canReparent = !node.parent;

  // 没真的拖动：什么也别做。
  // 这里如果顺手 render()，节点内部 DOM 会被重建，
  // 浏览器紧接着派发的 click 就会落到已移除的元素上，
  // 于是"点节点建立关联/选中"全部失效（这个坑踩过一次）。
  if (!st.moved) return;

  // 拖过就自动关掉自动排版，否则松手会被拉回原位，看着像拖不动
  if (st.moved && !graph.freeLayout) {
    graph.freeLayout = true;
    const btn = document.getElementById('layoutToggle');
    if (btn) btn.textContent = '自动排版：关';
    toastMsg('已切换为自由摆放', 2400);
  }

  // 松手吸附：离哪个节点近就连过去（不会连到自己的子孙上）
  if (canReparent) {
    // 浮空节点：就近吸附
    const hit = nearestParentFor(node);
    if (hit) {
      node.parent = hit.other.id;
      node.floating = false;
      node.axis = hit.axis;
      delete node.note;
      toastMsg('已接到「' + (hit.other.title || '节点') + '」下面');
    }
  } else if (st.reparentTo) {
    // 已有父节点：只有明确丢在别的节点上才改接
    const target = byId(st.reparentTo);
    if (target) {
      const dx = (target.x || 0) - node.x;
      const dy = (target.y || 0) - node.y;
      node.parent = target.id;
      node.floating = false;
      node.axis = Math.abs(dy) > Math.abs(dx) * 0.6 ? 'v' : 'h';
      delete node.note;
      toastMsg('已改接到「' + (target.title || '节点') + '」下面');
    }
  }
  markDirty();
  render();
}

/* 暂存区拖进画布 */
canvas.addEventListener('dragover', (e) => e.preventDefault());
canvas.addEventListener('drop', (e) => {
  e.preventDefault();
  const id = e.dataTransfer.getData('text/plain');
  const item = graph.staged.find((s) => s.id === id);
  if (!item) return;
  const rect = canvas.getBoundingClientRect();
  const hit = [...canvas.querySelectorAll('.node')].find((el) => {
    const r = el.getBoundingClientRect();
    return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
  });
  graph.staged = graph.staged.filter((s) => s.id !== id);
  const newId = 'n' + Date.now();
  const projectPath = item.path || '';            // 已经在项目里的文件
  const externalPath = item.source || '';         // 老条目：只有原始路径
  if (hit) {
    graph.nodes.push({ id: newId, title: item.title, parent: hit.dataset.id, axis: 'h' });
  } else {
    const p = toCanvas(e.clientX, e.clientY);
    graph.nodes.push({
      id: newId, title: item.title, note: '浮空 · 还没链接',
      parent: null, axis: null, floating: true, x: p.x, y: p.y,
    });
  }
  markDirty();
  render();
  ensureNodeVisible(graph.nodes.find((n) => n.id === newId));

  if (!(projectPath || externalPath) || !online || !window.MTApi) return;
  const node = graph.nodes.find((n) => n.id === newId);
  const fail = (msg) => {
    graph.staged.push(item);   // 挂不上就把条目放回去，别让用户白忙
    markDirty(); render();
    mtAlert('挂文件失败：' + msg);
  };
  (async () => {
    try {
      // 节点必须先存进项目，服务端才认得它
      await window.MTApi.saveGraph(projectId, {
        trunk: graph.trunk, nodes: graph.nodes, staged: graph.staged,
        links: graph.links, freeLayout: graph.freeLayout,
      });
      const r = projectPath
        ? await window.MTApi.stagedAttach(projectId, newId, projectPath)
        : await window.MTApi.attach(projectId, newId, externalPath);
      if (node) node.assets = (r.node && r.node.assets) || node.assets;
      markDirty(); render();
      toastMsg('已挂上 ' + ((node && node.assets && node.assets.length) || 0) + ' 个文件');
    } catch (e) {
      const msg = String(e.message || e);
      fail(/找不到这个文件/.test(msg) ? '这个文件已经不在原位置了，请用「📎 文件」重新选一次' : msg);
    }
  })();
});

/* ---------------------------------------------------------- 其它 */
document.getElementById('addNode').onclick = () => {
  const id = 'n' + Date.now();
  const rect = canvas.getBoundingClientRect();
  // 落在当前视口中间：要把平移和缩放算回去，否则会跑到画布外
  const cx = (rect.width / 2 - view.x) / view.scale - 80;
  const cy = (rect.height / 2 - view.y) / view.scale - 40;
  graph.nodes.push({
    id, title: '新节点', note: '浮空', parent: null, axis: null,
    floating: true, x: cx, y: cy,
  });
  markDirty();
  render();
  openEditor(graph.nodes.find((n) => n.id === id));
};

document.getElementById('history').onclick = () => openHistory();

/* ---------------------------------------------------------- 分支面板 */
async function refreshBranchPanel() {
  const list = document.getElementById('branchList');
  const box = document.getElementById('conflictBox');
  list.innerHTML = '<div class="hint">读取中…</div>';
  box.innerHTML = '';
  try {
    const d = await window.MTApi.branches(projectId);
    const cur = d.current;
    document.getElementById('branchBtn').textContent = '分支：' + cur;
    list.innerHTML = '';
    for (const b of d.branches || []) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:8px;background:var(--md-surface-container-high);' +
        'border-radius:10px;padding:8px 10px;font-size:12.5px';
      row.innerHTML = '<span style="flex:1">' + b + (b === cur ? ' <span style="opacity:.55">（当前）</span>' : '') + '</span>';
      if (b !== cur) {
        const co = document.createElement('button');
        co.className = 'btn text'; co.style.height = '26px'; co.textContent = '切换';
        co.onclick = async () => {
          try { await window.MTApi.checkout(projectId, b); await reloadProject(); await refreshBranchPanel(); }
          catch (e) { mtAlert('切换失败：' + e.message); }
        };
        const mg = document.createElement('button');
        mg.className = 'btn text'; mg.style.height = '26px'; mg.textContent = '合并到此';
        mg.onclick = async () => {
          const okMerge = await mtDialog({ title: '合并分支', body: '把 ' + b + ' 合并到 ' + cur + '？', okText: '合并' });
          if (!okMerge) return;
          try {
            const r = await window.MTApi.merge(projectId, b);
            if (r.merged) {
              await reloadProject();
              if (r.conflicts && r.conflicts.length) {
                await mtAlert('合并完成，但有冲突', '这些文件两边都改过，已经先保留「当前分支」的内容：\n\n  '
                  + r.conflicts.map((x) => x.path).join('\n  ')
                  + '\n\n需要的话可以从版本历史里把另一边的版本取回来。');
              } else {
                toastMsg('已合并 ' + b);
              }
            } else {
              await mtAlert('没有合并', r.message || '两边没有可合并的改动');
            }
            await refreshBranchPanel();
          } catch (e) { await mtAlert('合并失败', e.message); }
        };
        row.append(co, mg);
        const del = document.createElement('button');
        del.className = 'btn text'; del.style.height = '26px'; del.style.color = '#ffb4ab';
        del.textContent = '删除';
        del.onclick = async () => {
          const force = await mtDialog({ title: '删除分支', body: '删除分支 ' + b + '？\n\n如果它还有没合并的版本，会再问你一次。', okText: '删除', danger: true });
          if (!force) return;
          try {
            await window.MTApi.branchDelete(projectId, b, false);
            toastMsg('已删除分支 ' + b);
            await refreshBranchPanel();
          } catch (e) {
            // 未合并会被拦住：再问一次是否强制
            if (String(e.message).includes('没合并')) {
              if (!(await mtDialog({ title: '强制删除', body: '分支 ' + b + ' 还有没合并的版本，强制删除会丢掉这些版本。\n确定吗？', okText: '强制删除', danger: true }))) return;
              try {
                await window.MTApi.branchDelete(projectId, b, true);
                toastMsg('已强制删除 ' + b);
                await refreshBranchPanel();
              } catch (e2) { await mtAlert('删除失败', e2.message); }
            } else {
              mtAlert('删除失败：' + e.message);
            }
          }
        };
        row.append(del);
      }
      list.append(row);
    }

    const st = await window.MTApi.mergeState(projectId);
    if (st.merging || (st.conflicts || []).length) {
      const title = document.createElement('div');
      title.className = 'hint';
      title.textContent = '合并冲突（' + (st.conflicts || []).length + ' 个文件）';
      box.append(title);
      for (const f of st.conflicts || []) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12.5px;padding:4px 0';
        row.innerHTML = '<span style="flex:1">' + f + '</span>';
        const fine = document.createElement('button');
        fine.className = 'btn text'; fine.style.height = '26px'; fine.textContent = '逐段处理';
        fine.onclick = () => openMergeEditor(f);
        row.append(fine);
        for (const [label, side] of [['整体用我的', 'ours'], ['整体用对方', 'theirs']]) {
          const b = document.createElement('button');
          b.className = 'btn text'; b.style.height = '26px'; b.textContent = label;
          b.onclick = async () => {
            try { await window.MTApi.resolve(projectId, f, side); await refreshBranchPanel(); }
            catch (e) { mtAlert('处理失败：' + e.message); }
          };
          row.append(b);
        }
        box.append(row);
      }
      const abort = document.createElement('button');
      abort.className = 'btn text';
      abort.textContent = '放弃这次合并';
      abort.onclick = async () => {
        if (!(await mtDialog({ title: '放弃合并', body: '放弃这次合并，回到合并之前的状态？', okText: '放弃', danger: true }))) return;
        try { await window.MTApi.mergeAbort(projectId); await reloadProject(); await refreshBranchPanel(); }
        catch (e) { mtAlert('取消失败：' + e.message); }
      };
      box.append(abort);
    }
  } catch (e) {
    list.innerHTML = '<div class="hint">读取失败：' + e.message + '</div>';
  }
}

/* ---------------------------------------------------------- 三方合并编辑器 */
let mergeState = { file: null, blocks: [], choices: [] };

function buildMergeText() {
  const out = [];
  let ci = 0;
  for (const b of mergeState.blocks) {
    if (b.type === 'text') { out.push(...b.lines); continue; }
    const pick = mergeState.choices[ci++] || 'ours';
    if (pick === 'ours') out.push(...b.ours);
    else if (pick === 'theirs') out.push(...b.theirs);
    else if (pick === 'both') out.push(...b.ours, ...b.theirs);
    // pick === 'none' → 两边都不要
  }
  return out.join('\n');
}

function renderMergeBlocks() {
  const host = document.getElementById('mergeBlocks');
  host.innerHTML = '';
  let ci = 0;
  for (const b of mergeState.blocks) {
    if (b.type === 'text') {
      const box = document.createElement('pre');
      box.style.cssText = 'margin:0;font-size:12px;line-height:1.6;opacity:.65;white-space:pre-wrap;' +
        'background:var(--md-surface-container-high);border-radius:8px;padding:8px 10px;max-height:120px;overflow:auto';
      box.textContent = b.lines.join('\n');
      host.append(box);
      continue;
    }
    const idx = ci++;
    const card = document.createElement('div');
    card.style.cssText = 'border:1px solid var(--md-outline-variant);border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px';
    const head = document.createElement('div');
    head.className = 'hint';
    head.textContent = '第 ' + (idx + 1) + ' 段冲突';
    card.append(head);

    const row = document.createElement('div');
    row.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:8px';
    for (const [side, label] of [['ours', '我的'], ['theirs', '对方的']]) {
      const pane = document.createElement('div');
      const chosen = (mergeState.choices[idx] || 'ours') === side;
      pane.style.cssText = 'padding:8px;border-radius:8px;cursor:pointer;font-size:12px;white-space:pre-wrap;' +
        'line-height:1.6;border:1px solid ' + (chosen ? 'var(--md-primary)' : 'transparent') + ';' +
        'background:' + (chosen ? 'rgba(143,211,199,.12)' : 'var(--md-surface-container-high)');
      pane.textContent = (b[side] || []).join('\n') || '(空)';
      pane.onclick = () => { mergeState.choices[idx] = side; renderMergeBlocks(); };
      row.append(pane);
    }
    card.append(row);

    const acts = document.createElement('div');
    acts.style.cssText = 'display:flex;gap:8px';
    for (const [val, label] of [['both', '两个都留'], ['none', '这段都不要']]) {
      const btn = document.createElement('button');
      btn.className = 'btn text';
      btn.style.height = '26px';
      const chosen = mergeState.choices[idx] === val;
      btn.textContent = label + (chosen ? ' ✓' : '');
      btn.onclick = () => { mergeState.choices[idx] = val; renderMergeBlocks(); };
      acts.append(btn);
    }
    card.append(acts);
    host.append(card);
  }
}

async function openMergeEditor(file) {
  // 两个抽屉都在右侧，同时开会叠在一起
  document.getElementById('branchHost').classList.remove('open');
  try {
    const d = await window.MTApi.conflictRead(projectId, file);
    mergeState.file = file;
    mergeState.blocks = d.blocks || [];
    mergeState.choices = mergeState.blocks.filter((b) => b.type === 'conflict').map(() => 'ours');
    document.getElementById('mergeTitle').textContent = '处理冲突：' + file;
    document.getElementById('mergeHost').classList.add('open');
    renderMergeBlocks();
  } catch (e) { mtAlert('读取失败：' + e.message); }
}

document.getElementById('mergeCancel').addEventListener('click', () => {
  document.getElementById('mergeHost').classList.remove('open');
});
document.getElementById('mergeHost').addEventListener('click', (e) => {
  if (e.target.id === 'mergeHost') e.currentTarget.classList.remove('open');
});
document.getElementById('mergeSave').addEventListener('click', async () => {
  try {
    const r = await window.MTApi.conflictWrite(projectId, mergeState.file, buildMergeText(), true);
    document.getElementById('mergeHost').classList.remove('open');
    if ((r.remaining || []).length) {
      toastMsg('还剩 ' + r.remaining.length + ' 个文件有冲突');
    } else {
      const finish = await mtDialog({ title: '完成合并', body: '所有冲突都处理完了。\n\n现在就完成这次合并吗？', okText: '完成合并' });
      if (finish) {
        const m = await window.MTApi.mergeFinish(projectId);
        await reloadProject();
        toastMsg('合并完成');
      }
    }
    await refreshBranchPanel();
  } catch (e) { mtAlert('保存失败：' + e.message); }
});

async function reloadProject() {
  const d = await window.MTApi.load(projectId);
  graph.trunk = d.graph.trunk || graph.trunk;
  graph.nodes = d.graph.nodes || [];
  graph.staged = d.graph.staged || [];
  markDirty(false);
  render();
}

function openBranchPanel() {
  document.getElementById('branchHost').classList.add('open');
  refreshBranchPanel();
}

document.getElementById('branchBtn').addEventListener('click', openBranchPanel);
document.getElementById('branchClose').addEventListener('click', () => {
  document.getElementById('branchHost').classList.remove('open');
});
document.getElementById('branchHost').addEventListener('click', (e) => {
  if (e.target.id === 'branchHost') e.currentTarget.classList.remove('open');
});
document.getElementById('createBranch').addEventListener('click', async () => {
  const name = document.getElementById('newBranch').value.trim();
  if (!name) return;
  try {
    await window.MTApi.checkout(projectId, name);
    document.getElementById('newBranch').value = '';
    await reloadProject();
    await refreshBranchPanel();
  } catch (e) { mtAlert('新建失败：' + e.message); }
});

/* 摘要自动同步开关：关掉后外部改文件不会动节点摘要，点「同步摘要」才更新 */
let autoSummary = true;

/* 自动排版开关：关掉后节点可以随意摆放，连线按拖动后的位置重画 */
if (!document.getElementById('layoutToggle')) {
  const btn = document.createElement('button');
  btn.className = 'btn tonal';
  btn.id = 'layoutToggle';
  btn.title = '关闭后节点可以自由摆放，不再自动排列';
  btn.textContent = '自动排版：开';
  const toolbar = document.querySelector('.toolbar');
  const before = document.getElementById('history');
  toolbar.insertBefore(btn, before);
  btn.addEventListener('click', () => {
    const turningOff = !graph.freeLayout;
    if (turningOff) {
      // 先按自动排版算一遍位置，再冻结，避免所有节点堆在一起
      const all = [graph.trunk, ...graph.nodes].filter(Boolean);
      if (all.some((n) => typeof n.x !== 'number' || typeof n.y !== 'number')) {
        graph.freeLayout = false;
        computeLayout();
      }
    }
    graph.freeLayout = !graph.freeLayout;
    btn.textContent = '自动排版：' + (graph.freeLayout ? '关' : '开');
    if (graph.freeLayout) {
      // 切到自由排版时，先把当前位置固化下来，避免所有节点跳回原点
      for (const n of [graph.trunk, ...graph.nodes]) {
        if (n && typeof n.x !== 'number') { /* 已经有坐标 */ }
      }
    }
    markDirty();
    render();
    toastMsg(graph.freeLayout ? '已关闭自动排版，节点可以随便放' : '已开启自动排版');
  });
}
document.getElementById('autoSummary').addEventListener('click', async (ev) => {
  autoSummary = !autoSummary;
  ev.currentTarget.textContent = '摘要同步：' + (autoSummary ? '开' : '关');
  if (online && window.MTApi) {
    try { await window.MTApi.settings(projectId, { auto_summary: autoSummary }); } catch (e) { /* 忽略 */ }
  }
});

/** 退出前如实检查：有没有未保存的改动、有没有文件被别的程序占着 */
async function checkBeforeExit() {
  let st = null;
  try { st = await window.MTApi.status(projectId); } catch (e) { st = lastStatus; }
  lastStatus = st;
  const localDirty = dirty;
  const uncommitted = st && st.changed && st.changed.length;
  const locked = (st && st.locked) || [];

  if (!localDirty && !uncommitted) return { ok: true };

  // 有文件被占用：先老老实实说清楚，别假装能保存
  if (locked.length) {
    await mtAlert('还不能保存', '下面这些文件正被其它程序打开：\n\n  '
      + locked.slice(0, 5).join('\n  ')
      + (locked.length > 5 ? '\n  …还有 ' + (locked.length - 5) + ' 个' : '')
      + '\n\n请先在 Word / WPS / 记事本等程序里关闭它们，再回来保存。');
    return { ok: false, reason: 'locked' };
  }

  const names = (st && st.changed ? st.changed : []).filter((x) => !x.startsWith('.mt/'));
  const detail = names.length ? '\n\n待保存：' + names.slice(0, 5).join('、') + (names.length > 5 ? ' 等 ' + names.length + ' 个' : '') : '';
  const go = await mtDialog({
    title: '还有改动没有保存',
    body: (detail ? detail.replace(/^\n\n/, '') + '\n\n' : '') + '确定 = 现在保存并退出\n取消 = 留在当前页',
    okText: '保存并退出', cancelText: '留在当前页',
  });
  if (!go) return { ok: false, reason: 'cancel' };

  try {
    await saveGraphNow();
    const r = await window.MTApi.commit(projectId, {
      trunk: graph.trunk, nodes: graph.nodes, staged: graph.staged,
      links: graph.links, freeLayout: graph.freeLayout,
    }, 'mt: 离开前保存');
    if (!r.committed) {
      mtAlert('保存失败：' + (r.reason || '没有改动') + '\n\n（不会谎报成功，文件仍然保留在项目里）');
      return { ok: false, reason: 'not-committed' };
    }
    return { ok: true };
  } catch (e) {
    mtAlert('保存失败：' + e.message + '\n\n请处理后重试，或先留在页面里。');
    return { ok: false, reason: 'error' };
  }
}

document.getElementById('back').onclick = async () => {
  const r = await checkBeforeExit();
  if (!r.ok) return;
  location.href = 'index.html';
};

// 关标签页 / 刷新：让浏览器拦一下
// MT 外层的左箭头按下来时，走和页面内箭头一样的保存检查
window.addEventListener('message', (event) => {
  if (!event || !event.data || event.data.type !== 'mt-back') return;
  const btn = document.getElementById('back');
  if (btn) btn.click();
});

// 不再用浏览器原生的 beforeunload 弹窗（那个"是否离开网站"和整套界面不是一个风格）。
// 离开时尽力保存一次：刷新/关闭窗口没必要拦住用户，正文本来就在磁盘上，
// 没保存的是版本快照，丢了也只是少一个版本，不会丢稿。
window.addEventListener('pagehide', () => {
  const uncommitted = lastStatus && lastStatus.changed && lastStatus.changed.length;
  if (!dirty && !uncommitted) return;
  try {
    // 同步发不出去异步请求，这里只把状态落成草稿，下次打开会提示
    localStorage.setItem('mt.dirtyDraft.' + (projectId || ''), String(Date.now()));
  } catch (e) { /* 忽略 */ }
});

document.querySelectorAll('#layoutSwitch button').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.layout === layout) return;
    layout = btn.dataset.layout;
    document.querySelectorAll('#layoutSwitch button').forEach((b) => b.classList.toggle('on', b === btn));
    render();
  });
});

/* ---------------------------------------------------------- 编辑面板 */
let editingId = null;
const editorHost = document.getElementById('editorHost');
const editorName = document.getElementById('editorName');
const editorBody = document.getElementById('editorBody');

/** 让节点一定有对应的 .md 文件；没有就现在建一个。 */
async function ensureNodeFile(data) {
  if (!(online && window.MTApi)) return;
  if (data.file) return;
  try {
    const r = await window.MTApi.nodeWrite(projectId, data, data.body || '');
    data.file = r.file;
    data.summary = r.summary || data.summary;
    await window.MTApi.saveGraph(projectId, { trunk: graph.trunk, nodes: graph.nodes, staged: graph.staged });
  } catch (e) { /* 建不出来就算了，不影响编辑 */ }
}

/** 弹系统选择框并等结果；界面不会卡住，按钮只是显示等待中 */
async function pickFilePath(onBusy) {
  await window.MTApi.pickFile();
  if (onBusy) onBusy(true);
  for (let i = 0; i < 300; i += 1) {          // 最多等 5 分钟
    await new Promise((r) => setTimeout(r, 600));
    const r = await window.MTApi.pickPoll();
    if (r.running) continue;
    if (onBusy) onBusy(false);
    if (r.error) throw new Error(r.error);
    return r.picked || '';
  }
  if (onBusy) onBusy(false);
  return '';
}

/* ---------------------------------------------------------- Markdown 编辑器 */
function initMarkdownEditor() {
  const wrap = editorHost.querySelector('.editor');
  const preview = document.getElementById('mdPreview');
  const tabs = document.getElementById('mdTabs');
  const toolbar = document.getElementById('mdToolbar');
  if (!wrap || !preview || !tabs) return;

  const setMode = (mode) => {
    wrap.dataset.mode = mode;
    [...tabs.children].forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    if (mode !== 'edit') updatePreview();
  };
  tabs.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (btn) setMode(btn.dataset.mode);
  });

  function updatePreview() {
    preview.innerHTML = window.MTMarkdown ? window.MTMarkdown.render(editorBody.value) : '';
  }

  // 预览里的链接：原来点了没反应（相对路径还会按网页地址去解析，直接 404）。
  // 现在统一交给系统处理：网址交给浏览器，文件按项目目录解析后打开。
  preview.addEventListener('click', async (e) => {
    const a = e.target.closest('a');
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#')) return;
    try {
      if (/^(https?:|mailto:)/i.test(href)) {
        await window.MTApi.shellOpen(href, false);
        return;
      }
      // 站内相对路径：相对项目根目录，而不是相对这个网页
      const clean = decodeURI(href.split('#')[0]);
      const full = clean.startsWith('/')
        ? projectPath + clean
        : projectPath + '\\' + clean.replace(/\//g, '\\');
      await window.MTApi.shellOpen(full, false);
    } catch (err) {
      await mtAlert('打不开这个链接', href + '\n\n' + (err && err.message ? err.message : '文件可能不在了'));
    }
  });

  // 输入时实时预览（有预览区才更新）
  editorName.addEventListener('input', () => markEditorDirty(true));
  editorBody.addEventListener('input', () => {
    markEditorDirty(true);
    if (wrap.dataset.mode !== 'edit') updatePreview();
  });
  wrap.dataset.mode = 'split';
  updatePreview();

  // 工具条：在光标处包一层语法
  const wrapSelection = (before, after, placeholder) => {
    const start = editorBody.selectionStart;
    const end = editorBody.selectionEnd;
    const value = editorBody.value;
    const chosen = value.slice(start, end) || placeholder;
    editorBody.value = value.slice(0, start) + before + chosen + after + value.slice(end);
    const caret = start + before.length + chosen.length;
    editorBody.setSelectionRange(caret, caret);
    editorBody.focus();
    updatePreview();
  };

  const prefixLines = (prefix) => {
    const start = editorBody.selectionStart;
    const value = editorBody.value;
    const lineStart = value.lastIndexOf('\n', start - 1) + 1;
    editorBody.value = value.slice(0, lineStart) + prefix + value.slice(lineStart);
    editorBody.setSelectionRange(start + prefix.length, start + prefix.length);
    editorBody.focus();
    updatePreview();
  };

  toolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'bold') wrapSelection('**', '**', '粗体');
    else if (act === 'italic') wrapSelection('*', '*', '斜体');
    else if (act === 'code') wrapSelection('`', '`', '代码');
    else if (act === 'h2') prefixLines('## ');
    else if (act === 'ul') prefixLines('- ');
    else if (act === 'quote') prefixLines('> ');
    else if (act === 'link') wrapSelection('[', '](https://)', '链接文字');
  });

  // 供 openEditor 调用：换文章时刷新预览
  window.__refreshMdPreview = updatePreview;
}

function renderAssets(node) {
  const host = document.getElementById('assetList');
  const count = document.getElementById('assetCount');
  if (count) count.textContent = (node.assets || []).length ? '（' + node.assets.length + ' 个）' : '（空）';
  host.innerHTML = '';
  const list = node.assets || [];
  if (!list.length) {
    const p = document.createElement('div');
    p.className = 'hint';
    p.textContent = '还没有挂文件';
    host.append(p);
    return;
  }
  for (const a of list) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;' +
      'background:var(--md-surface-container-high);border-radius:8px;padding:5px 8px';
    row.innerHTML = '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + a.name + ' <span style="opacity:.5">'
      + (a.size > 1024 * 1024 ? (a.size / 1048576).toFixed(1) + ' MB' : Math.round(a.size / 1024) + ' KB')
      + '</span></span>';
    const open = document.createElement('button');
    open.className = 'btn text'; open.style.height = '24px'; open.textContent = '打开';
    open.onclick = async () => {
      try { await window.MTApi.openFile(projectId, a.path); toastMsg('已用系统程序打开 ' + a.name); }
      catch (e) { mtAlert('打开失败：' + e.message); }
    };
    const chk = document.createElement('button');
    chk.className = 'btn text'; chk.style.height = '24px'; chk.textContent = '校验';
    chk.title = '算这个文件的 SHA-256';
    chk.onclick = async () => {
      try {
        const r = await window.MTApi.hash(projectId, a.path);
        mtAlert(a.name + '\n\n大小：' + r.size + ' 字节\nSHA-256：\n' + r.sha256);
      } catch (e) { mtAlert('校验失败：' + e.message); }
    };
    const del = document.createElement('button');
    del.className = 'btn text'; del.style.height = '24px'; del.style.color = '#ffb4ab'; del.textContent = '移除';
    del.onclick = async () => {
      const purge = await mtDialog({
    title: '移除挂载', body: '移除「' + a.name + '」？\n\n确定 = 连项目里的副本一起删\n取消 = 只去掉记录，文件留着',
    okText: '连同文件一起删', cancelText: '只去掉记录', danger: true,
  });
      try {
        const r = await window.MTApi.assetRemove(projectId, node.id, a.path, purge);
        node.assets = r.node.assets || [];
        renderAssets(node);
        toastMsg(purge ? '已移除并删除副本' : '已移除记录');
      } catch (e) { await mtAlert('移除失败', e.message); }
    };
    row.append(open, chk, del);
    host.append(row);
  }
}

document.getElementById('pickBtn').addEventListener('click', async () => {
  const node = editingId ? byId(editingId) : null;
  if (!node) return;
  const btn = document.getElementById('pickBtn');
  const old = btn.textContent;
  try {
    const picked = await pickFilePath((busy) => {
      btn.textContent = busy ? '等待选择…' : old;
      btn.disabled = busy;
    });
    if (!picked) { toastMsg('没有选择文件'); return; }
    const res = await window.MTApi.attach(projectId, node.id, picked);
    node.assets = res.node.assets || [];
    renderAssets(node);
    markDirty(true);
    render();
    toastMsg('已复制到项目：' + res.asset.path);
  } catch (e) {
    mtAlert('挂文件失败：' + e.message);
  } finally {
    btn.textContent = old;
    btn.disabled = false;
  }
});

document.getElementById('attachBtn').addEventListener('click', async () => {
  const node = editingId ? byId(editingId) : null;
  if (!node) return;
  const source = document.getElementById('assetPath').value.trim();
  if (!source) { mtAlert('先填要挂的文件路径'); return; }
  try {
    const r = await window.MTApi.attach(projectId, node.id, source);
    node.assets = r.node.assets || [];
    document.getElementById('assetPath').value = '';
    renderAssets(node);
    markDirty(true);
    toastMsg('已复制到项目：' + r.asset.path);
  } catch (e) { mtAlert('挂文件失败：' + e.message); }
});

async function openEditor(data) {
  editorDirty = false;
  if (!data) return;
  editingId = data.id;
  await ensureNodeFile(data);
  if (online && window.MTApi && data.file) {
    try {
      const r = await window.MTApi.nodeRead(projectId, data.file);
      data.body = r.body || '';
    } catch (e) { /* 读不到就用内存里的 */ }
  }
  document.getElementById('editorTitle').textContent = data.id === graph.trunk.id ? '编辑主干' : '编辑节点';
  editorName.value = data.title || '';
  editorBody.value = data.body || '';
  markEditorDirty(false);
  const emptyEl = document.getElementById('editorEmpty');
  if (emptyEl) emptyEl.style.display = 'none';
  const wrapEl = editorHost.querySelector('.editor');
  if (wrapEl) wrapEl.style.display = 'flex';
  document.body.classList.add('editing');
  editorHost.classList.add('has-node');
  editorHost.classList.add('open');
  syncKindRow(data);
  renderAssets(data);
  // 有附件时自动展开"挂文件"，省得再点一次
  const box = document.querySelector('.asset-box');
  if (box && (data.assets || []).length) box.open = true;
  // 正文如果是非 Markdown 的主文档，提示去外部编辑
  const hint = document.getElementById('editorHint');
  if (hint) {
    const docs = (data.assets || []).filter((a) => /\.(docx?|pdf|xlsx?|pptx?|png|jpe?g)$/i.test(a.name));
    hint.style.display = docs.length ? 'block' : 'none';
    hint.textContent = docs.length
      ? '这个节点带着 Word/PDF 这类文档：' + docs[0].name + '，建议点「外部打开」用对应软件编辑，正文框只写笔记。'
      : '';
  }
  if (window.__refreshMdPreview) window.__refreshMdPreview();
  setTimeout(() => editorBody.focus(), 120);
}

/* 节点类型：决定它吸附到父节点时按主干还是分支连 */
document.getElementById('kindRow').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-kind]');
  if (!btn) return;
  const node = editingId ? byId(editingId) : null;
  if (!node) return;
  [...e.currentTarget.children].forEach((b) => b.classList.toggle('on', b === btn));
  const kind = btn.dataset.kind;
  if (kind === 'auto') delete node.preferredAxis;
  else node.preferredAxis = kind;
  markDirty();
});

function syncKindRow(node) {
  const row = document.getElementById('kindRow');
  const kind = (node && node.preferredAxis) || 'auto';
  [...row.children].forEach((b) => {
    if (b.dataset && b.dataset.kind) b.classList.toggle('on', b.dataset.kind === kind);
  });
}

function markEditorDirty(on) {
  const tag = document.getElementById('editorDirty');
  if (tag) tag.style.display = on ? 'inline' : 'none';
}

/* ---------------------------------------------------------- 编辑栏宽度
   按住左边那根细线拖动就能改宽窄，双击恢复默认；宽度记在 localStorage 里。 */
const EDITOR_MIN = 320;
const EDITOR_MAX = 760;
const EDITOR_DEFAULT = 400;

function applyEditorWidth(px) {
  const w = Math.max(EDITOR_MIN, Math.min(EDITOR_MAX, Math.round(px)));
  document.documentElement.style.setProperty('--editor-w', w + 'px');
  try { localStorage.setItem('mt.editorWidth', String(w)); } catch (e) { /* 隐身模式忽略 */ }
  return w;
}

function currentEditorWidth() {
  const raw = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--editor-w'), 10);
  return Number.isFinite(raw) && raw > 0 ? raw : EDITOR_DEFAULT;
}

function initEditorResizer() {
  try {
    const saved = parseInt(localStorage.getItem('mt.editorWidth') || '', 10);
    if (Number.isFinite(saved)) applyEditorWidth(saved);
  } catch (e) { /* 忽略 */ }

  const handle = document.getElementById('editorResizer');
  if (!handle) return;
  let startX = 0;
  let startW = 0;
  let dragging = false;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragging = true;
    startX = e.clientX;
    startW = currentEditorWidth();
    document.body.classList.add('resizing-editor');
  });

  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    // 面板贴右边：鼠标往左移 = 变宽
    applyEditorWidth(startW + (startX - e.clientX));
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('resizing-editor');
    // 等画布真的变窄/变宽之后再适配：鼠标松开的这一瞬间，
    // .slab 的 margin-right 还没重排完，立刻算会按旧宽度算，节点就会被判越界。
    setTimeout(() => {
      try { fitView(false); } catch (err) { /* 画布还没准备好就算了 */ }
    }, 120);
  });

  handle.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    applyEditorWidth(EDITOR_DEFAULT);
    try { fitView(false); } catch (err) { /* 忽略 */ }
  });
}

/** 从最近完成的任务里挑一个文件挂到当前节点 */
async function attachFromRecent() {
  if (!editingId) { toastMsg('先选一个节点'); return; }
  let tasks = [];
  try {
    const r = await fetch('/api/tasks').then((x) => x.json());
    tasks = (r.tasks || []).filter((t) => t.status === 'done' && (t.files || []).length);
  } catch (e) {
    mtAlert('读取失败', e.message);
    return;
  }
  const files = [];
  for (const t of tasks.slice(0, 30)) {
    for (const f of t.files) {
      if (f && f.path && ['video', 'audio', 'cover', 'subtitle', 'document'].includes(f.kind)) {
        files.push({ path: f.path, name: f.name || f.path, task: t.title || '' });
      }
    }
  }
  if (!files.length) {
    await mtAlert('没有可用的结果', '最近没有已完成的下载或转换任务。\n\n你也可以用「挂文件」直接选一个磁盘上的文件。');
    return;
  }
  const picked = await new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'mt-modal';
    const box = document.createElement('div');
    box.className = 'mt-modal-box mt-pick-box';
    box.innerHTML = '<div class="mt-modal-title">从最近的结果挂文件</div>'
      + '<div class="mt-modal-body">点一项就会复制进项目并挂到当前节点。</div>'
      + '<div class="mt-pick-list"></div>'
      + '<div class="mt-modal-row"><button class="btn text" data-act="cancel">取消</button></div>';
    const list = box.querySelector('.mt-pick-list');
    for (const f of files.slice(0, 40)) {
      const row = document.createElement('button');
      row.className = 'mt-pick-item';
      row.innerHTML = '<span class="mt-pick-name"></span><span class="mt-pick-sub"></span>';
      row.querySelector('.mt-pick-name').textContent = f.name;
      row.querySelector('.mt-pick-sub').textContent = f.task;
      row.onclick = () => { host.remove(); resolve(f); };
      list.append(row);
    }
    box.querySelector('[data-act="cancel"]').onclick = () => { host.remove(); resolve(null); };
    host.onclick = (e) => { if (e.target === host) { host.remove(); resolve(null); } };
    host.append(box);
    document.body.append(host);
  });
  if (!picked) return;
  try {
    await window.MTApi.attach(projectId, editingId, picked.path);
    await reloadProject();
    toastMsg('已挂上：' + picked.name);
  } catch (e) {
    await mtAlert('挂载失败', e.message);
  }
}

/** 问 AI：先在本地检索，再把命中的片段发给用户配置的端点。 */
async function askAI() {
  const host = document.createElement('div');
  host.className = 'mt-modal';
  host.innerHTML = '<div class="mt-modal-box mt-ask-box">'
    + '<div class="mt-modal-title">问 AI</div>'
    + '<div class="mt-modal-body">问题会先在本地资料里检索，命中的片段才会发到你配置的端点。</div>'
    + '<textarea class="input mt-ask-input" placeholder="例如：我论文里关于时钟门控的部分写了什么？"></textarea>'
    + '<div class="mt-ask-result"></div>'
    + '<div class="mt-modal-row"><button class="btn text" data-act="cancel">关闭</button>'
    + '<button class="btn" data-act="send">提问</button></div></div>';
  document.body.append(host);
  const input = host.querySelector('.mt-ask-input');
  const out = host.querySelector('.mt-ask-result');
  const sendBtn = host.querySelector('[data-act="send"]');
  const close = () => host.remove();
  host.querySelector('[data-act="cancel"]').onclick = close;
  host.onclick = (e) => { if (e.target === host) close(); };
  setTimeout(() => input.focus(), 50);

  sendBtn.onclick = async () => {
    const question = input.value.trim();
    if (!question) { input.focus(); return; }
    sendBtn.disabled = true;
    out.textContent = '正在检索并提问…';
    try {
      const r = await window.MTApi.aiAsk(question);
      const hits = (r.hits || []).map((h) => '· ' + h.project + ' / ' + h.node).join('\n');
      out.innerHTML = '';
      const ans = document.createElement('div');
      ans.className = 'mt-ask-answer';
      ans.textContent = r.answer || '(没有返回内容)';
      out.append(ans);
      if (hits) {
        const src = document.createElement('div');
        src.className = 'mt-ask-sources';
        src.textContent = '参考：\n' + hits;
        out.append(src);
      }
    } catch (e) {
      out.textContent = '失败：' + e.message;
    }
    sendBtn.disabled = false;
  };
}

/** 面板回到"没选节点"的状态（面板本身还在） */
function showEditorEmpty() {
  editorHost.classList.remove('has-node');
  const wrap = editorHost.querySelector('.editor');
  const empty = document.getElementById('editorEmpty');
  if (wrap) wrap.style.display = 'none';
  if (empty) empty.style.display = 'flex';
  editingId = null;
  document.body.classList.add('editing');
}

function closeEditor() {
  showEditorEmpty();
}

/* 把当前节点并进父节点，变成挂件 */
async function mergeIntoParent(node) {
  if (!node) return;
  await saveGraphNow();   // 先把最新结构写给服务端
  if (node.id === graph.trunk.id) { mtAlert('主干不能并入别的节点'); return; }
  if (!node.parent) { mtAlert('这个节点是浮空的，先把它接到某个节点下面才能并入'); return; }
  const hasAny = (n) => n.file || (n.assets && n.assets.length);
  if (!hasAny(node)) { mtAlert('这个节点既没有正文也没有挂文件，先写点内容或挂个文件再来并入'); return; }
  const kids = graph.nodes.filter((n) => n.parent === node.id);
  const empty = kids.filter((n) => !hasAny(n));
  if (empty.length) { mtAlert('子节点「' + empty[0].title + '」也是空的，先补内容或删掉它'); return; }
  if (online && window.MTApi) {
    const keepFiles = {};
    const gather = [node, ...graph.nodes.filter((n) => n.parent === node.id)];
    for (const n of gather) {
      if (!n.file) continue;
      try {
        const r = await window.MTApi.nodeRead(projectId, n.file);
        if (r && typeof r.body === 'string') keepFiles[n.file] = r.body;
      } catch (e) { /* 忽略 */ }
    }
    pushUndo('并入父节点', { files: keepFiles });
  }
  if (!(await mtDialog({
    title: '并入父节点',
    body: '把「' + node.title + '」' + (kids.length ? ' 及其 ' + kids.length + ' 个子节点' : '')
      + ' 并进父节点，变成它的挂件？\n\n文件不会丢，会挂到父节点的附件里；画布上这些节点会消失（可 Ctrl+Z 撤销）。',
    okText: '并入',
  }))) return;
  try {
    const r = await window.MTApi.nodeMerge(projectId, node.id);
    graph.trunk = r.graph.trunk || graph.trunk;
    graph.nodes = r.graph.nodes || graph.nodes;
    graph.links = r.graph.links || [];
    closeEditor();
    markDirty(false);
    render();
    toastMsg('已并入父节点，' + r.merged + ' 个文件变成挂件');
  } catch (e) { mtAlert('并入失败：' + e.message); }
}

document.getElementById('editorOpenFile').addEventListener('click', async () => {
  const node = editingId ? byId(editingId) : null;
  if (!node) return;
  if (!node.file) { mtAlert('这个节点还没有正文文件，先保存一次正文'); return; }
  try {
    await window.MTApi.openFile(projectId, node.file);
    toastMsg('已用系统程序打开 ' + node.file);
  } catch (e) { mtAlert('打开失败：' + e.message); }
});

document.getElementById('editorMerge').addEventListener('click', () => {
  mergeIntoParent(editingId ? byId(editingId) : null);
});

document.getElementById('editorLink').addEventListener('click', () => {
  const node = editingId ? byId(editingId) : null;
  if (node) openLinkMode(node);
});

document.getElementById('editorCancel').addEventListener('click', closeEditor);

// 键盘：Esc 关面板/取消连线，Ctrl+Enter 保存
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (linkModeFrom) { endLinkMode(); return; }
    if (editorHost.classList.contains('open')) { closeEditor(); return; }
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    if (editorHost.classList.contains('open')) {
      e.preventDefault();
      document.getElementById('editorSave').click();
    }
  }
});
canvas.addEventListener('contextmenu', (e) => {
  if (linkModeFrom) { e.preventDefault(); endLinkMode(); return; }
  if (e.target.closest('.node')) return;
  e.preventDefault();
  showCtxMenu(e.clientX, e.clientY, null);
});

/* 删除节点：连带子孙一起删，文件去留让用户决定，删完保存版本 */
document.getElementById('editorDelete').addEventListener('click', async () => {
  const node = editingId ? byId(editingId) : null;
  if (!node) { closeEditor(); return; }
  if (node.id === graph.trunk.id) { await mtAlert('主干不能删除'); return; }

  // 先数清楚会删掉多少（含所有层级）
  const doomed = new Set();
  const stack = [node.id];
  while (stack.length) {
    const cur = stack.pop();
    if (doomed.has(cur)) continue;
    doomed.add(cur);
    graph.nodes.filter((n) => n.parent === cur).forEach((n) => stack.push(n.id));
  }
  const withFile = graph.nodes.filter((n) => doomed.has(n.id) && n.file).length;
  const ok = await mtDialog({
    title: '删除节点',
    body: '删除「' + node.title + '」' + (doomed.size > 1 ? ' 及其 ' + (doomed.size - 1) + ' 个子节点' : '')
      + '？\n\n' + (withFile ? '其中有 ' + withFile + ' 个节点带 .md 文件，下一步再决定要不要一起删。' : '这些节点没有对应的文件。'),
    okText: '删除', danger: true,
  });
  if (!ok) return;
  await saveGraphNow();   // 确认之后才落盘：先弹窗再等网络，按钮不会像卡住
  // 记下这些节点的正文文件内容，撤销时能原样写回
  const keepFiles = {};
  for (const n of graph.nodes) {
    if (!doomed.has(n.id) || !n.file) continue;
    try {
      const r = await window.MTApi.nodeRead(projectId, n.file);
      if (r && typeof r.body === 'string') keepFiles[n.file] = r.body;
    } catch (e) { /* 读不到就算了 */ }
  }
  const purge = withFile ? await mtDialog({
    title: '磁盘文件要一起删吗',
    body: '确定 = 连 .md 文件一起删（之后可以 Ctrl+Z 撤销，内容能写回来）\n取消 = 只删节点，文件留在项目里',
    okText: '一起删', cancelText: '保留文件', danger: true,
  }) : false;

  if (online && window.MTApi) {
    const keepFiles = {};
    for (const n of graph.nodes) {
      if (!doomed.has(n.id) || !n.file) continue;
      try {
        const r = await window.MTApi.nodeRead(projectId, n.file);
        if (r && typeof r.body === 'string') keepFiles[n.file] = r.body;
      } catch (e) { /* 读不到就不记 */ }
    }
    pushUndo('删除节点', { files: keepFiles });
  }

  if (online && window.MTApi) {
    try {
      const r = await window.MTApi.nodeDelete(projectId, node.id, purge);
      graph.trunk = r.graph.trunk || graph.trunk;
      graph.nodes = r.graph.nodes || [];
      graph.staged = r.graph.staged || [];
      closeEditor();
      markDirty(false);
      render();
      let msg = '已删除 ' + r.removed + ' 个节点';
      if (r.removed_files.length) msg += '，同时删掉 ' + r.removed_files.length + ' 个文件';
      if ((r.orphan_assets || []).length) msg += '；' + r.orphan_assets.length + ' 个附件保留在项目里';
      if ((r.failed_files || []).length) msg += '；' + r.failed_files.length + ' 个文件删不掉（正被占用）';
      if (r.commit && r.commit.committed) {
        toastAction(msg, '撤销', async () => {
          try {
            const u = await window.MTApi.undo(projectId);
            graph.trunk = u.graph.trunk || graph.trunk;
            graph.nodes = u.graph.nodes || [];
            graph.staged = u.graph.staged || [];
            markDirty(false);
            render();
            toastMsg('已撤销这次删除');
          } catch (e) { mtAlert('撤销失败：' + e.message); }
        }, 10000);
      } else {
        toastMsg(msg + (r.commit && r.commit.error ? '（保存失败：' + r.commit.error + '）' : ''));
      }
    } catch (e) { mtAlert('删除失败：' + e.message); }
    return;
  }
  // 离线：只改内存
  graph.nodes = graph.nodes.filter((n) => !doomed.has(n.id));
  closeEditor();
  markDirty(true);
  render();
});
editorHost.addEventListener('click', (e) => { if (e.target === editorHost) closeEditor(); });

document.getElementById('editorSave').addEventListener('click', async () => {
  const node = editingId ? byId(editingId) : null;
  if (!node) { closeEditor(); return; }
  node.title = editorName.value.trim() || node.title;
  node.body = editorBody.value;

  // 正文写成项目里的 .md 文件：之后用 Typora / Word 改它也能被监听
  if (online && window.MTApi) {
    try {
      const r = await window.MTApi.nodeWrite(projectId, node, node.body);
      node.file = r.file;
      node.summary = r.summary;
      if (!node.floating) node.summary = node.summary || node.summary;
      markDirty(true);
      await window.MTApi.saveGraph(projectId, { trunk: graph.trunk, nodes: graph.nodes, staged: graph.staged });
    } catch (e) {
      toastMsg('写文件失败：' + e.message);
    }
  }
  // 摘要是正文的前两行，压成一行挂到节点上
  const text = (node.body || '').trim();
  node.summary = text
    ? text.split('\n').filter(Boolean).slice(0, 2).join(' ').slice(0, 46)
    : '';
  delete node.note;
  markDirty();
  render();
  closeEditor();
});

/* ---------------------------------------------------------- 接真后端
   有 server.py 在跑：读项目目录里的 .mt/graph.json，保存＝git commit。
   没在跑：退回内置假数据，页面仍然可以看效果。 */
let projectId = projectIdRaw;
let online = false;

async function bootAsync() {
  const api = window.MTApi;
  if (!api) return;
  online = await api.available().catch(() => false);
  if (!online) { console.log('未连接后端，使用内置示例数据'); return; }
  try {
    const d = await api.load(projectId);
    const g = d.graph || {};
    if (g && (g.trunk || (g.nodes && g.nodes.length))) {
      graph.trunk = g.trunk;
      graph.nodes = g.nodes || [];
      graph.staged = g.staged || [];
    graph.links = g.links || [];
    graph.freeLayout = !!g.freeLayout;
    }
    if (d.project) {
      document.getElementById('pName').textContent = d.project.name || project.name;
      document.getElementById('pPath').textContent = d.project.path || '';
      projectId = d.project.id || projectId;
    }
    markDirty(false);
    render();
    // 首次进项目时把内容框进视野，省得用户一进来啥也看不到
    if (!window.__autoFitted) {
      window.__autoFitted = true;
      // 适配交给 autoFitSoon（render 之后自动做），这里不再插一脚
    }
    const h = await api.head(projectId);
    if (h.known && h.head && h.known !== h.head) {
      const host = document.getElementById('bannerHost');
      if (host && !host.querySelector('.banner')) {
        const b = document.createElement('div');
        b.className = 'banner';
        b.innerHTML = '<span>⚠</span><span>这个项目被外部 git 修改过（现在 HEAD '
          + String(h.head).slice(0, 7) + '，上次记录 ' + String(h.known).slice(0, 7) + '）</span>'
          + '<span style="flex:1"></span>';
        const btn = document.createElement('button');
        btn.className = 'btn text';
        btn.textContent = '知道了';
        btn.onclick = () => { api.remember(projectId).catch(() => {}); b.remove(); };
        b.append(btn);
        host.append(b);
      }
    }
  } catch (e) {
    console.warn('读取项目失败：' + e.message);
  }
}

/** 带一个操作按钮的提示，例如「已删除 3 个节点 [撤销]」 */
function stackToasts() {
  const list = [...document.querySelectorAll('.mt-toast')];
  list.forEach((t, i) => { t.style.bottom = (26 + i * 46) + 'px'; });
}

function toastAction(text, label, onAction, ms = 8000) {
  const t = document.createElement('div');
  t.className = 'mt-toast';
  t.textContent = text;
  const btn = document.createElement('button');
  btn.textContent = label;
  btn.style.cssText = 'background:transparent;border:none;color:var(--md-primary);' +
    'font:inherit;cursor:pointer;padding:0 2px';
  btn.onclick = () => { t.remove(); onAction(); };
  t.append(btn);
  document.body.append(t);
  stackToasts();
  setTimeout(() => { t.remove(); stackToasts(); }, ms);
}

function toastMsg(text, ms = 1800) {
  const t = document.createElement('div');
  t.className = 'mt-toast';
  t.textContent = text;
  document.body.append(t);
  stackToasts();
  setTimeout(() => { t.remove(); stackToasts(); }, ms);
}

/* ---------------------------------------------------------- 监听状态 / 历史面板 */
let autoEnabled = true;
let lastStatus = null;   // 最近一次 /api/project/status 的结果，退出检查要用

function initWatchUI() {
  const chip = document.getElementById('watchStatus');
  const toggle = document.getElementById('autoToggle');
  const saveBtn2 = document.getElementById('save');

  toggle.addEventListener('click', async () => {
    autoEnabled = !autoEnabled;
    toggle.textContent = '自动保存：' + (autoEnabled ? '开' : '关');
    toggle.classList.toggle('tonal', autoEnabled);
    if (window.MTApi && online) {
      try { await window.MTApi.pause(projectId, !autoEnabled); } catch (e) { /* 忽略 */ }
    }
  });

  async function tick() {
    if (!(window.MTApi && online)) {
      chip.textContent = '未连接后端';
      return;
    }
    try {
      const w = (await window.MTApi.watch(projectId)).watch || {};
      if (w.paused) {
        chip.textContent = '已暂停' + (w.pause_reason ? '：' + w.pause_reason : '') + '（改动不会自动保存）';
        chip.style.color = '#e8b86d';
      } else if (w.pending_count) {
        chip.textContent = w.state === 'waiting'
          ? '检测到 ' + w.pending_count + ' 个文件改动，' + Math.ceil(w.seconds_left) + ' 秒后自动保存'
          : '等待保存 ' + w.pending_count + ' 个文件';
        chip.style.color = 'var(--md-primary)';
      } else {
        chip.textContent = '监听中 · 已自动保存 ' + (w.commits || 0) + ' 次';
        chip.style.color = '';
      }
      if (w.last_error) { chip.textContent = '出错了：' + w.last_error; chip.style.color = '#ffb4ab'; }
      saveBtn2.disabled = !w.pending_count;
      // 顺便记住磁盘状态：有没有未保存、有没有被别的程序占着
      window.MTApi.status(projectId).then((s) => { lastStatus = s; }).catch(() => {});
    } catch (e) {
      chip.textContent = '状态读取失败';
    }
  }
  tick();
  setInterval(tick, 2000);
}

function openHistory() {
  const host = document.getElementById('historyHost');
  const list = document.getElementById('historyList');
  host.classList.add('open');
  list.innerHTML = '<div class="hint">读取中…</div>';

  window.MTApi.log(projectId).then((d) => {
    let commits = d.commits || [];
    // 按时间从新到旧整体排序（服务端已经是这个顺序，这里再保证一次）
    commits = commits.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    list.innerHTML = '';
    if (!commits.length) {
      list.innerHTML = '<div class="hint">还没有保存过版本</div>';
      return;
    }

    // 按「今天 / 昨天 / 更早」分组，长列表更好扫
    const dayKey = (ts) => {
      const d2 = new Date((ts || 0) * 1000);
      const now = new Date();
      const same = (x, y) => x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
      if (same(d2, now)) return '今天';
      const yst = new Date(now.getTime() - 86400000);
      if (same(d2, yst)) return '昨天';
      const days = Math.floor((now - d2) / 86400000);
      if (days < 7) return '最近一周';
      return d2.getFullYear() + ' 年 ' + (d2.getMonth() + 1) + ' 月';
    };

    let lastGroup = null;
    for (const c of commits.slice(0, 60)) {
      const g = dayKey(c.ts);
      if (g !== lastGroup) {
        lastGroup = g;
        const head = document.createElement('div');
        head.className = 'history-group';
        head.textContent = g;
        list.append(head);
      }

      const row = document.createElement('div');
      row.className = 'history-row';
      const files = c.files || [];
      row.innerHTML =
        '<div class="history-main">' +
          '<div class="history-subject">' + (c.subject || '(无说明)').replace(/\n/g, ' ') + '</div>' +
          '<div class="history-meta">' + c.date + ' · ' + c.short +
            (files.length ? ' · ' + files.length + ' 个文件' : '') + '</div>' +
        '</div>';
      const acts = document.createElement('div');
      acts.className = 'history-acts';

      const btnBack = document.createElement('button');
      btnBack.className = 'btn text';
      btnBack.textContent = '回到这个版本';
      btnBack.onclick = (ev) => { ev.stopPropagation(); rollbackToCommit(c); };
      acts.append(btnBack);

      if (files.length) {
        const btnFiles = document.createElement('button');
        btnFiles.className = 'btn text';
        btnFiles.textContent = '文件';
        btnFiles.onclick = (ev) => {
          ev.stopPropagation();
          box.style.display = box.style.display === 'block' ? 'none' : 'block';
        };
        acts.append(btnFiles);
      }
      row.append(acts);

      const box = document.createElement('div');
      box.className = 'history-files';
      if (files.length) {
        for (const f of files) {
          const line = document.createElement('div');
          line.className = 'history-file';
          line.innerHTML = '<span class="history-file-name">' + f + '</span>';
          const b = document.createElement('button');
          b.className = 'mini-btn';
          b.textContent = '只还原这个文件';
          b.onclick = async (ev) => {
            ev.stopPropagation();
            const okFile = await mtDialog({
            title: '恢复单个文件', body: '把 ' + f + ' 恢复到 ' + c.short + ' 这个版本？\n\n只会改这一个文件。',
            okText: '恢复',
          });
          if (!okFile) return;
            try {
              const rr = await window.MTApi.restore(projectId, c.sha, [f]);
              await applyGraphFromServer(rr);
              toastMsg('已恢复 ' + f);
            } catch (e) { await mtAlert('恢复失败', e.message); }
          };
          line.append(b);
          box.append(line);
        }
      }
      row.onclick = () => { if (files.length) box.style.display = box.style.display === 'block' ? 'none' : 'block'; };

      const wrap = document.createElement('div');
      wrap.className = 'history-item';
      wrap.append(row, box);
      list.append(wrap);
    }
  }).catch((e) => { list.innerHTML = '<div class="hint">读取失败：' + e.message + '</div>'; });
}

/** 把服务端返回的最新结构套到画布上 */
async function applyGraphFromServer(resp) {
  let g = resp && resp.graph;
  if (!g) g = (await window.MTApi.load(projectId)).graph || {};
  graph.trunk = g.trunk || graph.trunk;
  graph.nodes = g.nodes || [];
  graph.staged = g.staged || [];
  graph.links = g.links || [];
  markDirty(false);
  render();
}

/** 回滚时有些文件因为太大没有存内容，这里如实告诉用户 */
async function reportSkippedFiles(resp, shortSha) {
  const skipped = (resp && resp.skipped) || [];
  if (!skipped.length) return;
  await mtAlert('这些大文件没有还原', '回到 ' + shortSha + ' 时，下面这些文件被跳过了：\n\n  '
    + skipped.slice(0, 12).join('\n  ')
    + (skipped.length > 12 ? '\n  …还有 ' + (skipped.length - 12) + ' 个' : '')
    + '\n\n它们超过 5MB，历史里只记了文件名和大小，没有存内容，所以没法还原。磁盘上的当前文件没有动。');
}

/** 整份回到某个版本 */
async function rollbackToCommit(c) {
  const ok = await mtDialog({
    title: '回到这个版本',
    body: '把整个项目回到 ' + c.short + '？\n\n'
      + '版本说明：' + (c.subject || '(无)') + '\n\n'
      + '确定 = 把所有文件恢复到那个版本的样子（会改动磁盘文件）\n'
      + '取消 = 什么都不做',
    okText: '回到这个版本',
  });
  if (!ok) return;
  try {
    const r = await window.MTApi.restore(projectId, c.sha, []);
    await applyGraphFromServer(r);
    await reportSkippedFiles(r, c.short);
    toastMsg('已整体回到 ' + c.short + '，确认无误后点「保存版本」记成一个新版本', 6000);
  } catch (e) {
    mtAlert('恢复失败：' + e.message);
  }
}

/** 清理没有被任何版本引用的内容块 */
async function cleanHistory() {
  let stats = null;
  try { stats = (await window.MTApi.historyStats(projectId)).stats; } catch (e) { /* 拿不到统计也能清 */ }
  const ok = await mtDialog({
    title: '清理历史数据',
    body: '历史库里目前有 ' + (stats ? stats.versions : '?') + ' 个版本、'
      + (stats ? (stats.bytes / 1024 / 1024).toFixed(2) + ' MB 内容' : '若干内容') + '。\n\n'
      + '清理会删掉「已经删掉的分支」留在历史库里的版本，以及没有人引用的内容块。'
      + '当前分支上的版本一个都不会少，项目里的文件也不会被改。',
    okText: '清理',
  });
  if (!ok) return;
  try {
    const r = await window.MTApi.gcProject(projectId);
    const freed = Math.max(0, (r.before.bytes - r.after.bytes) / 1024);
    await mtAlert('清理完成', freed > 0
      ? '释放了 ' + freed.toFixed(1) + ' KB，删掉 ' + (r.removed_versions || 0) + ' 个孤立版本、'
        + r.removed_blocks + ' 个没人引用的内容块。\n\n现在占用 '
        + (r.after.bytes / 1024 / 1024).toFixed(2) + ' MB。'
      : '没有可清理的内容（所有内容块都还有版本在用）。');
  } catch (e) {
    await mtAlert('清理失败', e.message);
  }
}

/** 把快照历史导出成真正的 git 仓库 */
async function exportGit() {
  const ok = await mtDialog({
    title: '导出为 Git 仓库',
    body: '会在项目旁边建一个「项目名-git」的文件夹，把这套历史重放成真正的 git 提交。\n\n'
      + '需要系统里装了 Git；原来的项目不受影响。',
    okText: '导出',
  });
  if (!ok) return;
  try {
    const r = await window.MTApi.exportGit(projectId);
    await mtAlert('导出完成', '共导出 ' + r.exported + ' 个版本。\n\n位置：\n' + r.target);
  } catch (e) {
    await mtAlert('导出失败', e.message);
  }
}

initEditorResizer();

const recentBtn = document.getElementById('editorRecent');
if (recentBtn) recentBtn.onclick = () => attachFromRecent();

const askBtn = document.getElementById('editorAsk');
if (askBtn) askBtn.onclick = () => askAI();

/** 打开项目文件夹，并把"能动什么、不能动什么"说清楚。 */
async function openProjectFolder() {
  const ok = await mtDialog({
    title: '打开项目文件夹',
    body: '这里就是你的项目文件夹，正文、附件、版本库都在里面。\n\n'
      + '【随便改没问题】\n'
      + '· .md 正文文件：用 Typora / VS Code 改，回来程序会认\n'
      + '· 附件（assets/ 下的文件）：改名、替换都行，下次挂载会重新认\n'
      + '· 新增普通文件：不会影响程序\n\n'
      + '【别动，动了会出问题】\n'
      + '· .mt/ 目录（版本库 history.db）：删了历史就没了，改坏了项目可能打不开\n'
      + '· .mt/graph.json：这是节点结构，改坏会导致节点对不上文件\n'
      + '· 直接把 .md 文件删掉：节点还在，但打开时正文是空的\n\n'
      + '要现在打开吗？',
    okText: '打开文件夹',
  });
  if (!ok) return;
  try {
    await window.MTApi.shellOpen(projectPath, false);
  } catch (e) {
    await mtAlert('打不开', e.message);
  }
}

const gcBtnEl = document.getElementById('historyGc');
if (gcBtnEl) gcBtnEl.onclick = () => cleanHistory();
const exportBtnEl = document.getElementById('historyExport');
if (exportBtnEl) exportBtnEl.onclick = () => exportGit();

/** 一键备份：把整个项目目录（含 .mt 版本库）打成一个 zip。 */
async function backupProject() {
  const ok = await mtDialog({
    title: '备份整个项目',
    body: '会把项目文件夹里的所有东西打成一个 zip：正文、附件、还有版本库（.mt 目录）。\n\n'
      + '解开这个 zip 就是一份完整的项目，不依赖程序本身。\n\n'
      + '默认存到「文档」文件夹，文件名带时间戳。',
    okText: '开始备份',
  });
  if (!ok) return;
  try {
    const r = await window.MTApi.backup(projectId);
    await mtAlert('备份完成', '共 ' + r.files + ' 个文件，' + (r.bytes / 1048576).toFixed(1) + ' MB\n\n' + r.path);
  } catch (e) {
    await mtAlert('备份失败', e.message);
  }
}
const backupBtnEl = document.getElementById('historyBackup');
if (backupBtnEl) backupBtnEl.onclick = () => backupProject();

const folderBtn = document.getElementById('openFolder');
if (folderBtn) folderBtn.onclick = () => openProjectFolder();

document.getElementById('historyClose').addEventListener('click', () => {
  document.getElementById('historyHost').classList.remove('open');
});
document.getElementById('historyHost').addEventListener('click', (e) => {
  if (e.target.id === 'historyHost') e.currentTarget.classList.remove('open');
});

// 自动适配的状态要在第一次 render 之前就位（let 有暂时性死区）
let userAdjustedView = false;
let autoFitTimer = 0;
function autoFitSoon() {
  if (userAdjustedView) return;
  clearTimeout(autoFitTimer);
  autoFitTimer = setTimeout(() => {
    try { fitView(false); } catch (e) { /* 忽略 */ }
  }, 80);
}

select(null);
showEditorEmpty();
render();
bootAsync();

initWatchUI();
initMarkdownEditor();
// 任何编辑框的输入都算"脏"，此时 Ctrl+Z 交给文本撤销
for (const el of document.querySelectorAll('#editorHost input, #editorHost textarea')) {
  el.addEventListener('input', () => { editorDirty = true; });
}

const undoBtnEl = document.getElementById('undoBtn');
const redoBtnEl = document.getElementById('redoBtn');
if (undoBtnEl) undoBtnEl.onclick = () => doUndo();
if (redoBtnEl) redoBtnEl.onclick = () => doRedo();
updateUndoButtons();
window.addEventListener('resize', render);
