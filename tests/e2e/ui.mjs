import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (n, c, e) => results.push({ n, ok: !!c, e: e === undefined ? '' : String(e) });
const curl = (a) => spawnSync('curl.exe', a, { encoding: 'utf8', windowsHide: true, timeout: 60000 }).stdout || '';
const getRaw = (p) => curl(['-s', '--ssl-no-revoke', '-m', '20', BASE + p]);
const TK = (() => { try { return JSON.parse(getRaw('/api/state')).api_token || ''; } catch { return ''; } })();
const post = (p, b) => {
  const raw = curl(['-s', '--ssl-no-revoke', '-m', '60', '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(b || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { raw }; }
};

const proj = 'D:\\_e2e_ui_' + Date.now();
try { fs.rmSync(proj, { recursive: true, force: true }); } catch {}
fs.mkdirSync(proj, { recursive: true });
const c = post('/api/project/create', { name: '界面端到端', path: proj });
const id = c.id;
const files = {};
for (const k of ['引言', '研究背景', '方法', '实验']) {
  const w = post('/api/project/node/write', { id, node: { id: 'n_' + k, title: k }, body: '# ' + k + ' 正文' });
  files[k] = w.file;
}
const g = post('/api/project/load', { id }).graph;
g.nodes = [
  { id: 'n1', title: '引言', parent: 't1', axis: 'h', file: files['引言'] },
  { id: 'n2', title: '研究背景', parent: 'n1', axis: 'h', file: files['研究背景'] },
  { id: 'n3', title: '方法', parent: 'n2', axis: 'h', file: files['方法'] },
  { id: 'n4', title: '实验', parent: 'n3', axis: 'v', file: files['实验'] },
];
post('/api/project/graph', { id, graph: g });
post('/api/project/commit-now', { id, message: 'mt: 界面测试' });

const browser = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=10660', '--user-data-dir=D:\\_e2e_profile_' + Date.now(), '--window-size=1400,880',
   BASE + '/projects/project.html?id=' + id], { windowsHide: true, detached: true, stdio: 'ignore' });
browser.unref();
await new Promise((r) => setTimeout(r, 9000));
const list = await (await fetch('http://127.0.0.1:10660/json/list')).json();
const target = list.find((t) => t.type === 'page' && String(t.url).indexOf('127.0.0.1:8931') >= 0) || list.find((t) => t.type === 'page') || list[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let n = 0; const pend = new Map(); const errs = [];
const send = (m, q) => new Promise((r) => { const i = ++n; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: q })); });
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') errs.push(String(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 160));
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result || m.error); pend.delete(m.id); } });
await Promise.race([
  new Promise((r) => ws.addEventListener('open', r)),
  new Promise((_, rej) => setTimeout(() => rej(new Error('CDP 连接超时')), 15000)),
]);
ws.addEventListener('error', () => {});
await send('Runtime.enable', {});
await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 880, deviceScaleFactor: 1, mobile: false });
const ev = async (x) => { const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true }); return r.exceptionDetails ? 'EXC ' + String(r.exceptionDetails.exception?.description).slice(0, 140) : r.result?.value; };
await new Promise((r) => setTimeout(r, 4500));

check('画布页加载', (await ev('document.title')).includes('项目'), await ev('document.title'));
check('节点 4+主干', (await ev("document.querySelectorAll('.node').length")) === 5, await ev("document.querySelectorAll('.node').length"));
check('编辑栏常驻', (await ev("getComputedStyle(document.getElementById('editorHost')).display")) !== 'none');
check('节点都在可视区', await ev("(()=>{const box=document.getElementById('wrap').getBoundingClientRect();return [...document.querySelectorAll('.node')].every(n=>{const r=n.getBoundingClientRect();return r.right<=box.right+2&&r.left>=box.left-2&&r.bottom<=box.bottom+2&&r.top>=box.top-2;});})()"));
check('工具栏按钮不拆字', await ev("(()=>{const bs=[...document.querySelectorAll('.toolbar .btn')];return bs.every(b=>b.getBoundingClientRect().height<44);})()"));

const clickNode = (sel) => "(()=>{const el=document.querySelector('" + sel + "');const rc=el.getBoundingClientRect();const x=rc.left+rc.width/2,y=rc.top+rc.height/2;const t=document.elementFromPoint(x,y);t.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,clientX:x,clientY:y,button:0,buttons:1}));t.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:x,clientY:y,button:0}));(document.elementFromPoint(x,y)||t).dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:x,clientY:y,button:0}));})()";

const beforeDelete = await ev('graph.nodes.length');
await ev(clickNode('.node[data-id="n4"]'));
await new Promise((r) => setTimeout(r, 1200));
check('单击打开编辑', (await ev("document.getElementById('editorName').value")) === '实验', await ev("document.getElementById('editorName').value"));
check('路径高亮', (await ev("[...document.querySelectorAll('.node.on-path')].map(e=>e.dataset.id).join(',')")) === 't1,n1,n2,n3,n4');

const before = await ev('graph.nodes.length');
await ev("document.querySelector('.node[data-id=\"n4\"] .branch-handle.right')?.click()");
await new Promise((r) => setTimeout(r, 1300));
const added = await ev('graph.nodes.length');
await ev("window.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}))");
await new Promise((r) => setTimeout(r, 1600));
const undone = await ev('graph.nodes.length');
check('新增节点', added === before + 1, before + '→' + added);
check('Ctrl+Z 撤销', undone === before, added + '→' + undone);

await ev(clickNode('.node[data-id="n4"]'));
await new Promise((r) => setTimeout(r, 900));
await ev("document.getElementById('editorDelete').click()");
await new Promise((r) => setTimeout(r, 800));
check('删除弹出应用内对话框', await ev("!!document.querySelector('.mt-modal')"));
await ev("document.querySelector('.mt-modal [data-act=\"ok\"]')?.click()");
await new Promise((r) => setTimeout(r, 800));
await ev("document.querySelector('.mt-modal [data-act=\"cancel\"]')?.click()");
await new Promise((r) => setTimeout(r, 2200));
const afterDelete = await ev('graph.nodes.length');
check('删除生效', afterDelete === beforeDelete - 1, beforeDelete + '→' + afterDelete);

await ev("document.getElementById('history').click()");
await new Promise((r) => setTimeout(r, 2200));
// 编辑栏宽度可拖动
check('有拖动手柄', await ev("!!document.getElementById('editorResizer')"));
const w0 = await ev("Math.round(document.getElementById('editorHost').getBoundingClientRect().width)");
await ev("(async()=>{const h=document.getElementById('editorResizer');const r=h.getBoundingClientRect();const y=r.top+r.height/2;const x0=r.left+4;h.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,clientX:x0,clientY:y}));window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:x0-180,clientY:y}));window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:x0-180,clientY:y}));await new Promise(r2=>setTimeout(r2,250));})()");
const w1 = await ev("Math.round(document.getElementById('editorHost').getBoundingClientRect().width)");
check('拖动后变宽', w1 > w0 + 100, w0 + '→' + w1);
check('宽度记进了 localStorage', (await ev("localStorage.getItem('mt.editorWidth')")) === String(w1));
check('拖宽后节点仍在可视区', await ev("(()=>{const box=document.getElementById('wrap').getBoundingClientRect();return [...document.querySelectorAll('.node')].every(n=>{const r=n.getBoundingClientRect();return r.left>=box.left-2&&r.right<=box.right+2;});})()"));
await ev("(async()=>{const h=document.getElementById('editorResizer');h.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));await new Promise(r=>setTimeout(r,250));})()");
check('双击恢复默认宽度', (await ev("Math.round(document.getElementById('editorHost').getBoundingClientRect().width)")) === 400);

// 弹窗必须是主题样式（不能是浏览器原生 confirm）
await ev("window.__native=0; window.confirm=()=>{window.__native++;return false;}; window.alert=()=>{window.__native++;}; window.prompt=()=>{window.__native++;return null;};")
await ev("document.getElementById('history').click()");
await new Promise((r) => setTimeout(r, 2200));
await ev("(()=>{const b=[...document.querySelectorAll('#historyHost .history-acts button')].find(x=>x.textContent==='回到这个版本'); if(b) b.click();})()");
await new Promise((r) => setTimeout(r, 800));
check('回滚确认是主题对话框', await ev("!!document.querySelector('.mt-modal')"));
check('没有弹浏览器原生确认框', (await ev('window.__native')) === 0, '原生调用 ' + (await ev('window.__native')) + ' 次');
await ev("document.querySelector('.mt-modal [data-act=\"cancel\"]')?.click()");
await new Promise((r) => setTimeout(r, 400));
await ev("document.getElementById('historyClose')?.click()");
await new Promise((r) => setTimeout(r, 400));

check('历史面板', (await ev("document.querySelector('#historyHost h3')?.textContent")) === '版本历史');
check('有导出按钮', (await ev("!!document.getElementById('historyExport')")) === true);
check('有版本条目', (await ev("document.querySelectorAll('.history-item').length")) >= 1);
await ev("document.getElementById('historyClose')?.click()");

check('无 JS 异常', errs.length === 0, errs.slice(0, 2).join(' | '));
check('服务仍在线', (() => { try { return !!JSON.parse(getRaw('/api/ping')).ok; } catch { return false; } })());

ws.close();
try { process.kill(browser.pid); } catch {}

const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.n + (x.e ? ' — ' + x.e : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);