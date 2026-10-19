/** 四个主页面，导航结构对齐 Kazumi 的 推荐 / 时间表 / 追番 / 我的。 */
import { api } from './api.js';
import { playQueue, playFile, playNextItem, player, openVideo } from './player.js';
import { icon } from './icons.js';
import {
  h, clear, card, button, chip, switchRow, field, pill, toast, dialog,
  escapeHtml, fmtSize, fmtDuration, fmtCount, fmtDate, busy, emptyState, mount } from './ui.js';
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

  mount(page, 
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '解析'),
      h('div', { class: 'spacer' }),
      store.config ? pill(store.config.logged_in ? '已登录 · 可解锁高画质' : '未登录 · 最高 720P',
        store.config.logged_in ? 'ok' : 'mute') : null,
      buildQuitButton()),
    h('p', { class: 'page-sub' },
      '粘贴视频链接或 App 分享文案即可。程序会自动识别平台，并列出真实可下载的档位。'
      + '音乐平台请到左边的「音乐」页处理。'),
    buildSearchCard(),
  );

  if (store.parsed) {
    mount(page, buildVideoCard());
    mount(page, buildDownloadCard());
    mount(page, buildStreamCard());
  } else {
    mount(page, h('div', { style: { marginTop: '12px' } },
      emptyState('search', '还没有解析任何视频',
        '在上面的输入框里粘贴一个视频链接或分享文案，然后点「解析」。')));
  }

  // 通道体检并进解析页了 —— 它本来就是"这个视频能用哪些通道"，和最上面那张卡片是一回事
  const probe = buildProbeSection();
  if (probe) mount(page, probe);

  mount(view, page);
}

/* ------------------------------------------------------------ 本地文件转换 */

/** 把音乐平台的加密文件解密、再转成用户选的通用格式。
    浏览器拿不到真实路径，所以走系统文件对话框，整条链路都留在本机。 */
function buildMusicSearchCard() {
  // 用多行输入：一行一个地址，可以一次贴一整张歌单的所有歌曲链接
  const input = h('textarea', {
    class: 'input',
    rows: 3,
    style: { resize: 'vertical', minHeight: '72px', fontFamily: 'inherit' },
    placeholder: '一行一个音乐链接，例如：\nmusic.163.com/song?id=xxx\ny.qq.com/n/ryqq/songDetail/xxx',
    oninput: (e) => { store.musicInput = e.target.value; },
  });
  input.value = store.musicInput || '';

  const lines = () => (store.musicInput || '').split('\n').map((s) => s.trim()).filter(Boolean);

  const status = h('div', { class: 'card-desc', style: { marginTop: '8px' } });
  const paint = () => {
    const n = lines().length;
    mount(clear(status), n > 1 ? ('共 ' + n + ' 个地址，可以一次全部下载') : '');
  };
  input.addEventListener('input', paint);
  paint();

  return card(
    h('div', { class: 'card-title', html: icon('headphones', 'outlined', 20) + '<span>音乐地址</span>' }),
    h('div', { class: 'card-desc' },
      '支持：' + ((store.config?.sites || []).filter((s) => s.media === 'music').map((s) => s.name).join(' · ')
        || '网易云音乐 · QQ音乐 · 酷狗音乐 · 酷我音乐')),
    h('div', { style: { marginTop: '10px' } }, input),
    // 歌单支持情况说明：放在真正点「解析歌单」的地方
    h('div', { class: 'hint-box', style: { marginTop: '10px' } },
      h('span', { html: icon('info', 'outlined', 20) }),
      h('div', {},
        h('b', {}, '歌单支持情况：'),
        h('div', {}, '✅ 网易云、QQ音乐 —— 可直接解析整张歌单'),
        h('div', {}, '❌ 酷我 —— 老接口只剩元数据（能读出歌单名和曲目数），歌曲列表已被移除；新接口有反爬'),
        h('div', {}, '❌ 酷狗 —— 接口需要 md5 签名，未接入'),
        h('div', {}, '❌ Spotify —— API 需要 OAuth，且本身不提供音频流，可用下面的「换源搜索」'),
        h('div', { style: { marginTop: '6px', opacity: '.85' } },
          '上面三家单曲链接仍可尝试直接解析下载，只是「整张歌单」用不了。'))),
    h('div', { class: 'btn-row', style: { marginTop: '10px' } },
      button('解析第一个', { variant: 'tonal', iconName: 'search', onClick: () => {
        const first = lines()[0];
        if (!first) { toast('请先粘贴音乐链接'); return; }
        store.musicInput = first;           // 只解析第一条
        runMusicParse();
      } }),
      button('解析歌单', { variant: 'tonal', iconName: 'music_note', onClick: async (e) => {
        const raw = (store.musicInput || '').trim().split('\n').map((s) => s.trim()).filter(Boolean)[0] || '';
        if (!raw) { toast('请先粘贴歌单链接'); return; }
        const btn = e.currentTarget; btn.disabled = true;
        mount(clear(status), busy('正在解析歌单…'));
        try {
          const pl = await api.playlistFetch(raw);
          playlistState.data = pl;
          mount(clear(status), 
            pill('《' + pl.title + '》' + pl.count + ' 首', 'ok'),
            '(' + (pl.songs || []).slice(0, 3).map((s) => s.name).join('、') + '…)');
          renderMusic(document.getElementById('view'));
          setTimeout(() => {
            const host = document.getElementById('view');
            const box = host?.querySelector('#playlistResult');
            if (box) box.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }, 120);
        } catch (err) {
          mount(clear(status), h('div', { class: 'hint-box' },
            h('span', { html: icon('error_outline', 'outlined', 20) }), h('div', {}, err.message)));
        }
        btn.disabled = false;
      } }),
      button('全部下载', { iconName: 'download', onClick: async (e) => {
        const items = lines();
        if (!items.length) { toast('请先粘贴音乐链接'); return; }
        const btn = e.currentTarget; btn.disabled = true;
        mount(clear(status), busy('正在批量创建下载任务…'));
        let ok = 0, bad = 0;
        for (const item of items) {
          try {
            await api.submit({ input: item, engine: 'auto', qn: 0, mode: 'auto' });
            ok += 1;
          } catch (err) {
            bad += 1;
          }
        }
        btn.disabled = false;
        mount(clear(status), pill('已创建 ' + ok + ' 个任务' + (bad ? '，' + bad + ' 个失败' : ''), bad ? 'warn' : 'ok'));
        document.dispatchEvent(new CustomEvent('tasks-changed'));
      } })),
    status,
    (playlistState.data ? h('div', { id: 'playlistResult', style: { marginTop: '14px' } },
      pill('《' + playlistState.data.title + '》' + playlistState.data.count + ' 首', 'ok'),
      h('div', { class: 'btn-row', style: { marginTop: '10px' } },
        button('全部加入下载', { iconName: 'download', onClick: async (e) => {
          const btn = e.currentTarget; btn.disabled = true;
          let ok = 0, bad = 0;
          for (const s of (playlistState.data.songs || [])) {
            try { await api.submit({ input: s.url, engine: 'auto', qn: 0, mode: 'auto' }); ok += 1; }
            catch (err) { bad += 1; }
          }
          btn.disabled = false;
          toast('已创建 ' + ok + ' 个任务' + (bad ? '，' + bad + ' 个失败' : ''), 5000);
          document.dispatchEvent(new CustomEvent('tasks-changed'));
        } }),
        button('保存为本地歌单', { variant: 'tonal', iconName: 'save', onClick: async () => {
          try {
            await api.playlistSave(playlistState.data);
            playlistState.local = null;
            toast('已保存到本地歌单');
            renderMusic(document.getElementById('view'));
          } catch (err) { toast('保存失败：' + err.message); }
        } })),
      h('div', { style: { maxHeight: '200px', overflow: 'auto', marginTop: '10px' } },
        ...(playlistState.data.songs || []).map((s, i) => h('div', {
          style: { padding: '3px 0', borderBottom: '1px solid var(--md-outline-variant)', fontSize: '13px' },
        }, (i + 1) + '. ' + s.name + ' — ' + s.artist)))
    ) : null),
  );
}

async function runMusicParse() {
  const raw = (store.musicInput || '').trim();
  if (!raw) { toast('请先粘贴音乐链接'); return; }
  // 复用解析页那套流程：它会把结果按媒体类型渲染回音乐页
  store.input = raw;
  const view = document.getElementById('view');
  const btn = view.querySelector('.search-field .btn');
  const original = btn?.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = ''; mount(btn, busy('解析中')); }
  try {
    store.parsed = await api.parse(raw, 'auto');
    if (!store.parsed?.info) throw new Error('接口没有返回信息');
    if (store.parsed.info.media !== 'music') toast('这个链接不是音乐平台的内容，已按视频处理');
    store.page = 1; store.selectedPages = new Set([1]); store.qn = 0; store.play = null;
    (store.parsed.info.media === 'music' ? renderMusic : renderHome)(view);
    loadStreams();
  } catch (error) {
    toast('解析失败：' + error.message.split('\n')[0], 5200);
    if (btn && original) { btn.disabled = false; btn.innerHTML = original; }
  }
}

const swapState = { items: null, q: '' };

/** 换源搜索：Spotify 这类只给元数据、不给音频流的平台，靠"按名字去别家找"来落地。 */
function buildSourceSwapCard() {
  const input = h('input', {
    class: 'input',
    placeholder: '歌名 + 歌手，例如：First Love 宇多田',
    value: swapState.q,
    oninput: (e) => { swapState.q = e.target.value; },
    onkeydown: (e) => { if (e.key === 'Enter') runSearch(); },
  });
  const holder = h('div', { style: { marginTop: '10px' } });

  async function runSearch() {
    const q = (swapState.q || '').trim();
    if (!q) { toast('请输入歌名'); return; }
    mount(clear(holder), h('div', { class: 'card-desc' }, busy('正在搜索…')));
    try {
      const r = await api.musicSearch(q, 10);
      swapState.items = r.items || [];
    } catch (err) {
      mount(clear(holder), h('div', { class: 'hint-box' },
        h('span', { html: icon('error_outline', 'outlined', 20) }), h('div', {}, err.message)));
      return;
    }
    paintResults();
  }

  function paintResults() {
    clear(holder);
    const items = swapState.items || [];
    if (!items.length) { mount(holder, h('div', { class: 'card-desc' }, '没有搜到。换个「歌名 歌手」试试。')); return; }
    mount(holder, h('div', { class: 'btn-row', style: { marginBottom: '8px' } },
      button('全部加入下载', { iconName: 'download', onClick: async (e) => {
        const btn = e.currentTarget; btn.disabled = true;
        let ok = 0;
        for (const s of items) {
          try { await api.submit({ input: s.url, engine: 'auto', qn: 0, mode: 'auto' }); ok += 1; } catch (err) {}
        }
        btn.disabled = false;
        toast('已创建 ' + ok + ' 个任务');
        document.dispatchEvent(new CustomEvent('tasks-changed'));
      } })),
      ...items.map((s, i) => h('div', {
        style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0', borderBottom: '1px solid var(--md-outline-variant)' },
      },
        h('div', { style: { flex: '1', minWidth: '0' } },
          h('div', { style: { fontSize: '13.5px' } }, (i + 1) + '. ' + s.name),
          h('div', { class: 'card-desc', style: { margin: '2px 0 0' } }, s.artist)),
        // 标出这条结果来自哪个平台 —— 换源搜索的意义就在于跨平台找
        pill(s.site_name || (s.site === 'qqmusic' ? 'QQ音乐' : s.site === 'wangyi' ? '网易云音乐' : (s.site || '未知')),
          s.site === 'wangyi' ? 'ok' : 'mute'),
        button('下载', { variant: 'text', iconName: 'download', onClick: async () => {
          try {
            await api.submit({ input: s.url, engine: 'auto', qn: 0, mode: 'auto' });
            toast('已加入下载队列');
            document.dispatchEvent(new CustomEvent('tasks-changed'));
          } catch (err) { toast('提交失败：' + err.message); }
        } }))));
  }
  if (swapState.items) paintResults();

  return card(
    h('div', { class: 'card-title', html: icon('sync', 'outlined', 20) + '<span>换源搜索（Spotify 等）</span>' }),
    h('div', { class: 'card-desc' },
      'Spotify 的接口只提供歌曲信息、**不提供音频流**，所以没法直接下载。'
      + '这里的做法是：按「歌名 + 歌手」去网易云搜同名歌曲，再从那边下载。'),
    h('div', { class: 'hint-box', style: { marginTop: '8px' } },
      h('span', { html: icon('info', 'outlined', 20) }),
      h('div', {},
        '当前这台机器访问 open.spotify.com 会超时，所以连 Spotify 的歌名也读不到。'
        + '要支持「粘贴 Spotify 链接自动换源」，需要能连通 Spotify 的网络环境。')),
    h('div', { class: 'search-field', style: { marginTop: '10px' } }, input,
      button('搜索', { iconName: 'search', onClick: runSearch })),
    holder,
  );
}

const libraryState = { items: null, dir: '' };

function buildMusicLibraryCard() {
  const holder = h('div', {});
  const paint = () => {
    clear(holder);
    if (!libraryState.items) {
      api.musicList().then((r) => { libraryState.items = r.items || []; libraryState.dir = r.dir || ''; paint(); })
        .catch(() => { libraryState.items = []; paint(); });
      mount(holder, h('div', { class: 'card-desc' }, busy('正在扫描音乐目录…')));
      return;
    }
    if (!libraryState.items.length) {
      mount(holder, emptyState('music_note', '音乐目录里还没有文件',
        '下载、转码或从视频提取的音轨会出现在这里，也可以直接导入本地歌曲。'),
        h('div', { class: 'btn-row', style: { justifyContent: 'center', marginTop: '10px' } },
          button('导入本地歌曲', { variant: 'tonal', iconName: 'upload', onClick: async () => {
            try {
              const picked = await api.pickLocalFiles('music');
              if (picked.cancelled) return;
              const r = await api.importMusic(picked.paths || []);
              toast(r.done.length ? '已导入 ' + r.done.length + ' 首' : '导入失败', 4000);
              libraryState.items = null;
              paint();
            } catch (err) { toast('导入失败：' + err.message); }
          } })));
      return;
    }
    const allItems = libraryState.items;
    // 默认收起，只露最近 2 首；点「展开全部」才展开
    const expanded = store.libExpand;
    // 展开后才有搜索框和过滤
    const query = expanded ? (store.libQuery || '').trim().toLowerCase() : '';
    const matched = query
      ? allItems.filter((x) => ((x.title || x.name) + ' ' + (x.artist || '')).toLowerCase().includes(query))
      : allItems;
    const shown = expanded ? matched : allItems.slice(0, 2);
    const list = shown.map((item, i) => h('div', {
      style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0', borderBottom: '1px solid var(--md-outline-variant)' },
    },
      h('div', { style: { flex: '1', minWidth: '0' } },
        // 有 ID3 标签就用标签里的曲名，没有再退回文件名
        h('div', { style: { fontSize: '13.5px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
          item.title || item.name),
        // 曲名下面小字：作者 · 大小（有专辑就带上专辑）
        h('div', { style: { fontSize: '11.5px', opacity: '.65', margin: '2px 0 0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
          (item.artist ? item.artist : (item.folder || '未知作者'))
            + (item.album ? ' · ' + item.album : '')
            + ' · ' + fmtSize(item.size))),
      button('加入歌单', { variant: 'text', iconName: 'add', title: '把这首歌加进某个歌单', onClick: async (ev) => {
        ev.stopPropagation();
        try {
        let lists = [];
        try { lists = (await api.playlistList()).playlists || []; } catch (err) { toast('读取歌单失败：' + err.message); return; }
        if (!lists.length) { toast('还没有歌单，先去「我的歌单」新建一个', 4500); return; }
        const picked = { value: null };
        const rows = lists.map((pl) => h('label', {
          style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 0', fontSize: '13px' },
        },
          h('input', { type: 'radio', name: 'pl', onchange: () => { picked.value = pl; } }),
          h('span', { style: { flex: '1' } }, pl.title),
          h('span', { class: 'card-desc', style: { margin: '0' } }, (pl.songs || []).length + ' 首')));
        const yes = await dialog({ title: '加入哪个歌单？', body: h('div', { style: { maxHeight: '300px', overflow: 'auto' } }, ...rows), confirm: '加入' });
        if (!yes || !picked.value) return;
        try {
          const r = await api.playlistAddSong(picked.value.site, picked.value.id, {
            id: item.path, name: item.name, artist: item.artist || '本地文件',
            url: 'file:///' + String(item.path).replace(/\\/g, '/'),
            cover: item.cover || '',
          });
          toast(r.added === false
            ? '这首歌已经在《' + picked.value.title + '》里了'
            : '已加入《' + picked.value.title + '》', 4000);
          // 必须让歌单卡片重新拉一次数据：playlistState.local 是模块级缓存，
          // 不清掉的话界面上还是旧的「N 首」，用户会以为没加进去（实测就是这样）。
          playlistState.local = null;
          renderPlayer(document.getElementById('view'));
        } catch (err) { toast('加入失败：' + err.message); }
        } catch (err) {
          // 任何意外都要让用户看见，不能静默
          toast('加入歌单出错：' + (err && err.message ? err.message : String(err)), 6000);
        }
      } }),
      button('定位', { variant: 'text', iconName: 'folder_open', title: '在资源管理器里打开它所在的位置', onClick: async () => {
        try { await api.reveal(item.path); } catch (err) { toast('定位失败：' + err.message); }
      } }),
      // 图标库里没有 arrow_forward / skip_next，用 sort 表示"插进队列"，
      // 比 chevron 好 —— 后者看着像"进入下一页"。
      button('下一首', { variant: 'text', iconName: 'sort', title: '添加到下一首播放', onClick: () => {
        playNextItem({ path: item.path, name: item.name });
        toast('已添加到下一首：' + item.name);
      } }),
      button('播放', { variant: 'text', iconName: 'play_arrow', onClick: () => {
        playQueue(libraryState.items.map((x) => ({ path: x.path, name: x.name })), i);
      } })));
    mount(holder, 
      h('div', { class: 'btn-row', style: { marginBottom: '8px' } },
        button('全部播放', { variant: 'tonal', iconName: 'play_arrow', onClick: () => {
          playQueue(libraryState.items.map((x) => ({ path: x.path, name: x.name })), 0);
        } }),

        button('导入本地歌曲', { variant: 'tonal', iconName: 'upload', onClick: async () => {
          try {
            const picked = await api.pickLocalFiles('music');
            if (picked.cancelled) return;
            const r = await api.importMusic(picked.paths || []);
            toast(r.done.length
              ? '已导入 ' + r.done.length + ' 首' + (r.failed.length ? '，' + r.failed.length + ' 个失败' : '')
              : '导入失败：' + ((r.failed[0] || {}).error || '没有可导入的文件'), 5000);
            libraryState.items = null;
            paint();
          } catch (err) { toast('导入失败：' + err.message); }
        } }),
        button('打开音乐目录', { variant: 'text', iconName: 'folder_open', onClick: async () => {
          try { await api.open(libraryState.dir || undefined); } catch (err) { toast('打开失败：' + err.message); }
        } }),
        button('刷新', { variant: 'text', iconName: 'refresh', onClick: () => {
          libraryState.items = null; paint();
        } })),
      // 搜索框只在展开后出现
      expanded
        ? h('div', { class: 'search-field', style: { margin: '4px 0 8px' } },
            h('input', {
              class: 'input', placeholder: '搜索曲名或作者…', value: store.libQuery || '',
              oninput: (e) => {
                store.libQuery = e.target.value;
                // 只重画列表，保留输入焦点
                const again = e.target.selectionStart;
                paint();
                const box = document.querySelector('.search-field input');
                if (box) { box.focus(); try { box.setSelectionRange(again, again); } catch (err) {} }
              },
            }))
        : null,
      h('div', { class: 'song-scroll' }, ...list),
      allItems.length
        ? h('div', { class: 'btn-row', style: { marginTop: '8px' } },
            expanded && query
              ? h('span', { class: 'card-desc', style: { margin: '0 8px 0 0', alignSelf: 'center' } },
                  '匹配 ' + matched.length + ' / ' + allItems.length + ' 首')
              : null,
            button(expanded ? '收起' : '展开全部 ' + allItems.length + ' 首', {
              variant: 'text', iconName: expanded ? 'expand_less' : 'expand_more',
              onClick: () => { store.libExpand = !store.libExpand; paint(); },
            }))
        : null);
  };
  paint();
  return card(
    h('div', { class: 'card-title', html: icon('music_note', 'outlined', 20) + '<span>歌曲列表</span>' }),
    h('div', { class: 'card-desc' }, '扫自音乐下载目录，点「播放」即可在应用内听，支持乱序与循环。'),
    h('div', { style: { marginTop: '10px' } }, holder),
  );
}

const playlistState = { data: null, local: null, detail: null , query: null };

/** 歌单内歌曲：一行两个，带封面 + 歌名 + 作者小字。 */
function buildSongGrid(pl) {
  const all = pl.songs || [];
  const query = (playlistState.query || '').trim().toLowerCase();
  const songs = query
    ? all.filter((s) => ((s.name || '') + ' ' + (s.artist || '')).toLowerCase().includes(query))
    : all;
  const searchBox = h('div', { class: 'search-field', style: { margin: '6px 0 2px' } },
    h('input', {
      class: 'input', placeholder: '在这个歌单里搜…', value: playlistState.query || '',
      oninput: (e) => {
        playlistState.query = e.target.value;
        const pos = e.target.selectionStart;
        renderPlayer(document.getElementById('view'));
        const box = document.querySelector('.search-field input');
        if (box) { box.focus(); try { box.setSelectionRange(pos, pos); } catch (err) {} }
      },
    }));
  if (!all.length) return h('span');
  const grid = h('div', {
    style: {
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
      gap: '10px', marginTop: '8px',
    },
  });
  for (let i = 0; i < songs.length; i += 2) {
    // 一次塞两个，保证"一行两个"在这个网格里稳定成立
    for (const s of songs.slice(i, i + 2)) {
      const idx = songs.indexOf(s) + 1;
      const cover = s.cover || s.cover_url || '';
      mount(grid, h('div', {
        style: {
          display: 'flex', alignItems: 'center', gap: '10px', padding: '8px',
          borderRadius: '10px', background: 'var(--md-surface-container)',
          border: '1px solid var(--md-outline-variant)', minWidth: '0',
        },
      },
        cover
          ? h('img', { src: api.coverUrl(cover), alt: '', loading: 'lazy',
              style: { width: '40px', height: '40px', borderRadius: '8px', objectFit: 'cover', flex: 'none' } })
          : h('span', { style: { width: '40px', height: '40px', borderRadius: '8px', flex: 'none',
              display: 'grid', placeItems: 'center', background: 'var(--md-surface-container-high)',
              color: 'var(--md-on-surface-variant)' },
              html: icon('music_note', 'outlined', 20) }),
        h('div', { style: { flex: '1', minWidth: '0' } },
          h('div', { style: { fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
            idx + '. ' + (s.name || '未命名')),
          h('div', { style: { fontSize: '11.5px', opacity: '.65', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: '2px' } },
            s.artist || '未知作者')),
        button('', { variant: 'text', iconName: 'play_arrow', title: '播放', onClick: () => {
          // 歌单里存的是 file:///D:/... 或平台链接；只有本地文件能直接播
          const raw = String(s.url || '');
          if (raw.startsWith('file:///')) {
            const local = decodeURIComponent(raw.slice(8)).replace(/\//g, '\\');
            playFile(local, s.name);
          } else {
            toast('这首是平台链接，先「全部下载」到本地再播放', 4200);
          }
        } }),
        button('', { variant: 'text', iconName: 'delete', title: '从歌单移除', onClick: async () => {
          try {
            await api.playlistRemoveSong(pl.site, pl.id, s.url);
            pl.songs = pl.songs.filter((x) => x.url !== s.url);
            renderPlayer(document.getElementById('view'));
          } catch (err) { toast('移除失败：' + err.message); }
        } })));
    }
  }
  return h('div', {},
    searchBox,
    h('div', { class: 'song-scroll' },
      songs.length ? grid : h('div', { class: 'card-desc', style: { padding: '16px 0' } },
        '没有匹配「' + playlistState.query + '」的歌曲')));
}

function buildPlaylistCard() {
  const holder = h('div', {});
  const state = () => playlistState;

  const paint = () => {
    clear(holder);
    if (!playlistState.local) {
      api.playlistList()
        .then((r) => { playlistState.local = r.playlists || []; paint(); })
        .catch(() => { playlistState.local = []; paint(); });
      mount(holder, h('div', { class: 'card-desc' }, busy('正在读取本地歌单…')));
      return;
    }
    // 详情视图
    const detail = playlistState.detail;
    if (detail) {
      const pl = (playlistState.local || []).find((x) => x.id === detail.id && x.site === detail.site);
      if (!pl) { playlistState.detail = null; paint(); return; }
      mount(holder, 
        h('div', { class: 'btn-row', style: { marginBottom: '10px' } },
          button('返回歌单列表', { variant: 'text', iconName: 'arrow_back', onClick: () => {
            playlistState.detail = null; playlistState.query = null; paint();
          } }),
          button('播放全部', { variant: 'tonal', iconName: 'play_arrow', onClick: () => {
            const list = (pl.songs || []).map((s) => ({ path: s.url, name: s.name }));
            // 歌单里的歌是平台链接，不能直接播本地文件；提示去下载
            toast('歌单里存的是平台链接，先「全部下载」到本地才能在播放器里听', 5000);
          } }),
          button('添加歌曲', { variant: 'tonal', iconName: 'add', onClick: async () => {
            // 从音乐目录里挑，勾中的加进这个歌单
            let lib = [];
            try { lib = (await api.musicList()).items || []; } catch (err) { toast('读取音乐目录失败：' + err.message); return; }
            if (!lib.length) { toast('音乐目录里还没有文件，先去下载或导入', 4500); return; }
            const picked = new Set();
            const rows = lib.slice(0, 200).map((it) => h('label', {
              style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0', fontSize: '13px' },
            },
              h('input', { type: 'checkbox', onchange: (e) => { if (e.target.checked) picked.add(it); else picked.delete(it); } }),
              h('span', { style: { flex: '1', minWidth: '0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, it.name),
              h('span', { class: 'card-desc', style: { margin: '0' } }, fmtSize(it.size))));
            const box = h('div', { style: { maxHeight: '320px', overflow: 'auto' } }, ...rows);
            const yes = await dialog({ title: '从本地音乐添加', body: box, confirm: '添加到歌单' });
            if (!yes || !picked.size) return;
            let ok = 0;
            for (const it of picked) {
              try {
                await api.playlistAddSong(pl.site, pl.id, {
                  id: it.path, name: it.name, artist: '本地文件', url: 'file:///' + it.path.replace(/\\/g, '/'),
                });
                ok += 1;
              } catch (err) { /* 单首失败不影响其它 */ }
            }
            toast('已添加 ' + ok + ' 首到《' + pl.title + '》');
            playlistState.local = null;
            paint();
          } }),
          button('全部下载', { iconName: 'download', onClick: async (e) => {
            const btn = e.currentTarget; btn.disabled = true;
            let ok = 0;
            for (const s of (pl.songs || [])) {
              try { await api.submit({ input: s.url, engine: 'auto', qn: 0, mode: 'auto' }); ok += 1; } catch (err) { /* 单首失败不影响其它 */ }
            }
            btn.disabled = false;
            toast('已创建 ' + ok + ' 个下载任务');
            document.dispatchEvent(new CustomEvent('tasks-changed'));
          } })),
        h('div', { class: 'card-desc' }, '《' + pl.title + '》' + (pl.songs || []).length + ' 首'),
        (pl.songs || []).length
          ? buildSongGrid(pl)
          : h('div', { style: { marginTop: '12px' } },
              emptyState('music_note', '这个歌单还是空的',
                '到「音乐」页用「换源搜索」找到歌，或解析一张歌单后整体保存进来。')),
      );
      return;
    }

    // 列表视图
    mount(holder, 
      h('div', { class: 'btn-row', style: { marginBottom: '10px' } },
        button('新建歌单', { variant: 'tonal', iconName: 'add', onClick: async () => {
          const nameInput = h('input', { class: 'input', placeholder: '给这个歌单起个名字' });
          const yes = await dialog({ title: '新建歌单', body: nameInput, confirm: '创建' });
          const name = (nameInput.value || '').trim();
          if (!yes || !name) return;
          try {
            await api.playlistCreate(name);
            playlistState.local = null;
            paint();
            toast('已新建《' + name + '》');
          } catch (err) { toast('新建失败：' + err.message); }
        } }),
        button('去音乐页解析歌单', { variant: 'text', iconName: 'search', onClick: () => {
          document.dispatchEvent(new CustomEvent('navigate', { detail: 'music' }));
        } })));

    if (!playlistState.local.length) {
      mount(holder, emptyState('music_note', '还没有本地歌单',
        '点上面的「新建歌单」自己建一个，或到「音乐」页粘贴歌单链接解析后保存。'));
      return;
    }
    for (const pl of playlistState.local) {
      mount(holder, h('div', {
        style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: '1px solid var(--md-outline-variant)', cursor: 'pointer' },
        onclick: (e) => { if (e.target.closest('button')) return; playlistState.query = null; playlistState.detail = { id: pl.id, site: pl.site }; paint(); },
      },
        h('div', { style: { flex: '1', minWidth: '0' } },
          h('b', {}, pl.title),
          h('div', { class: 'card-desc', style: { margin: '2px 0 0' } },
            (pl.songs || []).length + ' 首' + (pl.site === 'local' ? ' · 自建' : ''))),
        button('打开', { variant: 'text', iconName: 'chevron_right', onClick: () => {
          playlistState.detail = { id: pl.id, site: pl.site }; paint();
        } }),
        button('删除', { variant: 'text', iconName: 'delete', onClick: async () => {
          const yes = await dialog({ title: '删除这个歌单？', body: '只删除这份收藏，已下载的文件不受影响。', confirm: '删除', danger: true });
          if (!yes) return;
          await api.playlistRemove(pl.site, pl.id);
          playlistState.local = (playlistState.local || []).filter((x) => x !== pl);
          paint();
        } })));
    }
  };
  paint();

  return card(
    h('div', { class: 'card-title', html: icon('music_note', 'outlined', 20) + '<span>我的歌单</span>' }),
    h('div', { class: 'card-desc' }, '可以自己新建歌单，也可以把解析到的歌单保存下来。点歌单进入详情管理歌曲。'),
    h('div', { style: { marginTop: '10px' } }, holder),
  );
}

function buildLocalCard() {
  store.localFiles = store.localFiles || [];
  const list = h('div', { class: 'local-list' });
  // 记住用户选的输出格式：卡片会因为「文件列表变化 / 格式列表异步返回」重建，
  // 不记的话每次重建都会被重置回"保持原格式"。
  const targetSelect = h('select', {
    class: 'select',
    onchange: (event) => { store.localTarget = event.target.value; },
  });
  const status = h('div', { class: 'card-desc', style: { marginTop: '8px' } });
  let formats = store.localFormats;

  const renderList = () => {
    clear(list);
    if (!store.localFiles.length) {
      mount(list, h('div', { class: 'hint' }, '还没有选择文件。支持 .ncm / .qmc* / .mflac / .mgg / .kgm / .vpr / .kwm，也接受普通音频。'));
      return;
    }
    for (const item of store.localFiles) {
      const tag = item.encrypted
        ? pill('加密 · ' + String(item.kind || '').toUpperCase(), 'warn')
        : pill('通用格式', 'mute');
      mount(list, h('div', { class: 'local-row' },
        h('div', { class: 'grow' },
          h('div', { class: 'name' }, item.name),
          h('div', { class: 'hint' }, fmtSize(item.size) + (item.error ? ' · ' + item.error : ''))),
        tag));
    }
  };

  const renderTargets = () => {
    clear(targetSelect);
    if (!formats) {
      mount(targetSelect, h('option', { value: 'copy' }, '保持原格式（正在检测…）'));
      return;
    }
    for (const t of formats.targets || []) {
      mount(targetSelect, h('option', { value: t.id },
        t.label + (t.needs_ffmpeg && !formats.ffmpeg ? '（需要 ffmpeg）' : '')));
    }
    targetSelect.value = store.localTarget || 'copy';
  };

  const pick = async () => {
    try {
      const result = await api.pickLocalFiles('music');
      if (!result.count) { toast('没有选择文件'); return; }
      const scanned = await api.scanLocalFiles(result.paths);
      store.localFiles = scanned.items || [];
      const enc = store.localFiles.filter((i) => i.encrypted).length;
      toast('已选择 ' + store.localFiles.length + ' 个文件' + (enc ? '（其中 ' + enc + ' 个需要解密）' : ''));
      renderList();
    } catch (error) {
      mount(clear(status), '选择文件失败：' + error.message);
    }
  };

  const start = async () => {
    const paths = store.localFiles.map((i) => i.path);
    if (!paths.length) { toast('请先选择文件'); return; }
    try {
      store.localTarget = targetSelect.value;
      const result = await api.convertLocalFiles(paths, targetSelect.value);
      toast('已建立 ' + result.count + ' 个转换任务，进度见「历史记录」');
      store.localFiles = [];
      renderList();
    } catch (error) {
      mount(clear(status), '提交失败：' + error.message);
    }
  };

  renderList();
  renderTargets();
  if (!formats) {
    api.localFormats().then((data) => { formats = data; store.localFormats = data; renderTargets(); }).catch(() => {});
  }

  return card(
    h('div', { class: 'card-title', html: icon('music_note', 'outlined', 20) + '<span>本地文件转换</span>' }),
    h('div', { class: 'card-desc' },
      '把各音乐平台下载到的加密文件（.ncm / .qmc* / .mflac / .mgg / .kgm / .vpr / .kwm）' +
      '解密成通用音频，再按你选的格式输出。整个过程都在本机完成，文件不会上传到任何地方。'),
    h('div', { class: 'search-field' },
      h('div', { class: 'grow', id: 'localPathHint' },
        h('div', { class: 'hint' }, '点击右侧按钮选择文件（可多选）')),
      button('选择文件', { variant: 'tonal', iconName: 'folder_open', onClick: pick })),
    list,
    h('div', { class: 'section-label' }, '输出格式'),
    h('div', { class: 'select-wrap' }, targetSelect),
    status,
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('开始转换', { iconName: 'download', onClick: start })),
  );
}

function buildSearchCard() {
  const input = h('input', {
    class: 'input',
    placeholder: '粘贴视频链接或分享文案，例如 BV1GJ411x7h7 / v.douyin.com/xxx / xhslink.com/xxx',
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
    h('div', { class: 'card-desc', id: 'platformHint' }, platformHintText()),
    h('div', { class: 'search-field' }, input, runBtn),
    h('div', { style: { marginTop: '14px', display: 'flex', gap: '12px', alignItems: 'flex-end', flexWrap: 'wrap' } },
      h('div', { style: { minWidth: '260px', flex: '1' } },
        field('抓取通道', h('div', { class: 'select-wrap' }, engineSelect))),
      // 体检区现在就在本页下方，直接把结果跑出来就行，不用再跳页面
      button('全部通道体检', { variant: 'outlined', iconName: 'science', onClick: () => {
        store.probeInput = store.input;
        runProbe();
      } })),
  );
  return card1;
}

/** 各平台支持的输入形态，直接从注册表生成，避免文案和实现脱节。 */
function platformHintText() {
  const sites = store.config?.sites || [];
  if (!sites.length) return '正在加载支持的平台…';
  // 音乐平台有自己的页面，这里只列视频平台，免得两处重复
  const video = sites.filter((s) => s.media !== 'music');
  return '支持：' + (video.length ? video : sites).map((s) => s.name).join(' · ');
}

function siteOf(id) {
  return (store.config?.sites || []).find((s) => s.id === id) || null;
}

function fillEngineOptions(select, current) {
  clear(select);
  mount(select, h('option', { value: 'auto' }, '自动（按优先级依次尝试，选实际画质最高的）'));
  for (const engine of store.config?.engines || []) {
    const suffix = engine.available ? '' : '（当前不可用）';
    mount(select, h('option', { value: engine.id }, engine.name + suffix));
  }
  select.value = current || 'auto';
}

async function runParse() {
  const input = (store.input || '').trim();
  if (!input) { toast('请先粘贴视频链接或分享文案'); return; }
  const view = document.getElementById('view');
  const btn = view.querySelector('.search-field .btn');
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '';
  mount(btn, busy('解析中'));

  try {
    store.parsed = await api.parse(input, store.engine);   // { info, ref, engine, engine_name }
    if (!store.parsed?.info) throw new Error('接口没有返回视频信息');
    store.page = 1;
    store.selectedPages = new Set([1]);
    store.qn = 0;
    store.play = null;
    // 音乐结果回音乐页，视频结果回解析页 —— 两个页面都能发起解析
    const backToMusic = store.parsed.info.media === 'music';
    if (backToMusic) {
      // 在解析页粘了音乐链接：把这条链接原样带到音乐页，别让用户再粘一次
      store.musicInput = input;
      toast('这是音乐平台的链接，已跳到「音乐」页', 3600);
    }
    (backToMusic ? renderMusic : renderHome)(view);
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
  if (host) { mount(clear(host), h('div', { class: 'card-desc' }, busy('正在取流…'))); }
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
      mount(clear(host), h('div', { class: 'hint-box' },
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
    info.cover ? h('img', { src: api.coverUrl(info.cover, siteOf(info.site)), alt: '', loading: 'lazy' }) : null,
    info.duration ? h('span', { class: 'duration' }, fmtDuration(info.duration)) : null);

  const isMusic = info.media === 'music';
  const metaBox = h('div', { class: 'video-meta' },
    h('h2', { class: 'video-title' }, info.title || '(无标题)'),
    h('div', { class: 'video-up' },
      h('span', { html: icon(isMusic ? 'music_note' : 'verified', 'outlined', 16) }),
      meta.name || (isMusic ? '未知歌手' : '未知 UP 主'),
      h('span', { style: { opacity: '.6' } },
        '· ' + (isMusic ? (info.album || '单曲') : (info.tname || '未分类'))
        + ' · ' + fmtDate(info.pubdate))),
    h('div', { class: 'video-desc' }, info.desc || (isMusic ? '（没有简介）' : '（没有简介）')),
    h('div', { class: 'stat-row' },
      ...stats.map(([label, value]) =>
        h('div', {}, label + ' ', h('b', {}, fmtCount(value))))),
    h('div', { style: { marginTop: '12px', display: 'flex', gap: '8px', flexWrap: 'wrap' } },
      (() => {
        const s = siteOf(info.site) || siteOf('bilibili');
        return s ? pill(s.name) : null;
      })(),
      // 只有 B 站才有"通道"这个概念；其它平台这条信息没有意义
      (isBilibili() && store.parsed.engine_name)
        ? pill('通道：' + store.parsed.engine_name) : null,
      info.bvid ? pill(info.bvid, 'mute') : null,
      store.play ? pill(streamLabel(store.play),
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
    mount(list, row);
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
    mount(modeRow, chip(mode.name, {
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
    mount(qualityRow, h('span', { class: 'card-desc', style: { margin: '0' } }, '正在获取可用画质…'));
  } else {
    if (!actual.length) {
      mount(qualityRow, h('span', { class: 'card-desc', style: { margin: '0' } }, '当前通道没有报告画质档位'));
    }
    const isBilibili = (store.parsed?.info?.site || 'bilibili') === 'bilibili';
    // B 站的 qn 有自己的中文名；其它平台的 qn 是我们折算出来的占位值，
    // 直接显示反而会出现"qn0"这种东西，所以用流自己的描述。
    const labelOf = (qn, index) => {
      if (isBilibili) return qualityName(qn);
      const desc = (play.accept_desc || [])[index];
      if (desc) return String(desc);
      const stream = (play.videos || []).find((v) => v.qn === qn);
      if (stream && stream.resolution && stream.resolution !== '-') return stream.resolution;
      return play.durl.length ? '原始画质' : qualityName(qn);
    };
    actual.forEach((qn, index) => {
      const stream = (play.videos || []).find((v) => v.qn === qn);
      const tag = stream ? stream.resolution : (play.durl.length ? '单文件' : '');
      mount(qualityRow, chip(labelOf(qn, index), {
        active: store.qn === qn,
        tag,
        onClick: () => {
          store.qn = qn;
          renderHome(document.getElementById('view'));
          loadStreams();
        },
      }));
    });
    for (const qn of phantom) {
      mount(qualityRow, chip(qualityName(qn), {
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
    mount(codecSelect, h('option', { value: item.id }, item.name));
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
    h('div', { class: 'card-desc' },
      (store.parsed?.info?.media === 'music')
        ? '音质列表以「本次实际下发到的流」为准 —— 拿不到的档位（需要登录或会员）会标成虚线并说明原因。'
        : '清晰度列表以「本次实际下发到的流」为准 —— 各平台接口都可能虚报档位，这里只显示真实拿得到的。'),

    h('div', { class: 'section-label' }, '下载模式'),
    modeRow,

    h('div', { class: 'section-label' },
      store.parsed?.info?.media === 'music' ? '音质与格式' : '清晰度'),
    qualityRow,

    // 抓取通道是 B 站专属概念：其它平台只有一条官方接口，摆一个选择框只会让人困惑
    h('div', { class: 'section-label' }, isBilibili() ? '抓取通道与编码' : '编码'),
    h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '12px' } },
      isBilibili() ? h('div', { class: 'select-wrap' }, engineSelect) : null,
      h('div', { class: 'select-wrap' }, codecSelect)),

    streamHost,
    h('div', { class: 'btn-row', style: { marginTop: '16px' } },
      startBtn,
      button(store.parsed?.info?.media === 'music' ? '打开音乐目录' : '打开下载目录',
        { variant: 'text', iconName: 'folder_open', onClick: () => {
          // 音乐和视频分开放，这里要打开对应的那个
          const music = store.parsed?.info?.media === 'music';
          api.open(music ? (store.config?.settings?.music_dir || '') : '');
        } })),
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
  // 「单文件 / 第 N 段 / 已封装音轨」是 B 站 durl 的概念，其它平台这样说没人看得懂
  const onBili = store.parsed?.ref?.site ? store.parsed.ref.site === 'bilibili' : isBilibili();
  for (const d of play.durl || []) {
    rows.push(onBili
      ? ['单文件', '第 ' + d.order + ' 段', '已封装音轨', '—', d.size_text,
         d.order === 1 ? '免合并' : '']
      : [play.media === 'music' ? '音频' : '视频', d.order > 1 ? '第 ' + d.order + ' 段' : '完整文件',
         '—', '—', d.size_text, '']);
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
          h('div', {}, (() => {
            const list = play.phantom_quality.map((q) => qualityLabel(q)).join('、');
            // 解锁方式每个平台都不一样：B 站是 SESSDATA，网易云是 MUSIC_U，
            // QQ 音乐/酷狗/酷我是各自的 Cookie。写死 SESSDATA 会让其它平台莫名其妙。
            const key = {
              bilibili: 'SESSDATA', wangyi: 'MUSIC_U', qqmusic: 'Cookie',
              kugou: 'Cookie', kuwo: 'Cookie',
            }[store.parsed?.ref?.site] || '登录凭据';
            const where = siteOf(store.parsed?.ref?.site)?.name || '该平台';
            return '接口声称还支持 ' + list + '，但本次一个都没有下发。'
              + '这些档位需要登录（或会员）才能拿到，在「设置 → 平台登录」里填入 '
              + where + ' 的 ' + key + ' 就能解锁。';
          })()))
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

/* ---------------- 平台识别：让界面按平台说人话 ---------------- */

function siteHosts(site) {
  const urls = [site?.home, ...(site?.examples || [])];
  return urls.map((u) => {
    try { return new URL(u).hostname.replace(/^www\./, '').toLowerCase(); } catch { return ''; }
  }).filter(Boolean);
}

/** 从输入文本判断是哪个平台；判断不出来返回 null。 */
export function detectSite(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  let host = '';
  try { host = new URL(raw).hostname.replace(/^www\./, '').toLowerCase(); } catch { host = ''; }
  const sites = store.config?.sites || [];
  if (!host) {
    // BV 号 / av 号 / 纯数字分P：一定是 B 站
    if (/^(BV[0-9A-Za-z]{10}|av\d+)$/i.test(raw)) return 'bilibili';
    return null;
  }
  // b23.tv 之类的短链归到对应平台
  for (const s of sites) {
    if (siteHosts(s).some((x) => host === x || host.endsWith('.' + x))) return s.id;
  }
  return null;
}

/** 流的显示名。B 站说 DASH/durl 那套，其它平台说人话。 */
export function streamLabel(play) {
  if (!play) return null;
  if (play.media === 'music') return String(play.ext || 'mp3').toUpperCase() + ' 音频';
  if (!isBilibili()) {
    return play.is_dash ? '视频流 + 音频流' : '音视频合一的文件';
  }
  return play.is_dash ? 'DASH 音视频分离' : '单文件 MP4';
}

/** 当前界面语境下是哪个平台。 */
export function currentSite() {
  return store.parsed?.ref?.site || detectSite(store.probeInput || store.input) || 'bilibili';
}

export function isBilibili() {
  return currentSite() === 'bilibili';
}

/** 通道体检区。合并进解析页之后，非 B 站直接不显示（那 10 条通道是 B 站专属）。 */
function buildProbeSection() {
  const input = h('input', {
    class: 'input', placeholder: 'B 站 BV 号或视频链接', value: store.probeInput || store.input || '',
    oninput: (e) => { store.probeInput = e.target.value; },
    onkeydown: (e) => { if (e.key === 'Enter') runProbe(); },
  });

  // 合并进解析页之后，只有 B 站才显示这块 —— 那 10 条通道是 B 站专属的
  if (!isBilibili()) return null;

  const wrap = h('div', { style: { marginTop: '16px' } },
    h('div', { class: 'section-label' }, '通道体检'),
    card(
      h('div', { class: 'card-title', html: icon('science', 'outlined', 20) + '<span>这个视频能用哪些通道</span>' }),
      h('div', { class: 'card-desc' },
        '把同一个视频同时交给全部抓取通道，逐条报告真实结果。B 站的风控和接口策略经常变，' +
        '这里显示的是「此刻这台机器上」的实测可用性。体检会并发请求，约需 10～30 秒。'),
      h('div', { class: 'search-field' }, input,
        button('开始体检', { iconName: 'bolt', onClick: () => runProbe() })),
    ));

  if (store.probe) mount(wrap, buildProbeResult());
  return wrap;
}

async function runProbe() {
  const input = (store.probeInput || store.input || '').trim();
  if (!input) { toast('请先填写 B 站视频地址或 BV 号'); return; }
  const view = document.getElementById('view');
  renderHome(view);
  const host = view.querySelector('.page');
  const holder = h('div', { class: 'card', style: { marginTop: '8px' } },
    h('div', { class: 'card-desc' }, busy('正在把所有通道跑一遍，请稍候…')));
  mount(host, holder);
  try {
    store.probe = await api.probe(input, 0);
  } catch (error) {
    mount(clear(holder), h('div', { class: 'hint-box' },
      h('span', { html: icon('error_outline', 'outlined', 20) }),
      h('div', {}, '体检失败：' + error.message)));
    return;
  }
  renderHome(document.getElementById('view'));
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

  mount(page, 
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '历史记录'),
      h('div', { class: 'spacer' }),
      // 直接打开下载目录，不用先回解析页
      button('打开下载目录', { variant: 'text', iconName: 'folder_open', onClick: () => api.open() }),
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
    mount(page, emptyState('cloud_download', '还没有任何下载记录',
      '在「解析」页解析一个视频，然后点「开始下载」，任务就会出现在这里。'));
  } else {
    // 歌曲和视频分开列，各自折叠：默认只展开最近 2 条，其余点「显示全部」再看
    // 分类以**产物的真实后缀**为准，站点只作兜底。
    // 之前只看 site，于是从音乐站下的视频（或被旧版本标成 audio 的记录）会串到「歌曲」里。
    const VIDEO_EXT = /\.(mp4|mkv|webm|flv|ts|m4v|mov|avi|wmv)$/i;
    const AUDIO_EXT = /\.(mp3|flac|m4a|aac|wav|ogg|opus|wma)$/i;
    const isMusic = (t) => {
      const files = (t.files || []).map((f) => String(f.path || ""));
      if (files.some((p) => VIDEO_EXT.test(p))) return false;      // 有视频产物就是视频
      if (files.some((p) => AUDIO_EXT.test(p))) return true;       // 全是音频才是音乐
      // 没有产物（失败/取消/排队中）：退一步看输入本身
      const src = String(t.raw_input || t.input || "").split("?")[0];
      if (VIDEO_EXT.test(src)) return false;
      if (AUDIO_EXT.test(src)) return true;
      return MUSIC_SITE_IDS.has(t.site);
    };
    mount(page, buildTaskGroup('歌曲', tasks.filter(isMusic), 'music'));
    mount(page, buildTaskGroup('视频', tasks.filter((t) => !isMusic(t)), 'video'));
  }
  mount(view, page);
}

// 网易云适配器的真实 id 是 netease；早期歌单存储里写成了 wangyi，
// 两个都要认，否则已保存的老歌单会被漏掉。
const MUSIC_SITE_IDS = new Set(['netease', 'wangyi', 'qqmusic', 'kugou', 'kuwo']);

function buildTaskGroup(title, list, key) {
  if (!list.length) return h('span');
  // 进行中的任务永远显示，不参与折叠
  const active = list.filter((t) => ['queued', 'running', 'merging'].includes(t.status));
  const rest = list.filter((t) => !active.includes(t));
  const expanded = store.taskExpand && store.taskExpand[key];
  const shown = expanded ? rest : rest.slice(0, 2);

  const grid = h('div', { class: 'grid' });
  for (const t of [...active, ...shown]) mount(grid, buildTaskCard(t));

  return card(
    h('div', { class: 'card-title',
      html: icon(key === 'music' ? 'music_note' : 'movie', 'outlined', 20)
        + '<span>' + title + '（' + list.length + '）</span>' }),
    grid,
    rest.length > 2
      ? h('div', { class: 'btn-row', style: { marginTop: '10px' } },
          button(expanded ? '收起' : '显示全部 ' + rest.length + ' 条', {
            variant: 'text', iconName: expanded ? 'expand_less' : 'expand_more',
            onClick: () => {
              store.taskExpand = store.taskExpand || {};
              store.taskExpand[key] = !expanded;
              renderTasks(document.getElementById('view'));
            },
          }))
      : null,
  );
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
  // 失败/取消的任务进度条没有意义，别再让它动
  const stopped = task.status === 'error' || task.status === 'cancelled';
  const determinate = task.has_total || finished || stopped;
  const progress = stopped
    ? h('div', { class: 'progress stopped' },
        h('i', { style: { width: (task.status === 'error' ? (task.has_total ? percent : 100) : (task.has_total ? percent : 0)) + '%' } }))
    : h('div', { class: 'progress' + (determinate ? '' : ' indeterminate') },
        h('i', { style: { width: (finished ? 100 : task.has_total ? percent : 35) + '%' } }));

  const foot = h('div', { class: 'task-foot' },
    stopped
      ? h('span', {}, task.status === 'error' ? '已中止' : '已取消')
      : (task.has_total ? h('span', {}, percent.toFixed(1) + '%') : h('span', {}, task.progress?.phase || '准备中')),
    task.has_total ? h('span', {}, task.size_text + ' / ' + task.total_text) : null,
    h('span', {}, task.speed_text || '-'),
    active && task.has_total ? h('span', {}, '剩余 ' + (task.eta_text || '-')) : null,
    task.elapsed ? h('span', {}, '用时 ' + Math.round(task.elapsed) + 's') : null);

  const actions = h('div', { class: 'btn-row', style: { marginTop: '10px' } });
  if (active) {
    mount(actions, button('取消', { variant: 'text', iconName: 'close', onClick: async () => {
      await api.cancelTask(task.id); toast('已请求取消'); refreshTasks();
    } }));
  } else {
    mount(actions, button('重试', { variant: 'text', iconName: 'refresh', onClick: async () => {
      await api.retryTask(task.id); toast('已重新排队'); refreshTasks();
    } }));
    mount(actions, button('打开目录', { variant: 'text', iconName: 'folder_open', onClick: () => {
      api.open(task.directory);
    } }));
    // 视频任务抽出音轨存成音乐，之后就能在「播放」页听
    const vids = (task.files || []).filter((f) => f.kind === 'video' && /\.(mp4|mkv|webm|flv|ts|m4v)$/i.test(f.path || ''));
    if (vids.length) {
      mount(actions, button('提取音轨', { variant: 'text', iconName: 'music_note', onClick: async (e) => {
        const btn = e.currentTarget; btn.disabled = true;
        try {
          const r = await api.extractAudio(vids.map((v) => v.path), 'mp3');
          toast(r.done.length
            ? '已提取 ' + r.done.length + ' 个音轨到音乐目录，可在「播放」页收听'
            : '提取失败：' + ((r.failed[0] || {}).error || '未知原因'), 5000);
        } catch (err) {
          toast('提取失败：' + err.message, 5000);
        }
        btn.disabled = false;
      } }));
    }
    // 删除分两种，都要先把"会不会删文件、删哪些"说清楚
    mount(actions, button('移出列表', { variant: 'text', iconName: 'delete', onClick: async () => {
      const yes = await dialog({
        title: '从列表移出？',
        body: '只移除这条记录，磁盘上的文件会完整保留。',
        confirm: '移出',
      });
      if (yes) { await api.removeTask(task.id, false); refreshTasks(); }
    } }));
    mount(actions, button('删除并清理文件', { variant: 'text', iconName: 'delete', onClick: async () => {
      // 先把真正会被删掉的路径逐条列出来，用户确认后才动手
      let preview = { items: [], total: 0 };
      try { preview = await api.previewRemove(task.id); } catch (error) { toast('读取失败：' + error.message); return; }
      const items = preview.items || [];
      if (!items.length) {
        const yes = await dialog({
          title: '没有需要清理的文件',
          body: '这个任务目录里已经没有本程序生成的文件了。要把记录移出列表吗？',
          confirm: '移出',
        });
        if (yes) { await api.removeTask(task.id, false); refreshTasks(); }
        return;
      }
      // 清单太长时只显示前 30 项，避免弹窗变成一堵墙
      const shown = items.slice(0, 30);
      const list = shown.map((item) =>
        '· ' + (item.kind === 'dir' ? '［文件夹］' : '') + item.path + '（' + fmtSize(item.size) + '）').join('\n');
      const more = items.length > shown.length
        ? '\n… 另有 ' + (items.length - shown.length) + ' 项未显示（合计 ' + fmtSize(preview.total) + '）'
        : '';
      const yes = await dialog({
        title: '删除 ' + items.length + ' 项，共 ' + fmtSize(preview.total) + '？',
        body: '下面这些文件会被永久删除，无法恢复：\n\n' + list + more +
              '\n\n不属于本任务的文件不会被删除；所在文件夹只有在空了之后才移除。',
        confirm: '确认删除',
        danger: true,
      });
      if (yes) { await api.removeTask(task.id, true); refreshTasks(); toast('已删除 ' + items.length + ' 项'); }
    } }));
  }

  const files = (task.files || []).length
    ? h('div', { class: 'files' }, ...task.files.map((file) => h('button', {
        class: 'file-chip',
        title: file.path + '（点击在应用内播放）',
        onclick: () => {
          // 视频走覆盖层播放，音频走底部播放条 —— 都不再弹系统浏览器
          const isVideo = file.kind === 'video' || /\.(mp4|mkv|webm|flv|ts|m4v)$/i.test(file.path || '');
          if (isVideo) openVideo(file.path, file.name);
          else playFile(file.path, file.name);
        },
      }, h('span', { html: icon(
          file.kind === 'video' ? 'movie' : file.kind === 'audio' ? 'audiotrack'
          : file.kind === 'cover' ? 'image' : file.kind === 'nfo' ? 'description'
          : file.kind === 'subtitle' ? 'subtitles' : 'bolt', 'outlined', 15) }),
        file.name + (file.count ? '（' + file.count + ' 条）' : ''))))
    : null;

  return card(
    h('div', { class: 'task' },
      h('div', { class: 'cover' },
        task.cover ? h('img', { src: api.coverUrl(task.cover, siteOf(task.site)), alt: '', loading: 'lazy' }) : null),
      h('div', { class: 'body' },
        h('div', { class: 'name', title: task.title }, task.title || task.raw_input),
        h('div', { class: 'sub' },
          (task.page_title ? task.page_title + ' · ' : '') +
          (task.qn_label || '自动画质') + ' · ' +
          (task.engine_used || task.engine || '—') + ' · ' + (task.mode || '手动')),
          // 老版本的 tasks.json 里没有 mode 字段，直接拼会显示 undefined
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

  mount(page, 
    h('div', { class: 'page-head' }, h('h1', { class: 'page-title' }, '设置')),
    h('p', { class: 'page-sub' }, '主题外观、账号、下载选项、抓取通道、更新与自检。改动即时生效并写入 data/config.json。'),
    h('div', { class: 'split' },
      buildAppearanceCard(settings),
      buildCredentialsCard(),
      buildDownloadCard2(settings),
      // 视频下载和音乐下载是一对，紧挨着放，方便对照
      buildMusicDirCard(settings),
      buildExtrasCard(settings),
      buildBigjpgCard(settings),
      buildFfmpegCard(),
      buildEnginesCard(),
      buildToolsCard(),
      buildSitesCard(),
      buildUpdateCard(),
      buildProgramCard()),
  );
  mount(view, page);
}

/** 保存设置。返回是否成功 —— 调用方必须据此决定要不要改本地状态，
    否则保存失败时界面会显示"已保存"，用户粘贴的内容还被清空了。 */
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
    return true;
  } catch (error) {
    toast('保存失败：' + error.message);
    return false;
  }
}

function buildAppearanceCard(settings) {
  const swatches = h('div', { class: 'swatches' });
  for (const preset of store.config?.theme_presets || []) {
    const dot = h('div', { class: 'dot', style: { background: preset.color } },
      settings.theme_seed?.toLowerCase() === preset.color.toLowerCase()
        ? h('span', { html: icon('check', 'filled', 20) }) : null);
    mount(swatches, h('button', {
      class: 'swatch' + (settings.theme_seed?.toLowerCase() === preset.color.toLowerCase() ? ' active' : ''),
      onclick: () => saveSetting({ theme_seed: preset.color }),
    }, dot, h('span', {}, preset.label)));
  }

  const modeRow = h('div', { class: 'chips' });
  for (const mode of THEME_MODES) {
    mount(modeRow, chip(mode.label, {
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

/** 平台账号与凭据。

  每个平台的凭据由适配器自己声明（videograb/sites/*.py 的 credentials），
  界面只负责渲染 —— 新增平台时不用再改前端。 */
function buildCredentialsCard() {
  const sites = store.config?.sites || [];
  const withCreds = sites.filter((s) => (s.credentials || []).length);
  const without = sites.filter((s) => !(s.credentials || []).length);

  const children = [
    h('div', { class: 'card-title', html: icon('key', 'outlined', 20) + '<span>平台账号与凭据</span>' }),
    h('div', { class: 'card-desc' },
      '只有需要登录态的平台才出现在下面。各平台的凭据分开存放、互不影响，' +
      '一律只保存在本机的 data/config.json，不会上传到任何地方。'),
  ];
  if (!withCreds.length) {
    children.push(h('div', { class: 'card-desc' }, '当前所有平台都无需登录即可使用。'));
  }
  for (const site of withCreds) children.push(buildSiteCredentials(site));
  if (without.length) {
    children.push(h('div', { class: 'help' },
      h('div', { class: 'body' },
        h('b', {}, '无需登录的平台：'),
        without.map((s) => s.name).join(' · '))));
  }
  return card(...children);
}

function buildSiteCredentials(site) {
  const settings = store.config?.settings || {};
  const rows = [];

  for (const item of site.credentials || []) {
    const configured = !!settings['has_' + item.field];
    const input = h('input', {
      class: 'input',
      type: item.secret ? 'password' : 'text',
      placeholder: configured ? '已配置，重新粘贴可覆盖' : ('粘贴 ' + item.label + ' 的值'),
      autocomplete: 'off',
    });
    const result = h('div', { class: 'card-desc', style: { marginTop: '6px' } });

    const buttons = [button('保存', { onClick: async () => {
      const value = input.value.trim();
      if (!value) { toast('请先粘贴 ' + item.label + ' 的值'); return; }
      // 只有真的写进去了才清空输入框、才把徽标改成"已配置"
      if (!await saveSetting({ [item.field]: value }, false)) return;
      input.value = '';
      store.config.settings['has_' + item.field] = true;
      toast(site.name + ' 的 ' + item.label + ' 已保存');
      renderSettings(document.getElementById('view'));
    } })];
    // 校验目前只有 B 站有对应接口
    if (site.id === 'bilibili' && item.key === 'sessdata') {
      buttons.push(button('校验', { variant: 'tonal', onClick: () => checkLogin(result) }));
    }

    const details = (item.howto || []).length
      ? h('details', { class: 'help' },
          h('summary', {}, '如何获取 ' + item.label + '？（' + site.name + '）'),
          h('div', { class: 'body' },
            h('ol', {}, ...(item.howto || []).map((step) => h('li', { html: step }))),
            item.tip ? h('p', { class: 'tip', html: item.tip }) : null,
            item.warning ? h('p', { class: 'tip', html: '<b>' + item.warning + '</b>' }) : null))
      : null;

    rows.push(h('div', { class: 'cred-block' },
      h('div', { class: 'cred-head' },
        h('span', { class: 'cred-label' }, item.label),
        configured ? pill('已配置', 'ok') : pill('未配置', 'mute')),
      item.purpose ? h('div', { class: 'hint' }, item.purpose) : null,
      h('div', { class: 'search-field', style: { marginTop: '8px' } }, input, ...buttons),
      result,
      details));
  }

  return h('div', { class: 'cred-site' },
    h('div', { class: 'cred-site-head' },
      h('span', { class: 'site-dot', style: { background: site.color || 'var(--md-primary)' } }),
      h('span', { class: 'cred-site-name' }, site.name),
      site.home
        ? h('a', { class: 'cred-home', href: site.home, target: '_blank', rel: 'noreferrer' },
            site.home.replace(/^https?:\/\//, ''))
        : null),
    ...rows);
}

async function checkLogin(host) {
  mount(clear(host), busy('正在校验登录态…'));
  try {
    const info = await api.loginCheck();
    mount(clear(host), 
      pill(info.logged_in ? '登录态有效' : '未登录', info.logged_in ? 'ok' : 'mute'),
      ' ' + (info.detail || ''),
      info.name ? '（' + info.name + (info.vip ? ' · 大会员' : '') + '）' : '');
  } catch (error) {
    mount(clear(host), '校验失败：' + error.message);
  }
}

function buildDownloadCard2(settings) {
  const dir = h('input', {
    class: 'input', value: settings.download_dir || '',
    placeholder: '留空则用程序目录下的 downloads',
  });
  const dirHint = h('div', { class: 'card-desc', style: { margin: '6px 0 0' } });
  const refreshHint = (info) => {
    clear(dirHint);
    const actual = info?.settings?.download_dir || '';
    if (info?.settings?.download_dir_ok === false) {
      // 配置了但没生效 —— 必须说出来，否则用户以为改成功了
      mount(dirHint, pill('这个路径用不了，已回落到默认目录', 'warn'),
        h('div', { class: 'hint', style: { marginTop: '4px' } },
          '实际下载到：' + actual + '。请检查盘符是否存在、路径是否合法。'));
    } else {
      mount(dirHint, h('div', { class: 'hint' }, '当前下载到：' + actual));
    }
  };
  refreshHint(store.config);
  const template = h('input', { class: 'input', value: settings.filename_template || '{title}' });
  const threads = h('input', {
    class: 'input', type: 'number', min: '1', max: '32', value: settings.threads_per_task || 8,
  });
  const parallel = h('input', {
    class: 'input', type: 'number', min: '1', max: '8', value: settings.max_parallel_tasks || 2,
  });

  const qualitySelect = h('select', { class: 'select' });
  mount(qualitySelect, h('option', { value: '0' }, '自动（每次选实际可用的最高画质）'));
  for (const q of store.config?.qualities || []) {
    mount(qualitySelect, h('option', { value: String(q.qn) },
      q.name + (q.needs_vip ? ' · 需大会员' : q.needs_login ? ' · 需登录' : '')));
  }
  qualitySelect.value = String(settings.prefer_quality || 0);

  return card(
    h('div', { class: 'card-title', html: icon('download', 'outlined', 20) + '<span>视频下载</span>' }),
    h('div', { class: 'card-desc' }, '多线程分块下载，支持断点续传；中断后重新下载会自动跳过已完成的块。'),
    field('下载目录', h('div', { class: 'search-field' }, dir,
      button('选择…', { variant: 'tonal', iconName: 'folder_open', onClick: async () => {
        // 用系统文件夹选择框，避免手打路径出错
        try {
          const picked = await api.pickDownloadDir();
          if (picked.cancelled) return;
          dir.value = picked.download_dir || dir.value;
          store.config.settings.download_dir = dir.value;
          refreshHint(store.config);
          toast('下载目录已改为：' + dir.value);
        } catch (error) {
          toast('选择目录失败：' + error.message);
        }
      } }),
      button('打开', { variant: 'tonal', iconName: 'folder_open', onClick: () => api.open(dir.value) }))),
    dirHint,
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

function buildMusicDirCard(settings) {
  const input = h('input', {
    class: 'input', value: settings.music_dir || '',
    placeholder: '留空 = 下载目录下的「音乐」子文件夹',
  });
  return card(
    h('div', { class: 'card-title', html: icon('headphones', 'outlined', 20) + '<span>音乐下载</span>' }),
    h('div', { class: 'card-desc' },
      '音乐和视频分开放，避免混在一个文件夹里。留空则自动用「下载目录/音乐」。'),
    h('div', { class: 'search-field', style: { marginTop: '10px' } }, input,
      button('选择…', { variant: 'tonal', iconName: 'folder_open', onClick: async () => {
        try {
          const r = await api.pickMusicDir();
          if (r.cancelled) return;
          input.value = r.music_dir || input.value;
          store.config.settings.music_dir = r.music_dir;
          toast('音乐目录已改为：' + r.music_dir);
        } catch (error) { toast('选择失败：' + error.message); }
      } }),
      button('打开', { variant: 'tonal', iconName: 'folder_open', onClick: () => api.open(input.value) })),
    h('div', { class: 'btn-row', style: { marginTop: '10px' } },
      button('保存', { onClick: () => saveSetting({ music_dir: input.value.trim() }, false) })),
  );
}

function buildFfmpegCard() {
  const holder = h('div', { class: 'card-desc' }, busy('正在检查…'));
  const actions = h('div', { class: 'btn-row', style: { marginTop: '10px' } });

  const refresh = async () => {
    clear(holder); clear(actions);
    let info = {};
    try { info = await api.ffmpegStatus(); } catch (e) { mount(clear(holder), '检查失败：' + e.message); return; }
    if (info.found) {
      mount(holder, h('div', {}, pill('已就绪', 'ok')),
        h('div', { class: 'hint', style: { marginTop: '6px' } }, info.path));
      mount(actions, button('重新检查', { variant: 'text', iconName: 'refresh', onClick: refresh }));
    } else {
      mount(holder, '还没有 ffmpeg。转码、剪辑、音轨提取都需要它，第一次用到时再来这里下载即可。',
        h('div', { class: 'hint', style: { marginTop: '6px' } },
          '会下载到：' + (info.bin_dir || 'bin/') + '　·　来源：' + ((info.sources || []).join(' / ') || 'gyan.dev')));
      mount(actions, button('下载 ffmpeg', { iconName: 'download', onClick: async (e) => {
        const btn = e.currentTarget; btn.disabled = true;
        mount(clear(holder), busy('正在下载并解压（约 115 MB，慢的话要几分钟），请稍候…'));
        try {
          const r = await api.ffmpegInstall();
          toast('ffmpeg 已就绪' + (r.source ? '（' + r.source + '）' : ''));
        } catch (err) {
          toast('下载失败：' + err.message, 6000);
        }
        btn.disabled = false;
        refresh();
      } }));
    }
  };
  refresh();

  return card(
    h('div', { class: 'card-title', html: icon('movie', 'outlined', 20) + '<span>ffmpeg（按需下载）</span>' }),
    h('div', { class: 'card-desc' },
      '安装包里不内置 ffmpeg（内置会让安装包大 100 MB 以上）。需要转码或剪辑时在这里下载一次即可，'
      + '之后一直可用；也可以自己把别的版本丢进 bin/ 覆盖。'),
    h('div', { style: { marginTop: '10px' } }, holder),
    actions,
  );
}

function buildBigjpgCard(settings) {
  const key = h('input', {
    class: 'input', type: 'password',
    value: settings.bigjpg_key || '',
    placeholder: '可选：bigjpg 的 X-API-KEY（留空就走免费通道）',
  });
  return card(
    h('div', { class: 'card-title', html: icon('image', 'outlined', 20) + '<span>图片放大（bigjpg）</span>' }),
    h('div', { class: 'card-desc' },
      '默认走 bigjpg 网页版的免费通道，**不需要任何凭据**。下面的 API key 是可选的付费通道，'
      + '一般用不上，留空即可。'),
    h('div', { class: 'hint-box', style: { marginTop: '10px' } },
      h('span', { html: icon('warning_amber', 'outlined', 20) }),
      h('div', {}, '使用「图片」页时，你选择的图片会被上传到 bigjpg 的服务器处理。'
        + '这是本程序唯一会把文件发到外部的地方。')),
    h('div', { class: 'search-field', style: { marginTop: '12px' } }, key,
      button('保存', { onClick: () => saveSetting({ bigjpg_key: key.value.trim() }, false) })),
  );
}

function buildExtrasCard(settings) {
  return card(
    h('div', { class: 'card-title', html: icon('star', 'outlined', 20) + '<span>附加产物</span>' }),
    h('div', { class: 'card-desc' }, '每个视频独立目录，附加文件与视频同名，方便直接丢进播放器或媒体库。'),
    switchRow('封面', '下载最大宽度 1920 的封面图', settings.write_cover !== false,
      (v) => saveSetting({ write_cover: v }, false)),
    switchRow('弹幕（仅 B 站）', '保存 B 站 XML 弹幕，并额外转一份 ASS 字幕；其它平台暂时拿不到弹幕', settings.write_danmaku !== false,
      (v) => saveSetting({ write_danmaku: v }, false)),
    switchRow('字幕（仅 B 站）', '下载 UP 主上传的 CC 字幕并转成 SRT', settings.write_subtitle === true,
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
    mount(list, row);
  }

  return card(
    h('div', { class: 'card-title', html: icon('route', 'outlined', 20) + '<span>抓取通道</span>' }),
    h('div', { class: 'card-desc' },
      '顺序就是「自动」模式下的尝试顺序，越靠前越优先。关掉的通道不会参与解析与体检。'),
    list,
  );
}

/** 支持的平台一览。把每个平台真实的限制写在界面上，比 README 更不容易被忽略。 */
function buildSitesCard() {
  const sites = store.config?.sites || [];
  const list = h('div', {});
  for (const site of sites) {
    mount(list, h('div', { class: 'site-row' },
      h('span', { class: 'site-dot', style: { background: site.color || 'var(--md-primary)' } }),
      h('div', { class: 'grow' },
        h('div', { class: 'name' }, site.name,
          site.needs_login ? pill('需要登录', 'warn') : null),
        site.note ? h('div', { class: 'hint' }, site.note) : null,
        site.examples?.length
          ? h('div', { class: 'hint', style: { opacity: '.75' } },
              '例如：' + site.examples.join('　·　'))
          : null)));
  }
  return card(
    h('div', { class: 'card-title', html: icon('language', 'outlined', 20) + '<span>支持的平台</span>' }),
    h('div', { class: 'card-desc' },
      '把链接或分享文案粘到「解析」页即可，程序会自动识别平台。' +
      '各平台的风控与会员策略差别很大，下面是当前实测的能力边界。'),
    list,
  );
}

/** 更新与自检修复。 */
function buildUpdateCard() {
  const info = store.config || {};
  const result = h('div', { style: { marginTop: '10px' } });
  let pendingInstaller = '';

  const show = (node) => { mount(clear(result), node); };

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
          // 一键：下载 → 退出 → 静默装回原目录 → 自动重新打开
          button('下载并自动安装', { iconName: 'restart_alt', onClick: async (e2) => {
            const b = e2.currentTarget;
            const yes = await dialog({
              title: '下载并自动安装？',
              body: '安装过程中程序会自动退出，装好后自动重新打开。正在进行的下载会中断。',
              confirm: '开始更新',
            });
            if (!yes) return;
            b.disabled = true;
            const holder = h('div', { class: 'card-desc', style: { marginTop: '10px' } },
              busy('正在下载并准备安装…'));
            mount(result, holder);
            try {
              const got = await api.updateApply();
              mount(clear(holder), pill('安装程序已就绪，正在重启…', 'ok'));
              document.body.innerHTML =
                '<div style="display:grid;place-items:center;height:100vh;font:16px/1.8 system-ui;color:#888">' +
                '<div style="text-align:center">正在更新到 ' + (got.latest || '新版本') +
                '<br><span style="font-size:13px">程序会自动重新打开，请稍候</span></div></div>';
            } catch (error) {
              b.disabled = false;
              mount(clear(holder), h('div', { class: 'hint-box' },
                h('span', { html: icon('error_outline', 'outlined', 20) }),
                h('div', {}, '自动更新失败：' + error.message)));
            }
          } }),
          button('仅下载安装包', { iconName: 'cloud_download', onClick: async (e2) => {
            const b = e2.currentTarget; b.disabled = true;
            const holder = h('div', { class: 'card-desc', style: { marginTop: '10px' } }, busy('正在下载…'));
            mount(result, holder);
            try {
              const got = await api.updateDownload();
              pendingInstaller = got.path;
              mount(clear(holder), pill('已下载 ' + fmtSize(got.size), 'ok'));
              mount(result, h('div', { class: 'btn-row', style: { marginTop: '10px' } },
                button('运行安装程序', { iconName: 'restart_alt', onClick: async () => {
                  try {
                    const launched = await api.updateLaunch(pendingInstaller);
                    toast(launched.detail || '已启动安装程序');
                  } catch (error) { toast('启动失败：' + error.message); }
                } })));
            } catch (error) {
              mount(clear(holder), h('div', { class: 'hint-box' },
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
        for (const item of r.bad) mount(list, h('div', {}, item.path + ' —— ' + item.detail));
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
      for (const path of r.repaired) mount(list, h('div', {}, '✓ ' + path));
      for (const item of r.failed) mount(list, h('div', {}, '✗ ' + item.path + ' —— ' + item.error));
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
    (() => {
      const runtime = store.config?.webview;
      if (!runtime) return null;
      const label = { fixed: '自带运行时', system: '系统运行时', missing: '未安装', unknown: '未知' }[runtime.mode] || runtime.mode;
      return h('div', { class: 'kv' },
        h('span', {}, 'WebView2'),
        h('span', { title: runtime.detail || '' },
          label + (runtime.system_version && runtime.mode === 'system' ? ' ' + runtime.system_version : ''),
          runtime.ok ? '' : ' —— 缺少它只能退化到浏览器'));
    })(),
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('检查更新', { variant: 'tonal', iconName: 'cloud_download', onClick: check }),
      button('自检并修复', { iconName: 'shield', onClick: selfCheck })),
    result,
  );
}

/** 退出程序。以前藏在设置页最下面的「程序」卡片里，找起来很费劲 ——
    这是个高频操作，挪到首页右上角。 */
function buildQuitButton() {
  return button('退出程序', {
    variant: 'text', iconName: 'stop',
    onClick: async () => {
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
    },
  });
}

function buildProgramCard() {
  // file:// 打开时 location.origin 是字符串 "null"，直接拼会显示 "null/"。
  // 那种模式下也没有可点的地址，就只说"本地文件"。
  const origin = location.origin && location.origin !== 'null' ? location.origin : '';
  const address = origin ? origin + '/' : '（以文件方式打开，无固定地址）';
  return card(
    h('div', { class: 'card-title', html: icon('dns', 'outlined', 20) + '<span>程序</span>' }),
    h('div', { class: 'card-desc' },
      '桌面版没有控制台窗口。要退出程序请点「解析」页右上角的「退出程序」；' +
      '关掉窗口或浏览器标签页并不会停止后台服务。'),
    h('div', { class: 'kv' }, h('span', {}, '访问地址'),
      h('span', {}, h('a', { href: address, style: { color: 'var(--md-primary)' } }, address))),
    h('div', { class: 'kv' }, h('span', {}, '运行方式'),
      h('span', {}, location.protocol === 'file:' ? '本地文件' : '本地服务（仅监听 127.0.0.1）')),
  );
}

function buildToolsCard() {
  const list = h('div', {});
  const info = h('div', { class: 'card-desc' });

  const tools = (store.config?.engines || []).find((e) => e.id === 'external_tool');
  // 这一行要能被单独更新 —— 以前装完是整页重建，结果把提示一起冲掉了
  const channelState = h('span', {}, tools?.available ? '可用' : '未安装');

  const installBtn = button('一键安装 yt-dlp', { variant: 'tonal', iconName: 'cloud_download', onClick: async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    mount(clear(btn), busy('检查中'));
    mount(clear(info), '正在检查 yt-dlp（安装位置是项目自带的 .deps 目录，不会改动系统环境）…');
    try {
      const result = await api.installYtdlp();
      // 三种结果要分清楚，别都写成"安装成功"：
      // 打包版本来就内置了 yt-dlp，这时点按钮什么都不会装，得让用户看明白。
      const builtIn = String(result.tail || '').includes('已内置');
      mount(clear(info), 
        h('div', { style: { marginBottom: '6px' } },
          pill(result.ok ? (builtIn ? '已经内置，无需安装' : '安装成功') : '安装失败',
               result.ok ? 'ok' : 'err'),
          result.ok ? null : ' 返回码 ' + result.returncode),
        result.tail ? h('div', { class: 'hint', style: { whiteSpace: 'pre-wrap' } }, result.tail) : null,
        h('div', { class: 'hint', style: { opacity: '.7', marginTop: '4px' } },
          '位置：' + (result.deps_dir || '—')),
      );
      mount(clear(btn), result.ok && builtIn ? '已内置' : '重新安装');
      btn.disabled = false;
      // 只刷新这一行的状态，不整页重建
      const state = await api.state();
      store.config = state;
      const now = (state.engines || []).find((e) => e.id === 'external_tool');
      channelState.textContent = now?.available ? '可用' : '未安装';
    } catch (error) {
      mount(clear(info), '安装失败：' + error.message);
      btn.disabled = false;
      mount(clear(btn), '一键安装 yt-dlp');
    }
  } });

  mount(list, 
    h('div', { class: 'kv' }, h('span', {}, 'Python'), h('span', {}, store.config?.python || '内置运行时')),
    h('div', { class: 'kv' }, h('span', {}, 'ffmpeg'),
      h('span', {}, store.config?.ffmpeg || '未检测到（将使用内置纯 Python fMP4 合并器）')),
    h('div', { class: 'kv' }, h('span', {}, '外部下载器通道'), channelState),
  );

  return card(
    h('div', { class: 'card-title', html: icon('terminal', 'outlined', 20) + '<span>外部工具</span>' }),
    h('div', { class: 'card-desc' },
      '装上 yt-dlp 就多一条独立实现的抓取通道：它自带一套长期维护的解析器，可以和官方接口互相印证。' +
      '没有 ffmpeg 也完全不影响使用 —— 合并由内置的 fMP4 合并器完成。'),
    list,
    h('div', { class: 'btn-row', style: { marginTop: '12px' } }, installBtn),
    h('div', { style: { marginTop: '10px' } }, info),
  );
}

/* ================================================================ 音乐 */

export function renderMusic(view) {
  clear(view);
  const page = h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '音乐'),
      h('div', { class: 'spacer' })),
    h('p', { class: 'page-sub' },
      '粘贴网易云 / QQ音乐 / 酷狗 / 酷我 的链接或分享文案即可下载；'
      + '也可以把视频的音轨提取成音乐、把平台加密格式解密成通用音频，并在应用内播放、自建歌单。'),

    buildMusicSearchCard(),
    buildSourceSwapCard(),

    // 解析出音乐后，复用解析页那两张卡（音质/格式选择 + 下载）
    (store.parsed?.info?.media === 'music')
      ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '12px' } },
          buildVideoCard(), buildDownloadCard())
      : null,

    buildLocalCard(),
  );
  mount(view, page);
}

/* ================================================================ 音乐播放 */

export function renderPlayer(view) {
  clear(view);
  const page = h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '播放'),
      h('div', { class: 'spacer' }),
      pill('应用内播放 · 支持乱序与循环', 'mute')),
    h('p', { class: 'page-sub' },
      '这里只管播放：歌曲来自音乐下载目录和保存过的歌单。下载、转码请到左侧「音乐」页。'),
    // 左右两栏：左边歌曲列表 + 目录设置，右边歌单。窄屏自动叠成一列。
    h('div', { class: 'player-cols' },
      h('div', { class: 'player-col' },
        buildMusicLibraryCard(),
        // 储存位置也能在播放页直接改，不用再跑去设置
        buildMusicDirCard(store.config?.settings || {})),
      h('div', { class: 'player-col' },
        buildPlaylistCard())),
  );
  mount(view, page);
}


/* ================================================================ 图片（bigjpg） */

const BIGJPG_FEATURES = [
  ['最高 16 倍放大', '2x / 4x / 8x / 16x（免费账号只有 2x / 4x）'],
  ['降噪强度五档', '无 / 低 / 中 / 高 / 最高 —— 越高越干净，但也更容易糊掉细节'],
  ['照片与插画两种模式', '照片模式保留质感，卡通插画模式强化线条'],
  ['批量处理', '一次提交多张，并发上传后逐张建任务'],
  ['失败重试', '任务失败可以直接重试，不用重新上传'],
];

let bigjpgTimer = null;
const bigjpgState = { paths: [], results: [], info: null };

export function renderImage(view) {
  if (bigjpgTimer) { clearInterval(bigjpgTimer); bigjpgTimer = null; }
  clear(view);
  const page = h('div', { class: 'page' });
  mount(page, 
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '图片放大'),
      h('div', { class: 'spacer' }),
      pill('该页面使用 bigjpg', 'warn')),
    h('p', { class: 'page-sub' },
      '图片放大由第三方服务 bigjpg 提供。使用本页意味着你的图片会被上传到 bigjpg.cn 的服务器处理 —— '
      + '这是本程序里唯一会把文件发到外部的地方，请自行判断是否接受。'),
  );

  // 卡片之间要有明确间距 —— 直接塞进裸 div 会失去 .page 里各卡片自己的外边距，
  // 三张卡片就会粘在一起。用 flex + gap 强制分开。
  const infoHolder = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px' } });
  mount(page, h('div', { style: { marginTop: '12px' } }, infoHolder));

  function paintInfo() {
    clear(infoHolder);
    const info = bigjpgState.info;
    const feats = card(
      h('div', { class: 'card-title', html: icon('image', 'outlined', 20) + '<span>bigjpg 提供的功能</span>' }),
      h('div', { class: 'card-desc' }, '以下能力来自 bigjpg 官方接口，不是本程序自研的算法。'),
      h('div', { style: { marginTop: '10px' } },
        ...BIGJPG_FEATURES.map(([t, d]) => h('div', { style: { marginBottom: '8px' } },
          h('b', {}, t),
          h('div', { class: 'card-desc', style: { margin: '2px 0 0' } }, d)))),
    );
    mount(infoHolder, feats);
    mount(infoHolder, card(
      h('div', { class: 'card-title', html: icon('check_circle', 'outlined', 20) + '<span>免费通道已就绪</span>' }),
      h('div', { class: 'card-desc' },
        '用的是 bigjpg 官网自己在用的接口 —— 不需要注册、不需要登录、不需要 API key。'
        + '免费额度用完时 bigjpg 会返回 requires_vip，界面会如实显示。'),
    ));
  }

  if (bigjpgState.info) paintInfo();
  else api.bigjpgInfo().then((d) => { bigjpgState.info = d; paintInfo(); }).catch(() => paintInfo());

  // ---- 选择图片 + 参数 ----
  const pickLabel = h('div', { class: 'card-desc' },
    bigjpgState.paths.length ? ('已选择 ' + bigjpgState.paths.length + ' 张：' + bigjpgState.paths.map((p) => p.split(/[\\/]/).pop()).join('、'))
      : '支持 jpg / png / bmp / webp，单张不超过 20 MB。');
  const styleSel = h('select', { class: 'input' },
    Object.entries(bigjpgState.info?.styles || { art: '卡通插画', photo: '照片' })
      .map(([v, t]) => h('option', { value: v }, t)));
  const noiseSel = h('select', { class: 'input' },
    Object.entries(bigjpgState.info?.noises || { '-1': '无', 0: '低', 1: '中', 2: '高', 3: '最高' })
      .map(([v, t]) => h('option', { value: v, selected: v === '1' }, t)));
  const scaleSel = h('select', { class: 'input' },
    Object.entries(bigjpgState.info?.scales || { 1: '2 倍', 2: '4 倍', 3: '8 倍', 4: '16 倍' })
      .map(([v, t]) => h('option', { value: v }, t)));

  const resultHolder = h('div', { style: { marginTop: '12px' } });

  const uploadCard = card(
    h('div', { class: 'card-title', html: icon('upload', 'outlined', 20) + '<span>选择图片并放大</span>' }),
    pickLabel,
    h('div', { class: 'btn-row', style: { marginTop: '10px' } },
      button('选择图片', { variant: 'tonal', iconName: 'folder_open', onClick: async () => {
        try {
          const r = await api.pickLocalFiles('image');
          if (!r.cancelled) { bigjpgState.paths = r.paths || []; renderImage(document.getElementById('view')); }
        } catch (e) { toast('选择失败：' + e.message); }
      } })),
    h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: '12px', marginTop: '14px' } },
      field('图片类型', styleSel), field('降噪', noiseSel), field('放大倍数', scaleSel)),
    h('div', { class: 'btn-row', style: { marginTop: '14px' } },
      button('开始放大', { iconName: 'bolt', onClick: async (e) => {
        const btn = e.currentTarget;
        if (!bigjpgState.paths.length) { toast('先选择图片'); return; }
        btn.disabled = true;
        mount(clear(resultHolder), h('div', { class: 'card-desc' }, busy('正在上传并创建任务，图片会上传到 bigjpg…')));
        try {
          const r = await api.bigjpgSubmit(bigjpgState.paths, styleSel.value, noiseSel.value, scaleSel.value);
          bigjpgState.results = (r.results || []).map((x) => x.ok
            ? { file: x.file, tid: x.tid, status: 'new', status_text: '排队中', url: '', size: '' }
            : { file: x.file, error: x.error });
          paintResults();
          if (bigjpgState.results.some((x) => x.tid)) startPolling();
        } catch (err) {
          mount(clear(resultHolder), h('div', { class: 'hint-box' },
            h('span', { html: icon('error_outline', 'outlined', 20) }), h('div', {}, err.message)));
        }
        btn.disabled = false;
      } })),
  );
  uploadCard.style.marginTop = '12px';
  mount(page, uploadCard, resultHolder);

  function paintResults() {
    clear(resultHolder);
    if (!bigjpgState.results.length) return;
    const rows = bigjpgState.results.map((r) => h('tr', {},
      h('td', {}, r.file),
      h('td', {}, r.error ? pill('失败', 'err') : pill(r.status_text || r.status, r.status === 'success' ? 'ok' : (r.status === 'error' ? 'err' : 'mute'))),
      h('td', {}, r.size || '—'),
      h('td', {}, r.error ? h('span', { style: { color: 'var(--md-error)' } }, r.error)
        : (r.url ? h('a', { href: r.url, target: '_blank' }, '下载结果') : '—'))));
    mount(resultHolder, card(
      h('div', { class: 'card-title', html: icon('image', 'outlined', 20) + '<span>任务结果</span>' }),
      h('div', { style: { overflowX: 'auto' } },
        h('table', { class: 'matrix' },
          h('thead', {}, h('tr', {}, h('th', {}, '文件'), h('th', {}, '状态'), h('th', {}, '尺寸'), h('th', {}, '结果'))),
          h('tbody', {}, ...rows))),
      h('div', { class: 'btn-row', style: { marginTop: '10px' } },
        button('刷新状态', { variant: 'text', iconName: 'refresh', onClick: () => { pollOnce(); } })),
    ));
  }

  async function pollOnce() {
    const tids = bigjpgState.results.filter((r) => r.tid).map((r) => r.tid);
    if (!tids.length) return;
    try {
      const r = await api.bigjpgQuery(tids);
      const map = {};
      for (const it of r.items || []) map[it.tid] = it;
      bigjpgState.results = bigjpgState.results.map((x) => (x.tid && map[x.tid])
        ? { ...x, status: map[x.tid].status, status_text: map[x.tid].status_text, url: map[x.tid].url, size: map[x.tid].size }
        : x);
      paintResults();
    } catch (e) { /* 轮询失败不打扰用户 */ }
  }

  function startPolling() {
    if (bigjpgTimer) clearInterval(bigjpgTimer);
    bigjpgTimer = setInterval(async () => {
      await pollOnce();
      const pending = bigjpgState.results.some((r) => r.tid && (r.status === 'new' || r.status === 'process'));
      if (!pending && bigjpgTimer) { clearInterval(bigjpgTimer); bigjpgTimer = null; }
    }, 4000);
  }

  if (bigjpgState.results.length) { paintResults(); if (bigjpgState.results.some((r) => r.tid)) startPolling(); }
  mount(view, page);
}


/* ================================================================ 文档转换 */

const docState = { paths: [], target: 'docx', results: null };

const DOC_FORMATS = [
  ['docx', 'Word 文档', '段落 + 表格', ['xlsx', 'md', 'pdf']],
  ['xlsx', 'Excel 表格', '按行按列', ['docx', 'md', 'pdf']],
  ['pdf', 'PDF', '仅文本层，无版式', ['docx', 'xlsx', 'md']],
  ['md', 'Markdown', '纯文本 + 表格', ['docx', 'xlsx']],
];

export function renderDoc(view) {
  clear(view);
  const holder = h('div', {});
  const nameOf = (p) => String(p).split(/[\\/]/).pop();

  const paint = () => {
    clear(holder);
    const picked = docState.paths || [];
    const kinds = [...new Set(picked.map((p) => String(p).split('.').pop().toLowerCase()))];
    const targets = kinds.length === 1
      ? ((DOC_FORMATS.find((f) => f[0] === kinds[0]) || [0, 0, 0, ['docx', 'xlsx', 'md']])[3])
      : ['docx', 'xlsx', 'md'];
    const results = docState.results;

    return mount(holder, 
      card(
        h('div', { class: 'card-title', html: icon('description', 'outlined', 20) + '<span>选择文档并转换</span>' }),
        h('div', { class: 'card-desc' },
          '支持 Word(.docx) / Excel(.xlsx) / PDF / Markdown 互转。全部在本机完成，文件不会上传到任何地方。'),
        h('div', { class: 'btn-row', style: { marginTop: '10px' } },
          button('选择文件', { variant: 'tonal', iconName: 'folder_open', onClick: async () => {
            try {
              const r = await api.pickLocalFiles('doc');
              if (r.cancelled) return;
              docState.paths = r.paths || [];
              docState.results = null;
              const ks = [...new Set(docState.paths.map((p) => String(p).split('.').pop().toLowerCase()))];
              if (ks.length === 1) {
                const allowed = (DOC_FORMATS.find((f) => f[0] === ks[0]) || [0, 0, 0, ['docx']])[3];
                if (!allowed.includes(docState.target)) docState.target = allowed[0];
              }
              paint();
            } catch (err) { toast('选择失败：' + err.message); }
          } }),
          picked.length
            ? button('清空', { variant: 'text', iconName: 'close', onClick: () => {
                docState.paths = []; docState.results = null; paint();
              } })
            : null),
        picked.length
          ? h('div', { style: { marginTop: '10px' } },
              h('div', { class: 'card-desc' }, '已选 ' + picked.length + ' 个文件：'),
              h('div', { class: 'song-scroll', style: { maxHeight: '150px', marginTop: '6px' } },
                ...picked.map((p) => h('div', {
                  style: { fontSize: '12.5px', padding: '3px 0', whiteSpace: 'nowrap',
                           overflow: 'hidden', textOverflow: 'ellipsis' },
                }, nameOf(p)))))
          : h('div', { class: 'card-desc', style: { marginTop: '10px' } }, '还没有选择文件。'),
        picked.length
          ? h('div', { style: { marginTop: '12px' } },
              h('div', { class: 'card-desc' }, '输出格式'),
              h('div', { class: 'btn-row', style: { marginTop: '6px' } },
                ...targets.map((t) => button(
                  (DOC_FORMATS.find((f) => f[0] === t) || [t, t])[1],
                  { variant: docState.target === t ? 'tonal' : 'text',
                    onClick: () => { docState.target = t; paint(); } }))),
              h('div', { class: 'btn-row', style: { marginTop: '12px' } },
                button('开始转换', { iconName: 'bolt', onClick: async (e) => {
                  const btn = e.currentTarget; btn.disabled = true;
                  try {
                    const r = await api.convertDocs(docState.paths, docState.target);
                    docState.results = r;
                    if (r.done.length) toast('已转换 ' + r.done.length + ' 个文件');
                    else if (r.failed.length) toast('转换失败：' + r.failed[0].error, 7000);
                    paint();
                  } catch (err) { toast('转换失败：' + err.message, 7000); }
                  btn.disabled = false;
                } })))
          : null),
      card(
        h('div', { class: 'card-title', html: icon('info', 'outlined', 20) + '<span>能做什么、不能做什么</span>' }),
        h('div', { class: 'card-desc' }, 'Word / Excel / Markdown 之间是完整读写，表格会保留成真表格。'),
        h('div', { class: 'hint-box', style: { marginTop: '8px' } },
          h('span', { html: icon('info', 'outlined', 20) }),
          h('div', {},
            h('b', {}, 'Word / Excel 转 PDF 有两条通路，会自动选：'),
            h('div', { style: { marginTop: '4px' } },
              h('b', {}, '① 本机 Office'), '（优先）—— 版式最准，分页、页眉页脚都保留。'
              + '代价是第一次调用要冷启动 Word，约 5–10 秒。'),
            h('div', { style: { marginTop: '4px' } },
              h('b', {}, '② 无头 Edge / Chrome 打印'), '—— 不需要装 Office，'
              + '把文档转成 A4 网页再打印成 PDF。快（1–2 秒），但复杂文档的排版会和 Word 原样不同。'),
            h('div', { style: { marginTop: '6px', opacity: '.85' } },
              '装了 Office 就走 ①，没装自动落 ②。两条都不通时会明确告诉你原因，不会产出坏文件。'))),
        h('div', { class: 'hint-box', style: { marginTop: '8px' } },
          h('span', { html: icon('warning_amber', 'outlined', 20) }),
          h('div', {},
            h('b', {}, 'PDF 只抽文本，不还原版式：'),
            h('div', {}, '· 扫描件、图片型 PDF 没有文字层，抽不出来'),
            h('div', {}, '· 用 CID 子集字体嵌入的中文 PDF，抽出来可能是乱码'),
            h('div', {}, '· 版面、图片、页眉页脚都不会保留'))),
        h('div', { style: { marginTop: '10px' } },
          ...DOC_FORMATS.map(([ext, name, note, outs]) => h('div', {
            style: { fontSize: '12.5px', padding: '4px 0', borderBottom: '1px solid var(--md-outline-variant)' },
          }, h('b', {}, '.' + ext), ' ' + name + ' → ' + outs.map((o) => '.' + o).join(' / '),
            h('span', { class: 'card-desc', style: { margin: '0 0 0 6px' } }, note))))),
      (results && results.done && results.done.length)
        ? card(
            h('div', { class: 'card-title', html: icon('check_circle', 'outlined', 20) + '<span>转换结果</span>' }),
            h('div', { class: 'song-scroll', style: { maxHeight: '240px', marginTop: '8px' } },
              ...results.done.map((d) => h('div', {
                style: { display: 'flex', alignItems: 'center', gap: '8px', padding: '5px 0',
                         borderBottom: '1px solid var(--md-outline-variant)' },
              },
                h('div', { style: { flex: '1', minWidth: '0' } },
                  h('div', { style: { fontSize: '12.5px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
                    nameOf(d.output)),
                  h('div', { style: { fontSize: '11px', opacity: '.6' } },
                    '.' + d.source + ' → .' + d.target + ' · ' + fmtSize(d.size))),
                button('定位', { variant: 'text', iconName: 'folder_open',
                  onClick: () => api.reveal(d.output) })))),
            (results.failed && results.failed.length)
              ? h('div', { class: 'hint-box', style: { marginTop: '10px' } },
                  h('span', { html: icon('error_outline', 'outlined', 20) }),
                  h('div', {}, ...results.failed.map((f) => h('div', {}, nameOf(f.path) + '：' + f.error))))
              : null)
        : h('span'),
    );
  };

  mount(view, h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '文档转换'),
      h('div', { class: 'spacer' }),
      pill('PDF / Word / Excel / Markdown 互转', 'mute')),
    h('p', { class: 'page-sub' },
      '把 PDF 转成 Word 或 Excel，也能在 Word、Excel、Markdown 之间互相转换，输出格式自己选。'),
    holder));
  paint();
}


/* ================================================================ 视频剪辑 */

const VIDEO_EXT_RE = /\.(mp4|mkv|webm|flv|ts|m4v|mov|avi|wmv|mpg|mpeg)$/i;

export function renderClip(view) {
  clear(view);
  const holder = h('div', {});
  const nameOf = (p) => String(p).split(/[\\/]/).pop();

  const paint = () => {
    clear(holder);
    // 已下载的视频：从任务记录里捞
    const downloaded = [];
    for (const t of (store.tasks || [])) {
      for (const f of (t.files || [])) {
        if (f.kind === 'video' && VIDEO_EXT_RE.test(String(f.path || ''))) {
          downloaded.push({ path: f.path, name: f.name || nameOf(f.path), title: t.title || '' });
        }
      }
    }
    const local = clipState.paths || [];

    mount(holder, 
      card(
        h('div', { class: 'card-title', html: icon('videocam', 'outlined', 20) + '<span>本地视频剪辑</span>' }),
        h('div', { class: 'card-desc' },
          '选一个本地视频，在播放器里拖进度条、填起止时间就能裁一段。裁完直接存在原文件旁边。'),
        h('div', { class: 'btn-row', style: { marginTop: '10px' } },
          button('选择本地视频', { variant: 'tonal', iconName: 'folder_open', onClick: async () => {
            try {
              const r = await api.pickLocalFiles('video');
              if (r.cancelled || !r.paths?.length) return;
              clipState.paths = r.paths.filter((p) => VIDEO_EXT_RE.test(p));
              if (!clipState.paths.length) { toast('选的文件不是视频格式'); return; }
              paint();
              openVideo(clipState.paths[0], nameOf(clipState.paths[0]));
            } catch (err) { toast('选择失败：' + err.message); }
          } })),
        local.length
          ? h('div', { style: { marginTop: '10px' } },
              ...local.map((p) => h('div', {
                style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0',
                         borderBottom: '1px solid var(--md-outline-variant)' },
              },
                h('div', { style: { flex: '1', minWidth: '0', fontSize: '12.5px',
                                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } },
                  nameOf(p)),
                button('剪辑', { variant: 'text', iconName: 'play_arrow',
                  onClick: () => openVideo(p, nameOf(p)) }),
                button('定位', { variant: 'text', iconName: 'folder_open',
                  onClick: () => api.reveal(p) }))))
          : h('div', { class: 'card-desc', style: { marginTop: '10px' } }, '还没有选择本地视频。')),
      card(
        h('div', { class: 'card-title', html: icon('movie', 'outlined', 20) + '<span>已下载的视频</span>' }),
        h('div', { class: 'card-desc' }, '点「剪辑」直接在播放器里打开，不用再去历史记录里翻。'),
        downloaded.length
          ? h('div', { class: 'song-scroll', style: { maxHeight: '260px', marginTop: '8px' } },
              ...downloaded.slice(0, 50).map((f) => h('div', {
                style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 0',
                         borderBottom: '1px solid var(--md-outline-variant)' },
              },
                h('div', { style: { flex: '1', minWidth: '0' } },
                  h('div', { style: { fontSize: '12.5px', whiteSpace: 'nowrap', overflow: 'hidden',
                                      textOverflow: 'ellipsis' } }, f.name),
                  f.title ? h('div', { style: { fontSize: '11px', opacity: '.6', whiteSpace: 'nowrap',
                                               overflow: 'hidden', textOverflow: 'ellipsis' } }, f.title) : null),
                button('剪辑', { variant: 'text', iconName: 'play_arrow',
                  onClick: () => openVideo(f.path, f.name) }),
                button('定位', { variant: 'text', iconName: 'folder_open',
                  onClick: () => api.reveal(f.path) }))))
          : h('div', { class: 'card-desc', style: { marginTop: '10px' } },
              '还没有下载过视频。到「解析」页下一个，或者用上面的本地视频剪辑。')),
      card(
        h('div', { class: 'card-title', html: icon('info', 'outlined', 20) + '<span>怎么用</span>' }),
        h('div', { class: 'card-desc' }, '打开视频后，播放器下方有一条工具栏：'),
        h('div', { style: { marginTop: '8px', fontSize: '12.5px', lineHeight: '1.9' } },
          h('div', {}, '1. 播放到想要的起点，点「取当前为起点」'),
          h('div', {}, '2. 在「起点 / 终点」框里填时间，格式可以是 0:05、1:05、0:01:30'),
          h('div', {}, '3. 「快速裁剪」几秒出结果但对齐关键帧；「精确裁剪」逐帧准确但慢一些'),
          h('div', {}, '4. 裁完会在原文件旁边生成一个新文件，界面上会提示文件名')),
        h('div', { class: 'hint-box', style: { marginTop: '10px' } },
          h('span', { html: icon('warning_amber', 'outlined', 20) }),
          h('div', {}, '起止时间超出视频长度会直接报错，不会产出半个文件。'))),
    );
  };

  mount(view, h('div', { class: 'page' },
    h('div', { class: 'page-head' },
      h('h1', { class: 'page-title' }, '视频剪辑'),
      h('div', { class: 'spacer' }),
      pill('快速 / 精确裁剪', 'mute')),
    h('p', { class: 'page-sub' },
      '本地视频和已下载的视频都能裁。裁出来的新文件保存在原视频旁边。'),
    holder));
  paint();
}

const clipState = { paths: [] };
