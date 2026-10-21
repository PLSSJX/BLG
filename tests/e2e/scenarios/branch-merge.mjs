// 分支 / 合并 / 回滚 / 导出 的端到端场景测试
//
// 前提：Makise Toolkit 正在 127.0.0.1:8931 上运行；本脚本不启动也不停止它。
// 用法：node tests/e2e/scenarios/branch-merge.mjs
//
// 产品背景：版本历史是 SQLite 快照库（项目里的 .mt/history.db），不是 git。
//   POST /api/project/branches              列分支
//   POST /api/project/checkout {branch}     切分支，不存在就新建
//   POST /api/project/merge {branch}        三方合并 → {merged, conflicts:[{path,reason}], sha}
//   POST /api/project/restore {sha,files}   回滚 → {ok, restored, skipped}（skipped = 当年只记指纹的大文件）
//   POST /api/project/gc                    清理无引用内容块 → {removed_blocks, before, after}
//   POST /api/project/export-git            导出成真 git 仓库
//
// 每个项目一个独立临时目录 D:\_sc_b_<时间戳>_<序号>_<场景>。
// 跑完不删目录，出问题时可以直接翻文件对照。

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';

const PORT = 8931;
const BASE = 'http://127.0.0.1:' + PORT;
const results = [];
const check = (name, cond, extra) => { results.push({ name, ok: !!cond, extra: extra === undefined ? '' : String(extra) }); };

const curl = (args) => spawnSync('curl.exe', args, { encoding: 'utf8', windowsHide: true, timeout: 120000 }).stdout || '';
const getRaw = (p) => curl(['-s', '--ssl-no-revoke', '-m', '20', BASE + p]);
const token = () => { try { return JSON.parse(getRaw('/api/state')).api_token || ''; } catch { return ''; } };
const TK = token();
const post = (p, body) => {
  const raw = curl(['-s', '--ssl-no-revoke', '-m', '120', '-X', 'POST', '-H', 'Content-Type: application/json',
    '-H', 'X-MakiseToolkit-Token: ' + TK, '-d', JSON.stringify(body || {}), BASE + p]);
  try { return JSON.parse(raw); } catch { return { raw, _bad: true }; }
};
const alive = () => { try { return !!JSON.parse(getRaw('/api/ping')).ok; } catch { return false; } };

// ---------- 小工具 ----------
const STAMP = Date.now();
const pdir = (n, tag) => 'D:\\_sc_b_' + STAMP + '_' + n + '_' + tag;
const outDir = 'D:\\_sc_b_' + STAMP + '_out_git';
const busyDir = 'D:\\_sc_b_' + STAMP + '_out_busy';

const freshDir = (d) => { try { fs.rmSync(d, { recursive: true, force: true }); } catch {} fs.mkdirSync(d, { recursive: true }); return d; };
const at = (d, name) => d + '\\' + name;
const readText = (f) => { try { return fs.readFileSync(f, 'utf8'); } catch { return ''; } };
const hasFile = (f) => fs.existsSync(f);
const writeText = (f, s) => fs.writeFileSync(f, s, 'utf8');
const j = (v) => JSON.stringify(v);

const newProject = (name, dir) => { freshDir(dir); return post('/api/project/create', { name: name, path: dir }); };
const commitNow = (id, message) => post('/api/project/commit-now', { id: id, message: message });
const logOf = (id) => post('/api/project/log', { id: id }).commits || [];
const branchesOf = (id) => post('/api/project/branches', { id: id });
const headSha = (id) => ((logOf(id)[0] || {}).sha) || '';
// 接口读的字段叫 commit，任务描述里叫 sha —— 两个都发，哪种实现都不会读错
const restore = (id, sha, files) => post('/api/project/restore', { id: id, commit: sha, sha: sha, files: files || [] });
// 工作区快照（跳过 .mt），用来确认 gc 没碰用户文件
const treeOf = (dir) => {
  const out = {};
  const walk = (base, rel) => {
    for (const ent of fs.readdirSync(base, { withFileTypes: true })) {
      const r = rel ? rel + '/' + ent.name : ent.name;
      if (r === '.mt' || r.indexOf('.mt/') === 0) continue;
      const full = base + '\\' + ent.name;
      if (ent.isDirectory()) walk(full, r);
      else out[r] = fs.readFileSync(full, 'utf8');
    }
  };
  walk(dir, '');
  return out;
};

check('服务在线', alive());
check('有 token', TK.length > 0);

let fatal = '';
try {
  // ================= 1. 分支上改 A、主线改 B → 合并后两个都在 =================
  const d1 = pdir(1, 'merge');
  const c1 = newProject('分支合并场景', d1);
  check('创建项目', c1.ok === true, c1.id);
  const id1 = c1.id;
  check('历史是 .mt/history.db，不是 .git', hasFile(at(d1, '.mt\\history.db')) && !hasFile(at(d1, '.git')));

  const A_BASE = '# A\n\n初始 A\n';
  const A_FEAT = '# A\n\n分支改的 A\n';
  const B_BASE = '# B\n\n初始 B\n';
  const B_MAIN = '# B\n\n主线改的 B\n';
  writeText(at(d1, 'A.md'), A_BASE);
  writeText(at(d1, 'B.md'), B_BASE);
  check('保存基线版本', commitNow(id1, 'mt: 基线').committed === true);

  const co1 = post('/api/project/checkout', { id: id1, branch: 'feat' });
  check('新建并切到 feat 分支', co1.current === 'feat', co1.error || co1.current);
  writeText(at(d1, 'A.md'), A_FEAT);
  check('分支上保存 A 的改动', commitNow(id1, 'mt: 分支改 A').committed === true);

  const back1 = post('/api/project/checkout', { id: id1, branch: 'main' });
  check('切回 main', back1.current === 'main', back1.error || back1.current);
  check('切回后 A.md 回到基线内容', readText(at(d1, 'A.md')) === A_BASE, j(readText(at(d1, 'A.md'))));

  writeText(at(d1, 'B.md'), B_MAIN);
  check('主线保存 B 的改动', commitNow(id1, 'mt: 主线改 B').committed === true);

  const preMerge = headSha(id1);
  const mg1 = post('/api/project/merge', { id: id1, branch: 'feat' });
  check('合并成功', mg1.merged === true, mg1.message || mg1.error);
  check('合并没有冲突', Array.isArray(mg1.conflicts) && mg1.conflicts.length === 0, j(mg1.conflicts || []));
  check('合并后 A.md 是分支内容', readText(at(d1, 'A.md')) === A_FEAT, j(readText(at(d1, 'A.md'))));
  check('合并后 B.md 是主线内容', readText(at(d1, 'B.md')) === B_MAIN, j(readText(at(d1, 'B.md'))));
  check('合并返回了 sha', typeof mg1.sha === 'string' && mg1.sha.length >= 7, mg1.sha);
  const br1 = branchesOf(id1);
  check('合并后还留在 main，feat 也没被吃掉', br1.current === 'main' && (br1.branches || []).includes('feat'), (br1.branches || []).join(','));

  // ================= 4. 合并版本进历史；回滚到合并前 =================
  const log1 = logOf(id1);
  const mergeVersion = log1.find((x) => String(x.subject || '').includes('合并'));
  check('版本历史里能看到合并版本', !!mergeVersion, mergeVersion ? mergeVersion.subject : '没有合并版本');
  check('合并版本就是当前版本', !!mergeVersion && mergeVersion.sha === mg1.sha, mergeVersion ? mergeVersion.sha : '');
  check('合并版本记录了 A.md 的改动', !!mergeVersion && (mergeVersion.files || []).includes('A.md'), mergeVersion ? (mergeVersion.files || []).join(',') : '');

  const rb1 = restore(id1, preMerge, []);
  check('整份回滚到合并前', rb1.ok === true, rb1.error || '');
  check('回滚恢复了 A.md 与 B.md', Array.isArray(rb1.restored) && rb1.restored.includes('A.md') && rb1.restored.includes('B.md'), (rb1.restored || []).join(','));
  check('回滚没有跳过文件', Array.isArray(rb1.skipped) && rb1.skipped.length === 0, j(rb1.skipped || []));
  check('回滚后 A.md 回到基线内容', readText(at(d1, 'A.md')) === A_BASE, j(readText(at(d1, 'A.md'))));
  check('回滚后 B.md 是合并前的主线内容', readText(at(d1, 'B.md')) === B_MAIN, j(readText(at(d1, 'B.md'))));
  check('回滚不删版本历史', logOf(id1).some((x) => x.sha === mg1.sha), mg1.sha);

  const pr1 = restore(id1, mg1.sha, ['A.md']);
  check('只回滚 A.md', pr1.ok === true && j(pr1.restored) === j(['A.md']), j(pr1.restored || []));
  check('A.md 回到合并后的分支内容', readText(at(d1, 'A.md')) === A_FEAT, j(readText(at(d1, 'A.md'))));
  check('B.md 不受单文件回滚影响', readText(at(d1, 'B.md')) === B_MAIN, j(readText(at(d1, 'B.md'))));

  // ================= 2. 两边改同一个文件的不同位置 → 合并成功且两边内容都在 =================
  const d2 = pdir(2, 'lines');
  const c2 = newProject('不同位置合并', d2);
  check('创建项目（不同位置合并）', c2.ok === true, c2.id);
  const id2 = c2.id;
  const SHARED = '# 共享文件\n\nline1\nline2\nline3\nline4\nline5\n';
  writeText(at(d2, 'shared.md'), SHARED);
  commitNow(id2, 'mt: 共享基线');
  post('/api/project/checkout', { id: id2, branch: 'feat' });
  writeText(at(d2, 'shared.md'), SHARED.replace('line1\n', 'line1 分支改\n'));
  check('分支改同一文件的第一行', commitNow(id2, 'mt: 分支改第一行').committed === true);
  post('/api/project/checkout', { id: id2, branch: 'main' });
  writeText(at(d2, 'shared.md'), SHARED.replace('line5\n', 'line5 主线改\n'));
  check('主线改同一文件的最后一行', commitNow(id2, 'mt: 主线改最后一行').committed === true);

  const mg2 = post('/api/project/merge', { id: id2, branch: 'feat' });
  check('同一文件不同位置：合并成功', mg2.merged === true, mg2.message || mg2.error);
  check('同一文件不同位置：没有冲突', Array.isArray(mg2.conflicts) && mg2.conflicts.length === 0, j(mg2.conflicts || []));
  const mergedShared = readText(at(d2, 'shared.md'));
  check('合并后两边的改动都在', mergedShared.includes('line1 分支改') && mergedShared.includes('line5 主线改'), j(mergedShared));
  check('合并后中间几行没被动过', mergedShared.includes('line2\nline3\nline4'), j(mergedShared));
  check('合并结果是逐行拼起来的', mergedShared === '# 共享文件\n\nline1 分支改\nline2\nline3\nline4\nline5 主线改\n', j(mergedShared));

  // ================= 3. 两边改同一行 → 报冲突，保留当前分支内容 =================
  const d3 = pdir(3, 'conflict');
  const c3 = newProject('同一行冲突', d3);
  check('创建项目（同一行冲突）', c3.ok === true, c3.id);
  const id3 = c3.id;
  const CONFLICT_MAIN = '# 冲突\n\nalpha 主线版\nbeta\ngamma\n';
  const CONFLICT_FEAT = '# 冲突\n\nalpha 分支版\nbeta\ngamma\n';
  writeText(at(d3, 'conflict.md'), '# 冲突\n\nalpha\nbeta\ngamma\n');
  commitNow(id3, 'mt: 冲突基线');
  post('/api/project/checkout', { id: id3, branch: 'feat' });
  writeText(at(d3, 'conflict.md'), CONFLICT_FEAT);
  check('分支改同一行', commitNow(id3, 'mt: 分支改同一行').committed === true);
  post('/api/project/checkout', { id: id3, branch: 'main' });
  writeText(at(d3, 'conflict.md'), CONFLICT_MAIN);
  check('主线改同一行', commitNow(id3, 'mt: 主线改同一行').committed === true);

  const mg3 = post('/api/project/merge', { id: id3, branch: 'feat' });
  const conflicts3 = Array.isArray(mg3.conflicts) ? mg3.conflicts : [];
  check('两边改同一行：报出冲突', conflicts3.length === 1, j(conflicts3));
  check('冲突里带文件名', !!(conflicts3[0] && conflicts3[0].path === 'conflict.md'), j(conflicts3[0] || null));
  check('冲突里带原因', !!(conflicts3[0] && String(conflicts3[0].reason || '').length), j((conflicts3[0] || {}).reason || ''));
  const kept3 = readText(at(d3, 'conflict.md'));
  check('冲突时文件保留当前分支内容', kept3 === CONFLICT_MAIN, j(kept3));
  check('冲突时没有丢数据', kept3.includes('beta') && kept3.includes('gamma'), j(kept3));
  check('冲突时没把对方内容混进来', !kept3.includes('alpha 分支版'), j(kept3));
  check('冲突时没有写进冲突标记', !kept3.includes('<<<<<<<') && !kept3.includes('>>>>>>>'), j(kept3));

  // ================= 5. 切换分支 / 删除分支 =================
  const d5 = pdir(5, 'switch');
  const c5 = newProject('分支切换与删除', d5);
  check('创建项目（分支切换）', c5.ok === true, c5.id);
  const id5 = c5.id;
  writeText(at(d5, 'main.md'), '# main\n\n主线内容\n');
  commitNow(id5, 'mt: 主线基线');

  const co5a = post('/api/project/checkout', { id: id5, branch: 'feat' });
  check('新建 feat 分支', co5a.current === 'feat', co5a.error || co5a.current);
  const br5a = branchesOf(id5);
  check('分支列表里有 feat', (br5a.branches || []).includes('feat'), (br5a.branches || []).join(','));
  writeText(at(d5, 'feat-only.md'), '# feat\n\n分支独有文件\n');
  check('分支上新增文件并保存', commitNow(id5, 'mt: 分支加文件').committed === true);

  const co5m = post('/api/project/checkout', { id: id5, branch: 'main' });
  check('切回 main', co5m.current === 'main', co5m.error || co5m.current);
  check('切回后分支独有文件消失', !hasFile(at(d5, 'feat-only.md')));
  check('切回后主线文件内容正确', readText(at(d5, 'main.md')) === '# main\n\n主线内容\n', j(readText(at(d5, 'main.md'))));

  const co5f = post('/api/project/checkout', { id: id5, branch: 'feat' });
  check('再切到 feat 文件跟着回来', co5f.current === 'feat' && hasFile(at(d5, 'feat-only.md')), co5f.current);
  check('回来的文件内容正确', readText(at(d5, 'feat-only.md')) === '# feat\n\n分支独有文件\n', j(readText(at(d5, 'feat-only.md'))));

  const delCurrent = post('/api/project/branch/delete', { id: id5, branch: 'feat' });
  check('拒绝删除当前分支', delCurrent.ok === false, delCurrent.error || j(delCurrent));
  post('/api/project/checkout', { id: id5, branch: 'main' });
  const del5 = post('/api/project/branch/delete', { id: id5, branch: 'feat', force: true });
  check('删除已合并的分支', del5.ok === true && del5.deleted === 'feat', del5.error || del5.deleted);
  const br5 = branchesOf(id5);
  check('分支列表只剩 main', j(br5.branches) === j(['main']), (br5.branches || []).join(','));
  check('删除分支后还在 main', br5.current === 'main', br5.current);
  check('被删分支的文件没留在工作区', !hasFile(at(d5, 'feat-only.md')));

  // ================= 6. gc：反复回滚 / 重提交堆出来的内容块 =================
  const d6 = pdir(6, 'gc');
  const c6 = newProject('内容块清理', d6);
  check('创建项目（gc）', c6.ok === true, c6.id);
  const id6 = c6.id;
  writeText(at(d6, 'g.md'), '# g\n\n第一版\n');
  commitNow(id6, 'mt: gc 第一版');
  const v1 = headSha(id6);
  const blobs0 = ((post('/api/project/history-stats', { id: id6 }).stats) || {}).blobs;
  check('拿到初始内容块数', typeof blobs0 === 'number' && blobs0 >= 2, blobs0);

  writeText(at(d6, 'g.md'), '# g\n\n第二版\n');
  commitNow(id6, 'mt: gc 第二版');
  restore(id6, v1, []);                       // 回滚：把第二版从工作区拿掉
  writeText(at(d6, 'g.md'), '# g\n\n第三版\n');
  check('回滚后重新提交成功', commitNow(id6, 'mt: 回滚后重新提交').committed === true);
  writeText(at(d6, 'g.md'), '# g\n\n第四版\n');
  commitNow(id6, 'mt: gc 第四版');
  const treeBeforeGc = treeOf(d6);
  const headBeforeGc = headSha(id6);

  const gc1 = post('/api/project/gc', { id: id6 });
  const bs = gc1.before || {};
  const as = gc1.after || {};
  check('gc 返回了统计', Number.isInteger(gc1.removed_blocks) && gc1.removed_blocks >= 0 && typeof bs.blobs === 'number' && typeof as.blobs === 'number',
    j({ removed_blocks: gc1.removed_blocks, before: bs, after: as }));
  check('重复提交确实堆出了内容块', typeof bs.blobs === 'number' && bs.blobs >= blobs0 + 3, blobs0 + ' -> ' + bs.blobs);
  check('gc 不动版本数', typeof bs.versions === 'number' && bs.versions > 0 && as.versions === bs.versions, bs.versions + ' -> ' + as.versions);
  check('gc 的块数账目对得上', as.blobs === bs.blobs - gc1.removed_blocks, bs.blobs + ' - ' + gc1.removed_blocks + ' = ' + as.blobs);
  check('gc 后占用不会变大', typeof as.bytes === 'number' && as.bytes <= bs.bytes, bs.bytes + ' -> ' + as.bytes);
  check('gc 不影响当前文件', j(treeOf(d6)) === j(treeBeforeGc), '工作区文件清单与内容一致');
  check('gc 后 g.md 还是最新内容', readText(at(d6, 'g.md')) === '# g\n\n第四版\n', j(readText(at(d6, 'g.md'))));
  check('gc 后工作区干净', post('/api/project/status', { id: id6 }).dirty === false);
  check('gc 后 HEAD 没变', headBeforeGc.length === 40 && headSha(id6) === headBeforeGc, headBeforeGc);
  const gc2 = post('/api/project/gc', { id: id6 });
  check('再 gc 一次没有可删的块', gc2.removed_blocks === 0, gc2.removed_blocks);

  // ================= 7. 导出成真 git 仓库 =================
  const d7 = pdir(7, 'export');
  const c7 = newProject('导出 git 仓库', d7);
  check('创建项目（导出）', c7.ok === true, c7.id);
  const id7 = c7.id;
  writeText(at(d7, 'export.md'), '# 导出\n\n第一版\n');
  writeText(at(d7, 'notes.md'), '# 说明\n\n说明文件\n');
  commitNow(id7, 'mt: 导出基线');
  writeText(at(d7, 'export.md'), '# 导出\n\n第二版\n');
  commitNow(id7, 'mt: 导出第二版');

  try { fs.rmSync(outDir, { recursive: true, force: true }); } catch {}
  const eg = post('/api/project/export-git', { id: id7, target: outDir });
  check('导出 git 成功', eg.ok === true && eg.exported >= 3, eg.error || (eg.exported + ' 个提交'));
  check('导出目录里有 .git', hasFile(at(outDir, '.git')));
  check('导出目录里有 export.md 且是最新内容', readText(at(outDir, 'export.md')) === '# 导出\n\n第二版\n', j(readText(at(outDir, 'export.md'))));
  check('导出目录里有 notes.md', readText(at(outDir, 'notes.md')) === '# 说明\n\n说明文件\n', j(readText(at(outDir, 'notes.md'))));

  const gitCount = spawnSync('git', ['-C', outDir, 'rev-list', '--count', 'HEAD'], { encoding: 'utf8', windowsHide: true, timeout: 30000 });
  const countText = String(gitCount.stdout || '').trim();
  check('导出仓库的提交数对得上', countText === String(eg.exported), countText + '/' + eg.exported + ' ' + String(gitCount.stderr || '').slice(0, 80));
  const gitFiles = spawnSync('git', ['-C', outDir, 'ls-files'], { encoding: 'utf8', windowsHide: true, timeout: 30000 });
  const listed = String(gitFiles.stdout || '');
  check('导出仓库确实提交了正文文件', listed.includes('export.md') && listed.includes('notes.md'), listed.replace(/\r?\n/g, ' ').slice(0, 120));

  freshDir(busyDir);
  writeText(at(busyDir, 'occupied.txt'), 'x');
  const busy = post('/api/project/export-git', { id: id7, target: busyDir });
  check('拒绝导出到非空目录', busy.ok === false && String(busy.error || '').includes('不是空的'), busy.error || '');
} catch (e) {
  fatal = String((e && e.message) || e);
}
check('所有场景跑完没有中断', fatal === '', fatal);

const passed = results.filter((x) => x.ok).length;
for (const x of results) console.log((x.ok ? '✅ ' : '❌ ') + x.name + (x.extra ? ' — ' + x.extra : ''));
console.log('\n临时目录: D:\\_sc_b_' + STAMP + '_*');
console.log('\n结果: ' + passed + '/' + results.length);
process.exit(passed === results.length ? 0 : 1);
