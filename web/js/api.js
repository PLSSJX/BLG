/** 后端 JSON API 的极薄封装。 */

// 写操作要带令牌，防止浏览器里其它页面跨站调用本机接口
let API_TOKEN = '';

export function setToken(token) {
  API_TOKEN = token || '';
}

/** 给需要自己发请求的模块用（比如项目联动） */
export function getToken() {
  return API_TOKEN;
}

/** 通用 POST：项目相关的接口用它，不用往 api 对象里一个个加方法 */
export async function postJson(path, body) {
  return request(path, { method: 'POST', body: JSON.stringify(body || {}) });
}

async function request(path, options = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (API_TOKEN) headers['X-MakiseToolkit-Token'] = API_TOKEN;
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
  // 自绘标题栏的三个按钮
  paperSearch: (query, opts) => post('/api/papers/search', Object.assign({ query }, opts || {})),
  paperDownload: (url, name, meta) => post('/api/papers/download', { url, name, meta: meta || {} }),
  paperList: () => post('/api/papers/list', {}),
  paperLibrary: () => post('/api/papers/library', {}),
  paperCite: (id, style) => post('/api/papers/cite', { id, style }),
  paperLibraryRemove: (id) => post('/api/papers/library/remove', { id }),
  paperExport: (format) => post('/api/papers/export', { format }),
  aiStatus: () => post('/api/ai/status', {}),
  aiRebuild: () => post('/api/ai/rebuild', {}),
  aiSearch: (query, project) => post('/api/ai/search', { query, project }),
  aiAsk: (question, useContext, project, assistant) => post('/api/ai/ask', { question, use_context: useContext !== false, project, assistant }),
  aiAssistants: (items, active) => post('/api/ai/assistants', { assistants: items, active }),
  aiChatList: () => post('/api/ai/chat/list', {}),
  aiChatDelete: (id) => post('/api/ai/chat/delete', { id }),
  aiChatClear: () => post('/api/ai/chat/clear', {}),
  ocrStatus: () => post('/api/ocr/status', {}),
  ocrInstall: () => post('/api/ocr/install', {}),
  ocrRun: (path, limit) => post('/api/ocr/run', { path, limit: limit || 0 }),
  ocrInstallTesseract: () => post('/api/ocr/install-tesseract', {}),
  ocrInstallFrom: (path) => post('/api/ocr/install-from', { path }),
  playlistImport: (input, limit, only) => post('/api/playlist/import', { input, limit, only }),
  playlistCancel: (site, id) => post('/api/playlist/cancel', { site, id }),
  playlistRetrySong: (site, id, url) => post('/api/playlist/retry-song', { site, id, url }),
  playlistAdvance: () => post('/api/playlist/advance', {}),
  playlistDownloadSong: (site, id, url) => post('/api/playlist/download-song', { site, id, url }),
  playlistResolve: (site, id, url) => post('/api/playlist/resolve', { site, id, url }),
  bigjpgHistory: () => post('/api/bigjpg/history', {}),
  docCancel: () => post('/api/doc/cancel', {}),
  shellOpen: (path, reveal) => post('/api/shell/open', { path, reveal: !!reveal }),
  checkCredential: (site, value) => post('/api/credentials/check', { site, value }),
  movePreview: (field, value, from) => post('/api/settings/move-preview', { field, value, from }),
  moveApply: (field, value, from) => post('/api/settings/move-apply', { field, value, from }),
  windowReady: () => post('/api/window/ready', {}),
  frontendError: (message, where) => post('/api/window/frontend-error', { message, where }),
  windowDrag: () => post('/api/window/drag', {}),
  windowResize: (edge) => post('/api/window/resize', { edge }),
  windowMinimize: () => post('/api/window/minimize', {}),
  windowMaximize: () => post('/api/window/maximize', {}),
  windowClose: () => post('/api/window/close', {}),
  tasks: () => get('/api/tasks'),
  engines: () => get('/api/engines'),

  parse: (input, engine = 'auto') => post('/api/parse', { input, engine }),
  play: (input, engine, qn, p) => post('/api/play', { input, engine, qn, p }),
  probe: (input, qn = 0) => post('/api/probe', { input, qn }),

  submit: (payload) => post('/api/tasks', payload),
  cancelTask: (id) => post('/api/tasks/cancel', { id }),
  retryTask: (id) => post('/api/tasks/retry', { id }),
  previewRemove: (id) => post('/api/tasks/preview-remove', { id }),
  removeTask: (id, deleteFiles = false) =>
    post('/api/tasks/remove', { id, delete_files: deleteFiles }),
  clearTasks: () => post('/api/tasks/clear'),

  saveSettings: (settings) => post('/api/settings', { settings }),
  engineOrder: (order, enabled) => post('/api/engines/order', { order, enabled }),

  // 把主题色同步到 Windows 窗口边框
  setWindowFrame: (frame) => post('/api/window/frame', frame),

  // 本地音乐文件：选择 / 预检 / 转换
  pickLocalFiles: (kind = 'music') => post('/api/local/pick', { kind }),
  pickDownloadDir: () => post('/api/local/pickdir', {}),
  pickMusicDir: () => post('/api/local/pickdir', { field: 'music_dir' }),
  musicList: () => post('/api/music/list', {}),
  musicSearch: (q, limit = 10) => post('/api/music/search', { q, limit }),
  extractAudio: (paths, target = 'mp3') => post('/api/video/extract-audio', { paths, target }),
  importMusic: (paths) => post('/api/music/import', { paths }),
  trimVideo: (path, start, end, precise = false) => post('/api/video/trim', { path, start, end, precise }),
  convertDocs: (paths, target) => post('/api/doc/convert', { paths, target }),
  demuxVideo: (path, mode, target) => post('/api/video/demux', { path, mode, target }),
  playlistFetch: (input) => post('/api/playlist/fetch', { input }),
  playlistSave: (playlist) => post('/api/playlist/save', { playlist }),
  playlistList: (deep) => post('/api/playlist/list', { deep: !!deep }),
  playlistCreate: (title) => post('/api/playlist/create', { title }),
  playlistAddSong: (site, id, song) => post('/api/playlist/add-song', { site, id, song }),
  playlistRemoveSong: (site, id, url) => post('/api/playlist/remove-song', { site, id, url }),
  playlistRemove: (site, id) => post('/api/playlist/remove', { site, id }),
  ffmpegStatus: () => post('/api/ffmpeg/status', {}),
  ffmpegInstall: () => post('/api/ffmpeg/install', {}),
  bigjpgInfo: () => post('/api/bigjpg/info', {}),
  bigjpgSubmit: (paths, style, noise, x2) => post('/api/bigjpg/submit', { paths, style, noise, x2 }),
  bigjpgQuery: (tids) => post('/api/bigjpg/query', { tids }),
  bigjpgRetry: (tids) => post('/api/bigjpg/retry', { tids }),
  scanLocalFiles: (paths) => post('/api/local/decryptable', { paths }),
  convertLocalFiles: (paths, target, bitrate = '') =>
    post('/api/tasks/convert', { paths, target, bitrate }),
  localFormats: (source = '') => get('/api/local/formats?source=' + encodeURIComponent(source)),

  open: (path) => post('/api/open', { path }),
  reveal: (path) => post('/api/open', { path, reveal: true }),
  quit: () => post('/api/quit', {}),
  updateCheck: () => post('/api/update/check', {}),
  updateVerify: () => post('/api/update/verify', {}),
  updateRepair: () => post('/api/update/repair', {}),
  updateDownload: () => post('/api/update/download', {}),
  updateLaunch: (path) => post('/api/update/launch', { path }),
  updateApply: () => post('/api/update/apply', {}),
  ping: () => get('/api/ping'),
  loginCheck: () => post('/api/login/check', {}),
  installYtdlp: () => post('/api/tools/install', {}),
  probeJx: (bvid) => post('/api/tools/jx', { bvid }),

  mediaUrl: (path) => '/media?path=' + encodeURIComponent(path),
  // 各平台图床都有 Referer 防盗链，封面一律走后端代理。
  // Referer 由后端按域名自己决定，前端不传 —— 传了就等于把代理变成任意地址跳板。
  coverUrl: (url) => (url ? '/api/cover?url=' + encodeURIComponent(url) : ''),
};
