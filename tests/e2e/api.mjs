
import { spawnSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import crypto from 'node:crypto';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (name, cond, extra) => { results.push({ name, ok: !!cond, extra: extra === undefined ? '' : String(extra) }); };

const curl = (args) => spawnSync('curl.exe', args, { encoding: 'utf8', windowsHide: true, timeout: 120000 }).stdout || '';
const getRaw = (p) => curl(['-s', '--ssl-no-revoke', '-m', '20', BASE + p]);
const token = () => { try { return JSON.parse(getRaw('/api/state')).api_token || ''; } catch { return ''; } };
const TK = token();
const post = (p, body) => {
  const raw = curl(['-s', '--ssl-no-revoke', '-m', '90', '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(body || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { raw, _bad: true }; }
};
const alive = () => { try { return !!JSON.parse(getRaw('/api/ping')).ok; } catch { return false; } };

// ---------- 1. 基础 ----------
check('服务在线', alive());
check('有 token', TK.length > 0);

const nonEmpty = 'D:\\_e2e_ne_' + Date.now();
try { fs.rmSync(nonEmpty, { recursive: true, force: true }); } catch {}
fs.mkdirSync(nonEmpty, { recursive: true });
fs.writeFileSync(nonEmpty + '\\x.md', 'x', 'utf8');
const rej = post('/api/project/create', { name: '非空', path: nonEmpty });
check('拒绝非空文件夹', rej.ok === false && String(rej.error || '').includes('不是空的'), rej.error);

const proj = 'D:\\_e2e_proj_' + Date.now();
try { fs.rmSync(proj, { recursive: true, force: true }); } catch {}
fs.mkdirSync(proj, { recursive: true });
const c = post('/api/project/create', { name: '端到端', path: proj });
check('创建项目', c.ok === true, c.id);
const id = c.id;
check('创建后已有首个版本', (post('/api/project/log', { id }).commits || []).length >= 1);

// ---------- 2. 附件 ----------
const docxSrc = 'C:\\Users\\25486\\Desktop\\dwsy\\示波器实验报告.docx';
const videoSrc = 'C:\\Users\\25486\\Desktop\\valclip\\VALORANT 08-30-2026 21-24-27-262.mp4';
const a1 = post('/api/project/attach', { id, nodeId: 't1', source: docxSrc });
check('挂 docx', a1.ok === true, (a1.asset || {}).name);
const mirror = proj + '\\' + String((a1.asset || {}).path || '').replace(/\//g, '\\');
const h = (f) => crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
check('docx 镜像内容一致', fs.existsSync(mirror) && h(mirror) === h(docxSrc));
const a2 = post('/api/project/attach', { id, nodeId: 't1', source: videoSrc });
check('挂 21MB 视频', a2.ok === true, (a2.asset || {}).size);
check('大文件后服务仍在', alive());

// ---------- 3. 节点读写 ----------
const w = post('/api/project/node/write', { id, node: { id: 'n1', title: '方法' }, body: '# 方法\n\n两级时钟门控。' });
check('写节点文件', !!w.file, w.file);
const r = post('/api/project/node/read', { id, file: w.file });
check('读回正文', (r.body || '').includes('两级时钟门控'));

// ---------- 4. 结构 ----------
const g = post('/api/project/load', { id }).graph;
g.nodes = [
  { id: 'n1', title: '引言', parent: 't1', axis: 'h', file: w.file },
  { id: 'n2', title: '方法', parent: 'n1', axis: 'h', file: w.file },
  { id: 'n3', title: '实验', parent: 'n2', axis: 'v', file: w.file },
];
post('/api/project/graph', { id, graph: g });
check('保存结构', (post('/api/project/load', { id }).graph.nodes || []).length === 3);

// ---------- 5. 版本 ----------
const v1 = post('/api/project/commit-now', { id, message: 'mt: 第一版' });
check('保存版本', v1.committed === true, (v1.files || []).length + ' 文件');
fs.writeFileSync(proj + '\\后来加的.md', '后来', 'utf8');
post('/api/project/commit-now', { id, message: 'mt: 第二版' });
const logs = post('/api/project/log', { id }).commits || [];
check('版本列表 >=2', logs.length >= 2, logs.length);
const back = post('/api/project/restore', { id, sha: logs[logs.length - 1].sha, files: [] });
check('整份回滚', back.ok === true);
check('回滚清理多余文件', !fs.existsSync(proj + '\\后来加的.md'));

// ---------- 6. 自动保存 ----------
fs.writeFileSync(proj + '\\自动.md', '# 自动\n\n' + Date.now(), 'utf8');
await new Promise((r2) => setTimeout(r2, 38000));
const autoLogs = post('/api/project/log', { id }).commits || [];
check('自动保存触发', autoLogs.some((x) => String(x.subject).startsWith('mt: 自动保存')));

// ---------- 7. 导出 ----------
const eg = post('/api/project/export-git', { id });
check('导出 git', eg.ok === true && eg.exported > 0, eg.exported + ' 个提交');

// ---------- 8. 分支 ----------
const br = post('/api/project/branches', { id });
check('分支含 main', (br.branches || []).includes('main'), (br.branches || []).join(','));

// ---------- 9. 占用检测 ----------
const lockFile = proj + '\\锁定.md';
fs.writeFileSync(lockFile, '锁', 'utf8');
const fh = fs.openSync(lockFile, 'r+');
let lockMsg = '';
try { const rr = post('/api/project/commit-now', { id, message: 'mt: 锁定' }); lockMsg = JSON.stringify(rr); }
catch (e) { lockMsg = String(e); }
finally { fs.closeSync(fh); }
check('占用时拒绝保存并说明', lockMsg.includes('还开着') || lockMsg.includes('locked') || lockMsg.includes('false'), lockMsg.slice(0, 90));

// ---------- 10. 删除 + 撤销 ----------
const del = post('/api/project/node/delete', { id, nodeId: 'n2', delete_files: false });
check('删中间节点连带子孙', del.removed === 2, '删了 ' + del.removed);
check('撤销', post('/api/project/undo', { id }).reverted === true);
check('撤销后服务仍在', alive());

const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.name + (x.extra ? ' — ' + x.extra : ''));
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
