/* 原型的前端接口层。
   有 server.py 在跑就走真 git；没有就退回本地假数据，页面照样能看。 */
const BASE = '';

/* 在 MT 里运行时，写操作要带 X-MakiseToolkit-Token（防跨站调用） */
let TOKEN = '';
async function ensureToken() {
  if (TOKEN) return TOKEN;
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    TOKEN = (d && d.api_token) || '';
  } catch (e) { TOKEN = ''; }
  return TOKEN;
}

async function call(path, body) {
  const token = await ensureToken();
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['X-MakiseToolkit-Token'] = token;
  const r = await fetch(BASE + path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({ ok: false, error: '响应不是 JSON' }));
  if (!data.ok) throw new Error(data.error || '请求失败');
  return data;
}

let alive = null;
async function available() {
  if (alive !== null) return alive;
  try {
    const st = await fetch(BASE + '/api/state');
    const d = await st.json();
    alive = !!(d && d.ok);      // MT 内置了项目管理，只要应用在跑就可用
  } catch (e) {
    alive = false;
  }
  return alive;
}

window.MTApi = {
  available,
  list: () => call('/api/projects'),
  create: (name, path) => call('/api/project/create', { name, path }),
  load: (id) => call('/api/project/load', { id }),
  saveGraph: (id, graph) => call('/api/project/graph', { id, graph }),
  commit: (id, graph, message) => call('/api/project/commit', { id, graph, message }),
  status: (id) => call('/api/project/status', { id }),
  log: (id) => call('/api/project/log', { id }),
  historyStats: (id) => call('/api/project/history-stats', { id }),
  gcProject: (id) => call('/api/project/gc', { id }),
  exportGit: (id) => call('/api/project/export-git', { id }),
  restore: (id, commit, files) => call('/api/project/restore', { id, commit, files }),
  head: (id) => call('/api/project/head', { id }),
  remember: (id) => call('/api/project/remember', { id }),
  watch: (id) => call('/api/project/watch', { id }),
  pause: (id, paused) => call('/api/project/pause', { id, paused }),
  commitNow: (id) => call('/api/project/commit-now', { id }),
  show: (id, sha) => call('/api/project/show', { id, sha }),
  nodeWrite: (id, node, body) => call('/api/project/node/write', { id, node, body }),
  nodeRead: (id, file) => call('/api/project/node/read', { id, file }),
  settings: (id, patch) => call('/api/project/settings', { id, patch }),
  branches: (id) => call('/api/project/branches', { id }),
  checkout: (id, branch) => call('/api/project/checkout', { id, branch }),
  merge: (id, branch) => call('/api/project/merge', { id, branch }),
  resolve: (id, file, side) => call('/api/project/resolve', { id, file, side }),
  mergeState: (id) => call('/api/project/merge-state', { id }),
  mergeAbort: (id) => call('/api/project/merge-abort', { id }),
  nodeMerge: (id, nodeId) => call('/api/project/node/merge', { id, nodeId }),
  nodeDelete: (id, nodeId, deleteFiles) => call('/api/project/node/delete', { id, nodeId, delete_files: deleteFiles }),
  stagedRemove: (id, stagedId) => call('/api/project/staged/remove', { id, stagedId }),
  branchDelete: (id, branch, force) => call('/api/project/branch/delete', { id, branch, force }),
  pickFile: () => call('/api/project/pick-file', {}),
  pickPoll: () => call('/api/project/pick-file/poll', {}),
  rename: (id, name) => call('/api/project/rename', { id, name }),
  deleteProject: (id, deleteFolder) => call('/api/project/delete', { id, delete_folder: deleteFolder }),
  stagedAddFile: (id, source) => call('/api/project/staged/add-file', { id, source }),
  stagedAttach: (id, nodeId, path) => call('/api/project/staged/attach', { id, nodeId, path }),
  aiAsk: (question) => call('/api/ai/ask', { question, use_context: true }),
  attach: (id, nodeId, source) => call('/api/project/attach', { id, nodeId, source }),
  assetRemove: (id, nodeId, path, deleteFile) => call('/api/project/asset/remove', { id, nodeId, path, delete_file: deleteFile }),
  openFile: (id, path) => call('/api/project/open', { id, path }),
  assetRead: (id, path) => call('/api/project/asset/read', { id, path }),
  assetWrite: (id, path, text) => call('/api/project/asset/write', { id, path, text }),
  hash: (id, path) => call('/api/project/hash', { id, path }),
  undo: (id) => call('/api/project/undo', { id }),
  conflictRead: (id, file) => call('/api/project/conflict/read', { id, file }),
  conflictWrite: (id, file, content, resolve) => call('/api/project/conflict/write', { id, file, content, resolve }),
  mergeFinish: (id) => call('/api/project/merge-finish', { id }),
};
