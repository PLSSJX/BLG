// Makise Toolkit 端到端测试：边界值与异常操作
//
// 覆盖：
//   1. 项目名 / 节点标题用超长(200 字)、emoji、斜杠冒号问号、纯空格、只有扩展名
//   2. 节点标题叫 "a.md" 时不生成 "a.md.md"
//   3. 空项目直接保存版本 -> committed=false，不报错
//   4. 不存在的项目 id / 不存在的版本 sha -> ok=false 且错误可读，不能 500
//   5. 非空文件夹建项目 -> 明确拒绝
//   6. 挂一个不存在的文件 -> 明确报错
//   7. >5MB 的大文件能挂载、能保存，回滚时出现在 skipped 里
//   8. 文件被独占打开时保存 -> 拒绝并点名是哪个文件
//   9. 同一个项目并发 5 个保存请求 -> 服务不崩，之后状态仍正常
//  10. 中文路径 + 空格路径的项目
//
// 约定与 tests/e2e/api.mjs 一致：纯 Node ESM，curl.exe 打 127.0.0.1:8931，
// 从 GET /api/state 拿 api_token，POST 带 X-MakiseToolkit-Token 头。
// 每个项目一个独立临时目录（D:/_sc_c_<时间戳>/<用例名>），全过退出码 0，
// 最后一行为 "结果: x/y"。
//
// 用法：先启动 MakiseToolkit，再 node tests/e2e/scenarios/edge-cases.mjs

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const ROOT = 'D:/_sc_c_' + Date.now();
const DOCX_DIR = 'C:/Users/25486/Desktop/dwsy';
const VIDEO_DIR = 'C:/Users/25486/Desktop/valclip';

const results = [];
const check = (name, cond, extra) => {
  results.push({ name, ok: !!cond, extra: extra === undefined ? '' : String(extra) });
};

// ---------- curl 封装 ----------
// 状态码靠 -w 追加在响应后面。不能用 "@__HTTP__" 这种开头是 @ 的格式串：
// curl 会把 -w 的 "@..." 当成"从文件里读格式"，直接报 Failed to open。
const MARK = String.fromCharCode(10) + '__HTTP__';
const curlRaw = (args) => spawnSync('curl.exe', args, {
  encoding: 'utf8', windowsHide: true, timeout: 180000,
}).stdout || '';

const splitStatus = (out) => {
  const i = out.lastIndexOf(MARK);
  if (i < 0) return { status: 0, text: out };
  const status = parseInt(out.slice(i + MARK.length), 10);
  return { status: Number.isFinite(status) ? status : 0, text: out.slice(0, i) };
};
const parseJson = (text) => {
  try { return JSON.parse(text); } catch { return { _bad: true, _raw: String(text).slice(0, 200) }; }
};

const GET = (p, timeout = 20) => {
  const s = splitStatus(curlRaw(['-s', '--ssl-no-revoke', '-m', String(timeout),
    '-w', MARK + '%{http_code}', BASE + p]));
  return { status: s.status, data: parseJson(s.text) };
};

const TK = (() => { try { return String(GET('/api/state').data.api_token || ''); } catch { return ''; } })();

const postRaw = (p, body, timeout = 120) => {
  const s = splitStatus(curlRaw(['-s', '--ssl-no-revoke', '-m', String(timeout),
    '-w', MARK + '%{http_code}',
    '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK,
    '-d', JSON.stringify(body || {}), BASE + p]));
  return { status: s.status, data: parseJson(s.text) };
};
const post = (p, body, timeout) => postRaw(p, body, timeout).data;

// 并发用：Promise.all 里每个请求一个 curl 进程
const curlAsync = (args) => new Promise((resolve) => {
  const cp = spawn('curl.exe', args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  cp.stdout.setEncoding('utf8');
  cp.stdout.on('data', (d) => { out += d; });
  cp.stderr.resume();
  cp.on('error', () => resolve(''));
  cp.on('close', () => resolve(out));
});
const postAsync = async (p, body, timeout = 120) => {
  const out = await curlAsync(['-s', '--ssl-no-revoke', '-m', String(timeout),
    '-w', MARK + '%{http_code}',
    '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK,
    '-d', JSON.stringify(body || {}), BASE + p]);
  const s = splitStatus(out);
  return { status: s.status, data: parseJson(s.text) };
};

const alive = () => { try { return GET('/api/ping', 10).data.ok === true; } catch { return false; } };
const shortErr = (d) => {
  if (!d) return '';
  if (d.error) return String(d.error).slice(0, 100);
  if (d._bad) return '非 JSON 响应：' + String(d._raw).slice(0, 80);
  return '';
};

// ---------- 小工具 ----------
const mkdir = (slug) => {
  const p = path.join(ROOT, slug);
  fs.mkdirSync(p, { recursive: true });
  return p;
};
const onDisk = (proj, rel) => !!rel && fs.existsSync(path.join(proj, String(rel)));
const sha256 = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const walk = (dir) => {
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
};
const pickFile = (dir, exts, minSize, maxSize) => {
  try {
    if (!fs.existsSync(dir)) return null;
    const want = exts.map((x) => x.toLowerCase());
    const rows = [];
    for (const name of fs.readdirSync(dir)) {
      if (!want.includes(path.extname(name).toLowerCase())) continue;
      let size = 0;
      try { size = fs.statSync(path.join(dir, name)).size; } catch { continue; }
      if (size < minSize || size > maxSize) continue;
      rows.push({ name, full: path.join(dir, name), size });
    }
    rows.sort((a, b) => a.size - b.size);
    return rows[0] || null;
  } catch { return null; }
};
const stemOf = (file) => path.basename(String(file || ''), '.md');
// safe_filename 重名时会加 -2、-3
const isFallback = (file, base) => {
  const s = stemOf(file);
  if (s === base) return true;
  if (!s.startsWith(base + '-')) return false;
  const rest = s.slice(base.length + 1);
  return rest.length > 0 && rest.split('').every((ch) => ch >= '0' && ch <= '9');
};
const short = (v, n = 30) => {
  const s = String(v === undefined || v === null ? '' : v);
  return s.length > n ? s.slice(0, n) + '…(' + s.length + ' 字)' : s;
};

// 一段用例出意外也不该带走整个脚本：记一条失败，继续跑后面的边界
async function section(name, fn) {
  try { await fn(); } catch (e) {
    const line = String((e && e.stack) || e).split(String.fromCharCode(10))[0];
    check('【' + name + '】这一段没有中途抛异常', false, line.slice(0, 170));
  }
}

// ---------- 0. 服务 ----------
check('服务在线', alive(), BASE + '/api/ping');
check('拿到 api_token', TK.length > 0, TK ? '长度 ' + TK.length : 'GET /api/state 里没有 api_token');

if (TK.length === 0 || results.some((x) => !x.ok)) {
  const early = results.filter((x) => x.ok).length;
  for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.name + (x.extra ? ' — ' + x.extra : ''));
  console.log('');
  console.log('服务没起来或拿不到 token：先启动 MakiseToolkit 再跑这个脚本。');
  console.log('结果: ' + early + '/' + results.length);
  process.exit(1);
}

fs.mkdirSync(ROOT, { recursive: true });
console.log('临时目录: ' + ROOT);

// ---------- 1. 项目名边界 ----------
await section('项目名边界', async () => {
  const cases = [
    { slug: 'p-long-name', label: '超长(200字)', name: '长'.repeat(200) },
    { slug: 'p-emoji', label: 'emoji', name: '📚 论文 🚀✨' },
    { slug: 'p-slash', label: '斜杠冒号问号', name: 'a/b:c?d 报告' },
    { slug: 'p-spaces', label: '纯空格', name: '     ' },
    { slug: 'p-ext', label: '只有扩展名', name: '报告.docx' },
  ];
  for (const item of cases) {
    const dir = mkdir(item.slug);
    // 纯空格的名字会被后端退回到文件夹名
    const expectName = item.name.trim() || path.basename(dir);
    const r = postRaw('/api/project/create', { name: item.name, path: dir });
    check('项目名 ' + item.label + ' 能建项目',
      r.status === 200 && r.data.ok === true && !!r.data.id, 'HTTP ' + r.status + ' ' + shortErr(r.data));
    if (!r.data.id) continue;
    const graph = post('/api/project/load', { id: r.data.id }).graph || {};
    const title = String((graph.trunk || {}).title || '');
    check('项目名 ' + item.label + ' 原样存进结构文件', title === expectName, short(title));
  }
});

// ---------- 2. 节点标题边界 ----------
await section('节点标题边界', async () => {
  const proj = mkdir('p-node-title');
  const c = post('/api/project/create', { name: '标题边界', path: proj });
  check('标题用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id) return;

  const write = (title) => post('/api/project/node/write', {
    id, node: { id: 'n-' + Math.random().toString(36).slice(2, 8), title },
    body: '# 标题' + String.fromCharCode(10) + String.fromCharCode(10) + '正文内容 ' + title.slice(0, 10),
  });

  const long = write('长'.repeat(200));
  check('超长标题能写出文件', !!long.file && onDisk(proj, long.file), long.file || shortErr(long));
  check('超长标题的文件名截到 60 字以内',
    !!long.file && stemOf(long.file).length <= 60, long.file ? stemOf(long.file).length + ' 字' : '');

  const emoji = write('📚 论文 🚀✨');
  check('emoji 标题原样保留在文件名里',
    !!emoji.file && String(emoji.file).includes('📚') && onDisk(proj, emoji.file), emoji.file || shortErr(emoji));

  const slash = write('a/b:c?d 报告');
  const badChars = ['/', ':', '?', '*', '"', '<', '>', '|', String.fromCharCode(92)];
  check('斜杠标题不会造出子目录', !!slash.file && !String(slash.file).includes('/'), slash.file || shortErr(slash));
  check('斜杠冒号问号都换成了安全字符',
    !!slash.file && !badChars.some((ch) => path.basename(slash.file).includes(ch)), slash.file || '');

  const spaces = write('     ');
  check('纯空格标题退回「未命名」',
    isFallback(spaces.file, '未命名') && onDisk(proj, spaces.file), spaces.file || shortErr(spaces));

  const ext = write('报告.docx');
  check('「报告.docx」标题不会变成 报告.docx.md',
    isFallback(ext.file, '报告') && onDisk(proj, ext.file), ext.file || shortErr(ext));

  const aMd = write('a.md');
  check('「a.md」标题生成 a.md 而不是 a.md.md',
    aMd.file === 'a.md' && onDisk(proj, 'a.md'), aMd.file || shortErr(aMd));
  const doubled = walk(proj).map((f) => path.basename(f)).filter((n) => n.toLowerCase().endsWith('.md.md'));
  check('整个项目里没有 .md.md 双扩展名文件', doubled.length === 0, doubled.join(',') || '0 个');

  const read = post('/api/project/node/read', { id, file: long.file });
  check('超长标题写进去的正文能读回',
    String(read.body || '').includes('正文内容'), String(read.body || '').length + ' 字');
});

// ---------- 3. 空项目保存版本 ----------
await section('空项目保存', async () => {
  const proj = mkdir('p-empty');
  const c = postRaw('/api/project/create', { name: '空项目', path: proj });
  check('空项目建好了', c.status === 200 && c.data.ok === true && !!c.data.id, shortErr(c.data));
  const id = c.data.id;
  if (!id) return;

  const st = postRaw('/api/project/status', { id });
  check('空项目刚建好是干净的', st.data.dirty === false,
    'dirty=' + st.data.dirty + ' changed=' + short((st.data.changed || []).join(','), 60));

  const r = postRaw('/api/project/commit-now', { id, message: 'mt: 空项目保存' });
  check('空项目直接保存不报错', r.status === 200 && r.data.ok === true,
    'HTTP ' + r.status + ' ' + shortErr(r.data));
  check('空项目直接保存 committed=false', r.data.committed === false, 'committed=' + r.data.committed);
  check('空项目直接保存给出了原因',
    typeof r.data.reason === 'string' && r.data.reason.length > 0, r.data.reason || '(没有 reason)');
});

// ---------- 4. 不存在的 id / 不存在的版本 sha ----------
await section('不存在的项目 id', async () => {
  const bogus = 'p-not-exist-' + Date.now();
  const routes = ['/api/project/load', '/api/project/commit-now', '/api/project/log',
    '/api/project/status', '/api/project/restore'];
  const rows = routes.map((route) => ({ route, r: postRaw(route, { id: bogus }) }));
  check('不存在的项目 id 一律 400',
    rows.every((x) => x.r.status === 400),
    rows.map((x) => x.route.replace('/api/project/', '') + ':' + x.r.status).join(' '));
  check('不存在的项目 id 返回 ok=false 且能看懂',
    rows.every((x) => x.r.data.ok === false
      && String(x.r.data.error || '').includes('找不到')
      && !String(x.r.data.error || '').includes('服务器内部错误')),
    shortErr(rows[0].r.data));
});

await section('不存在的版本 sha', async () => {
  const proj = mkdir('p-bad-sha');
  const c = post('/api/project/create', { name: '坏版本号', path: proj });
  check('坏版本号用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id) return;

  const bogusSha = 'deadbeef' + Date.now().toString(16);
  // 界面用 commit，老脚本用 sha —— 两个键都给，免得测成"忽略参数然后回滚 HEAD~1"
  const r = postRaw('/api/project/restore', { id, commit: bogusSha, sha: bogusSha, files: [] });
  check('不存在的版本 sha 返回 ok=false', r.data.ok === false,
    'HTTP ' + r.status + ' ' + shortErr(r.data));
  check('不存在的版本 sha 错误可读',
    String(r.data.error || '').includes('找不到这个版本'), shortErr(r.data));
  // 这条是需求里的硬要求。如果产品侧没有把版本解析失败的异常转成 ApiError，
  // 这里会红 —— 那是真的 500，不是脚本误报。
  check('不存在的版本 sha 不是 500', r.status !== 500, 'HTTP ' + r.status);
  check('坏版本号请求之后服务仍在', alive());
});

// ---------- 5. 非空文件夹建项目 ----------
await section('非空文件夹', async () => {
  const dir = mkdir('p-nonempty');
  fs.writeFileSync(path.join(dir, '已有.md'), 'x', 'utf8');
  const r = postRaw('/api/project/create', { name: '非空', path: dir });
  check('非空文件夹明确拒绝建项目', r.status === 400 && r.data.ok === false,
    'HTTP ' + r.status + ' ' + shortErr(r.data));
  check('拒绝时说明里面已经有什么',
    String(r.data.error || '').includes('不是空') && String(r.data.error || '').includes('已有.md'),
    shortErr(r.data));
  check('拒绝后没留下半成品结构', !fs.existsSync(path.join(dir, '.mt')),
    fs.existsSync(path.join(dir, '.mt')) ? '有 .mt/' : '无 .mt/');
});

// ---------- 6. 挂一个不存在的文件 ----------
await section('挂不存在的文件', async () => {
  const proj = mkdir('p-bad-attach');
  const c = post('/api/project/create', { name: '挂错文件', path: proj });
  check('挂错文件用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id) return;

  const missing = path.join(ROOT, '不存在的素材-' + Date.now() + '.docx');
  const r = postRaw('/api/project/attach', { id, nodeId: 't1', source: missing });
  check('挂不存在的文件明确报错',
    r.status === 400 && r.data.ok === false && String(r.data.error || '').includes('找不到这个文件'),
    'HTTP ' + r.status + ' ' + shortErr(r.data));
  check('报错里带上了那个文件的名字',
    String(r.data.error || '').includes(path.basename(missing)), short(r.data.error || '', 80));

  const empty = postRaw('/api/project/attach', { id, nodeId: 't1', source: '' });
  check('什么都不填也明确报错',
    empty.status === 400 && String(empty.data.error || '').includes('没有指定'), shortErr(empty.data));
  check('挂错之后项目里没有多出 assets', !fs.existsSync(path.join(proj, 'assets')),
    fs.existsSync(path.join(proj, 'assets')) ? '有 assets/' : '无 assets/');
});

// ---------- 7. 超大文件（>5MB）挂载 / 保存 / 回滚 skipped ----------
await section('超大文件', async () => {
  const mp4 = pickFile(VIDEO_DIR, ['.mp4'], 5 * 1024 * 1024 + 1, Number.MAX_SAFE_INTEGER);
  check('在 valclip 里找到 >5MB 的 mp4 素材', !!mp4,
    mp4 ? mp4.name + ' / ' + Math.round(mp4.size / 1048576) + 'MB' : VIDEO_DIR + ' 里没找到');

  const proj = mkdir('p-large');
  const c = post('/api/project/create', { name: '大文件项目', path: proj });
  check('大文件用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id || !mp4) return;

  const a = postRaw('/api/project/attach', { id, nodeId: 't1', source: mp4.full }, 180);
  const asset = (a.data || {}).asset || {};
  check('大文件能挂载', a.status === 200 && a.data.ok === true && asset.size === mp4.size,
    'HTTP ' + a.status + ' ' + shortErr(a.data));
  const mirror = asset.path ? path.join(proj, asset.path) : '';
  check('大文件镜像大小一致', !!mirror && fs.existsSync(mirror) && fs.statSync(mirror).size === mp4.size,
    asset.path || '');
  check('桌面上的源文件没被动过',
    fs.existsSync(mp4.full) && fs.statSync(mp4.full).size === mp4.size, mp4.name);

  const cm = postRaw('/api/project/commit-now', { id, message: 'mt: 挂大文件' }, 180);
  check('大文件能保存版本', cm.status === 200 && cm.data.committed === true,
    'HTTP ' + cm.status + ' ' + shortErr(cm.data));

  const log = post('/api/project/log', { id }).commits || [];
  const bigCommit = log.find((x) => String(x.subject).includes('挂大文件'));
  check('版本清单里记着这个文件',
    !!bigCommit && (bigCommit.files || []).some((f) => String(f).includes(mp4.name)),
    bigCommit ? short((bigCommit.files || []).join(' | '), 90) : '没找到那个提交');

  fs.writeFileSync(path.join(proj, '小改动.md'), '# 小改动' + String.fromCharCode(10) + Date.now(), 'utf8');
  const c2 = postRaw('/api/project/commit-now', { id, message: 'mt: 再存一版' });
  check('再存一版，为回滚做准备', c2.status === 200 && c2.data.committed === true, shortErr(c2.data));

  const sha = bigCommit ? bigCommit.sha : '';
  const back = postRaw('/api/project/restore', { id, commit: sha, sha, files: [] }, 180);
  check('回滚到含大文件的版本成功', back.status === 200 && back.data.ok === true,
    'HTTP ' + back.status + ' ' + shortErr(back.data));
  const skipped = (back.data.skipped || []).map((x) => String(x).split(String.fromCharCode(92)).join('/'));
  check('大文件回滚时出现在 skipped 里',
    skipped.some((x) => x.endsWith(mp4.name)), skipped.join(' | ') || 'skipped 是空的');
  check('被跳过的大文件仍然躺在磁盘上', !!mirror && fs.existsSync(mirror), mirror || '');
  check('回滚顺手清掉了后来才有的文件',
    !fs.existsSync(path.join(proj, '小改动.md')),
    fs.existsSync(path.join(proj, '小改动.md')) ? '还在' : '已删');
});

// ---------- 8. 文件被独占打开 ----------
await section('文件被独占打开', async () => {
  const proj = mkdir('p-locked');
  const c = post('/api/project/create', { name: '占用测试', path: proj });
  check('占用用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id) return;

  const rel = '占用中.md';
  const abs = path.join(proj, rel);
  fs.writeFileSync(abs, '# 占用中' + String.fromCharCode(10) + Date.now(), 'utf8');
  const fh = fs.openSync(abs, 'r+');
  let r;
  try { r = postRaw('/api/project/commit-now', { id, message: 'mt: 占用时保存' }); }
  finally { fs.closeSync(fh); }

  check('文件被独占打开时拒绝保存', r.status === 400 && r.data.ok === false,
    'HTTP ' + r.status + ' ' + shortErr(r.data));
  check('拒绝时点名是哪个文件', String(r.data.error || '').includes('占用中.md'), shortErr(r.data));
  const committed = (post('/api/project/log', { id }).commits || [])
    .some((x) => String(x.subject).includes('占用时保存'));
  check('被占用时没有偷偷提交成功', !committed, committed ? '居然提交了' : '没有提交');

  const after = postRaw('/api/project/commit-now', { id, message: 'mt: 放开后保存' });
  check('文件放开后能正常保存', after.status === 200 && after.data.committed === true, shortErr(after.data));
});

// ---------- 9. 同项目并发 5 个保存请求 ----------
await section('并发保存', async () => {
  const proj = mkdir('p-concurrent');
  const c = post('/api/project/create', { name: '并发保存', path: proj });
  check('并调用例项目已建好', !!c.id, shortErr(c));
  const id = c.id;
  if (!id) return;

  const body = '# 并发' + String.fromCharCode(10) + String.fromCharCode(10) + Date.now();
  fs.writeFileSync(path.join(proj, '并发.md'), body, 'utf8');
  const before = (post('/api/project/log', { id }).commits || []).length;

  const answers = await Promise.all([0, 1, 2, 3, 4].map((i) => postAsync('/api/project/commit-now', {
    id, message: 'mt: 并发 ' + (i + 1),
  })));

  check('5 个并发请求都拿到了 HTTP 响应', answers.every((x) => x.status > 0),
    answers.map((x) => x.status).join(','));
  check('5 个并发请求都是合法 JSON', answers.every((x) => !x.data._bad),
    answers.filter((x) => x.data._bad).length + ' 个不是 JSON');
  check('并发保存没有打出 5xx', answers.every((x) => x.status < 500),
    answers.map((x) => x.status).join(','));
  check('并发保存至少真的存下了一版', answers.some((x) => x.data.committed === true),
    short(answers.map((x) => (x.data.committed === true ? 'committed' : String(x.data.reason || '失败'))).join(' | '), 90));

  check('并发之后服务没崩', alive());
  const st = postRaw('/api/project/status', { id });
  check('并发之后状态接口正常', st.status === 200 && st.data.ok === true && !!st.data.project,
    'HTTP ' + st.status);
  const logAfter = (post('/api/project/log', { id }).commits || []).length;
  check('并发之后版本历史还能读', logAfter >= before + 1, before + ' -> ' + logAfter);

  fs.writeFileSync(path.join(proj, '并发之后.md'), '# 之后' + String.fromCharCode(10) + Date.now(), 'utf8');
  const after = postRaw('/api/project/commit-now', { id, message: 'mt: 并发之后' });
  check('并发之后还能继续保存', after.status === 200 && after.data.committed === true, shortErr(after.data));
  const disk = fs.existsSync(path.join(proj, '并发.md'))
    ? fs.readFileSync(path.join(proj, '并发.md'), 'utf8') : '';
  check('并发之后正文没被写坏', disk.includes('并发'), short(disk.replace(String.fromCharCode(10), ' '), 40));
});

// ---------- 10. 中文路径 + 空格路径 ----------
await section('中文和空格路径', async () => {
  const proj = mkdir('中文 路径 项目');
  const c = postRaw('/api/project/create', { name: '中文空格', path: proj });
  check('中文+空格路径能建项目',
    c.status === 200 && c.data.ok === true && !!c.data.id, 'HTTP ' + c.status + ' ' + shortErr(c.data));
  const id = c.data.id;
  if (!id) return;
  check('中文+空格路径下生成了 .mt 结构目录', fs.existsSync(path.join(proj, '.mt')));

  const w = post('/api/project/node/write', {
    id, node: { id: 'n1', title: '中文 空格 节点' },
    body: '# 中文空格' + String.fromCharCode(10) + String.fromCharCode(10) + '路径里带空格也要能写。',
  });
  check('中文+空格路径能写节点文件',
    !!w.file && onDisk(proj, w.file) && String(w.file).includes('中文 空格 节点'), w.file || shortErr(w));

  const cm = postRaw('/api/project/commit-now', { id, message: 'mt: 中文空格' });
  check('中文+空格路径能保存版本', cm.status === 200 && cm.data.committed === true, shortErr(cm.data));

  const rd = post('/api/project/node/read', { id, file: w.file });
  check('中文+空格路径能读回正文',
    String(rd.body || '').includes('路径里带空格'), short(rd.body || '', 30));

  const docx = pickFile(DOCX_DIR, ['.docx'], 1024, 8 * 1024 * 1024);
  check('在 dwsy 里找到 docx 素材', !!docx, docx ? docx.name : DOCX_DIR + ' 里没找到');
  if (docx) {
    const a = postRaw('/api/project/attach', { id, nodeId: 't1', source: docx.full });
    const assetPath = ((a.data || {}).asset || {}).path || '';
    check('中文+空格路径能挂素材',
      a.status === 200 && a.data.ok === true && !!assetPath, shortErr(a.data));
    check('挂进来的 docx 内容和原文件一致',
      !!assetPath && onDisk(proj, assetPath) && sha256(path.join(proj, assetPath)) === sha256(docx.full),
      assetPath);
  }
});

// ---------- 收尾 ----------
await section('收尾', async () => {
  check('跑完服务仍然在线', alive());
  const st = GET('/api/state');
  check('跑完 token 仍然有效', st.status === 200 && String(st.data.api_token || '') === TK);
});

// ---------- 汇总 ----------
const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.name + (x.extra ? ' — ' + x.extra : ''));
console.log('');
console.log('结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
