/**
 * 端到端：节点与文件生命周期
 *
 * 覆盖：版本保存 / 回滚、删除与撤销、连续快速保存、节点改名后保存、
 *       删除中间节点后整份回滚、相同内容不产生新版本。
 *
 * 每个场景使用独立的临时项目目录（D:\_sc_a_<时间戳>\sN），
 * 每一步都同时核对「服务端状态」和「磁盘文件」。
 *
 * 说明：删除/回滚后「树结构是否恢复」取决于 .mt/graph.json 有没有进版本库；
 *       脚本按产品应然行为断言，失败时会逐条指出差异。
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (name, cond, extra) => { results.push({ name, ok: !!cond, extra: extra === undefined ? '' : String(extra) }); };

const curl = (args) => spawnSync('curl.exe', args, { encoding: 'utf8', windowsHide: true, timeout: 120000 }).stdout || '';
const getRaw = (p) => curl(['-s', '--ssl-no-revoke', '-m', '20', BASE + p]);
const TK = (() => { try { return JSON.parse(getRaw('/api/state')).api_token || ''; } catch { return ''; } })();
const post = (p, body) => {
  const raw = curl(['-s', '--ssl-no-revoke', '-m', '90', '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(body || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { ok: false, error: '不是 JSON：' + String(raw).slice(0, 80) }; }
};
const alive = () => { try { return !!JSON.parse(getRaw('/api/ping')).ok; } catch { return false; } };

// ---------- 路径与磁盘 ----------
const abs = (p, rel) => path.join(p.dir, String(rel || '').split('/').join(path.sep));
const exists = (p, rel) => { try { return fs.statSync(abs(p, rel)).isFile(); } catch { return false; } };
const diskText = (p, rel) => { try { return fs.readFileSync(abs(p, rel), 'utf8'); } catch { return null; } };
const walk = (dir, base, out) => {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === '.mt' || e.name === '.git') continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walk(full, base, out);
    else out.push(path.relative(base, full).split(path.sep).join('/'));
  }
  return out.sort();
};
const listFiles = (dir) => walk(dir, dir, []);

// ---------- 接口 ----------
const writeNode = (p, node, body) => post('/api/project/node/write', { id: p.id, node, body });
const readNode = (p, file) => post('/api/project/node/read', { id: p.id, file });
const loadGraph = (p) => post('/api/project/load', { id: p.id }).graph || {};
const nodesOf = (p) => loadGraph(p).nodes || [];
const saveNodes = (p, nodes) => {
  const g = loadGraph(p);
  return post('/api/project/graph', { id: p.id, graph: { trunk: g.trunk || { id: 't1', title: path.basename(p.dir) }, nodes, staged: g.staged || [] } });
};
const commitNow = (p, message) => post('/api/project/commit-now', { id: p.id, message });
const logOf = (p) => post('/api/project/log', { id: p.id }).commits || [];
const restore = (p, sha, files) => post('/api/project/restore', { id: p.id, commit: sha, files: files || [] });
const delNode = (p, nodeId, deleteFiles) => post('/api/project/node/delete', { id: p.id, nodeId, delete_files: !!deleteFiles });
const undo = (p) => post('/api/project/undo', { id: p.id });
const status = (p) => post('/api/project/status', { id: p.id });
const head = (p) => post('/api/project/head', { id: p.id });

// ---------- 结构快照 ----------
const sig = (nodes) => JSON.stringify((nodes || [])
  .map((n) => [n.id, n.title || '', n.parent || '', n.axis || '', n.file || ''])
  .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)));
const ids = (nodes) => (nodes || []).map((n) => n.id).join(',');

// ---------- 项目 / 场景 ----------
const ROOT = 'D:\\_sc_a_' + Date.now();
let seq = 0;
const project = (label) => {
  const dir = path.join(ROOT, 's' + (++seq));
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
  fs.mkdirSync(dir, { recursive: true });
  const r = post('/api/project/create', { name: label, path: dir });
  check('建项目：' + label, r.ok === true && !!r.id, r.error || r.id || '');
  return { id: r.id, dir, label };
};
const seedNodes = (p, defs) => {
  const files = {};
  for (const d of defs) files[d.id] = writeNode(p, { id: d.id, title: d.title }, d.body).file;
  const nodes = defs.map((d) => ({ id: d.id, title: d.title, parent: d.parent, axis: d.axis, file: files[d.id] }));
  saveNodes(p, nodes);
  return { files, nodes };
};
const scenario = (name, fn) => {
  try { fn(); } catch (e) { check(name + '：未抛异常', false, (e && e.message) || e); }
};

check('服务在线', alive());
check('有 token', TK.length > 0);

// ============================================================
// 1. 建项目 → 写正文 → 保存 → 改 → 保存 → 回滚第一版 → 再改 → 保存
// ============================================================
scenario('场景 1 版本流', () => {
  const p = project('场景1 版本流');
  if (!p.id) return;
  const defs = [{ id: 'n1', title: '方法', parent: 't1', axis: 'h', body: '# 方法\n\n第一版正文' }];
  const s = seedNodes(p, defs);
  const f = s.files.n1;
  check('场景1 正文文件已分配', !!f && exists(p, f), f);

  const c1 = commitNow(p, 'mt: 场景1 第一版');
  check('场景1 保存第一版', c1.committed === true, c1.reason || c1.sha);
  const log1 = logOf(p);
  const sha1 = (log1[0] || {}).sha;
  check('场景1 第一版已落盘', diskText(p, f) === defs[0].body, diskText(p, f));

  const body2 = '# 方法\n\n第二版正文，多写了一句。';
  const w2 = writeNode(p, { id: 'n1', title: '方法', file: f }, body2);
  check('场景1 改正文写回同一文件', w2.file === f, w2.file);
  const c2 = commitNow(p, 'mt: 场景1 第二版');
  check('场景1 保存第二版', c2.committed === true, c2.reason);
  check('场景1 第二版内容正确', diskText(p, f) === body2 && (readNode(p, f).body || '') === body2, diskText(p, f));
  check('场景1 版本数 +1', logOf(p).length === log1.length + 1, log1.length + '→' + logOf(p).length);

  const beforeRollback = listFiles(p.dir);
  const r1 = restore(p, sha1, []);
  check('场景1 回滚到第一版', r1.ok === true, r1.error);
  check('场景1 回滚后磁盘是第一版', diskText(p, f) === defs[0].body, diskText(p, f));
  check('场景1 回滚后接口读回第一版', (readNode(p, f).body || '') === defs[0].body, (readNode(p, f).body || '').slice(0, 40));
  check('场景1 回滚不动文件清单', JSON.stringify(listFiles(p.dir)) === JSON.stringify(beforeRollback), listFiles(p.dir).join(','));

  const body3 = '# 方法\n\n第三版正文，回滚之后再改。';
  const w3 = writeNode(p, { id: 'n1', title: '方法', file: f }, body3);
  check('场景1 回滚后再改仍写同一文件', w3.file === f, w3.file);
  const c3 = commitNow(p, 'mt: 场景1 第三版');
  check('场景1 回滚后再保存', c3.committed === true, c3.reason);
  check('场景1 第三版内容正确', diskText(p, f) === body3 && (readNode(p, f).body || '') === body3, diskText(p, f));

  const log3 = logOf(p);
  check('场景1 历史顺序正确', !!(log3[0] && log3[0].subject === 'mt: 场景1 第三版'
    && log3[1] && log3[1].subject === 'mt: 场景1 第二版'
    && log3[2] && log3[2].subject === 'mt: 场景1 第一版'), log3.slice(0, 3).map((x) => x.subject).join(' | '));
  check('场景1 第一版仍在历史里', log3.some((x) => x.sha === sha1), String(sha1 || '').slice(0, 7));
  const nodes = nodesOf(p);
  check('场景1 服务端结构未受影响', nodes.length === 1 && nodes[0].id === 'n1' && nodes[0].file === f, ids(nodes));
  check('场景1 服务端与磁盘一致', (readNode(p, f).body || '') === diskText(p, f));
});

// ============================================================
// 2. 删节点（不删文件）→ 撤销 → 再删（删文件）→ 撤销 → 文件原样回来
// ============================================================
scenario('场景 2 删除与撤销', () => {
  const p = project('场景2 删除撤销');
  if (!p.id) return;
  const s = seedNodes(p, [
    { id: 'n1', title: '甲', parent: 't1', axis: 'h', body: '甲正文' },
    { id: 'n2', title: '乙', parent: 'n1', axis: 'h', body: '乙正文' },
    { id: 'n3', title: '丙', parent: 'n2', axis: 'v', body: '丙正文' },
  ]);
  const base = commitNow(p, 'mt: 场景2 基线');
  check('场景2 建立基线版本', base.committed === true, base.reason);
  const baseSha = (logOf(p)[0] || {}).sha;
  const baseSig = sig(nodesOf(p));
  const baseFiles = listFiles(p.dir);
  const text = { n1: diskText(p, s.files.n1), n2: diskText(p, s.files.n2), n3: diskText(p, s.files.n3) };

  // ---- 第一段：只删节点，文件留在项目里 ----
  const d1 = delNode(p, 'n2', false);
  check('场景2 删除中间节点连带子孙', d1.removed === 2, d1.removed);
  check('场景2 保留文件时磁盘一个不少', JSON.stringify(listFiles(p.dir)) === JSON.stringify(baseFiles), listFiles(p.dir).join(','));
  check('场景2 保留文件时内容没动', diskText(p, s.files.n2) === text.n2 && diskText(p, s.files.n3) === text.n3, diskText(p, s.files.n2));
  check('场景2 删除后结构只剩甲', ids(nodesOf(p)) === 'n1', ids(nodesOf(p)));
  check('场景2 删除留下可撤销版本', !!(d1.commit && d1.commit.committed === true), JSON.stringify(d1.commit || {}).slice(0, 120));

  const u1 = undo(p);
  check('场景2 第一次撤销成功', u1.reverted === true, u1.error);
  check('场景2 第一次撤销恢复树结构', sig(nodesOf(p)) === baseSig, sig(nodesOf(p)));
  check('场景2 第一次撤销不动文件', diskText(p, s.files.n2) === text.n2 && diskText(p, s.files.n3) === text.n3, diskText(p, s.files.n2));

  // 若撤销没能恢复基线，先把基线补回来，保证第二段测的仍是「删文件 + 撤销」
  const repair = restore(p, baseSha, []);
  saveNodes(p, s.nodes);
  commitNow(p, 'mt: 场景2 重建基线');
  check('场景2 第二段基线就绪', repair.ok === true && sig(nodesOf(p)) === baseSig && diskText(p, s.files.n2) === text.n2, sig(nodesOf(p)));

  // ---- 第二段：连磁盘文件一起删，再撤销 ----
  const d2 = delNode(p, 'n2', true);
  check('场景2 再删中间节点连带子孙', d2.removed === 2, d2.removed);
  check('场景2 连带删掉文件', !exists(p, s.files.n2) && !exists(p, s.files.n3) && exists(p, s.files.n1), listFiles(p.dir).join(','));
  check('场景2 再删结构只剩甲', ids(nodesOf(p)) === 'n1', ids(nodesOf(p)));
  check('场景2 再删留下版本', !!(d2.commit && d2.commit.committed === true), JSON.stringify(d2.commit || {}).slice(0, 120));
  const u2 = undo(p);
  check('场景2 第二次撤销成功', u2.reverted === true, u2.error);
  check('场景2 文件内容原样回来', diskText(p, s.files.n2) === text.n2 && diskText(p, s.files.n3) === text.n3, diskText(p, s.files.n2));
  check('场景2 接口读回原内容', (readNode(p, s.files.n2).body || '') === text.n2, (readNode(p, s.files.n2).body || '').slice(0, 30));
  check('场景2 第二次撤销恢复树结构', sig(nodesOf(p)) === baseSig, sig(nodesOf(p)));
  check('场景2 磁盘清单回到基线', JSON.stringify(listFiles(p.dir)) === JSON.stringify(baseFiles), listFiles(p.dir).join(','));
});

// ============================================================
// 3. 连续快速保存 10 次（每次改一个字符）
// ============================================================
scenario('场景 3 连续快速保存', () => {
  const p = project('场景3 快速保存');
  if (!p.id) return;
  const node = { id: 'n1', title: '连写', parent: 't1', axis: 'h' };
  const f = writeNode(p, node, '起始内容').file;
  saveNodes(p, [{ ...node, file: f }]);
  const c0 = commitNow(p, 'mt: 场景3 起点');
  check('场景3 起点版本', c0.committed === true, c0.reason);
  const baseCount = logOf(p).length;

  const bodies = [];
  let committedAll = true;
  let firstBad = '';
  for (let i = 1; i <= 10; i += 1) {
    const body = '第 ' + i + ' 次保存的正文';
    bodies.push(body);
    writeNode(p, { ...node, file: f }, body);
    const c = commitNow(p, 'mt: 场景3 第 ' + i + ' 次');
    if (c.committed !== true) {
      committedAll = false;
      if (!firstBad) firstBad = '第 ' + i + ' 次：' + JSON.stringify(c).slice(0, 100);
    }
  }
  check('场景3 10 次都建立了版本', committedAll, firstBad);
  const logs = logOf(p);
  check('场景3 版本数正好 +10', logs.length === baseCount + 10, baseCount + '→' + logs.length);
  const expect = [];
  for (let i = 10; i >= 1; i -= 1) expect.push('mt: 场景3 第 ' + i + ' 次');
  check('场景3 历史顺序从新到旧', JSON.stringify(logs.slice(0, 10).map((x) => x.subject)) === JSON.stringify(expect), logs.slice(0, 10).map((x) => x.subject).join(' | '));
  check('场景3 最新内容落盘', diskText(p, f) === bodies[9], diskText(p, f));
  check('场景3 接口与磁盘一致', (readNode(p, f).body || '') === bodies[9]);
  check('场景3 没有多余文件', JSON.stringify(listFiles(p.dir)) === JSON.stringify(['PROJECT.md', '连写.md']), listFiles(p.dir).join(','));

  const fifth = logs.find((x) => x.subject === 'mt: 场景3 第 5 次');
  const r5 = restore(p, (fifth || {}).sha, []);
  check('场景3 回滚到第 5 次内容正确', r5.ok === true && (readNode(p, f).body || '') === bodies[4], (readNode(p, f).body || '').slice(0, 30));
  const r10 = restore(p, (logs[0] || {}).sha, []);
  check('场景3 再回到最新内容正确', r10.ok === true && diskText(p, f) === bodies[9], diskText(p, f));
});

// ============================================================
// 4. 节点改名后再次保存，不产生多余文件
// ============================================================
scenario('场景 4 改名后保存', () => {
  const p = project('场景4 改名保存');
  if (!p.id) return;
  const first = writeNode(p, { id: 'n1', title: '原题目' }, '原始正文');
  const f = first.file;
  saveNodes(p, [{ id: 'n1', title: '原题目', parent: 't1', axis: 'h', file: f }]);
  commitNow(p, 'mt: 场景4 原始');
  const files0 = listFiles(p.dir);
  check('场景4 初始正文文件唯一', files0.filter((x) => x.endsWith('.md') && x !== 'PROJECT.md').length === 1, files0.join(','));

  const w2 = writeNode(p, { id: 'n1', title: '改过的题目', file: f }, '改名后的正文');
  check('场景4 改名保存沿用原文件', w2.file === f, (w2.file || '') + ' / ' + f);
  saveNodes(p, [{ id: 'n1', title: '改过的题目', parent: 't1', axis: 'h', file: w2.file }]);
  const c1 = commitNow(p, 'mt: 场景4 改名后保存');
  check('场景4 改名后保存成功', c1.committed === true, c1.reason);
  check('场景4 没有产生多余文件', JSON.stringify(listFiles(p.dir)) === JSON.stringify(files0), listFiles(p.dir).join(','));
  check('场景4 磁盘内容更新', diskText(p, f) === '改名后的正文', diskText(p, f));
  const n1 = nodesOf(p);
  check('场景4 服务端标题已改', n1.length === 1 && n1[0].title === '改过的题目', n1.length === 1 ? n1[0].title : ids(n1));
  check('场景4 服务端仍指向原文件', n1.length === 1 && n1[0].file === f, n1.length === 1 ? n1[0].file : '');
  check('场景4 没有新名字的文件', !exists(p, '改过的题目.md') && !exists(p, '原题目-2.md'));

  const w3 = writeNode(p, { id: 'n1', title: '再改一次', file: f }, '第二次改名后的正文');
  saveNodes(p, [{ id: 'n1', title: '再改一次', parent: 't1', axis: 'h', file: w3.file }]);
  const c2 = commitNow(p, 'mt: 场景4 第二次改名');
  check('场景4 第二次改名保存', c2.committed === true && w3.file === f, c2.reason || w3.file);
  check('场景4 两次改名后文件仍然唯一', JSON.stringify(listFiles(p.dir)) === JSON.stringify(files0), listFiles(p.dir).join(','));
  check('场景4 服务端与磁盘一致', (readNode(p, f).body || '') === diskText(p, f));
});

// ============================================================
// 5. 删除中间节点（连带子孙）→ 保存 → 回滚到删除前
// ============================================================
scenario('场景 5 删除中间节点后回滚', () => {
  const p = project('场景5 删除回滚');
  if (!p.id) return;
  const s = seedNodes(p, [
    { id: 'n1', title: '开头', parent: 't1', axis: 'h', body: '开头正文' },
    { id: 'n2', title: '中间', parent: 'n1', axis: 'h', body: '中间正文' },
    { id: 'n3', title: '结尾', parent: 'n2', axis: 'v', body: '结尾正文' },
  ]);
  const c = commitNow(p, 'mt: 场景5 删除前');
  check('场景5 建立删除前版本', c.committed === true, c.reason);
  const preSha = (logOf(p)[0] || {}).sha;
  const preSig = sig(nodesOf(p));
  const preFiles = listFiles(p.dir);
  const text = { n1: diskText(p, s.files.n1), n2: diskText(p, s.files.n2), n3: diskText(p, s.files.n3) };

  const d = delNode(p, 'n2', true);
  check('场景5 删除中间节点连带子孙', d.removed === 2, d.removed);
  check('场景5 删除后文件消失', !exists(p, s.files.n2) && !exists(p, s.files.n3), listFiles(p.dir).join(','));
  check('场景5 删除后结构只剩开头', ids(nodesOf(p)) === 'n1', ids(nodesOf(p)));

  const save = commitNow(p, 'mt: 场景5 删除后保存');
  const st = status(p);
  check('场景5 保存后工作区干净', st.dirty === false, (st.changed || []).join(',') || save.reason || '');

  const r = restore(p, preSha, []);
  check('场景5 回滚到删除前', r.ok === true, r.error);
  check('场景5 树结构恢复', sig(nodesOf(p)) === preSig, sig(nodesOf(p)));
  check('场景5 文件全部回来', JSON.stringify(listFiles(p.dir)) === JSON.stringify(preFiles), listFiles(p.dir).join(','));
  check('场景5 文件内容原样', diskText(p, s.files.n2) === text.n2 && diskText(p, s.files.n3) === text.n3, diskText(p, s.files.n2));
  check('场景5 接口读回原内容', (readNode(p, s.files.n2).body || '') === text.n2);

  const c2 = commitNow(p, 'mt: 场景5 回滚后保存');
  check('场景5 回滚后可以再保存', c2.committed === true, c2.reason);
  check('场景5 服务端与磁盘一致', sig(nodesOf(p)) === preSig && diskText(p, s.files.n2) === text.n2 && diskText(p, s.files.n3) === text.n3, sig(nodesOf(p)));
});

// ============================================================
// 6. 同一节点反复写入相同内容，不产生新版本
// ============================================================
scenario('场景 6 相同内容不产生版本', () => {
  const p = project('场景6 相同内容');
  if (!p.id) return;
  const node = { id: 'n1', title: '常量', parent: 't1', axis: 'h' };
  const f = writeNode(p, node, '一字不差的内容').file;
  saveNodes(p, [{ ...node, file: f }]);
  const c0 = commitNow(p, 'mt: 场景6 基线');
  check('场景6 建立基线版本', c0.committed === true, c0.reason);
  const log0 = logOf(p);
  const head0 = (head(p) || {}).head;
  const files0 = listFiles(p.dir);

  let noopAll = true;
  let reason = '';
  for (let i = 1; i <= 3; i += 1) {
    writeNode(p, { ...node, file: f }, '一字不差的内容');
    const c = commitNow(p, 'mt: 场景6 重复写 ' + i);
    if (c.committed !== false) noopAll = false;
    reason = String(c.reason || '');
  }
  check('场景6 反复写相同内容不建版本', noopAll, reason);
  check('场景6 回报没有改动', reason.includes('没有改动'), reason);
  const log1 = logOf(p);
  check('场景6 版本数没变', log1.length === log0.length, log0.length + '→' + log1.length);
  check('场景6 HEAD 没变', (head(p) || {}).head === head0, String((head(p) || {}).head || '').slice(0, 7));
  check('场景6 内容没变', diskText(p, f) === '一字不差的内容', diskText(p, f));
  check('场景6 文件清单没变', JSON.stringify(listFiles(p.dir)) === JSON.stringify(files0), listFiles(p.dir).join(','));
  check('场景6 服务端与磁盘一致', (readNode(p, f).body || '') === diskText(p, f));
});

check('服务仍在线', alive());

const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.name + (x.extra ? ' — ' + x.extra : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
