import { spawn, spawnSync } from 'node:child_process';
import fsn from 'node:fs';

const BASE = 'http://127.0.0.1:8931';
const results = [];
const check = (n, c, e) => results.push({ n, ok: !!c, e: e === undefined ? '' : String(e) });
const curl = (a) => spawnSync('curl.exe', a, { encoding: 'utf8', windowsHide: true, timeout: 60000 }).stdout || '';
const TK = JSON.parse(curl(['-s', BASE + '/api/state'])).api_token;
const post = (p, b) => {
  const raw = curl(['-s', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(b || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { raw }; }
};
const dir = 'D:\\_ui_btn_' + Date.now();
fsn.mkdirSync(dir, { recursive: true });

const browser = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=10810', '--window-size=1417,928',
   '--user-data-dir=D:\\_ui_btn_p_' + Date.now(), BASE + '/#projects'],
  { windowsHide: true, detached: true, stdio: 'ignore' });
browser.unref();
await new Promise((r) => setTimeout(r, 9000));
const list = await (await fetch('http://127.0.0.1:10810/json/list')).json();
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
await new Promise((r) => setTimeout(r, 4500));

const ev = async (x) => {
  const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true });
  return r.exceptionDetails ? 'EXC ' + String(r.exceptionDetails.exception?.description).slice(0, 150) : r.result?.value;
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const FRAME = "document.querySelector('.projects-frame').contentDocument";
const helperCode = "window.__fev = function (body) { var d = " + FRAME + "; if (!d) return 'no-frame'; try { return new Function('d', body)(d); } catch (e) { return 'ERR ' + e.message; } }; 'ok'";
await ev(helperCode);
const fev = async (body) => await ev('window.__fev(' + JSON.stringify(body) + ')');

check('项目 iframe 已加载', (await ev("!!document.querySelector('.projects-frame')")) === true);
await wait(2500);
const title = await fev("const t = d.querySelector('h1, h2, .title'); return t ? t.textContent : '无';");
check('列表页渲染出标题', String(title).includes('项目'), title);

const newBtn = await fev("const b = Array.from(d.querySelectorAll('button')).filter(function (x) { return /新建|创建/.test(x.textContent); }); return b.length ? (b[0].id || b[0].className) : '';");
check('有新建项目按钮', !!newBtn, newBtn);
await fev("const b = Array.from(d.querySelectorAll('button')).filter(function (x) { return /新建|创建/.test(x.textContent); })[0]; b.click(); return 'ok';");
await wait(1000);
const modalTitle = await fev("const m = d.querySelector('.mt-modal-title, .dialog h3'); return m ? m.textContent : '无';");
check('新建弹出对话框', String(modalTitle) !== '无', modalTitle);

const DIR = JSON.stringify(dir);
await fev("const i = d.querySelectorAll('.mt-modal input, .dialog input'); if (i[0]) { i[0].value = '巡检列表项目'; i[0].dispatchEvent(new Event('input', { bubbles: true })); } if (i[1]) { i[1].value = " + DIR + "; i[1].dispatchEvent(new Event('input', { bubbles: true })); } return 'filled';");
const filled = await fev("return [d.getElementById('projectName') ? d.getElementById('projectName').value : '无name', d.getElementById('projectPath') ? d.getElementById('projectPath').value : '无path'].join(' | ');");
console.log('填表结果:', filled);
const clickRes = await fev("const b = d.getElementById('confirm'); if (!b) return 'no-confirm'; b.click(); return 'clicked';");
console.log('创建按钮:', clickRes);
await wait(3500);
const after = await fev("const m = d.querySelector('.dialog h3'); return m ? m.textContent : '无对话框';");
console.log('点击后对话框:', after);
const names1 = post('/api/projects', {}).projects.map((p) => p.name);
check('新建项目真的建出来', names1.includes('巡检列表项目'), names1.join(','));

await ev('location.reload()');
await wait(7000);
await ev(helperCode);
const cards = await fev("return Array.from(d.querySelectorAll('.project-card')).map(function (c) { return c.textContent; }).join('|');");
check('刷新后卡片在', String(cards).includes('巡检列表项目'), String(cards).slice(0, 60));

const cardBtn = await fev("const c = Array.from(d.querySelectorAll('.project-card')).find(function (x) { return /巡检列表项目/.test(x.textContent); }); const b = c ? c.querySelector('button') : null; return b ? b.textContent : '';");
check('卡片上有重命名按钮', cardBtn === '重命名', cardBtn);
await fev("const c = Array.from(d.querySelectorAll('.project-card')).find(function (x) { return /巡检列表项目/.test(x.textContent); }); c.querySelector('button').click(); return 'ok';");
await wait(1000);
await fev("const i = d.querySelector('.mt-modal input'); if (i) { i.value = '改名后的项目'; i.dispatchEvent(new Event('input', { bubbles: true })); } const b = d.querySelector('.mt-modal [data-act=\"ok\"]'); if (b) b.click(); return 'ok';");
await wait(3000);
const names2 = post('/api/projects', {}).projects.map((p) => p.name);
check('重命名生效', names2.includes('改名后的项目'), names2.join(','));

await ev('location.reload()');
await wait(7000);
await ev(helperCode);
const delClick = await fev("const c = Array.from(d.querySelectorAll('.project-card')).find(function (x) { return /改名后的项目/.test(x.textContent); }); if (!c) return 'no-card'; const b = Array.from(c.querySelectorAll('button')).find(function (x) { return /删除/.test(x.textContent); }); if (b) { b.click(); return 'clicked'; } return 'no-btn';");
check('卡片上有删除按钮', delClick === 'clicked', delClick);
await wait(1000);
const delModal = await fev("const m = d.querySelector('.mt-modal-title'); return m ? m.textContent : '没打开';");
check('删除弹确认框', String(delModal).includes('删除'), delModal);
const delBody = await fev("const b = d.querySelector('.mt-modal-body'); return b ? b.textContent.slice(0, 40) : '';");
check('删除框说清后果', String(delBody).includes('不可恢复') || String(delBody).includes('一起删'), delBody);
await fev("const b = d.querySelector('.mt-modal [data-act=\"ok\"]') || Array.from(d.querySelectorAll('.mt-modal button, .dialog button')).find(function (x) { return /删除|确定/.test(x.textContent); }); if (b) b.click(); return b ? 'ok' : 'no-btn';");
await wait(4000);
const names3 = post('/api/projects', {}).projects.map((p) => p.name);
check('删除后列表里没有了', !names3.includes('改名后的项目'), names3.join(','));
check('删除后文件夹也删了', !fsn.existsSync(dir));

check('外壳左上角按钮存在', (await ev("!!document.getElementById('railSearch')")) === true);
check('导航项齐全', (await ev("document.querySelectorAll('.destination').length")) >= 5, await ev("document.querySelectorAll('.destination').length"));
const fabLabel = await ev("(document.getElementById('railSearch').getAttribute('title') || '') + '/' + (document.getElementById('railSearch').className || '')");
check('列表态按钮是新建', String(fabLabel).includes('新建') || String(fabLabel).includes('plus'), fabLabel);

check('无 JS 异常', errs.length === 0, errs.slice(0, 3).join(' | '));

ws.close();
try { process.kill(browser.pid); } catch (e) {}
const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.n + (x.e ? ' — ' + x.e : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
