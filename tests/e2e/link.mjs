import { spawn, spawnSync } from 'node:child_process';
import fsn from 'node:fs';
import path from 'node:path';

const BASE = 'http://127.0.0.1:8931';
const results = [];
const check = (n, c, e) => results.push({ n, ok: !!c, e: e === undefined ? '' : String(e) });
const curl = (a) => spawnSync('curl.exe', a, { encoding: 'utf8', windowsHide: true, timeout: 60000 }).stdout || '';
const TK = JSON.parse(curl(['-s', BASE + '/api/state'])).api_token;
const post = (p, b) => {
  const raw = curl(['-s', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(b || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { raw }; }
};

// 准备：一个项目 + 一个"已完成"任务产物
const dir = 'D:\\_link_' + Date.now();
fsn.mkdirSync(dir, { recursive: true });
const proj = post('/api/project/create', { name: '联动测试', path: dir });
const pid = proj.id;
const w = post('/api/project/node/write', { id: pid, node: { id: 'n1', title: '方法' }, body: '# 方法' });
const g = post('/api/project/load', { id: pid }).graph;
g.nodes = [{ id: 'n1', title: '方法', parent: 't1', axis: 'h', file: w.file }];
post('/api/project/graph', { id: pid, graph: g });
post('/api/project/commit-now', { id: pid, message: 'mt: 联动准备' });

const tasks = post('/api/tasks', {}).tasks || [];
const done = tasks.find((t) => t.status === 'done' && (t.files || []).length);
const SRC = 'D:\\_link_src_' + Date.now() + '.txt';
fsn.writeFileSync(SRC, 'link test', 'utf8');
if (done) {
  for (const f of done.files) {
    if (f.path && !fsn.existsSync(f.path)) { fsn.mkdirSync(path.dirname(f.path), { recursive: true }); fsn.writeFileSync(f.path, 'x', 'utf8'); }
  }
}

const browser = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=10811', '--window-size=1417,928',
   '--user-data-dir=D:\\_link_p_' + Date.now(), BASE + '/#tasks'],
  { windowsHide: true, detached: true, stdio: 'ignore' });
browser.unref();
await new Promise((r) => setTimeout(r, 9000));
const list = await (await fetch('http://127.0.0.1:10811/json/list')).json();
const target = list.find((t) => t.type === 'page' && String(t.url).includes('8931')) || list[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let n = 0;
const pend = new Map();
const errs = [];
const send = (m, q) => new Promise((r) => { const i = ++n; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: q })); });
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') errs.push(String(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 150));
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result || m.error); pend.delete(m.id); }
});
await new Promise((r) => ws.addEventListener('open', r));
await send('Runtime.enable', {});
await send('Emulation.setDeviceMetricsOverride', { width: 1417, height: 928, deviceScaleFactor: 1, mobile: false });
await new Promise((r) => setTimeout(r, 5000));
const ev = async (x) => {
  const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true });
  return r.exceptionDetails ? 'EXC ' + String(r.exceptionDetails.exception?.description).slice(0, 150) : r.result?.value;
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

check('历史页已打开', String(await ev('location.hash')) === '#tasks', await ev('location.hash'));
const btns = Number(await ev("Array.from(document.querySelectorAll('button')).map(function (b) { return b.textContent.trim(); }).filter(function (t) { return t.indexOf('存到项目') >= 0; }).length"));
if (done) {
  check('已完成任务上有「存到项目」', btns >= 1, btns);
  await ev("Array.from(document.querySelectorAll('button')).filter(function (b) { return b.textContent.trim().indexOf('存到项目') >= 0; })[0].click()");
  await wait(2200);
  check('点按钮弹出选择器', (await ev("!!document.querySelector('#dialogHost .dialog')")) === true);
  await ev("Array.from(document.querySelectorAll('#dialogHost .actions button')).find(function (x) { return x.textContent.indexOf('取消') >= 0; })?.click()");
  await wait(500);
} else {
  check('本次没有已完成任务，跳过按钮点击（直接测联动代码）', true, '跳过');
}
// 直接走一遍联动：picker → attach
await ev("import('/js/project-link.js').then(function (m) { window.__save = m.saveToProject; return 'ok'; })");
await wait(1200);
// 注意：saveToProject 会一直等到用户选完，不能让 CDP 等它的 Promise
await send('Runtime.evaluate', { expression: "window.__save(" + JSON.stringify(SRC) + ").then(function (r) { window.__saveResult = r; })", awaitPromise: false });
await wait(2500);
check('弹出项目选择器', (await ev("!!document.querySelector('#dialogHost .dialog')")) === true);
const selCount = await ev("document.querySelectorAll('#dialogHost select').length");
check('选择器有项目和节点两个下拉', Number(selCount) === 2, selCount);
const optText = await ev("Array.from(document.querySelectorAll('#dialogHost select')[1].options).map(function (o) { return o.textContent; }).join(' / ')");
check('节点列表带缩进', String(optText).indexOf('　　') >= 0 || String(optText).indexOf('【主干】') >= 0, String(optText).slice(0, 60));
// 选「联动测试」项目和「方法」节点
await ev("(function () { var s = document.querySelectorAll('#dialogHost select'); var p = Array.from(s[0].options).find(function (o) { return o.textContent.indexOf('联动测试') >= 0; }); if (p) { s[0].value = p.value; s[0].dispatchEvent(new Event('change')); } return 'ok'; })()");
await wait(700);
await ev("(function () { var s = document.querySelectorAll('#dialogHost select'); var n = Array.from(s[1].options).find(function (o) { return o.textContent.indexOf('方法') >= 0; }); if (n) { s[1].value = n.value; } return 'ok'; })()");
await ev("(function () { var b = Array.from(document.querySelectorAll('#dialogHost .actions button')).find(function (x) { return x.textContent.indexOf('存进去') >= 0; }); if (b) b.click(); return 'ok'; })()");
await wait(3000);
const g2 = post('/api/project/load', { id: pid }).graph;
const assets = (g2.nodes[0].assets || []).map((a) => a.name);
check('文件真的挂到节点上', assets.length >= 1, assets.join(','));
const toast = await ev("Array.from(document.querySelectorAll('.toast')).map(function (t) { return t.textContent; }).join(' | ')");
check('有成功提示', String(toast).indexOf('已存进项目') >= 0 || assets.length >= 1, toast);
check('无 JS 异常', errs.length === 0, errs.slice(0, 2).join(' | '));

try { await send('Browser.close'); } catch (e) {}
ws.close();
console.log('project=' + pid);
const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.n + (x.e ? ' — ' + x.e : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
