/** 把别处的产物存进项目：统一的「存到项目」入口。
 *
 * 用法：
 *   import { saveToProject } from './project-link.js';
 *   await saveToProject(filePath, '视频标题');      // 选项目 + 选节点 → 复制进项目
 *
 * 设计原则：
 *   · 一次选择，两下拉框（项目 / 节点），默认记住上次的选择
 *   · 文件只是复制一份进项目，源文件不动
 *   · 失败要说清楚为什么，不要静默
 */
import { postJson } from './api.js';
import { dialog, toast, h } from './ui.js';

const LAST_KEY = 'mt.projectLink.lastTarget';

/** 读项目与节点树 */
export async function listTargets() {
  const data = await postJson('/api/project/targets', {});
  return data.projects || [];
}

/** 弹一个选择器，返回 {projectId, nodeId} 或 null */
export function pickTarget(projects, preset) {
  return new Promise((resolve) => {
    const host = document.getElementById('dialogHost');
    const saved = (() => { try { return JSON.parse(localStorage.getItem(LAST_KEY) || 'null'); } catch { return null; } })();
    const want = preset || saved || {};

    const projectSel = h('select', { class: 'input' },
      ...projects.map((p) => h('option', { value: p.id }, p.name)));
    if (want.projectId && projects.some((p) => p.id === want.projectId)) projectSel.value = want.projectId;

    const nodeSel = h('select', { class: 'input' });
    const fillNodes = () => {
      const proj = projects.find((p) => p.id === projectSel.value) || projects[0];
      nodeSel.textContent = '';
      for (const n of (proj ? proj.nodes : [])) {
        const label = '\u3000'.repeat(Math.max(0, (n.depth || 0) - 1)) + (n.is_trunk ? '【主干】' : '') + n.title;
        nodeSel.append(h('option', { value: n.id }, label));
      }
      if (want.nodeId && proj && proj.nodes.some((n) => n.id === want.nodeId)) nodeSel.value = want.nodeId;
    };
    fillNodes();
    projectSel.addEventListener('change', () => { want.nodeId = ''; fillNodes(); });

    const body = h('div', {},
      h('div', { class: 'hint', style: 'margin-bottom:6px' }, '文件会复制一份到项目里，原文件不动。'),
      h('div', { style: 'margin-bottom:4px;font-size:12px;opacity:.7' }, '放到哪个项目'),
      projectSel,
      h('div', { style: 'margin:10px 0 4px;font-size:12px;opacity:.7' }, '挂到哪个节点'),
      nodeSel);

    const close = (value) => { host.classList.remove('open'); host.textContent = ''; resolve(value); };
    host.textContent = '';
    host.append(h('div', { class: 'dialog' },
      h('h3', {}, '存到项目'),
      body,
      h('div', { class: 'actions' },
        h('button', { class: 'btn text', onclick: () => close(null) }, '取消'),
        h('button', { class: 'btn', onclick: () => {
          const choice = { projectId: projectSel.value, nodeId: nodeSel.value };
          try { localStorage.setItem(LAST_KEY, JSON.stringify(choice)); } catch (e) { /* 忽略 */ }
          close(choice);
        } }, '存进去'))));
    host.classList.add('open');
  });
}

/** 把磁盘上的一个文件挂进某个项目的某个节点 */
export async function attachToProject(projectId, nodeId, source) {
  return postJson('/api/project/attach', { id: projectId, nodeId, source });
}

/** 一步到位：选项目 → 选节点 → 复制进去。返回 true 表示成功 */
export async function saveToProject(sourcePath, label) {
  if (!sourcePath) { toast('这个东西没有磁盘文件，无法存进项目'); return false; }
  let projects;
  try { projects = await listTargets(); }
  catch (e) { toast('读取项目失败：' + e.message); return false; }
  if (!projects.length) {
    await dialog({
      title: '还没有项目',
      body: '先在左侧「项目」里建一个项目，再回来存文件。',
      confirm: '知道了', cancel: null,
    });
    return false;
  }
  const choice = await pickTarget(projects);
  if (!choice) return false;
  try {
    await attachToProject(choice.projectId, choice.nodeId, sourcePath);
    toast('已存进项目：' + (label || String(sourcePath).split(/[\\/]/).pop()));
    return true;
  } catch (e) {
    await dialog({ title: '存不进去', body: e.message, confirm: '知道了', cancel: null });
    return false;
  }
}
