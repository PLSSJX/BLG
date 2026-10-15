/** 四个主页面，导航结构对齐 Kazumi 的 推荐 / 时间表 / 追番 / 我的。 */
import { api } from './api.js';
import { icon } from './icons.js';
import {
  h, clear, card, button, chip, switchRow, field, pill, toast, dialog,
  escapeHtml, fmtSize, fmtDuration, fmtCount, fmtDate, busy, emptyState,
} from './ui.js';
import { applyTheme, THEME_MODES } from './theme.js';

/** 全局共享状态。 */
export const store = {
  config: null,
  parsed: null,      // { info, ref, engine }
  play: null,        // PlayInfo
  input: '',
  engine: 'auto',
  qn: 0,
  mode: 'auto',
  page: 1,                 // 流详情预览的是哪一 P
  selectedPages: null,     // 要下载的分 P 集合（Set<number>）
  probe: null,
  probeInput: '',
};

const QUALITY_HINT = {
  127: '需要大会员', 126: '需要大会员', 125: '需要大会员', 120: '需要大会员',
  116: '需要大会员', 112: '需要大会员', 100: '需要大会员', 80: '需要登录',
  74: '需要大会员', 64: '需要登录',
};

/* ================================================================ 推荐（解析） */

export function renderHome(view) {
  clear(view);
  const page = h('div', { class: 'page' });

  page.append(
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '解析'),
      h('div', { class: 'spacer' }),
      store.config ? pill(store.config.logged_in ? '已登录 · 可解锁高画质' : '未登录 · 最高 720P',
        store.config.logged_in ? 'ok' : 'mute') : null),
    h('p', { class: 'page-sub' }, '输入 BV 号、av 号、完整链接、b23.tv 短链或番剧链接，程序会自动挑选可用的抓取通道并列出真实可下载的画质。'),
    buildSearchCard(),
  );

  if (store.parsed) {
    page.append(buildVideoCard());
    page.append(buildDownloadCard());
    page.append(buildStreamCard());
  } else {
    page.append(h('div', { style: { marginTop: '12px' } },
      emptyState('search', '还没有解析任何视频',
        '在上面的输入框里粘贴一个 B 站视频地址，然后点「解析」。')));
  }

  view.append(page);
}

function buildSearchCard() {
  const input = h('input', {
    class: 'input',
    placeholder: '例如 BV1GJ411x7h7，或 https://www.bilibili.com/video/BV1GJ411x7h7/?p=2',
    value: store.input,
    oninput: (e) => { store.input = e.target.value; },
    onkeydown: (e) => { if (e.key === 'Enter') runParse(); },
  });
  const engineSelect = h('select', {
    class: 'select',
    onchange: (e) => { store.engine = e.target.value; },
  });
  fillEngineOptions(engineSelect, store.engine);

  const runBtn = button('解析', { iconName: 'search', onClick: () => runParse() });

  const card1 = card(
    h('div', { class: 'card-title', html: icon('link', 'outlined', 20) + '<span>视频地址</span>' }),
    h('div', { class: 'card-desc' }, '支持：BV 号 · av 号 · 完整视频链接 · b23.tv 短链 · 番剧 ep/ss 号'),
    h('div', { class: 'search-field' }, input, runBtn),
    h('div', { style: { marginTop: '14px', display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap' } },
      h('div', { style: { minWidth: '260px', flex: '1' } },
        field('抓取通道', h('div', { class: 'select-wrap' }, engineSelect))),
      button('全部通道体检', { variant: 'outlined', iconName: 'science', onClick: () => {
        store.probeInput = store.input;
        document.dispatchEvent(new CustomEvent('navigate', { detail: 'matrix' }));
      } })),
  );
  return card1;
}

function fillEngineOptions(select, current) {
  clear(select);
  select.append(h('option', { value: 'auto' }, '自动（按优先级依次尝试，选实际画质最高的）'));
  for (const engine of store.config?.engines || []) {
    const suffix = engine.available ? '' : '（当前不可用）';
    select.append(h('option', { value: engine.id }, engine.name + suffix));
  }
  select.value = current || 'auto';
}

async function runParse() {
  const input = (store.input || '').trim();
  if (!input) { toast('请先填写视频地址或 BV 号'); return; }
  const view = document.getElementById('view');
  const btn = view.querySelector('.search-field .btn');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '';
  btn.append(busy('解析中'));

  try {
    store.parsed = await api.parse(input, store.engine);   // { info, ref, engine, engine_name }
    if (!store.parsed?.info) throw new Error('接口没有返回视频信息');
    store.page = 1;
    store.selectedPages = new Set([1]);
    store.qn = 0;
    store.play = null;
    renderHome(view);
    // 元数据先出来，再去取流，两段式让界面不至于长时间空白
    loadStreams();
  } catch (error) {
    toast('解析失败：' + error.message.split('\n')[0], 5200);
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

// 连点分P / 画质 / 通道会连发多个 /api/play，只认最后发出去的那一次，
// 否则先发后到的响应会把新的播放信息覆盖掉，界面和 store.page/store.qn 对不上。
let streamRequestId = 0;

async function loadStreams() {
  if (!store.parsed) return;
  const requestId = ++streamRequestId;
  const view = document.getElementById('view');
  const host = view.querySelector('#streamHost');
  if (host) { clear(host).append(h('div', { class: 'card-desc' }, busy('正在取流…'))); }
  try {
    // 注意解包：接口返回的是 { ok, play, engine, engine_name } 信封，
    // store.play 要存里面的 play 本身，否则所有字段都是 undefined。
    const response = await api.play(store.input, store.engine, store.qn, store.page);
    if (requestId !== streamRequestId) return;
    store.play = response.play || null;
    if (store.play && !store.qn && store.play.accept_quality?.length) {
      store.qn = store.play.accept_quality[0];
    }
  } catch (error) {
    if (requestId !== streamRequestId) return;
    store.play = null;
    if (host) {
      clear(host).append(h('div', { class: 'hint-box' },
        h('span', { html: icon('error_outline', 'outlined', 20) }),
        h('div', {}, '取流失败：' + error.message.split('\n').slice(0, 4).join(' / '))));
    }
    return;
  }
  renderHome(document.getElementById('view'));
}

function buildVideoCard() {
  const info = store.parsed.info;
  const meta = info.owner || {};
  const stat = info.stat || {};
  const stats = [
    ['播放', stat.view], ['弹幕', stat.danmaku], ['点赞', stat.like],
    ['投币', stat.coin], ['收藏', stat.favorite], ['分享', stat.share],
  ].filter(([, v]) => v !== undefined && v !== null);

  const thumb = h('div', { class: 'thumb' },
    info.cover ? h('img', { src: api.coverUrl(info.cover), alt: '', loading: 'lazy' }) : null,
    info.duration ? h('span', { class: 'duration' }, fmtDuration(info.duration)) : null);

  const metaBox = h('div', { class: 'video-meta' },
    h('h2', { class: 'video-title' }, info.title || '(无标题)'),
    h('div', { class: 'video-up' },
      h('span', { html: icon('verified', 'outlined', 16) }),
      meta.name || '未知 UP 主',
      h('span', { style: { opacity: '.6' } }, '· ' + (info.tname || '未分类') + ' · ' + fmtDate(info.pubdate))),
    h('div', { class: 'video-desc' }, info.desc || '（没有简介）'),
    h('div', { class: 'stat-row' },
      ...stats.map(([label, value]) =>
        h('div', {}, label + ' ', h('b', {}, fmtCount(value))))),
    h('div', { style: { marginTop: '12px', display: 'flex', gap: '8px', flexWrap: 'wrap' } },
      pill('通道：' + (store.parsed.engine_name || store.parsed.engine)),
      info.bvid ? pill(info.bvid, 'mute') : null,
      store.play ? pill(store.play.is_dash ? 'DASH 音视频分离' : '单文件 MP4',
        store.play.is_dash ? 'warn' : 'ok') : null),
  );

  const children = [h('div', { class: 'video-hero' }, thumb, metaBox)];

  if ((info.pages || []).length > 1) {
    children.push(buildPagePicker(info));
  }
  return card(...children);
}

/** 分 P 勾选列表：显示每一 P 的标题和时长，可以多选后批量下载。 */
function buildPagePicker(info) {
  const pages = info.pages || [];
  if (!(store.selectedPages instanceof Set)) {
    store.selectedPages = new Set([store.page || 1]);
  }
  // 只有当这一 P 真的存在时才保留选中状态
  const valid = new Set(pages.map((p) => p.page));
  for (const n of [...store.selectedPages]) if (!valid.has(n)) store.selectedPages.delete(n);
  if (!store.selectedPages.size) store.selectedPages.add(pages[0]?.page || 1);

  const counter = h('span', { class: 'pill' });
  const previewNote = h('span', { class: 'card-desc', style: { margin: '0' } });
  const list = h('div', { class: 'page-list' });
  const rows = new Map();

  const previewPage = () => Math.min(...store.selectedPages);

  const refresh = (reloadStreams) => {
    for (const [number, node] of rows) {
      const on = store.selectedPages.has(number);
      node.classList.toggle('checked', on);
      const box = node.querySelector('.checkbox');
      box.classList.toggle('on', on);
      box.innerHTML = on ? icon('check', 'filled', 16) : '';
    }
    counter.textContent = '已选 ' + store.selectedPages.size + ' / ' + pages.length + ' 个分 P';
    const preview = previewPage();
    previewNote.textContent = '下方「流详情」展示的是 P' + preview +
      '，下载会按勾选逐个建立任务。';
    if (reloadStreams && preview !== store.page) {
      store.page = preview;
      store.play = null;
      renderHome(document.getElementById('view'));
      loadStreams();
    }
  };

  for (const p of pages) {
    const number = p.page;
    const on = store.selectedPages.has(number);
    const row = h('div', {
      class: 'page-row' + (on ? ' checked' : ''),
      title: p.part || ('第 ' + number + ' P'),
      onclick: () => {
        if (store.selectedPages.has(number)) store.selectedPages.delete(number);
        else store.selectedPages.add(number);
        if (!store.selectedPages.size) store.selectedPages.add(number);   // 至少留一个
        refresh(true);
      },
    },
      h('span', { class: 'checkbox' + (on ? ' on' : ''), html: on ? icon('check', 'filled', 16) : '' }),
      h('span', { class: 'page-no' }, 'P' + number),
      h('span', { class: 'page-name' }, p.part || '(未命名)'),
      p.duration ? h('span', { class: 'page-dur' }, fmtDuration(p.duration)) : null);
    rows.set(number, row);
    list.append(row);
  }

  const toolbar = h('div', { class: 'btn-row', style: { marginBottom: '10px' } },
    button('全选', { variant: 'text', iconName: 'check_circle', onClick: () => {
      pages.forEach((p) => store.selectedPages.add(p.page));
      refresh(true);
    } }),
    button('反选', { variant: 'text', iconName: 'sync', onClick: () => {
      pages.forEach((p) => {
        if (store.selectedPages.has(p.page)) store.selectedPages.delete(p.page);
        else store.selectedPages.add(p.page);
      });
      if (!store.selectedPages.size) store.selectedPages.add(pages[0].page);
      refresh(true);
    } }),
    button('清空', { variant: 'text', iconName: 'close', onClick: () => {
      store.selectedPages = new Set([pages[0].page]);
      refresh(true);
    } }),
    h('div', { class: 'spacer' }),
    counter);

  refresh(false);

  return h('div', {},
    h('div', { class: 'section-label' }, '分 P（共 ' + pages.length + ' 个）'),
    toolbar,
    list,
    h('div', { style: { marginTop: '8px' } }, previewNote));
}

function buildDownloadCard() {
  const play = store.play;
  const engines = store.config?.engines || [];
  const modes = store.config?.modes || [];

  // 模式
  const modeRow = h('div', { class: 'chips' });
  for (const mode of modes) {
    modeRow.append(chip(mode.name, {
      active: store.mode === mode.id,
      title: mode.desc,
      onClick: () => { store.mode = mode.id; renderHome(document.getElementById('view')); },
    }));
  }

  // 通道
  const engineSelect = h('select', {
    class: 'select',
    onchange: (e) => {
      store.engine = e.target.value;
      store.play = null;
      renderHome(document.getElementById('view'));
      loadStreams();
    },
  });
  fillEngineOptions(engineSelect, store.engine);

  // 清晰度：实际下发的摆在前面，接口虚报的用虚线标出来
  const qualityRow = h('div', { class: 'chips' });
  const actual = play?.accept_quality || [];
  const phantom = play?.phantom_quality || [];
  const qualityName = (qn) => {
    const found = (store.config?.qualities || []).find((q) => q.qn === qn);
    return found ? found.name : ('qn' + qn);
  };
  if (!play) {
    qualityRow.append(h('span', { class: 'card-desc', style: { margin: '0' } }, '正在获取可用画质…'));
  } else {
    if (!actual.length) {
      qualityRow.append(h('span', { class: 'card-desc', style: { margin: '0' } }, '当前通道没有报告画质档位'));
    }
    for (const qn of actual) {
      const stream = (play.videos || []).find((v) => v.qn === qn);
      const tag = stream ? stream.resolution : (play.durl.length ? '单文件' : '');
      qualityRow.append(chip(qualityName(qn), {
        active: store.qn === qn,
        tag,
        onClick: () => {
          store.qn = qn;
          renderHome(document.getElementById('view'));
          loadStreams();
        },
      }));
    }
    for (const qn of phantom) {
      qualityRow.append(chip(qualityName(qn), {
        phantom: true,
        tag: QUALITY_HINT[qn] || '未下发',
        title: '接口声称支持该档位，但本次实际没有下发（' + (QUALITY_HINT[qn] || '需要更高权限') + '）',
        onClick: () => toast('该画质当前不可用：' + (QUALITY_HINT[qn] || '接口没有实际下发')),
      }));
    }
  }

  // 编码偏好
  const codecSelect = h('select', {
    class: 'select',
    onchange: (e) => saveSetting({ prefer_codec: e.target.value }, false),
  });
  for (const item of store.config?.codec_preferences || []) {
    codecSelect.append(h('option', { value: item.id }, item.name));
  }
  codecSelect.value = store.config?.settings?.prefer_codec || 'auto';

  const selectedCount = store.selectedPages instanceof Set ? store.selectedPages.size : 0;
  const startBtn = button(selectedCount > 1 ? ('开始下载（' + selectedCount + ' 个分 P）') : '开始下载', {
    iconName: 'download',
    disabled: !play,
    onClick: () => submitTask(),
  });

  const streamHost = h('div', { id: 'streamHost', style: { marginTop: '12px' } },
    play ? null : h('div', { class: 'card-desc', style: { margin: '0' } }, busy('正在取流…')));

  return card(
    h('div', { class: 'card-title', html: icon('tune', 'outlined', 20) + '<span>下载设置</span>' }),
    h('div', { class: 'card-desc' }, '清晰度列表以「本次实际下发到的流」为准 —— B 站接口常会虚报档位，这里只显示真实拿得到的。'),

    h('div', { class: 'section-label' }, '下载模式'),
    modeRow,

    h('div', { class: 'section-label' }, '清晰度'),
    qualityRow,

    h('div', { class: 'section-label' }, '抓取通道与编码'),
    h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '12px' } },
      h('div', { class: 'select-wrap' }, engineSelect),
      h('div', { class: 'select-wrap' }, codecSelect)),

    streamHost,
    h('div', { class: 'btn-row', style: { marginTop: '16px' } },
      startBtn,
      button('打开下载目录', { variant: 'text', iconName: 'folder_open', onClick: () => api.open() })),
  );
}

function buildStreamCard() {
  const play = store.play;
  if (!play) return h('span');
  const rows = [];
  for (const v of play.videos || []) {
    rows.push(['视频', v.resolution, v.codec_text, (v.bandwidth / 1000).toFixed(0) + ' kbps', v.size_text,
      v.qn === store.qn ? '当前选中' : '']);
  }
  for (const a of play.audios || []) {
    rows.push(['音频', '—', a.codec_text, (a.bandwidth / 1000).toFixed(0) + ' kbps', a.size_text,
      a.note || '']);
  }
  for (const d of play.durl || []) {
    rows.push(['单文件', '第 ' + d.order + ' 段', '已封装音轨', '—', d.size_text,
      d.order === 1 ? '免合并' : '']);
  }

  const table = h('table', { class: 'matrix' },
    h('thead', {}, h('tr', {},
      h('th', {}, '类型'), h('th', {}, '分辨率'), h('th', {}, '编码'),
      h('th', {}, '码率'), h('th', {}, '大小'), h('th', {}, '备注'))),
    h('tbody', {}, ...rows.map((row) => h('tr', {}, ...row.map((cell) => h('td', {}, cell))))));

  return card(
    h('div', { class: 'card-title', html: icon('layers', 'outlined', 20) + '<span>流详情</span>' }),
    h('div', { class: 'card-desc' },
      '通道：' + play.engine_name + '　·　' + (play.note || '')),
    table,
    play.phantom_quality?.length
      ? h('div', { class: 'hint-box', style: { marginTop: '12px' } },
          h('span', { html: icon('info', 'outlined', 20) }),
          h('div', {}, '接口声称还支持 ' + play.phantom_quality.map((q) => qualityLabel(q)).join('、') +
            '，但本次一个都没有下发。这些档位需要登录或大会员，在「我的」里填 SESSDATA 就能解锁。'))
      : null);
}

function qualityLabel(qn) {
  const found = (store.config?.qualities || []).find((q) => q.qn === qn);
  return found ? found.name : ('qn' + qn);
}

async function submitTask() {
  // 多 P 视频按勾选批量建任务：每个分 P 一个独立任务，
  // 进度、重试、失败原因互不影响。
  const selected = store.selectedPages instanceof Set
    ? [...store.selectedPages].sort((a, b) => a - b)
    : [store.page || 1];
  try {
    const result = await api.submit({
      input: store.input, engine: store.engine, qn: store.qn, mode: store.mode,
      ...(selected.length > 1 ? { pages: selected } : { page: selected[0] || 1 }),
    });
    toast(selected.length > 1
      ? '已加入 ' + (result.count || selected.length) + ' 个下载任务（共 ' + selected.length + ' 个分 P）'
      : '已加入下载队列');
    document.dispatchEvent(new CustomEvent('tasks-changed'));
    document.dispatchEvent(new CustomEvent('navigate', { detail: 'tasks' }));
    return result;
  } catch (error) {
    toast('提交失败：' + error.message, 4200);
  }
}

/* ================================================================ 时间表（通道体检） */

export function renderMatrix(view) {
  clear(view);
  const page = h('div', { class: 'page' });
  const input = h('input', {
    class: 'input', placeholder: 'BV 号或视频链接', value: store.probeInput || store.input || '',
    oninput: (e) => { store.probeInput = e.target.value; },
    onkeydown: (e) => { if (e.key === 'Enter') runProbe(); },
  });

  page.append(
    h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, '通道体检')),
    h('p', { class: 'page-sub' },
      '把同一个视频同时交给全部抓取通道，逐条报告真实结果。B 站的风控和接口策略经常变，' +
      '这里显示的是「此刻这台机器上」的实测可用性，而不是写死的宣传。'),
    card(
      h('div', { class: 'card-title', html: icon('science', 'outlined', 20) + '<span>通道体检</span>' }),
      h('div', { class: 'card-desc' }, '体检会并发请求所有通道，大约需要 10～30 秒。'),
      h('div', { class: 'search-field' }, input, button('开始体检', { iconName: 'bolt', onClick: () => runProbe() })),
    ),
  );

  if (store.probe) page.append(buildProbeResult());
  view.append(page);
}

async function runProbe() {
  const input = (store.probeInput || store.input || '').trim();
  if (!input) { toast('请先填写视频地址或 BV 号'); return; }
  const view = document.getElementById('view');
  renderMatrix(view);
  const host = view.querySelector('.page');
  const holder = h('div', { class: 'card', style: { marginTop: '8px' } },
    h('div', { class: 'card-desc' }, busy('正在把所有通道跑一遍，请稍候…')));
  host.append(holder);
  try {
    store.probe = await api.probe(input, 0);
  } catch (error) {
    clear(holder).append(h('div', { class: 'hint-box' },
      h('span', { html: icon('error_outline', 'outlined', 20) }),
      h('div', {}, '体检失败：' + error.message)));
    return;
  }
  renderMatrix(document.getElementById('view'));
}

function buildProbeResult() {
  const report = store.probe;
  const okCount = report.ok_count;
  const total = report.total;
  const rows = report.results.map((item) => {
    const best = item.best;
    return h('tr', { class: item.ok ? 'is-ok' : 'is-fail' },
      h('td', {},
        h('div', { style: { fontWeight: '600' } }, item.name),
        h('div', { class: 'why' }, item.badge ? '「' + item.badge + '」' : '')),
      h('td', {}, item.ok ? pill('可用', 'ok') : pill('不可用', 'err')),
      h('td', { class: 'q' }, best ? best.resolution : '—',
        best ? h('div', { class: 'why' }, best.label + (best.codec_text ? ' · ' + best.codec_text : '')) : null),
      h('td', { class: 'ms' }, (item.ms || 0) + ' ms'),
      h('td', { class: 'why' },
        (item.note || item.error || '').replace(/https?:\/\/\S+/g, '').trim(),
        item.phantom?.length
          ? h('div', { style: { marginTop: '4px' } },
              '虚报档位：' + item.phantom.map((q) => qualityLabel(q)).join('、'))
          : null),
    );
  });

  return h('div', {},
    card(
      h('div', { class: 'card-title', html: icon('hub', 'outlined', 20) + '<span>体检结果</span>' }),
      h('div', { class: 'card-desc' },
        '目标 ' + report.identity + '　·　可用通道 ',
        h('b', { style: { color: 'var(--md-primary)' } }, okCount + ' / ' + total),
        '　（普通投稿视频可用的官方通道 5～6 条，番剧走 PGC 通道，第三方解析与 TV 端当前已失效）'),
      h('div', { style: { overflowX: 'auto' } },
        h('table', { class: 'matrix' },
          h('thead', {}, h('tr', {},
            h('th', {}, '通道'), h('th', {}, '结果'), h('th', {}, '实际最高画质'),
            h('th', {}, '耗时'), h('th', {}, '说明'))),
          h('tbody', {}, ...rows))),
    ),
  );
}

/* ================================================================ 追番（下载任务） */

export function renderTasks(view) {
  clear(view);
  const page = h('div', { class: 'page' });
  const stats = store.taskStats || {};

  page.append(
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '历史记录'),
      h('div', { class: 'spacer' }),
      button('清除已完成', { variant: 'text', iconName: 'delete', onClick: async () => {
        const result = await api.clearTasks();
        toast('已清除 ' + result.removed + ' 条记录');
        refreshTasks();
      } }),
      button('打开目录', { variant: 'text', iconName: 'folder_open', onClick: () => api.open() })),
    h('p', { class: 'page-sub' }, '所有下载任务的历史记录，进行中的任务每 0.8 秒刷新一次进度。记录保存在 data/tasks.json，重启程序后依然在。'),
    card(
      h('div', { class: 'stat-row' },
        h('div', {}, '进行中 ', h('b', {}, String(stats.running || 0))),
        h('div', {}, '排队 ', h('b', {}, String(stats.queued || 0))),
        h('div', {}, '已完成 ', h('b', {}, String(stats.done || 0))),
        h('div', {}, '失败 ', h('b', {}, String(stats.error || 0))),
        h('div', {}, '合并方式 ', h('b', {}, stats.ffmpeg ? 'ffmpeg' : '内置 fMP4 合并器'))),
    ),
  );

  const tasks = store.tasks || [];
  if (!tasks.length) {
    page.append(emptyState('cloud_download', '还没有任何下载记录',
      '在「解析」页解析一个视频，然后点「开始下载」，任务就会出现在这里。'));
  } else {
    const grid = h('div', { class: 'grid', style: { marginTop: '12px' } });
    for (const task of tasks) grid.append(buildTaskCard(task));
    page.append(grid);
  }
  view.append(page);
}

const STATUS_TEXT = {
  queued: ['排队中', 'mute'], running: ['下载中', 'warn'], merging: ['合并中', 'warn'],
  done: ['已完成', 'ok'], error: ['失败', 'err'], cancelled: ['已取消', 'mute'],
};

function buildTaskCard(task) {
  const [label, variant] = STATUS_TEXT[task.status] || ['未知', 'mute'];
  const active = task.status === 'running' || task.status === 'merging' || task.status === 'queued';
  const percent = task.percent || 0;

  // 元数据类任务没有字节总数；已经跑完的就直接给满格，不要再转圈
  const finished = task.status === 'done';
  const determinate = task.has_total || finished;
  const progress = h('div', { class: 'progress' + (determinate ? '' : ' indeterminate') },
    h('i', { style: { width: (finished ? 100 : task.has_total ? percent : 35) + '%' } }));

  const foot = h('div', { class: 'task-foot' },
    task.has_total ? h('span', {}, percent.toFixed(1) + '%') : h('span', {}, task.progress?.phase || '准备中'),
    task.has_total ? h('span', {}, task.size_text + ' / ' + task.total_text) : null,
    h('span', {}, task.speed_text || '-'),
    active && task.has_total ? h('span', {}, '剩余 ' + (task.eta_text || '-')) : null,
    task.elapsed ? h('span', {}, '用时 ' + Math.round(task.elapsed) + 's') : null);

  const actions = h('div', { class: 'btn-row', style: { marginTop: '10px' } });
  if (active) {
    actions.append(button('取消', { variant: 'text', iconName: 'close', onClick: async () => {
      await api.cancelTask(task.id); toast('已请求取消'); refreshTasks();
    } }));
  } else {
    actions.append(button('重试', { variant: 'text', iconName: 'refresh', onClick: async () => {
      await api.retryTask(task.id); toast('已重新排队'); refreshTasks();
    } }));
    actions.append(button('打开目录', { variant: 'text', iconName: 'folder_open', onClick: () => {
      api.open(task.directory);
    } }));
    actions.append(button('删除', { variant: 'text', iconName: 'delete', onClick: async () => {
      const yes = await dialog({
        title: '删除这条记录？',
        body: '只从列表里移除记录，磁盘上的文件会保留。',
        confirm: '删除',
      });
      if (yes) { await api.removeTask(task.id, false); refreshTasks(); }
    } }));
  }

  const files = (task.files || []).length
    ? h('div', { class: 'files' }, ...task.files.map((file) => h('button', {
        class: 'file-chip',
        title: file.path,
        onclick: () => window.open(api.mediaUrl(file.path), '_blank'),
      }, h('span', { html: icon(
          file.kind === 'video' ? 'movie' : file.kind === 'audio' ? 'audiotrack'
          : file.kind === 'cover' ? 'image' : file.kind === 'nfo' ? 'description'
          : file.kind === 'subtitle' ? 'subtitles' : 'bolt', 'outlined', 15) }),
        file.name + (file.count ? '（' + file.count + ' 条）' : ''))))
    : null;

  return card(
    h('div', { class: 'task' },
      h('div', { class: 'cover' },
        task.cover ? h('img', { src: api.coverUrl(task.cover), alt: '', loading: 'lazy' }) : null),
      h('div', { class: 'body' },
        h('div', { class: 'name', title: task.title }, task.title || task.raw_input),
        h('div', { class: 'sub' },
          (task.page_title ? task.page_title + ' · ' : '') +
          (task.qn_label || '自动画质') + ' · ' +
          (task.engine_used || task.engine) + ' · ' + task.mode),
        progress,
        foot,
      )),
    files,
    task.error ? h('div', { class: 'hint-box', style: { marginTop: '10px' } },
      h('span', { html: icon('error_outline', 'outlined', 20) }), h('div', {}, task.error)) : null,
    h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', marginTop: '10px' } },
      pill(label, variant), h('div', { class: 'spacer' }), actions),
    task.log?.length ? h('details', { style: { marginTop: '8px' } },
      h('summary', { style: { cursor: 'pointer', fontSize: '12px', color: 'var(--md-on-surface-variant)' } },
        '运行日志（' + task.log.length + ' 行）'),
      h('div', { class: 'log' }, task.log.join('\n'))) : null,
  );
}

export async function refreshTasks() {
  try {
    const data = await api.tasks();
    store.tasks = data.tasks;
    store.taskStats = data.stats;
  } catch { /* 轮询失败静默处理，下一轮会补上 */ }
  document.dispatchEvent(new CustomEvent('tasks-rendered'));
}

/* ================================================================ 我的（设置） */

export function renderSettings(view) {
  clear(view);
  const settings = store.config?.settings || {};
  const page = h('div', { class: 'page' });

  page.append(
    h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, '设置')),
    h('p', { class: 'page-sub' }, '主题外观、账号、下载选项、抓取通道、更新与自检。改动即时生效并写入 data/config.json。'),
    h('div', { class: 'split' },
      buildAppearanceCard(settings),
      buildAccountCard(settings),
      buildDownloadCard2(settings),
      buildExtrasCard(settings),
      buildEnginesCard(),
      buildToolsCard(),
      buildUpdateCard(),
      buildProgramCard()),
  );
  view.append(page);
}

async function saveSetting(patch, rerender = true) {
  try {
    const result = await api.saveSettings(patch);
    store.config.settings = result.settings;
    if (patch.theme_seed !== undefined || patch.theme_mode !== undefined || patch.dynamic_color !== undefined) {
      applyTheme({
        seed: result.settings.theme_seed,
        mode: result.settings.theme_mode,
        dynamic: result.settings.dynamic_color,
      });
    }
    if (rerender) renderSettings(document.getElementById('view'));
  } catch (error) {
    toast('保存失败：' + error.message);
  }
}

function buildAppearanceCard(settings) {
  const swatches = h('div', { class: 'swatches' });
  for (const preset of store.config?.theme_presets || []) {
    const dot = h('div', { class: 'dot', style: { background: preset.color } },
      settings.theme_seed?.toLowerCase() === preset.color.toLowerCase()
        ? h('span', { html: icon('check', 'filled', 20) }) : null);
    swatches.append(h('button', {
      class: 'swatch' + (settings.theme_seed?.toLowerCase() === preset.color.toLowerCase() ? ' active' : ''),
      onclick: () => saveSetting({ theme_seed: preset.color }),
    }, dot, h('span', {}, preset.label)));
  }

  const modeRow = h('div', { class: 'chips' });
  for (const mode of THEME_MODES) {
    modeRow.append(chip(mode.label, {
      active: settings.theme_mode === mode.id,
      onClick: () => saveSetting({ theme_mode: mode.id }),
    }));
  }

  return card(
    h('div', { class: 'card-title', html: icon('palette', 'outlined', 20) + '<span>外观</span>' }),
    h('div', { class: 'card-desc' },
      '九种预设主题色与 Kazumi 完全一致，色板也由同一份 Material 3 算法（SchemeTonalSpot）算出。'),
    h('div', { class: 'section-label' }, '主题色'),
    swatches,
    h('div', { class: 'section-label' }, '明暗模式'),
    modeRow,
    switchRow('动态取色', '关闭后使用内置的降级色板（官方算法库加载失败时也会自动降级）',
      settings.dynamic_color !== false, (value) => saveSetting({ dynamic_color: value }, false)),
  );
}

function buildAccountCard(settings) {
  const input = h('input', {
    class: 'input', type: 'password', placeholder: '粘贴浏览器里的 SESSDATA 值',
    autocomplete: 'off',
  });
  const result = h('div', { class: 'card-desc' });

  return card(
    h('div', { class: 'card-title', html: icon('key', 'outlined', 20) + '<span>账号</span>' }),
    h('div', { class: 'card-desc' },
      '不填也能用：未登录时 DASH 最高 480P、单文件通道最高 720P。填入 SESSDATA 后可解锁 1080P 及以上（4K/HDR 另需大会员）。'),
    h('div', { class: 'search-field' }, input,
      button('保存', { onClick: async () => {
        await saveSetting({ sessdata: input.value.trim() }, false);
        input.value = '';
        toast('已保存');
        checkLogin(result);
      } }),
      button('校验', { variant: 'tonal', onClick: () => checkLogin(result) })),
    h('div', { style: { marginTop: '10px' } }, result),
    buildSessdataHelp(),
  );
}

/** SESSDATA 获取教学。写成分步说明，普通用户照着做就能拿到。 */
function buildSessdataHelp() {
  const step = (text) => h('li', { html: text });
  return h('details', { class: 'help' },
    h('summary', {}, '如何获取 SESSDATA？'),
    h('div', { class: 'body' },
      h('ol', {},
        step('用浏览器打开 <a href="https://www.bilibili.com" target="_blank" rel="noreferrer">bilibili.com</a> 并<strong>登录</strong>你的账号。'),
        step('按 <code>F12</code> 打开开发者工具（或者右键页面 → 检查）。'),
        step('切换到 <strong>Application</strong>（中文版叫「应用」或「应用程序」）标签页。'),
        step('在左侧展开 <strong>Cookies</strong> → 点选 <code>https://www.bilibili.com</code>。'),
        step('在右侧列表里找到名字为 <code>SESSDATA</code> 的那一行，双击它的 <strong>Value</strong> 列并整行复制。'),
        step('回到这里粘贴到上面的输入框，点「保存」，再点「校验」确认登录态有效。'),
        ),
      h('p', { class: 'tip' },
        '小技巧：也可以在开发者工具的 Console 里执行 ',
        h('code', {}, 'document.cookie.match(/SESSDATA=([^;]+)/)[1]'),
        ' 直接把值打印出来。'),
      h('p', { class: 'tip' },
        h('b', {}, '这个值等同于你的登录凭证'),
        '，只会保存在本机的 ',
        h('code', {}, 'data/config.json'),
        ' 里，不会上传到任何地方。请不要把它发给别人；在 B 站「退出登录」后它会立即失效。'),
    ));
}

async function checkLogin(host) {
  clear(host).append(busy('正在校验登录态…'));
  try {
    const info = await api.loginCheck();
    clear(host).append(
      pill(info.logged_in ? '登录态有效' : '未登录', info.logged_in ? 'ok' : 'mute'),
      ' ' + (info.detail || ''),
      info.name ? '（' + info.name + (info.vip ? ' · 大会员' : '') + '）' : '');
  } catch (error) {
    clear(host).append('校验失败：' + error.message);
  }
}

function buildDownloadCard2(settings) {
  const dir = h('input', { class: 'input', value: settings.download_dir || '' });
  const template = h('input', { class: 'input', value: settings.filename_template || '{title}' });
  const threads = h('input', {
    class: 'input', type: 'number', min: '1', max: '32', value: settings.threads_per_task || 8,
  });
  const parallel = h('input', {
    class: 'input', type: 'number', min: '1', max: '8', value: settings.max_parallel_tasks || 2,
  });

  const qualitySelect = h('select', { class: 'select' });
  qualitySelect.append(h('option', { value: '0' }, '自动（每次选实际可用的最高画质）'));
  for (const q of store.config?.qualities || []) {
    qualitySelect.append(h('option', { value: String(q.qn) },
      q.name + (q.needs_vip ? ' · 需大会员' : q.needs_login ? ' · 需登录' : '')));
  }
  qualitySelect.value = String(settings.prefer_quality || 0);

  return card(
    h('div', { class: 'card-title', html: icon('download', 'outlined', 20) + '<span>下载</span>' }),
    h('div', { class: 'card-desc' }, '多线程分块下载，支持断点续传；中断后重新下载会自动跳过已完成的块。'),
    field('下载目录', h('div', { class: 'search-field' }, dir,
      button('打开', { variant: 'tonal', iconName: 'folder_open', onClick: () => api.open(dir.value) }))),
    h('div', { style: { height: '12px' } }),
    field('文件名模板（可用 {title} {up} {bvid} {quality} {engine}）', template),
    h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginTop: '12px' } },
      field('每个任务的线程数', threads),
      field('同时下载的任务数', parallel)),
    h('div', { style: { height: '12px' } }),
    field('默认清晰度', h('div', { class: 'select-wrap' }, qualitySelect)),
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('保存', { onClick: () => saveSetting({
        download_dir: dir.value, filename_template: template.value,
        threads_per_task: Number(threads.value) || 8,
        max_parallel_tasks: Number(parallel.value) || 2,
        prefer_quality: Number(qualitySelect.value) || 0,
      }, false) })),
    switchRow('合并后保留原始分离流', '默认合并成功就删掉 .m4s / .m4a，打开后会保留在 _streams 目录',
      settings.keep_streams === true, (value) => saveSetting({ keep_streams: value }, false)),
  );
}

function buildExtrasCard(settings) {
  return card(
    h('div', { class: 'card-title', html: icon('star', 'outlined', 20) + '<span>附加产物</span>' }),
    h('div', { class: 'card-desc' }, '每个视频独立目录，附加文件与视频同名，方便直接丢进播放器或媒体库。'),
    switchRow('封面', '下载最大宽度 1920 的封面图', settings.write_cover !== false,
      (v) => saveSetting({ write_cover: v }, false)),
    switchRow('弹幕', '保存 B 站 XML 弹幕，并额外转一份 ASS 字幕', settings.write_danmaku !== false,
      (v) => saveSetting({ write_danmaku: v }, false)),
    switchRow('字幕', '下载 UP 主上传的 CC 字幕并转成 SRT', settings.write_subtitle === true,
      (v) => saveSetting({ write_subtitle: v }, false)),
    switchRow('NFO 元数据', '生成 Kodi / Jellyfin 可识别的 .nfo', settings.write_nfo === true,
      (v) => saveSetting({ write_nfo: v }, false)),
  );
}

function buildEnginesCard() {
  const engines = store.config?.engines || [];
  const order = engines.map((e) => e.id);
  const enabled = engines.filter((e) => e.enabled).map((e) => e.id);
  const list = h('div', {});

  const persist = () => api.engineOrder(order, enabled)
    .then(() => { toast('通道设置已保存'); return api.state(); })
    .then((state) => { store.config = state; renderSettings(document.getElementById('view')); })
    .catch((error) => toast('保存失败：' + error.message));

  for (let index = 0; index < order.length; index++) {
    const id = order[index];
    const engine = engines.find((e) => e.id === id);
    if (!engine) continue;
    const row = h('div', { class: 'switch-row' },
      h('div', { class: 'grow' },
        h('div', { class: 'name' }, (index + 1) + '. ' + engine.name +
          (engine.badge ? '　' : '')),
        h('div', { class: 'hint' }, engine.desc)),
      h('div', { class: 'btn-row' },
        h('button', { class: 'btn text small', title: '上移', onclick: () => {
          if (index === 0) return;
          [order[index - 1], order[index]] = [order[index], order[index - 1]];
          persist();
        } }, '↑'),
        h('button', { class: 'btn text small', title: '下移', onclick: () => {
          if (index === order.length - 1) return;
          [order[index + 1], order[index]] = [order[index], order[index + 1]];
          persist();
        } }, '↓'),
        h('button', {
          class: 'switch' + (engine.enabled ? ' on' : ''),
          role: 'switch', 'aria-label': engine.name,
          onclick: (event) => {
            const on = !event.currentTarget.classList.contains('on');
            event.currentTarget.classList.toggle('on', on);
            const at = enabled.indexOf(id);
            if (on && at < 0) enabled.push(id);
            if (!on && at >= 0) enabled.splice(at, 1);
            persist();
          },
        })),
    );
    if (engine.badge) {
      row.querySelector('.name').append(pill(engine.badge, engine.available ? 'ok' : 'warn'));
    }
    list.append(row);
  }

  return card(
    h('div', { class: 'card-title', html: icon('route', 'outlined', 20) + '<span>抓取通道</span>' }),
    h('div', { class: 'card-desc' },
      '顺序就是「自动」模式下的尝试顺序，越靠前越优先。关掉的通道不会参与解析与体检。'),
    list,
  );
}

/** 更新与自检修复。 */
function buildUpdateCard() {
  const info = store.config || {};
  const result = h('div', { style: { marginTop: '10px' } });
  let pendingInstaller = '';

  const show = (node) => { clear(result).append(node); };

  const check = async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    show(busy('正在检查更新…'));
    try {
      const r = await api.updateCheck();
      const rows = h('div', {},
        h('div', { class: 'kv' }, h('span', {}, '当前版本'), h('span', {}, r.current)),
        h('div', { class: 'kv' }, h('span', {}, '最新版本'), h('span', {}, r.latest)),
        h('div', { class: 'kv' }, h('span', {}, '更新源'), h('span', {}, r.source)),
        r.generated ? h('div', { class: 'kv' }, h('span', {}, '清单生成于'), h('span', {}, r.generated)) : null);
      // 所有节点先攒齐再一次性写入：之前连着 clear(result) 三次，
      // 后面的会依次把前面的版本行、更新说明抹掉。
      const blocks = [
        rows,
        h('div', { style: { marginTop: '10px' } },
          r.has_update ? pill('有新版本可用', 'ok') : pill('已经是最新版本', 'mute')),
      ];
      if (r.notes) {
        blocks.push(h('div', { class: 'card-desc', style: { marginTop: '8px' } }, r.notes));
      }
      if (r.has_update) {
        blocks.push(h('div', { class: 'btn-row', style: { marginTop: '12px' } },
          button('下载安装包', { iconName: 'cloud_download', onClick: async (e2) => {
            const b = e2.currentTarget; b.disabled = true;
            const holder = h('div', { class: 'card-desc', style: { marginTop: '10px' } }, busy('正在下载…'));
            result.append(holder);
            try {
              const got = await api.updateDownload();
              pendingInstaller = got.path;
              clear(holder).append(pill('已下载 ' + fmtSize(got.size), 'ok'));
              result.append(h('div', { class: 'btn-row', style: { marginTop: '10px' } },
                button('运行安装程序', { iconName: 'restart_alt', onClick: async () => {
                  try {
                    const launched = await api.updateLaunch(pendingInstaller);
                    toast(launched.detail || '已启动安装程序');
                  } catch (error) { toast('启动失败：' + error.message); }
                } })));
            } catch (error) {
              clear(holder).append(h('div', { class: 'hint-box' },
                h('span', { html: icon('error_outline', 'outlined', 20) }),
                h('div', {}, error.message + ' 也可以到发布页手动下载。')));
            }
            b.disabled = false;
          } }),
          button('打开发布页', { variant: 'text', iconName: 'open_in_new',
            onClick: () => window.open(r.release_page, '_blank') })));
      }
      show(h('div', {}, ...blocks));
    } catch (error) {
      show(h('div', { class: 'hint-box' },
        h('span', { html: icon('error_outline', 'outlined', 20) }),
        h('div', {}, '检查失败：' + error.message)));
    }
    btn.disabled = false;
  };

  const selfCheck = async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    show(busy('正在逐个比对文件哈希…'));
    try {
      const r = await api.updateVerify();
      if (r.ok) {
        show(h('div', {},
          pill('自检通过', 'ok'),
          h('div', { class: 'card-desc', style: { marginTop: '8px' } },
            '共比对 ' + r.checked + ' 个文件，全部与仓库基线一致。更新源：' + r.source)));
      } else {
        const list = h('div', { class: 'log' });
        for (const item of r.bad) list.append(h('div', {}, item.path + ' —— ' + item.detail));
        show(h('div', {},
          pill(r.bad.length + ' 个文件异常', 'err'),
          h('div', { class: 'card-desc', style: { marginTop: '8px' } },
            '共比对 ' + r.checked + ' 个文件，正常 ' + r.ok_count + ' 个。'),
          list,
          h('div', { class: 'btn-row', style: { marginTop: '10px' } },
            button('一键修复', { iconName: 'restart_alt', onClick: repairNow }))));
      }
    } catch (error) {
      show(h('div', { class: 'hint-box' },
        h('span', { html: icon('error_outline', 'outlined', 20) }),
        h('div', {}, '自检失败：' + error.message)));
    }
    btn.disabled = false;
  };

  const repairNow = async (event) => {
    if (event) event.currentTarget.disabled = true;
    show(busy('正在重新下载不一致的文件…'));
    try {
      const r = await api.updateRepair();
      const list = h('div', { class: 'log' });
      for (const path of r.repaired) list.append(h('div', {}, '✓ ' + path));
      for (const item of r.failed) list.append(h('div', {}, '✗ ' + item.path + ' —— ' + item.error));
      if (!r.targets.length) {
        show(h('div', {}, pill('无需修复', 'ok'),
          h('div', { class: 'card-desc', style: { marginTop: '8px' } }, '所有文件都与基线一致。')));
      } else {
        show(h('div', {},
          pill(r.ok ? '修复完成' : '部分失败', r.ok ? 'ok' : 'err'),
          h('div', { class: 'card-desc', style: { marginTop: '8px' } },
            '更新源：' + r.source + '　共处理 ' + r.targets.length + ' 个文件' +
            (r.need_restart ? '　·　重启程序后生效' : '')),
          list));
      }
    } catch (error) {
      show(h('div', { class: 'hint-box' },
        h('span', { html: icon('error_outline', 'outlined', 20) }),
        h('div', {}, '修复失败：' + error.message)));
    }
    if (event) event.currentTarget.disabled = false;
  };

  const modeText = {
    webview: '内置 WebView2 独立窗口',
    'edge-app': 'Edge 应用模式窗口',
    browser: '系统默认浏览器',
    server: '纯服务模式（无窗口）',
  }[info.window_mode] || info.window_mode || '未知';

  return card(
    h('div', { class: 'card-title', html: icon('sync', 'outlined', 20) + '<span>更新与自检</span>' }),
    h('div', { class: 'card-desc' },
      '自检会把本地文件逐个算一遍 SHA-256，和仓库里的 update/manifest.json 比对；' +
      '对不上或者缺失的会自动重新下载。界面文件会被修复到程序目录下的 web/ 覆盖层。'),
    h('div', { class: 'kv' }, h('span', {}, '运行方式'), h('span', {}, modeText)),
    h('div', { class: 'kv' }, h('span', {}, '程序目录'),
      h('span', {}, info.base_dir || '—')),
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('检查更新', { variant: 'tonal', iconName: 'cloud_download', onClick: check }),
      button('自检并修复', { iconName: 'shield', onClick: selfCheck })),
    result,
  );
}

function buildProgramCard() {
  const address = location.origin + '/';
  return card(
    h('div', { class: 'card-title', html: icon('dns', 'outlined', 20) + '<span>程序</span>' }),
    h('div', { class: 'card-desc' },
      '桌面版没有控制台窗口，所以退出程序要从这里操作。关掉浏览器标签页并不会停止后台服务。'),
    h('div', { class: 'kv' }, h('span', {}, '访问地址'),
      h('span', {}, h('a', { href: address, style: { color: 'var(--md-primary)' } }, address))),
    h('div', { class: 'kv' }, h('span', {}, '运行方式'),
      h('span', {}, location.protocol === 'file:' ? '本地文件' : '本地服务（仅监听 127.0.0.1）')),
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('退出程序', { variant: 'outlined', iconName: 'stop', onClick: async () => {
        const yes = await dialog({
          title: '退出程序？',
          body: '后台服务会停止，正在进行的下载会中断（可重启后重试）。',
          confirm: '退出', danger: true,
        });
        if (!yes) return;
        try { await api.quit(); } catch { /* 服务关掉时连接被切断是正常的 */ }
        document.body.innerHTML =
          '<div style="display:grid;place-items:center;height:100vh;font:16px/1.8 system-ui;color:#888">' +
          '<div style="text-align:center">程序已退出<br><span style="font-size:13px">现在可以关闭此页面了</span></div></div>';
      } })),
  );
}

function buildToolsCard() {
  const list = h('div', {});
  const info = h('div', { class: 'card-desc' });

  const installBtn = button('一键安装 yt-dlp', { variant: 'tonal', iconName: 'cloud_download', onClick: async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    clear(btn).append(busy('安装中'));
    clear(info).append('正在把 yt-dlp 装进项目自带的 .deps 目录（不会改动系统环境）…');
    try {
      const result = await api.installYtdlp();
      clear(info).append(result.ok ? pill('安装成功', 'ok') + ' ' + result.deps_dir
        : pill('安装失败', 'err') + ' 返回码 ' + result.returncode);
      const state = await api.state();
      store.config = state;
      renderSettings(document.getElementById('view'));
    } catch (error) {
      clear(info).append('安装失败：' + error.message);
      btn.disabled = false;
      clear(btn);
    }
  } });

  const tools = (store.config?.engines || []).find((e) => e.id === 'external_tool');
  list.append(
    h('div', { class: 'kv' }, h('span', {}, 'Python'), h('span', {}, store.config?.python || '内置运行时')),
    h('div', { class: 'kv' }, h('span', {}, 'ffmpeg'),
      h('span', {}, store.config?.ffmpeg || '未检测到（将使用内置纯 Python fMP4 合并器）')),
    h('div', { class: 'kv' }, h('span', {}, '外部下载器通道'),
      h('span', {}, tools?.available ? '可用' : '未安装')),
  );

  return card(
    h('div', { class: 'card-title', html: icon('terminal', 'outlined', 20) + '<span>外部工具</span>' }),
    h('div', { class: 'card-desc' },
      '装上 yt-dlp 就多一条独立实现的抓取通道：它自带一套长期维护的 B 站解析器，可以和官方接口互相印证。' +
      '没有 ffmpeg 也完全不影响使用 —— 合并由内置的 fMP4 合并器完成。'),
    list,
    h('div', { class: 'btn-row', style: { marginTop: '12px' } }, installBtn),
    h('div', { style: { marginTop: '10px' } }, info),
  );
}
