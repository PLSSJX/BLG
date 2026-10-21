import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (n, c, e) => results.push({ n, ok: !!c, e: e === undefined ? '' : String(e) });
const curl = (a) => spawnSync('curl.exe', a, { encoding: 'utf8', windowsHide: true, timeout: 60000 }).stdout || '';
const getRaw = (p) => curl(['-s', '--ssl-no-revoke', '-m', '20', BASE + p]);
const TK = (() => { try { return JSON.parse(getRaw('/api/state')).api_token || ''; } catch { return ''; } })();
const post = (p, b) => { const raw = curl(['-s', '--ssl-no-revoke', '-m', '60', '-X', 'POST', '-H', 'Content-Type: application/json', '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(b || {}), BASE + p]); try { return JSON.parse(raw); } catch { return { raw }; } };

// 准备一个干净项目
const proj = 'D:\\_btn_' + Date.now();
fs.mkdirSync(proj, { recursive: true });
const c = post('/api/project/create', { name: '按钮巡检', path: proj });
const id = c.id;
const files = {};
for (const k of ['引言', '方法', '实验']) {
  const w = post('/api/project/node/write', { id, node: { id: 'n_' + k, title: k }, body: '# ' + k + '\n\n正文' });
  files[k] = w.file;
}
const g = post('/api/project/load', { id }).graph;
g.nodes = [
  { id: 'n1', title: '引言', parent: 't1', axis: 'h', file: files['引言'] },
  { id: 'n2', title: '方法', parent: 'n1', axis: 'h', file: files['方法'] },
  { id: 'n3', title: '实验', parent: 'n2', axis: 'v', file: files['实验'] },
];
post('/api/project/graph', { id, graph: g });
post('/api/project/commit-now', { id, message: 'mt: 巡检准备' });

const browser = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=10800', '--window-size=1417,928',
   '--user-data-dir=D:\\_btn_p_' + Date.now(), BASE + '/projects/project.html?id=' + id],
  { windowsHide: true, detached: true, stdio: 'ignore' });
browser.unref();
await new Promise((r) => setTimeout(r, 9000));
const list = await (await fetch('http://127.0.0.1:10800/json/list')).json();
const target = list.find((t) => t.type === 'page' && String(t.url).includes('8931')) || list[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let n = 0; const pend = new Map(); const errs = [];
const send = (m, q) => new Promise((r) => { const i = ++n; pend.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: q })); });
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data);
  if (m.method === 'Runtime.exceptionThrown') errs.push(String(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 160));
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result || m.error); pend.delete(m.id); } });
await new Promise((r) => ws.addEventListener('open', r));
await send('Runtime.enable', {});
await send('Emulation.setDeviceMetricsOverride', { width: 1417, height: 928, deviceScaleFactor: 1, mobile: false });
await new Promise((r) => setTimeout(r, 4500));
const ev = async (x) => { const r = await send('Runtime.evaluate', { expression: x, awaitPromise: true, returnByValue: true }); return r.exceptionDetails ? 'EXC ' + String(r.exceptionDetails.exception?.description).slice(0, 140) : r.result?.value; };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await ev("window.__openNode = function (id) {\n  const el = document.querySelector('.node[data-id=\"' + id + '\"]');\n  if (!el) return 'no-node';\n  const rc = el.getBoundingClientRect();\n  const x = rc.left + rc.width / 2, y = rc.top + rc.height / 2;\n  const t = document.elementFromPoint(x, y) || el;\n  t.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y, button: 0, buttons: 1 }));\n  t.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: x, clientY: y, button: 0 }));\n  (document.elementFromPoint(x, y) || el).dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y, button: 0 }));\n  return 'ok';\n};\n'ok'");
const click = (sel) => ev("(()=>{const e=document.querySelector('" + sel + "'); if(!e) return 'missing'; e.click(); return 'ok';})()");
const modal = async () => await ev("document.querySelector('.mt-modal') ? document.querySelector('.mt-modal-title').textContent : ''");
const okModal = async () => { await ev("document.querySelector('.mt-modal [data-act=\"ok\"]')?.click()"); await wait(600); };
const cancelModal = async () => { await ev("document.querySelector('.mt-modal [data-act=\"cancel\"]')?.click()"); await wait(400); };
const nodeCount = async () => await ev('graph.nodes.length');
const closeAnyModal = async () => {
  for (let i = 0; i < 3; i++) {
    const n = await ev("document.querySelectorAll('.mt-modal').length");
    if (!n) return;
    await ev("(()=>{const m=document.querySelector('.mt-modal'); const b=m.querySelector('[data-act=\"cancel\"]')||m.querySelector('[data-act=\"ok\"]'); if(b) b.click(); else m.remove();})()");
    await wait(300);
  }
};

// ---------- 顶部按钮 ----------
check('撤销按钮可点', (await ev("document.getElementById('undoBtn').disabled")) === false);
check('重做按钮可点', (await ev("document.getElementById('redoBtn').disabled")) === false);
await ev("document.querySelectorAll('.mt-toast').forEach(t=>t.remove())");
await click('#undoBtn'); await wait(400);
check('空栈撤销给提示', (await ev("[...document.querySelectorAll('.mt-toast')].map(t=>t.textContent).join('')")).includes('没有可撤销'), await ev("[...document.querySelectorAll('.mt-toast')].map(t=>t.textContent).join('|')"));
await ev("document.querySelectorAll('.mt-toast').forEach(t=>t.remove())");
await click('#redoBtn'); await wait(400);
check('空栈重做给提示', (await ev("[...document.querySelectorAll('.mt-toast')].map(t=>t.textContent).join('')")).includes('没有可恢复'), await ev("[...document.querySelectorAll('.mt-toast')].map(t=>t.textContent).join('|')"));

await ev("document.querySelector('#layoutSwitch button[data-layout=\"mindmap\"]').click()"); await wait(600);
check('切换到思维导图', (await ev("document.querySelector('#layoutSwitch button[data-layout=\"mindmap\"]').classList.contains('on')")));
await ev("document.querySelector('#layoutSwitch button[data-layout=\"timeline\"]').click()"); await wait(600);
check('切回时间轴', (await ev("document.querySelector('#layoutSwitch button[data-layout=\"timeline\"]').classList.contains('on')")));

const before = await nodeCount();
await ev("document.querySelector('.node[data-id=\"n3\"] .branch-handle.right').click()"); await wait(1200);
check('主干手柄加节点', (await nodeCount()) === before + 1, before + '→' + (await nodeCount()));
await cancelModal();
await ev("document.querySelector('.node[data-id=\"n3\"] .branch-handle.right') || document.querySelector('.branch-handle.down').click()"); await wait(800);
await ev("document.querySelector('.mt-modal [data-act=\"ok\"]')?.click() || document.querySelector('[data-act=\"main\"]')?.click()"); await wait(1200);
check('添加节点按钮可用', (await nodeCount()) >= before + 1, await nodeCount());

// ---------- 编辑栏 ----------
await closeAnyModal();
check('节点可点击', (await ev("window.__openNode('n2')")) === 'ok');
await wait(1200);
check('点节点打开编辑', (await ev("document.getElementById('editorName').value")) === '方法', await ev("document.getElementById('editorName').value"));
await ev("document.getElementById('editorName').value = '方法改名'; document.getElementById('editorName').dispatchEvent(new Event('input',{bubbles:true}))");
await wait(300);
await click('#editorSave'); await wait(1800);
const savedTitle = post('/api/project/load', { id }).graph.nodes.map((x) => x.title).join(',');
check('保存改名', savedTitle.includes('方法改名'), savedTitle);

// 关联节点（保存后编辑栏会关掉，这里重新打开）
await closeAnyModal();
await ev("window.__openNode('n2')"); await wait(1300);
check('重新打开编辑栏', (await ev("document.getElementById('editorHost').classList.contains('open')")) === true);
await click('#editorLink'); await wait(800);
const linkMode = await ev("document.getElementById('canvas').classList.contains('linking')");
check('进入关联模式', linkMode === true, String(linkMode));
await ev("window.__openNode('n1')"); await wait(1400);
check('关联建立', (await ev('(graph.links||[]).length')) >= 1, await ev('(graph.links||[]).length'));
// 关联模式会一直停留（可以连着点），测试里要按 Esc 退出
await ev("window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))"); await wait(600);
check('Esc 退出关联模式', (await ev("document.getElementById('canvas').classList.contains('linking')")) === false);

// 挂文件（用输入框 + 挂上）：关联模式会关掉编辑栏，先重新打开节点
await closeAnyModal();
await ev("window.__openNode('n2')"); await wait(1300);
check('挂文件前编辑栏是开的', (await ev("document.getElementById('editorHost').classList.contains('open')")) === true);
const SRC_FILE = 'D:\\_btn_attach_' + Date.now() + '.txt';
fs.writeFileSync(SRC_FILE, 'hello', 'utf8');
await ev("document.querySelector('.asset-box').open = true");
await ev("document.getElementById('assetPath').value = " + JSON.stringify(SRC_FILE));
await click('#attachBtn'); await wait(1200);
const attachToast = await ev("Array.from(document.querySelectorAll('.mt-toast')).map(function(t){return t.textContent;}).join(' | ')");
const assetsTotal = post('/api/project/load', { id }).graph.nodes.reduce((a, n) => a + ((n.assets || []).length), 0);
check('挂文件按钮可用', assetsTotal >= 1, '附件总数 ' + assetsTotal + ' 提示=' + attachToast);
check('挂文件后出现移除按钮', (await ev("document.querySelectorAll('#assetList .mini-btn, #assetList button').length")) >= 1);

// 从最近结果（挂文件后编辑栏会关，重新打开）
await closeAnyModal();
await ev("window.__openNode('n2')"); await wait(1300);
await click('#editorRecent'); await wait(1800);
const recentModal = await modal();
check('从最近结果有反馈', !!recentModal, recentModal);
await okModal(); await cancelModal();

// 问 AI（只验证对话框能开，不发请求）
await closeAnyModal();
await ev("window.__openNode('n2')"); await wait(1200);
await click('#editorAsk'); await wait(800);
check('问 AI 弹出对话框', (await ev("!!document.querySelector('.mt-ask-box')")) === true);
check('问 AI 说明写了隐私边界', String(await ev("(document.querySelector('.mt-ask-box .mt-modal-body')||{}).textContent||''")).includes('本地'), await ev("(document.querySelector('.mt-ask-box .mt-modal-body')||{}).textContent||''"));
await ev("document.querySelector('.mt-ask-box [data-act=\"cancel\"]')?.click()"); await wait(400);

// 撤销 / 重做按钮真的能改结构
await closeAnyModal();
const n0 = await nodeCount();
await ev("document.querySelector('.node[data-id=\"n3\"] .branch-handle.right').click()"); await wait(1200); await cancelModal();
await click('#undoBtn'); await wait(1200);
check('撤销按钮生效', (await nodeCount()) === n0, n0 + '→' + (await nodeCount()));
await click('#redoBtn'); await wait(1200);
check('重做按钮生效', (await nodeCount()) === n0 + 1, await nodeCount());
await click('#undoBtn'); await wait(1000);

// 工具栏
await closeAnyModal();
const autoBefore = await ev("document.getElementById('autoToggle').textContent");
await click('#autoToggle'); await wait(800);
const autoAfter = await ev("document.getElementById('autoToggle').textContent");
check('自动保存开关变化', autoBefore !== autoAfter, autoBefore + ' → ' + autoAfter);
await click('#autoToggle'); await wait(400);
const sumBefore = await ev("document.getElementById('autoSummary').textContent");
await click('#autoSummary'); await wait(600);
check('摘要同步开关变化', sumBefore !== (await ev("document.getElementById('autoSummary').textContent")));
await click('#autoSummary'); await wait(300);
const layoutOn = await ev("document.getElementById('layoutToggle').textContent");
await click('#layoutToggle'); await wait(800);
check('自动排版开关变化', layoutOn !== (await ev("document.getElementById('layoutToggle').textContent")));
await click('#layoutToggle'); await wait(300);

// 暂存区
await closeAnyModal();
const staged0 = await ev('(graph.staged||[]).length');
await click('#stageAddText'); await wait(700);
const stageModal = await modal();
check('文本框按钮弹对话框', !!stageModal, stageModal);
await okModal();
check('暂存区多了一项', (await ev('(graph.staged||[]).length')) === staged0 + 1, staged0 + '→' + (await ev('(graph.staged||[]).length')));
await wait(600);
check('暂存项有移除按钮', (await ev("document.querySelectorAll('#stageItems .stage-del').length")) >= 1);
await ev("document.querySelector('#stageItems .stage-del')?.click()"); await wait(900);
check('暂存项能移除', (await ev('(graph.staged||[]).length')) === staged0);

// 历史面板
await closeAnyModal();
await click('#history'); await wait(3000);
check('历史面板打开', (await ev("document.getElementById('historyHost').classList.contains('open')")) === true);
check('有回到版本按钮', (await ev("[...document.querySelectorAll('#historyHost .history-acts button')].some(b=>b.textContent==='回到这个版本')")));
await ev("[...document.querySelectorAll('#historyHost .history-acts button')].find(b=>b.textContent==='回到这个版本')?.click()"); await wait(800);
check('回滚弹主题对话框', !!(await modal()), await modal());
await cancelModal();
await ev("[...document.querySelectorAll('#historyHost .history-acts button')].find(b=>b.textContent==='文件')?.click()"); await wait(600);
check('展开文件列表', (await ev("document.querySelectorAll('#historyHost .history-file').length")) >= 1);
await click('#historyGc'); await wait(700);
check('清理按钮弹对话框', !!(await modal()), await modal());
await okModal(); await cancelModal();
await click('#historyExport'); await wait(700);
check('导出按钮弹对话框', !!(await modal()), await modal());
await cancelModal();
await click('#historyClose'); await wait(500);
check('历史面板可关闭', (await ev("document.getElementById('historyHost').classList.contains('open')")) === false);

// 分支面板
await closeAnyModal();
await click('#branchBtn'); await wait(3000);
check('分支面板打开', (await ev("document.querySelector('#branchHost') && document.querySelector('#branchHost').classList.contains('open')")) === true);
check('有新建分支按钮', (await ev("[...document.querySelectorAll('#branchHost button')].some(b=>/新建|创建/.test(b.textContent))")));
await ev("document.getElementById('newBranch').value = '巡检分支'");
await ev("document.getElementById('createBranch').click()"); await wait(1800);
const brs = post('/api/project/branches', { id }).branches || [];
check('分支真的建出来', brs.includes('巡检分支'), brs.join(','));
await ev("document.getElementById('branchHost').classList.remove('open')");

// 边线右键菜单（说明 / 删除）
await closeAnyModal();
await ev("(()=>{const hit=document.querySelectorAll('.edge-hit')[0]; if(!hit) return; const len=hit.getTotalLength(); const pt=hit.getPointAtLength(len/2); const svg=document.getElementById('edges').getBoundingClientRect(); hit.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:svg.left+pt.x,clientY:svg.top+pt.y}));})()");
await wait(700);
check('线上右键出菜单', (await ev("!!document.querySelector('.edge-tip, .ctx-menu, #edgeMenu')")));
await ev("(()=>{const b=[...document.querySelectorAll('.edge-tip button, .ctx-menu button')].find(x=>/说明/.test(x.textContent)); if(b) b.click();})()");
await wait(700);
check('改说明弹输入框', (await ev("!!document.querySelector('.mt-modal-input')")));
await cancelModal();

check('无 JS 异常', errs.length === 0, errs.slice(0, 3).join(' | '));

try { await send('Browser.close'); } catch (e) {}
ws.close();
const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.n + (x.e ? ' — ' + x.e : ''));
console.log('\\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);