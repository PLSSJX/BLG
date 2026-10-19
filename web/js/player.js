/** 应用内播放器：底部播放条 + 队列 / 乱序 / 单曲循环。 */
import { h, clear, button, fmtDuration } from './ui.js';
import { icon } from './icons.js';
import { api } from './api.js';

export const player = {
  queue: [],
  index: -1,
  shuffle: false,
  loop: 'off',        // off | one | all
  audio: null,
};

function current() {
  return player.queue[player.index] || null;
}

function mediaUrl(path) {
  return api.mediaUrl(path);
}

/** 彻底释放一个媒体元素。
 *
 *  只把元素从 DOM 里摘掉是不够的：媒体元素脱离文档后仍会继续播放/下载，
 *  必须 pause + 摘掉 src + load() 才会真正停下并断开连接。
 */
function releaseMedia(el) {
  if (!el) return;
  try { el.pause(); } catch (e) { /* 忽略 */ }
  el.removeAttribute('src');
  try { el.load(); } catch (e) { /* 忽略 */ }
}

/** 打乱队列，但把当前这首留在最前面，避免正在听的一下被换掉。 */
function reshuffle() {
  const cur = current();
  const rest = player.queue.filter((x) => x !== cur);
  for (let i = rest.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  player.queue = cur ? [cur, ...rest] : rest;
  player.index = cur ? 0 : -1;
}

export function playQueue(items, startIndex = 0) {
  player.queue = items.slice();
  // 空队列不能留下 index=0 + 上一条音频还在响的状态，否则播放条会显示着
  // 上一首而队列已经空了，next/prev 全部无声失效。
  if (!player.queue.length) {
    releaseMedia(player.audio);
    player.index = -1;
    render();
    return;
  }
  player.index = Math.max(0, Math.min(startIndex, player.queue.length - 1));
  if (player.shuffle) reshuffle();
  playCurrent();
}

export function playFile(path, name = '') {
  playQueue([{ path, name: name || path.split(/[\\/]/).pop() }], 0);
}

function playCurrent() {
  const item = current();
  if (!item || !player.audio) return;
  player.audio.src = mediaUrl(item.path);
  player.audio.play().catch(() => {});
  render();
}

export function next(auto = false) {
  if (!player.queue.length) return;
  if (!player.audio) return;
  if (auto && player.loop === 'one') {
    player.audio.currentTime = 0;
    player.audio.play().catch(() => {});
    return;
  }
  const last = player.index >= player.queue.length - 1;
  // 只有"列表循环"和"乱序"才会绕回开头；单曲循环和关闭都不该跳
  if (last && player.loop !== 'all' && player.loop !== 'shuffle' && auto) {
    player.audio.pause();
    render();
    return;
  }
  player.index = (player.index + 1) % player.queue.length;
  playCurrent();
}

export function prev() {
  if (!player.queue.length) return;
  if (player.audio && player.audio.currentTime > 3) {
    player.audio.currentTime = 0;
    return;
  }
  player.index = (player.index - 1 + player.queue.length) % player.queue.length;
  playCurrent();
}

export function toggle() {
  if (!player.audio || !current()) return;
  if (player.audio.paused) player.audio.play().catch(() => {});
  else player.audio.pause();
  render();
}

/** 循环模式一个按钮循环切换：关闭 → 列表循环 → 单曲循环 → 乱序播放 → 关闭。
 *  乱序本来就是"换一种顺序继续播"，和循环是一类东西，拆两个按钮反而让人猜。 */
function cycleLoop() {
  player.loop = player.loop === 'off' ? 'all'
    : player.loop === 'all' ? 'one'
      : player.loop === 'one' ? 'shuffle' : 'off';
  if (player.loop === 'shuffle') {
    player.shuffle = true;
    reshuffle();
  } else {
    player.shuffle = false;
  }
  render();
}

/** 把一首歌插到当前这首后面（"添加到下一首播放"）。 */
export function playNextItem(item) {
  if (!item) return;
  const at = Math.max(0, player.index);
  player.queue.splice(at + 1, 0, item);
  render();
}

/** 重画底部播放条。任何状态变化后调用。 */
export function render() {
  const bar = document.getElementById('playerBar');
  if (!bar) return;
  const item = current();
  if (!item) {
    bar.hidden = true;
    document.body.classList.remove('has-player');
    return;
  }
  bar.hidden = false;
  document.body.classList.add('has-player');

  const audio = player.audio;
  const pct = audio && audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
  const bar2 = h('div', { class: 'pb-progress', title: '点击跳转', onclick: (e) => {
    if (!audio || !audio.duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
  } }, h('i', { style: { width: pct.toFixed(2) + '%' } }));

  clear(bar).append(
    renderQueuePanel(),
    h('div', { class: 'pb-info' },
      h('div', { class: 'pb-title' }, item.name || '未命名'),
      h('div', { class: 'pb-sub' },
        (player.index + 1) + ' / ' + player.queue.length
        + (player.loop === 'one' ? ' · 单曲循环' : player.loop === 'all' ? ' · 列表循环'
          : player.loop === 'shuffle' ? ' · 乱序播放' : ' · 播完停止'))),
    // 已播时间必须带个钩子：timeupdate 只改进度条宽度的话，这行数字会永远停在 00:00
    h('div', { class: 'pb-time', dataset: { role: 'elapsed' } }, fmtDuration(audio?.currentTime || 0)),
    bar2,
    h('div', { class: 'pb-time' }, fmtDuration(audio?.duration || 0)),
    h('div', { class: 'pb-buttons' },
      button('', { variant: 'text', iconName: 'arrow_back', title: '上一首', onClick: () => prev() }),
      button('', { variant: 'text', iconName: audio && !audio.paused ? 'pause' : 'play_arrow',
        title: '播放/暂停', onClick: () => toggle() }),
      button('', { variant: 'text', iconName: 'chevron_right', title: '下一首', onClick: () => next(false) }),
      // 图标库里没有 repeat / repeat_one / shuffle，只有 sync 和 refresh，
      // 所以图标在两者之间切换，同时用一个短文字标出当前模式 —— 光看图标猜不出来。
      button(LOOP_LABEL[player.loop] || '顺序', {
        variant: 'text',
        iconName: player.loop === 'one' || player.loop === 'shuffle' ? 'refresh' : 'sync',
        title: '播放顺序：顺序 → 列表循环 → 单曲循环 → 乱序播放',
        onClick: cycleLoop,
        class: 'btn text' + (player.loop !== 'off' ? ' active' : ''),
      }),
      button('', { variant: 'text', iconName: 'sort', title: '查看播放列表',
        class: 'btn text' + (QUEUE_STATE.open ? ' active' : ''),
        onClick: () => { QUEUE_STATE.open = !QUEUE_STATE.open; render(); } }),
      button('', { variant: 'text', iconName: 'close', title: '关闭播放器', onClick: () => {
        releaseMedia(player.audio);
        player.queue = []; player.index = -1; render();
      } })));
}

export function initPlayer() {
  if (player.audio) return;
  const audio = new Audio();
  audio.preload = 'metadata';
  audio.addEventListener('ended', () => next(true));
  audio.addEventListener('timeupdate', () => {
    const bar = document.querySelector('#playerBar .pb-progress > i');
    if (bar && audio.duration) {
      bar.style.width = ((audio.currentTime / audio.duration) * 100).toFixed(2) + '%';
    }
    // 进度条会动但时间数字不会动 —— 以前只更新了宽度，没更新这行文字
    const elapsed = document.querySelector('#playerBar .pb-time[data-role="elapsed"]');
    if (elapsed) elapsed.textContent = fmtDuration(audio.currentTime);
  });
  audio.addEventListener('loadedmetadata', render);
  audio.addEventListener('play', render);
  audio.addEventListener('pause', render);
  player.audio = audio;
  render();
}


/* ---------------------------------------------------------------- 视频播放 */

/** 关掉当前覆盖层的方法。再开一个视频前用它把上一次完整收干净。 */
let closeCurrent = null;

/** 在应用内直接放视频（不弹浏览器）。
 *
 *  用一个覆盖层而不是底部播放条 —— 视频需要画面，夹在底部条里没法看。
 *  /media 已经支持 Range，可以直接拖动进度。
 */
const LOOP_LABEL = { off: '顺序', all: '列表', one: '单曲', shuffle: '乱序' };
const QUEUE_STATE = { open: false, expanded: false };

/** 当前播放队列：和歌单展开逻辑一致 —— 默认只露 2 首，展开后在框内滚动。 */
function renderQueuePanel() {
  if (!QUEUE_STATE.open) return null;
  const items = player.queue || [];
  const shown = QUEUE_STATE.expanded ? items : items.slice(0, 2);
  const rows = shown.map((item) => {
    const real = items.indexOf(item);
    const active = real === player.index;
    return h('div', {
      style: {
        display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 8px',
        borderRadius: '8px', cursor: 'pointer',
        background: active ? 'var(--md-secondary-container)' : 'transparent',
      },
      onclick: () => { player.index = real; playCurrent(); render(); },
    },
      h('span', { style: { width: '18px', opacity: '.55', fontSize: '11.5px', flex: 'none' } }, String(real + 1)),
      h('span', {
        style: { flex: '1', minWidth: '0', fontSize: '12.5px', whiteSpace: 'nowrap',
                 overflow: 'hidden', textOverflow: 'ellipsis',
                 fontWeight: active ? '600' : '400' },
      }, item.name || String(item.path || '').split(/[\\/]/).pop()),
      active ? h('span', { style: { fontSize: '11px', opacity: '.7' } }, '正在播') : null,
    );
  });
  return h('div', { class: 'queue-panel' },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '2px 4px 6px' } },
      h('b', { style: { fontSize: '12.5px' } }, '播放列表'),
      h('span', { style: { fontSize: '11.5px', opacity: '.6', flex: '1' } },
        items.length + ' 首' + (player.loop === 'shuffle' ? ' · 乱序' : '')),
      button('', { variant: 'text', iconName: 'close', title: '收起',
        onClick: () => { QUEUE_STATE.open = false; render(); } })),
    h('div', { class: 'song-scroll', style: { maxHeight: '260px' } }, ...rows),
    items.length > 2
      ? h('div', { class: 'btn-row', style: { marginTop: '6px' } },
          button(QUEUE_STATE.expanded ? '收起' : '展开全部 ' + items.length + ' 首', {
            variant: 'text',
            iconName: QUEUE_STATE.expanded ? 'expand_less' : 'expand_more',
            onClick: () => { QUEUE_STATE.expanded = !QUEUE_STATE.expanded; render(); },
          }))
      : null);
}

export function openVideo(path, name = '') {
  // 覆盖层会把底部播放条整个盖住（z-index 90 > 60），所以开始看视频时先把音乐停下，
  // 否则两路声音一起响，而用户没有任何办法按下底下那条播放条的暂停。
  if (player.audio && !player.audio.paused) player.audio.pause();
  // 上一次的覆盖层（如果还开着）先完整收掉 —— releaseMedia 会把旧 <video> 停掉并断开，
  // 同时把它挂在 document 上的 keydown 监听摘掉。只 host.innerHTML = '' 的话旧片子会继续播。
  if (closeCurrent) closeCurrent();

  const host = document.getElementById('videoHost') || (() => {
    const node = document.createElement('div');
    node.id = 'videoHost';
    node.className = 'video-host';
    document.body.append(node);
    return node;
  })();

  host.innerHTML = '';
  host.classList.add('open');

  const video = document.createElement('video');
  video.className = 'video-player';
  video.controls = true;
  video.autoplay = true;
  video.src = mediaUrl(path);

  const title = document.createElement('div');
  title.className = 'video-overlay-title';
  title.textContent = name || String(path).split(/[\\/]/).pop();

  const close = document.createElement('button');
  close.className = 'btn text video-close';
  close.innerHTML = icon('close', 'outlined', 22);
  close.title = '关闭';

  // 关闭时一定要把 keydown 监听一起摘掉：以前用 X 或点背景关闭时监听留在 document 上，
  // 每开一次视频就多一条，闭包里还攥着已经扔掉的 <video>（内存泄漏）。
  const onKey = (event) => { if (event.key === 'Escape') closeAll(); };
  const closeAll = () => {
    releaseMedia(video);
    document.removeEventListener('keydown', onKey);
    if (closeCurrent === closeAll) closeCurrent = null;
    host.classList.remove('open');
    host.innerHTML = '';
  };
  closeCurrent = closeAll;
  close.onclick = closeAll;
  host.onclick = (event) => { if (event.target === host) closeAll(); };
  document.addEventListener('keydown', onKey);

  // ---- 简单剪辑：填起止时间就能裁一段 ----
  const t0 = document.createElement('input');
  t0.className = 'input trim-input';
  t0.placeholder = '起点 0:05';
  const t1 = document.createElement('input');
  t1.className = 'input trim-input';
  // 终点留空在后端就是"一直裁到结尾"（server 传 end="" -> -to 不追加），
  // 所以这里直接把这个语义写在提示里，别再让它跟着播放时间跳来跳去。
  t1.placeholder = '终点（留空=结尾）';
  t1.title = '留空表示从起点一直裁到视频结尾';
  const note = document.createElement('span');
  note.className = 'trim-note';

  // 点进度条时把当前时间填进起点，省得手打
  video.addEventListener('timeupdate', () => {
    if (!t0.value) t0.placeholder = '起点 ' + fmtDuration(video.currentTime);
  });

  const tools = document.createElement('div');
  tools.className = 'video-tools';
  const mkBtn = (label, handler, variant) => {
    const b = document.createElement('button');
    b.className = 'btn ' + (variant || 'text');
    b.textContent = label;
    b.onclick = handler;
    return b;
  };
  const snap = () => { t0.value = fmtDuration(video.currentTime); };
  const btnFast = mkBtn('快速裁剪', () => doTrim(false), '');
  const btnPrecise = mkBtn('精确裁剪', () => doTrim(true), 'tonal');

  const doTrim = async (precise) => {
    note.textContent = '';
    const start = t0.value.trim();
    const end = t1.value.trim();
    if (!start) { note.textContent = '请填起点'; return; }
    note.textContent = '裁剪中…';
    // 一次 ffmpeg 要跑几秒到几十秒，期间不能让人再点一次并发跑第二份
    btnFast.disabled = true;
    btnPrecise.disabled = true;
    try {
      const r = await api.trimVideo(path, start, end, precise);
      note.textContent = '完成：' + r.output.split(/[\\/]/).pop() + '（' + (r.size / 1048576).toFixed(1) + ' MB）'
        + (precise ? '' : '　⚠ 快速裁剪会对齐到关键帧，实际起止可能和填写的时间有偏差；要精确请用「精确裁剪」');
    } catch (err) {
      note.textContent = '失败：' + err.message;
    } finally {
      btnFast.disabled = false;
      btnPrecise.disabled = false;
    }
  };

  tools.append(
    mkBtn('取当前为起点', snap),
    t0, t1,
    btnFast,
    btnPrecise,
    note);

  const frame = document.createElement('div');
  frame.className = 'video-frame';
  frame.append(title, close, video, tools);
  host.append(frame);
  video.play().catch(() => {});
}
