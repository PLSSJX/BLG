"""歌单解析：把各平台的歌单链接变成一份歌曲清单。

目前支持网易云。QQ音乐 / 酷狗 / 酷我也能按同样的方式扩展（各自的接口不同）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://music.163.com/", "Cookie": "appver=2.0.2"}

# 尽量放宽：官方分享出来的形态有好几种，例如
#   music.163.com/playlist?id=…
#   music.163.com/#/my/m/music/playlist?id=…
NETEASE_PLAYLIST = re.compile(r"music\.163\.com/\S*?playlist\??.*?[?&#]id=(\d+)", re.I)
NETEASE_PLAYLIST_ALT = re.compile(r"music\.163\.com/playlist\?id=(\d+)", re.I)


class PlaylistError(RuntimeError):
    pass


def _get(url: str, timeout: int = 30) -> dict:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise PlaylistError("请求歌单失败：" + str(exc)[:140]) from None


def _netease(pid: str, limit: int = 200) -> dict:
    data = _get("https://music.163.com/api/v6/playlist/detail?id=" + pid)
    playlist = data.get("playlist") or {}
    if not playlist:
        raise PlaylistError("网易云没有返回这个歌单（可能需要登录，或歌单不存在）")

    ids = [t.get("id") for t in (playlist.get("trackIds") or []) if t.get("id")]
    ids = ids[:limit]
    if not ids:
        raise PlaylistError("这个歌单里没有歌曲")

    # v6 的 playlist 只带前 10 首的完整信息，其余要按 id 批量取
    songs: list = []
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        url = ("https://music.163.com/api/song/detail?ids="
               + urllib.parse.quote(json.dumps(chunk)))
        for row in (_get(url).get("songs") or []):
            artists = row.get("ar") or row.get("artists") or []
            songs.append({
                "id": str(row.get("id")),
                "name": row.get("name") or "",
                "artist": "、".join(a.get("name") or "" for a in artists) or "未知",
                "album": ((row.get("al") or row.get("album") or {}) or {}).get("name") or "",
                "url": "https://music.163.com/song?id=" + str(row.get("id")),
            })
    # 真实曲目数以上游为准：trackIds 才是完整列表，songs 可能被 limit 截过。
    # 只报 len(songs) 会让 500 首的歌单显示成「共 200 首」，被砍掉的没有任何提示。
    total = int(playlist.get("trackCount") or len(playlist.get("trackIds") or []) or len(songs))
    return {
        "site": "wangyi",
        "id": pid,
        "title": playlist.get("name") or "未命名歌单",
        "cover": playlist.get("coverImgUrl") or "",
        "count": len(songs),
        "total": total,
        "truncated": len(songs) < total,
        "songs": songs,
    }


QQ_PLAYLIST = re.compile(r"y\.qq\.com/(?:n/)?(?:ryqq|yqq)/playlist/(\d+)", re.I)
QQ_PLAYLIST_ALT = re.compile(r"y\.qq\.com/.*?disstid=(\d+)", re.I)
# App 分享出来的 taoge 链接：i.y.qq.com/n2/m/share/details/taoge.html?…&id=…
QQ_TAOGE = re.compile(r"(?:i\.)?y\.qq\.com/\S*?taoge\.html\?.*?[?&]id=(\d+)", re.I)

_QQ_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://y.qq.com/"}


def _qq(disstid: str, limit: int = 200) -> dict:
    """QQ 音乐歌单。

    走的是播放页自己在用的老接口 fcg_ucc_getcdinfo_byids_cp.fcg，
    不需要登录、不需要签名，返回里 cdlist[0].songlist 就是全部曲目。
    """
    url = ("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
           "?type=1&json=1&utf8=1&onlysong=0&format=json&g_tk=5381"
           "&loginUin=0&hostUin=0&disstid=" + disstid)
    try:
        req = urllib.request.Request(url, headers=_QQ_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise PlaylistError("请求 QQ 音乐歌单失败：" + str(exc)[:140]) from None

    cdlist = data.get("cdlist") or []
    if not cdlist:
        raise PlaylistError("QQ 音乐没有返回这个歌单（可能已删除，或需要在网页上登录才能看）")
    cd = cdlist[0]
    songs: list = []
    for row in (cd.get("songlist") or [])[:limit]:
        mid = str(row.get("songmid") or row.get("mid") or "").strip()
        if not mid or mid == "0":
            continue
        singers = row.get("singer") or []
        songs.append({
            "id": str(mid),
            "name": row.get("songname") or row.get("name") or "",
            "artist": "、".join(p.get("name") or "" for p in singers) or "未知",
            "album": row.get("albumname") or "",
            "url": "https://y.qq.com/n/ryqq/songDetail/" + str(mid),
        })
    if not songs:
        raise PlaylistError("这个 QQ 音乐歌单里没有歌曲")
    total = int(cd.get("songnum") or len(songs))
    return {
        "site": "qqmusic",
        "id": disstid,
        "title": cd.get("dissname") or "未命名歌单",
        "cover": cd.get("logo") or "",
        "count": len(songs),
        "total": total,
        "truncated": len(songs) < total,
        "songs": songs,
    }


KUWO_PLAYLIST = re.compile(r"kuwo\.cn/(?:playlist_detail/|playlist/)(\d+)", re.I)
KUWO_PLAYLIST_ALT = re.compile(r"kuwo\.cn/.*?[?&](?:pid|id)=(\d+)")


def _kuwo(pid: str, limit: int = 200) -> dict:
    """酷我歌单。

    新版 www.kuwo.cn/api/www/... 需要 csrf token（不带就回
    "The request is illegal!"），但老的 nplserver.kuwo.cn/pl.svc 不需要。
    """
    url = ("https://nplserver.kuwo.cn/pl.svc?op=getlistinfo&pid=" + pid
           + "&pn=0&rn=" + str(min(limit, 200)) + "&encode=utf8&keyset=pl2012&identity=kuwo&pcmp4=1")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.kuwo.cn/"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise PlaylistError("请求酷我歌单失败：" + str(exc)[:140]) from None
    if str(data.get("result")) != "ok":
        raise PlaylistError("酷我没有返回这个歌单（可能已删除或需要登录）")
    rows = data.get("musiclist") or []
    songs: list = []
    for row in rows[:limit]:
        rid = row.get("rid") or row.get("id")
        if not rid:
            continue
        songs.append({
            "id": str(rid),
            "name": row.get("name") or row.get("songname") or "",
            "artist": row.get("artist") or row.get("singer") or "未知",
            "album": row.get("album") or "",
            "url": "https://www.kuwo.cn/play_detail/" + str(rid),
        })
    if not songs:
        # 实测：老接口还能返回歌单名、封面、总曲目数（total 有值），
        # 但 musiclist 已经固定是空的；新版 api/www 要 csrf token，
        # 而首页现在只下发反爬 cookie（Hm_Iuvt_…）不再给 kw_token。
        raise PlaylistError(
            "酷我的老接口还能读出歌单信息（《" + str(data.get("title") or "") + "》共 "
            + str(data.get("total") or 0) + " 首），但**已经不再返回歌曲列表**，"
            "新版接口又被反爬拦住了。酷我歌单暂时不可用。")
    return {
        "site": "kuwo",
        "id": pid,
        "title": data.get("title") or "未命名歌单",
        "cover": data.get("pic") or "",
        "count": int(data.get("total") or len(songs)),
        "songs": songs,
    }


def fetch(text: str, limit: int = 200) -> dict:
    """把歌单链接解析成歌曲清单。识别不出平台就抛 PlaylistError。"""
    raw = (text or "").strip()
    if not raw:
        raise PlaylistError("请先粘贴歌单链接")
    for pattern in (NETEASE_PLAYLIST, NETEASE_PLAYLIST_ALT):
        match = pattern.search(raw)
        if match:
            return _netease(match.group(1), limit)
    for pattern in (QQ_PLAYLIST, QQ_PLAYLIST_ALT, QQ_TAOGE):
        match = pattern.search(raw)
        if match:
            return _qq(match.group(1), limit)
    for pattern in (KUWO_PLAYLIST, KUWO_PLAYLIST_ALT):
        match = pattern.search(raw)
        if match:
            return _kuwo(match.group(1), limit)
    if re.fullmatch(r"\d{5,}", raw):
        # 纯数字：按网易云歌单 id 处理
        return _netease(raw, limit)
    raise PlaylistError(
        "没有认出这个歌单链接。目前支持：\n"
        "· 网易云 music.163.com/playlist?id=…\n"
        "· QQ音乐 y.qq.com/n/ryqq/playlist/…\n"
        "酷我（歌曲列表已被移除）、酷狗（要签名）、Spotify（要 OAuth）目前不支持歌单。")


# ---------------------------------------------------------------- 本地歌单

import os
import tempfile
import threading
import time
from pathlib import Path

from .config import DATA_DIR

PLAYLISTS_PATH = DATA_DIR / "playlists.json"
MAX_LOCAL = 50                       # 本地最多留这么多份歌单，超出的丢最旧的

# playlists.json 的"读 → 改 → 写"必须整段串行。
#
# server.py 用的是 ThreadingHTTPServer，两个请求（比如连着收藏两份歌单）会同时
# 进来，而 add_local 是"读全表 → 插一条 → 写全表"：
#   · 后写的那个把先写的整份覆盖掉 —— 实测 20 线程并发保存最后只剩 1 份；
#   · Windows 上 os.replace 撞上另一个线程正在读的句柄会直接抛
#     PermissionError [WinError 5]，实测 20 线程里 14 个线程炸掉、残留 15 个 .tmp。
# tasks.py 里 _save_lock 是同一类问题的既有修法，这里补上同样的一把锁。
_STORE_LOCK = threading.RLock()


def _decode(raw: str) -> list:
    """把文件内容解析成歌单列表。结构和内容都不对时抛 ValueError。"""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("顶层不是 JSON 对象")
    items = data.get("playlists")
    if not isinstance(items, list):
        raise ValueError("playlists 不是数组")
    # 只留 dict：文件被手工改坏成 [1, 2, 3] 时，后面 x.get() 会抛 AttributeError，
    # 保存功能从此永远 500，用户只能自己删文件才能恢复。
    return [x for x in items if isinstance(x, dict)]


def _read_state() -> tuple:
    """读歌单表，返回 (items, state)。

    state：ok / missing / corrupt（内容读不懂）/ unreadable（文件被占用读不了）。
    "文件不存在"和"读不了"必须分开：后者若也当成空表，下一次保存就会把
    用户已经收藏的歌单全部覆盖掉（实测一次 read 失败丢 10 份）。
    """
    for attempt in range(4):
        try:
            # utf-8-sig：记事本另存为会带 BOM，带 BOM 时 json.loads 直接抛错，
            # 于是整份收藏在界面上"凭空消失"（然后被下一次保存抹掉）。
            raw = PLAYLISTS_PATH.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return [], "missing"
        except UnicodeDecodeError:
            return [], "corrupt"
        except OSError:
            time.sleep(0.05 * (attempt + 1))     # 可能正被另一个线程 replace
            continue
        try:
            return _decode(raw), "ok"
        except ValueError:
            return [], "corrupt"
    return [], "unreadable"


def _quarantine() -> None:
    """把读不懂的文件挪到 .bad，别让下一次保存把它直接覆盖掉。"""
    try:
        os.replace(PLAYLISTS_PATH, PLAYLISTS_PATH.with_suffix(".json.bad"))
    except OSError:
        pass


def _load_for_update() -> list:
    """保存/删除前读一次表。读不了就报错：宁可这次保存失败，也不覆盖旧数据。"""
    items, state = _read_state()
    if state == "corrupt":
        _quarantine()
        return []
    if state == "unreadable":
        raise PlaylistError(
            "本地歌单文件读不出来（" + str(PLAYLISTS_PATH) + "）。"
            "为避免把已经收藏的歌单覆盖掉，这次没有保存，请稍后重试。")
    return items


def _replace_with_retry(source: str, target) -> None:
    """os.replace 在 Windows 上会被"目标文件正被打开"打断（WinError 5/32），退一步重试。"""
    for index in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if index == 4:
                raise
            time.sleep(0.03 * (index + 1))


def load_local() -> list:
    """读回本地收藏。任何读取失败都返回空表，绝不抛异常（界面要能起来）。"""
    with _STORE_LOCK:
        items, _state = _read_state()
    return items


def save_local(items: list) -> None:
    items = [x for x in (items or []) if isinstance(x, dict)]
    target_dir = PLAYLISTS_PATH.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"playlists": items}, ensure_ascii=False, indent=1)
    handle, name = tempfile.mkstemp(dir=str(target_dir), prefix="playlists-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        with _STORE_LOCK:
            _replace_with_retry(name, PLAYLISTS_PATH)
    except BaseException:
        # 落盘失败时别把半截 tmp 留在 data/ 里（实测能攒出十几个）
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def add_local(pl: dict) -> dict:
    """把一份歌单收进本地。同 id 的覆盖，不重复累积。

    校验放在这里而不是只放在接口层：以前接口传一个 {} 也能"保存成功"，
    于是 playlists.json 里会多出一条 id=""、site="" 的空歌单，界面上显示成
    「未命名歌单」，而且 remove_local("", "") 还能把它删掉 —— 空标识一旦
    混进比较条件，删的就是"所有空标识"的那条，不是用户点的那条。
    """
    if not isinstance(pl, dict):
        raise ValueError("歌单数据格式不对")
    pid = str(pl.get("id") or "").strip()
    site = str(pl.get("site") or "").strip()
    if not pid:
        raise ValueError("歌单缺少 id")
    if not site:
        raise ValueError("歌单缺少来源平台")
    songs = pl.get("songs") or []
    if not isinstance(songs, list):
        raise ValueError("歌单里的歌曲列表格式不对")
    entry = {
        "id": pid,
        "site": site,
        "title": str(pl.get("title") or "").strip() or "未命名歌单",
        "cover": str(pl.get("cover") or ""),
        "songs": songs,
        "saved_at": int(time.time()),
    }
    # 读-改-写整段必须在锁内。只锁住"写"是不够的：两个线程会先后读到同一份旧表，
    # 后写的把先写的那条盖掉（实测 10 条已有 + 5 个并发保存 = 只剩 11 条）。
    with _STORE_LOCK:
        items = _load_for_update()
        items = [x for x in items
                 if str(x.get("id")) != entry["id"] or x.get("site") != entry["site"]]
        items.insert(0, entry)
        save_local(items[:MAX_LOCAL])
    return entry


def remove_local(site: str, pid: str) -> bool:
    with _STORE_LOCK:
        items = _load_for_update()
        kept = [x for x in items
                if not (str(x.get("id")) == str(pid) and x.get("site") == site)]
        if len(kept) == len(items):
            return False
        save_local(kept)
    return True


# ---------------------------------------------------------------- 换源搜索

SITE_NAMES = {"wangyi": "网易云音乐", "qqmusic": "QQ音乐", "kuwo": "酷我", "kugou": "酷狗"}


def _search_netease(keyword: str, limit: int = 10, errors: list | None = None) -> list:
    url = ("https://music.163.com/api/search/get?s=" + urllib.parse.quote(keyword)
           + "&type=1&limit=" + str(limit))
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        if errors is not None:
            errors.append("网易云搜索失败：" + str(exc)[:120])
        return []
    out = []
    for s in ((raw.get("result") or {}).get("songs") or []):
        artists = s.get("artists") or []
        out.append({
            "site": "wangyi",
            "site_name": SITE_NAMES["wangyi"],
            "id": str(s.get("id")),
            "name": s.get("name") or "",
            "artist": "、".join(a.get("name") or "" for a in artists),
            "url": "https://music.163.com/song?id=" + str(s.get("id")),
        })
    return out


def _search_qq(keyword: str, limit: int = 10, errors: list | None = None) -> list:
    # client_search_cp 这个老接口现在固定回 500（实测 HTTPError 500），
    # 而 search_for_qq_cp 返回同样的 data.song.list 结构、仍然可用。
    # 之前用它 + except 静默返回 []，结果换源搜索里 QQ 音乐一条都出不来，
    # 界面上看着"就是没有 QQ 结果"，没有任何提示。
    url = ("https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp?p=1&n=" + str(limit)
           + "&w=" + urllib.parse.quote(keyword)
           + "&format=json&inCharset=utf8&outCharset=utf-8&platform=yqq.json&needNewCode=0")
    try:
        req = urllib.request.Request(url, headers=_QQ_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        if errors is not None:
            errors.append("QQ音乐搜索失败：" + str(exc)[:120])
        return []
    out = []
    song = (raw.get("data") or {}).get("song") or {}
    for x in (song.get("list") or []):
        mid = str(x.get("songmid") or "").strip()
        # 实测这个接口会混进 songmid="0" 的条目（如 "First Love/宇多田光"），
        # 拼出来的歌曲页是 .../songDetail/0，点开是空页 —— 直接丢掉。
        if not mid or mid == "0":
            continue
        singers = x.get("singer") or []
        out.append({
            "site": "qqmusic",
            "site_name": SITE_NAMES["qqmusic"],
            "id": str(mid),
            "name": x.get("songname") or "",
            "artist": "、".join(s.get("name") or "" for s in singers),
            "url": "https://y.qq.com/n/ryqq/songDetail/" + str(mid),
        })
    return out


# 上一次换源搜索里各平台的失败原因。界面不显示它，但接口会带回去，
# 免得某个平台挂了以后只表现为"就是搜不到"，无从排查。
_LAST_SEARCH_ERRORS: list = []


def last_search_errors() -> list:
    return list(_LAST_SEARCH_ERRORS)


def search(keyword: str, limit: int = 10) -> list:
    """换源搜索：同时查网易云和 QQ 音乐，每条结果都标出它来自哪个平台。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    errors: list = []
    half = max(3, limit // 2)
    found = _search_netease(kw, half, errors) + _search_qq(kw, half, errors)
    _LAST_SEARCH_ERRORS[:] = errors
    # 去重只在同一个平台内部做。
    # 以前是全局按"同名同歌手"去重、网易云优先，于是 QQ 那边同一首歌**必然**被
    # 丢掉 —— 而换源搜索要的正是"同一首歌在另一个平台上的版本"，等于把功能自己
    # 取消了（实测搜 "First Love 宇多田" 只剩网易云 4 条）。
    seen: set = set()
    unique = []
    for item in found:
        key = (item.get("site") or "",
               (item.get("name") or "").strip().lower(),
               (item.get("artist") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit]
