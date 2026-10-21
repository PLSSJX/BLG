import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (n, c, e) => results.push({ n, ok: !!c, e: e === undefined ? '' : String(e) });
const curl = (a) => spawnSync('curl.exe', a, { encoding: 'utf8', windowsHide: true, timeout: 60000 }).stdout || '';
const TK = (() => { try { return JSON.parse(curl(['-s', BASE + '/api/state'])).api_token || ''; } catch { return ''; } })();
const post = (p, b) => { const raw = curl(['-s', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(b || {}), BASE + p]); try { return JSON.parse(raw); } catch { return { raw }; } };

// 建一个项目用于测试
const proj = 'D:\\_e2e_hdr_' + Date.now();
fs.mkdirSync(proj, { recursive: true });
const c = post('/api/project/create', { name: '按钮端到端', path: proj });
const id = c.id;
check('准备项目', !!id, id);

const browser = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=10690', '--user-data-dir=D:\\_e2e_profile_' + Date.now(), '--window-size=1400,880', BASE + '/#home'],
  { windowsHide: true, detached: true, stdio: 'ignore' });
browser.unref();
await new Promise((r) => setTimeout(r, 9000));
const list = await (await fetch('http://127.0.0.1:10690/json/list')).json();
const target = list.find((t) => t.type === 'page' && String(t.url).indexOf('127.0.0.1:8931') >= 0) || list.find((t) => t.type === 'page') || list[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let n = 0; const pend = new Map(); const errs = [];
const send = (m, q) => new Promise((r) => { const i = ++n; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: q })); });
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') errs.push(String(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 150));
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result || m.error); pend.delete(m.id); } });
await Promise.race([
  new Promise((r) => ws.addEventListener('open', r)),
  new Promise((_, rej) => setTimeout(() => rej(new Error('CDP 连接超时')), 15000)),
]);
ws.addEventListener('error', () => {});
await send('Runtime.enable', {});
await new Promise((r) => setTimeout(r, 3000));
const ev = async (x) => { const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true }); return r.exceptionDetails ? 'EXC ' + String(r.exceptionDetails.exception?.description).slice(0, 150) : r.result?.value; };
const fab = async () => await ev("(()=>{const f=document.getElementById('railSearch');return (f.dataset.mode||'-')+'|'+f.title;})()");
const framePath = async () => await ev("document.querySelector('.projects-frame').contentWindow.location.pathname");

check('别的页面是返回箭头', (await fab()).startsWith('back'), await fab());
await ev("document.getElementById('railSearch').click()");
await new Promise((r) => setTimeout(r, 2500));
check('点箭头到项目页', (await ev('location.hash')) === '#projects', await ev('location.hash'));
check('列表页是加号', (await fab()).startsWith('new'), await fab());
await ev("document.getElementById('railSearch').click()");
await new Promise((r) => setTimeout(r, 1200));
check('加号打开新建对话框', await ev("document.querySelector('.projects-frame').contentDocument.getElementById('dialogHost').classList.contains('open')"));
await ev("document.querySelector('.projects-frame').contentDocument.getElementById('cancel').click()");
const cardsBefore = await ev("document.querySelector('.projects-frame').contentDocument.querySelectorAll('.project-card').length");
check('卡片上有删除项目', await ev("[...document.querySelector('.projects-frame').contentDocument.querySelectorAll('.mini-btn')].some(b=>b.textContent==='删除项目')"));

await ev("document.querySelector('.projects-frame').contentDocument.querySelector('.project-card').click()");
await new Promise((r) => setTimeout(r, 4000));
check('进入画布', (await framePath()) === '/projects/project.html', await framePath());
check('画布态是退出箭头', (await fab()).startsWith('exit'), await fab());
await ev("document.getElementById('railSearch').click()");
await new Promise((r) => setTimeout(r, 4500));
check('退出回到列表', (await framePath()) === '/projects/index.html', await framePath());
check('回到列表是加号', (await fab()).startsWith('new'), await fab());

await ev("(()=>{const d=document.querySelector('.projects-frame').contentDocument; const b=[...d.querySelectorAll('.mini-btn')].find(x=>x.textContent==='删除项目'); b && b.click();})()");
await new Promise((r) => setTimeout(r, 1500));
check('删除弹出应用内对话框', await ev("!!document.querySelector('.projects-frame').contentDocument.querySelector('.mt-modal')"));
await ev("document.querySelector('.projects-frame').contentDocument.querySelector('.mt-modal [data-act=\"ok\"]').click()");
await new Promise((r) => setTimeout(r, 5000));
const left = await ev("document.querySelector('.projects-frame').contentDocument.querySelectorAll('.project-card').length");
const names = await ev("[...document.querySelector('.projects-frame').contentDocument.querySelectorAll('.project-name')].map(e=>e.textContent).join(',')");
check('删除后卡片减一', left === cardsBefore - 1, cardsBefore + '→' + left);
check('被删的项目不在列表里', !String(names).includes('按钮端到端'), names);
check('项目文件夹已删掉', !fs.existsSync(proj), proj);
check('无 JS 异常', errs.length === 0, errs.slice(0, 2).join(' | '));

ws.close();
try { process.kill(browser.pid); } catch {}
const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.n + (x.e ? ' — ' + x.e : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);