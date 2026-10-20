/* 原型用的假数据。真实接入后用 git 索引替代。 */
const STORE_KEY = 'mt-projects';

/* file:// 下访问 localStorage 会抛 SecurityError（Chromium 限制），
   原型要能双击直接打开，所以这里做一层容错：不行就退回内存。 */
const _mem = {};
const store = {
  get(k) {
    try { return localStorage.getItem(k); } catch (e) { return _mem[k] === undefined ? null : _mem[k]; }
  },
  set(k, v) {
    try { localStorage.setItem(k, v); } catch (e) { _mem[k] = v; }
  },
};

const MOCK = [
  {
    id: 'p1',
    name: '论文写作 · 低功耗芯片',
    path: 'D:\\我的项目\\低功耗芯片',
    branches: 4,
    commits: 37,
    updated: '2 小时前',
    external: false,
  },
  {
    id: 'p2',
    name: '开题报告',
    path: 'D:\\我的项目\\开题报告',
    branches: 2,
    commits: 12,
    updated: '昨天',
    external: true,
    externalNote: '这个项目被外部 git 修改过（3 个提交）',
  },
];

function loadProjects() {
  const raw = store.get(STORE_KEY);
  if (raw) { try { return JSON.parse(raw); } catch (e) { /* 坏数据就退回假数据 */ } }
  store.set(STORE_KEY, JSON.stringify(MOCK));
  return MOCK.slice();
}

function saveProjects(list) {
  store.set(STORE_KEY, JSON.stringify(list));
}

function createProject(name, path) {
  const list = loadProjects();
  const item = {
    id: 'p' + Date.now(),
    name,
    path,
    branches: 0,
    commits: 0,
    updated: '刚刚',
    external: false,
  };
  list.unshift(item);
  saveProjects(list);
  return item;
}

/* 画布初始结构：一条主干 + 两条分支 + 一个浮空节点 */
const MOCK_GRAPH = {
  trunk: { id: 't1', title: '低功耗芯片设计', note: '主干 · 来自 引言.md' },
  nodes: [
    { id: 'n1', title: '研究背景', note: '3 条备注', parent: 't1', branch: 'b1', kind: 'branch' },
    { id: 'n2', title: '相关工作', note: '已挂 2 个文件', parent: 't1', branch: 'b2', kind: 'branch' },
    { id: 'n3', title: 'DVS 方案', parent: 'n1', branch: 'b1', kind: 'leaf' },
    { id: 'n4', title: '动态电压调节', parent: 'n1', branch: 'b1', kind: 'leaf' },
    { id: 'n5', title: '存内计算', parent: 'n2', branch: 'b2', kind: 'leaf' },
    { id: 'n6', title: '浮空的思考', note: '还没决定挂在哪', parent: null, branch: null, kind: 'floating' },
  ],
  staged: [
    { id: 's1', title: '参考文献.pdf' },
    { id: 's2', title: '实验数据.csv' },
  ],
};

/* 挂到全局：file:// 下 ES 模块会被 CORS 拦住，原型直接用普通脚本 */
window.MTData = { loadProjects, saveProjects, createProject, MOCK_GRAPH };
