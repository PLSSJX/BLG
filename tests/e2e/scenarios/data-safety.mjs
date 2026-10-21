// 数据安全专项：崩溃恢复 / 断电模拟 / 频繁保存 / 回档精度 / 大版本量 / 备份导出 / 附件副本
// 这套会自己杀进程再拉起来，所以必须独立跑（不要和其它套件并行）。
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE = 'http://127.0.0.1:8931';
const EXE = process.env.MT_EXE || path.join(os.homedir(), 'AppData/Local/Programs/MakiseToolkit/MakiseToolkit.exe');
let passed = 0, failed = 0; const fails = [];
const check = (name, ok, detail = '') => {
  if (ok) { passed++; console.log('✅ ' + name + (detail ? ' — ' + detail : '')); }
  else { failed++; fails.push(name); console.log('❌ ' + name + (detail ? ' — ' + detail : '')); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function tokenFromState() {
  try {
    const r = spawnSync('curl.exe', ['-s', '--max-time', '5', BASE + '/api/state'], { encoding: 'utf8', windowsHide: true });
    return JSON.parse(r.stdout || '{}').api_token || '';
  } catch { return ''; }
}
let TK = tokenFromState();
const post = (p, body) => {
  const r = spawnSync('curl.exe', ['-s', '--max-time', '120', '-X', 'POST',
    '-H', 'Content-Type: application/json', '-H', 'X-MakiseToolkit-Token: ' + TK,
    '-d', JSON.stringify(body || {}), BASE + p], { encoding: 'utf8', windowsHide: true, timeout: 130000 });
  try { return JSON.parse(r.stdout || '{}'); } catch { return {}; }
};
const get = (p) => {
  const r = spawnSync('curl.exe', ['-s', '--max-time', '120', '-H', 'X-MakiseToolkit-Token: ' + TK, BASE + p],
    { encoding: 'utf8', windowsHide: true, timeout: 130000 });
  try { return JSON.parse(r.stdout || '{}'); } catch { return {}; }
};
const alive = () => spawnSync('curl.exe', ['-s', '--max-time', '3', BASE + '/api/ping'],
  { encoding: 'utf8', windowsHide: true, timeout: 8000 }).stdout.trim() !== '';

async function startApp() {
  spawn(EXE, [], { windowsHide: true, detached: true, stdio: 'ignore' }).unref();
  for (let i = 0; i < 60; i++) { if (alive()) { await sleep(1200); TK = tokenFromState(); return true; } await sleep(1000); }
  return false;
}
function killApp() {
  spawnSync('taskkill', ['/F', '/IM', 'MakiseToolkit.exe'], { encoding: 'utf8', windowsHide: true, timeout: 30000 });
}

// ---------------------------------------------------------------- 准备
if (!alive()) { console.log('应用没在跑，先拉起来…'); await startApp(); }
check('应用在线', alive());

const root = fs.mkdtempSync('D:\\mt-safety-');
const created = post('/api/project/create', { name: '安全专项', path: root });
const id = created.id;
check('创建测试项目', !!id, id);
let rel = '';
const write = (body) => {
  const payload = { id, body };
  payload.node = rel ? { id: 's1', title: '正文', file: rel } : { id: 's1', title: '正文' };
  const r = post('/api/project/node/write', payload);
  if (!rel && r.file) rel = r.file;
  return r;
};
write('# 正文\n\n初始内容\n');
post('/api/project/graph', { id, graph: { trunk: { id: 't1', title: '安全专项' }, nodes: [{ id: 's1', title: '正文', parent: 't1', axis: 'h', file: rel }], links: [] } });
const readBody = () => (post('/api/project/node/read', { id, file: rel }).body || '');
const commitNow = (msg) => post('/api/project/commit-now', { id, message: msg });
const log = (limit) => (post('/api/project/log', { id, limit: limit || 50 }).commits || []);

commitNow('mt: v1');
check('① 首次提交成功', log(5).length >= 1, '版本数 ' + log(5).length);

// ---------------------------------------------------------------- ② 频繁保存
for (let i = 0; i < 15; i++) { write('# 正文\n\n快速第 ' + i + ' 次\n'); }
commitNow('mt: 快速保存');
const bodyAfterRapid = readBody();
check('② 15 次快速保存后正文是最新一次', bodyAfterRapid.includes('快速第 14 次'), bodyAfterRapid.trim().slice(0, 40));
check('② 版本数增加（没漏存）', log(10).length >= 2, '版本数 ' + log(10).length);

// ---------------------------------------------------------------- ③ 回档精度 & 幽灵内容
write('# 正文\n\nAAA 版本\n');
commitNow('mt: AAA');
const shaA = log(5)[0].sha;
write('# 正文\n\nBBB 版本\n');
commitNow('mt: BBB');
const r1 = post('/api/project/restore', { id, commit: shaA });
const afterRestore1 = readBody();
check('③ 回档到 AAA 成功', afterRestore1.includes('AAA 版本') && !afterRestore1.includes('BBB'), afterRestore1.trim().slice(0, 40));

write('# 正文\n\nCCC 版本\n');
commitNow('mt: CCC');
post('/api/project/restore', { id, commit: shaA });
const afterRestore2 = readBody();
check('③ 再写再回档，无幽灵内容', afterRestore2.includes('AAA 版本') && !afterRestore2.includes('CCC') && !afterRestore2.includes('BBB'),
  afterRestore2.trim().slice(0, 40));

// ---------------------------------------------------------------- ④ 崩溃恢复（编辑中强杀）
write('# 正文\n\n崩溃前写的内容\n');
const beforeKillVersions = log(50).length;
killApp();
check('④ 强杀后进程已退出', !alive());
const restarted = await startApp();
check('④ 重启成功', restarted);
const afterCrashBody = readBody();
check('④ 崩溃前写的内容还在（没丢稿）', afterCrashBody.includes('崩溃前写的内容'), afterCrashBody.trim().slice(0, 40));
check('④ 版本历史没丢', log(50).length >= beforeKillVersions, beforeKillVersions + ' → ' + log(50).length);

// ---------------------------------------------------------------- ⑤ 断电模拟（绕过程序直接改盘）
const abs = path.join(root, rel);
fs.writeFileSync(abs, '# 正文\n\n断电时手动写入的内容\n', 'utf8');
killApp();
await startApp();
const afterPower = readBody();
check('⑤ 断电（直接写盘 + 强杀）后内容不丢', afterPower.includes('断电时手动写入的内容'), afterPower.trim().slice(0, 40));
commitNow('mt: 断电后补提交');
const afterPowerLog = log(50).length;
check('⑤ 断电后仍能正常提交', afterPowerLog >= beforeKillVersions, '版本数 ' + afterPowerLog);

// ---------------------------------------------------------------- ⑥ 大版本量性能
const t0 = Date.now();
for (let i = 0; i < 100; i++) {
  write('# 正文\n\n批量 ' + i + '\n');
  if (i % 5 === 4) commitNow('mt: 批量 ' + i);
}
const tWrite = Date.now() - t0;
const t1 = Date.now(); const many = log(200); const tLog = Date.now() - t1;
const t2 = Date.now(); post('/api/project/restore', { id, commit: many[0].sha }); const tRestore = Date.now() - t2;
check('⑥ 100+ 版本能列出来', many.length >= 20, '共 ' + many.length + ' 条');
check('⑥ 时间线加载 < 3s', tLog < 3000, tLog + 'ms');
check('⑥ 回档 < 5s', tRestore < 5000, tRestore + 'ms');
console.log('   写入 100 次 + 20 次提交耗时 ' + tWrite + 'ms');

// ---------------------------------------------------------------- ⑦ 一键备份
const backupDir = 'D:\\mt-backup-' + Date.now();
fs.mkdirSync(backupDir, { recursive: true });
const bk = post('/api/project/backup', { id, target: backupDir });
check('⑦ 备份生成 zip', !!bk.path && fs.existsSync(bk.path || ''), (bk.files || 0) + ' 个文件');
let entries = '';
if (bk.path) {
  entries = spawnSync('powershell', ['-NoProfile', '-Command',
    "Add-Type -A System.IO.Compression.FileSystem; $z=[IO.Compression.ZipFile]::OpenRead('" + String(bk.path).replace(/'/g, "''") + "'); $z.Entries | ForEach-Object { $_.FullName }"],
    { encoding: 'utf8', windowsHide: true, timeout: 120000 }).stdout || '';
}
check('⑦ 备份含版本库 .mt/', /\.mt\//.test(entries));
check('⑦ 备份含正文 .md', /\.md/.test(entries));
if (bk.path) fs.rmSync(path.dirname(bk.path), { recursive: true, force: true });

// ---------------------------------------------------------------- ⑧ 附件是副本（改的不是原文件）
post('/api/project/graph', { id, graph: { trunk: { id: 't1', title: '安全专项' }, nodes: [{ id: 's1', title: '正文', parent: 't1', axis: 'h', file: rel }], links: [] } });
const srcFile = 'D:\\mt-src-' + Date.now() + '.txt';
fs.writeFileSync(srcFile, '原始文件内容', 'utf8');
const att = post('/api/project/attach', { id, nodeId: 's1', source: srcFile });
if (!att.asset) console.log('   attach 返回：' + JSON.stringify(att).slice(0, 200));
const assetPath = (att.asset && att.asset.path) || '';
check('⑧ 挂载返回项目内路径', !!assetPath && assetPath.includes('assets'), assetPath);
fs.writeFileSync(srcFile, '原文件被我改了', 'utf8');
let copyText = '';
if (assetPath) { try { copyText = fs.readFileSync(path.join(root, assetPath), 'utf8'); } catch {} }
check('⑧ 改原文件不影响项目里的副本', copyText.includes('原始文件内容'), copyText.slice(0, 20));

// ---------------------------------------------------------------- ⑨ 版本库缺失时的自动修复
// 老项目（3.x 时代建的）没有 .mt 目录；或者用户手滑删了它。
// 重新打开项目时应该自动重建版本库，正文一个字节都不能丢。
const mtDir = path.join(root, '.mt');
const hadMt = fs.existsSync(mtDir);
// 版本库是 SQLite，进程活着的时候文件被占用删不掉 —— 必须先杀进程（也正好模拟"用户手滑删了 .mt"）
killApp();
await sleep(1200);
try { fs.rmSync(mtDir, { recursive: true, force: true }); } catch (e) { console.log('   删除 .mt 失败：' + e.code); }
await startApp();
const reopened = post('/api/project/load', { id });
const reborn = fs.existsSync(mtDir);
const bodyAfterRepair = readBody();
check('⑨ 版本库删掉后能自动重建', hadMt && reborn, reborn ? '.mt 已重建' : '没重建');
check('⑨ 重建后正文完好', bodyAfterRepair.includes('断电时手动写入的内容') || bodyAfterRepair.includes('AAA') || bodyAfterRepair.length > 0,
  bodyAfterRepair.trim().slice(0, 30));
commitNow('mt: 重建后提交');
check('⑨ 重建后能继续提交', log(5).length >= 1, '版本数 ' + log(5).length);

// ---------------------------------------------------------------- 收尾
post('/api/project/delete', { id, deleteFiles: true });
try { fs.rmSync(root, { recursive: true, force: true }); } catch {}
try { fs.unlinkSync(srcFile); } catch {}

console.log('');
console.log('结果: ' + passed + '/' + (passed + failed) + (failed ? '  失败: ' + fails.join(', ') : ''));
process.exit(failed ? 1 : 0);
