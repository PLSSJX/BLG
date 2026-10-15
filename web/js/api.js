/** 后端 JSON API 的极薄封装。 */

// 写操作要带令牌，防止浏览器里其它页面跨站调用本机接口
let API_TOKEN = '';

export function setToken(token) {
  API_TOKEN = token || '';
}

async function request(path, options = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (API_TOKEN) headers['X-Video-Grabing-Token'] = API_TOKEN;
  const res = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    throw new Error('服务端返回了非 JSON 内容（HTTP ' + res.status + '）');
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(payload.error || ('请求失败：HTTP ' + res.status));
  }
  return payload;
}

const get = (path) => request(path);
const post = (path, body) => request(path, { method: 'POST', body: JSON.stringify(body || {}) });

export const api = {
  state: () => get('/api/state'),
  tasks: () => get('/api/tasks'),
  engines: () => get('/api/engines'),

  parse: (input, engine = 'auto') => post('/api/parse', { input, engine }),
  play: (input, engine, qn, p) => post('/api/play', { input, engine, qn, p }),
  probe: (input, qn = 0) => post('/api/probe', { input, qn }),

  submit: (payload) => post('/api/tasks', payload),
  cancelTask: (id) => post('/api/tasks/cancel', { id }),
  retryTask: (id) => post('/api/tasks/retry', { id }),
  removeTask: (id, deleteFiles = false) =>
    post('/api/tasks/remove', { id, delete_files: deleteFiles }),
  clearTasks: () => post('/api/tasks/clear'),

  saveSettings: (settings) => post('/api/settings', { settings }),
  engineOrder: (order, enabled) => post('/api/engines/order', { order, enabled }),

  open: (path) => post('/api/open', { path }),
  quit: () => post('/api/quit', {}),
  updateCheck: () => post('/api/update/check', {}),
  updateVerify: () => post('/api/update/verify', {}),
  updateRepair: () => post('/api/update/repair', {}),
  updateDownload: () => post('/api/update/download', {}),
  updateLaunch: (path) => post('/api/update/launch', { path }),
  ping: () => get('/api/ping'),
  loginCheck: () => post('/api/login/check', {}),
  installYtdlp: () => post('/api/tools/install', {}),
  probeJx: (bvid) => post('/api/tools/jx', { bvid }),

  mediaUrl: (path) => '/media?path=' + encodeURIComponent(path),
  // 各平台图床都有 Referer 防盗链，封面一律走后端代理，并把平台首页一起带上
  coverUrl: (url, site) => {
    if (!url) return '';
    const home = site && site.home ? '&ref=' + encodeURIComponent(site.home) : '';
    return '/api/cover?url=' + encodeURIComponent(url) + home;
  },
};
