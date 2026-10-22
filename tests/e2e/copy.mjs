// 文案自检：书面语、去 AI 腔、术语一致、残留与死代码。
// 这条不启动浏览器，单纯扫源码里的字符串，几秒钟跑完。
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(process.cwd());
const FILES = [
  'web/js/app.js', 'web/js/pages.js', 'web/js/player.js', 'web/js/ui.js',
  'web/js/ask.js', 'web/js/theme.js', 'web/js/api.js',
  'web/projects/js/canvas.js', 'web/projects/project.html', 'web/projects/index.html',
  'web/index.html', 'docs/index.html', 'README.md',
];

const rules = [
  {
    name: '口语词',
    // 面向用户的中文里不该出现的聊天腔
    pattern: /(搞定|搞成|弄成|随便|别当|算了|吧[。！]|啦[。！]|哦[。，]|呗|哈[。！]|嘛[。，]|存好了|点左边|粘进来|掂量|一口气|瞎|贼|想找什么|一起搜|过两天|悄悄|没接|看看|试一下|放出来|拿掉|堆在这儿)/,
    hint: '换成书面表达',
  },
  {
    name: 'AI 腔',
    pattern: /(不是[^。]{2,14}而是|不仅[^。]{2,14}而且|值得注意的是|总而言之|综上所述|首先[^。]{0,10}其次|一言以蔽之|在一定程度上可以认为)/,
    hint: '拆成直接陈述，别用对仗套话',
  },
  {
    name: '破折号堆叠',
    pattern: /——[^—\n]{0,20}——/,
    hint: '一句话里出现两个破折号，改成短句',
  },
  {
    name: '旧术语',
    // 已经改过的命名，别在别处漏回来
    pattern: /(附加工具箱(?!（默认关闭）)|文献检索|文献库|历史记录页)/,
    hint: '统一成「其他功能 / 检索 / 收着的论文」',
  },
  {
    name: '残留标记',
    pattern: /(TODO|FIXME|XXX:|待补|占位文字|lorem ipsum)/i,
    hint: '删掉或补完',
  },
  {
    name: '空的交互壳',
    // onclick 为空、href="#" 这种点了没反应的入口
    pattern: /(onclick\s*=\s*["']\s*["']|href\s*=\s*["']#["']\s*>\s*(?:<)|onClick:\s*\(\)\s*=>\s*\{\s*\})/,
    hint: '要么给出行为，要么去掉',
  },
];

/** 只取"用户看得见的文字"：JS 里是字符串字面量，HTML/MD 里是去掉注释和标签后的文本。
 *  代码注释不参与检查 —— 注释里说"卡片""随便"不影响界面。 */
function visibleTexts(rel, line) {
  if (rel.endsWith('.js')) {
    const out = [];
    // '...' / "..." / `...`，够用即可，不做完整词法
    const re = /'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g;
    let m;
    while ((m = re.exec(line))) out.push(m[1] ?? m[2] ?? m[3] ?? '');
    return out;
  }
  if (rel.endsWith('.html')) {
    return [line.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ' ')];
  }
  return [line];
}

const hits = [];
for (const rel of FILES) {
  const file = path.join(ROOT, rel);
  if (!fs.existsSync(file)) continue;
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    const trimmed = line.trim();
    // 整行注释直接跳过
    if (trimmed.startsWith('//') || trimmed.startsWith('/*') || trimmed.startsWith('*') || trimmed.startsWith('#')) return;
    for (const text of visibleTexts(rel, line)) {
      if (!text) continue;
      for (const rule of rules) {
        const m = text.match(rule.pattern);
        if (m) hits.push({ rel, line: i + 1, rule: rule.name, text: m[0], hint: rule.hint });
      }
    }
  });
}

// 术语一致性：同一概念只允许一种主说法
// 界面内同一概念必须统一；网站用比喻（卡片）是另一回事，分开算
const GROUPS = [
  { name: '界面', files: FILES.filter((f) => f.startsWith('web/') && !f.endsWith('.html') || f.startsWith('web/projects/')) },
  { name: '网站', files: FILES.filter((f) => f.startsWith('docs/') || f === 'README.md') },
];
const PAIRS = [['回退', '回档'], ['节点', '卡片']];
const inconsistency = [];
for (const group of GROUPS) {
  for (const [keep, drop] of PAIRS) {
    let bad = 0;
    for (const rel of group.files) {
      const file = path.join(ROOT, rel);
      if (!fs.existsSync(file)) continue;
      // 只数"用户看得到的文字"，注释不算：歌单卡片、设置卡片这些说法和思维导图的节点不是一回事
      for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
        const trimmed = line.trim();
        if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) continue;
        for (const text of visibleTexts(rel, line)) {
          bad += (text.match(new RegExp(drop, 'g')) || []).length;
        }
      }
    }
    if (bad) inconsistency.push(group.name + '里出现了「' + drop + '」' + bad + ' 处（统一用「' + keep + '」）');
  }
}

// 删除自检：已移除的功能不该还有入口
const removed = [
  { name: '“打开目录”重复按钮', pattern: /'打开目录'/ },
  { name: '重复的标题拼接', pattern: /<title>MakiseToolkit · MakiseToolkit<\/title>/ },
];
const dangling = [];
for (const item of removed) {
  for (const rel of FILES) {
    const file = path.join(ROOT, rel);
    if (!fs.existsSync(file)) continue;
    const text = fs.readFileSync(file, 'utf8');
    const count = (text.match(new RegExp(item.pattern.source, 'g')) || []).length;
    if (count > 1) dangling.push(item.name + ' 仍出现 ' + count + ' 次（' + rel + '）');
  }
}

const problems = hits.length + inconsistency.length + dangling.length;
for (const h of hits.slice(0, 40)) {
  console.log('❌ [' + h.rule + '] ' + h.rel + ':' + h.line + '  «' + h.text + '»  → ' + h.hint);
}
for (const s of inconsistency) console.log('❌ [术语] ' + s);
for (const s of dangling) console.log('❌ [残留] ' + s);
console.log('');
console.log('结果: ' + (problems ? '发现 ' + problems + ' 处问题' : '文案检查通过'));
process.exit(problems ? 1 : 0);
