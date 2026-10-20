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


_AUDIO_EXT = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".wma", ".ape"}


def _pick_audio(files) -> str:
    """从任务产物里挑出音频文件路径。"""
    for f in files or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        if not path:
            continue
        if f.get("kind") == "audio" or Path(path).suffix.lower() in _AUDIO_EXT:
            return path
    return ""


def find_local(site: str, pid: str):
    """按 site+id 找歌单，找不到返回 None。"""
    for pl in load_local():
        if str(pl.get("id")) == str(pid) and pl.get("site") == site:
            return pl
    return None


_FAILED_STATUS = {"error", "failed", "canceled", "cancelled", "stopped"}


def advance(get_task, submit=None, limit: int = 5) -> int:
    """把还没排上队的歌补进去，让队列一直保持 limit 个在跑。

    「中止一首之后要接下一首」就是靠这里：某个任务被取消或失败后，
    它不再算"在跑"，下一首自然会被补上。
    """
    if submit is None:
        return 0
    with _STORE_LOCK:
        items = _load_for_update()
    changed = 0
    for pl in items:
        # 被「取消下载」过的歌单处于暂停状态，不要再自动排队 ——
        # 用户点的是"整体取消"，如果还自动接下一首，等于没取消。
        if pl.get("paused"):
            continue
        songs = pl.get("songs") or []
        live = 0
        pending = []
        for song in songs:
            if song.get("local"):
                continue
            tid = song.get("task_id")
            if not tid:
                pending.append(song)
                continue
            try:
                task = get_task(tid)
            except Exception:
                task = None
            if task is None:
                pending.append(song)
                continue
            status = str(getattr(task, "status", "")).lower()
            if status in _FAILED_STATUS:
                # 失败/取消：清掉关联，但**必须计次**。
                # 之前这里无条件重新入队，而 advance 在每次读歌单时都会跑，
                # 结果一首一直失败的歌会被无限重试（列表每 0.8 秒刷新一次），
                # 任务列表会被同一个失败刷屏。
                song.pop("task_id", None)
                tries = int(song.get("attempts") or 0) + 1
                song["attempts"] = tries
                if tries < 2:
                    pending.append(song)
                else:
                    song["gave_up"] = True     # 放弃自动重试，等用户手动
            else:
                song["attempts"] = 0
                live += 1
        for song in pending:
            if live >= limit:
                break
            if song.get("gave_up"):
                continue
            # 本地歌曲（url 是 file:///）不该进下载队列 —— 它已经在磁盘上了。
            # 之前没挡，这些条目会被当成待下载的歌排进队列，下载器报
            # "无法识别的链接"，界面上就出现一条莫名其妙的失败记录。
            if not str(song.get("url") or "").startswith(("http://", "https://")):
                continue
            try:
                task = submit(str(song.get("url") or ""))
                song["task_id"] = task.id
                live += 1
                changed += 1
            except Exception:
                continue
    if changed:
        save_local(items)
    return changed


def set_paused(site: str, pid: str, paused: bool) -> None:
    """暂停/恢复一个歌单的自动排队。"""
    with _STORE_LOCK:
        items = _load_for_update()
    for pl in items:
        if str(pl.get("id")) == str(pid) and pl.get("site") == site:
            if paused:
                pl["paused"] = True
            else:
                pl.pop("paused", None)
    save_local(items)


def cancel_all(get_task, cancel) -> int:
    """取消这个歌单里所有还在跑的任务，并把歌单标成暂停。返回取消数量。"""
    stopped = 0
    for pl in load_local():
        for song in (pl.get("songs") or []):
            tid = song.get("task_id")
            if not tid or song.get("local"):
                continue
            try:
                task = get_task(tid)
                status = str(getattr(task, "status", "")).lower() if task else ""
                if task is None or status in _FAILED_STATUS or status == "done":
                    continue
                cancel(tid)
                stopped += 1
            except Exception:
                continue
    return stopped


def backfill(get_task) -> int:
    """把已经下载完的任务回填成歌单条目的本地路径。

    用「懒回填」而不是后台线程：每次读歌单时顺手查一下，永远准确，
    不用管线程生命周期，也不会漏掉程序关闭期间完成的任务。
    """
    try:
        from .tasks import STATUS_DONE
    except Exception:
        STATUS_DONE = "done"
    with _STORE_LOCK:
        items = _load_for_update()
    changed = 0
    for pl in items:
        for song in (pl.get("songs") or []):
            if song.get("local") or not song.get("task_id"):
                continue
            try:
                task = get_task(song["task_id"])
            except Exception:
                continue
            if task is None or getattr(task, "status", "") != STATUS_DONE:
                continue
            path = _pick_audio(getattr(task, "files", None))
            if path:
                song["local"] = path
                changed += 1
    if changed:
        save_local(items)
    return changed


def create_empty(title: str) -> dict:
    """手动新建一个空歌单。"""
    name = (title or "").strip()
    if not name:
        raise ValueError("请填写歌单名称")
    if len(name) > 60:
        name = name[:60]
    pid = "local-" + str(int(time.time() * 1000))
    return add_local({"id": pid, "site": "local", "title": name, "songs": []})


def add_song(site: str, pid: str, song: dict) -> dict:
    """往指定歌单里加一首歌（提供完整歌曲信息，不只是 id）。"""
    if not isinstance(song, dict):
        raise ValueError("歌曲数据格式不对")
    url = str(song.get("url") or "").strip()
    name = str(song.get("name") or "").strip()
    if not url or not name:
        raise ValueError("歌曲缺少名字或地址")
    with _STORE_LOCK:
        items = _load_for_update()
        target = next((x for x in items
                       if str(x.get("id")) == str(pid) and x.get("site") == site), None)
        if target is None:
            raise ValueError("找不到这个歌单")
        songs = list(target.get("songs") or [])
        if any(str(s.get("url")) == url for s in songs):
            # 已经有了：把 added=False 带回去，前端才能提示"已在歌单内"，
            # 而不是每次都假装加成功了。
            already = dict(target)
            already["added"] = False
            return already
        entry = {"id": str(song.get("id") or ""), "name": name,
                 "artist": str(song.get("artist") or ""),
                 "url": url}
        # 这几个字段是「导入并下载」流程要用的：
        #   cover    界面缩略图
        #   task_id  对应的下载任务，用来判断下载完了没有
        #   local    下载完成后的本地路径，播放实际用它
        # 以前这里只存四个字段，多传的会被丢掉。
        for extra in ("cover", "task_id", "local"):
            if song.get(extra):
                entry[extra] = song[extra]
        songs.append(entry)
        target["songs"] = songs
        save_local(items)
        return target


def _song_key(url: str) -> str:
    """把歌曲链接归一成一个稳定的 key。

    同一首歌的链接写法千差万别：
        https://music.163.com/song?id=123
        https://music.163.com/#/song?id=123&userid=456
        https://music.163.com/m/song?id=123
    按字符串比对永远匹配不上，所以提取平台 + 歌曲 id 作为 key。
    """
    import re as _re
    u = str(url or "").strip()
    if not u:
        return ""
    m = _re.search(r"[?&#]id=(\d+)", u)
    if m and "music.163.com" in u:
        return "netease:" + m.group(1)
    m = _re.search(r"songmid=([0-9A-Za-z]+)", u)
    if m:
        return "qq:" + m.group(1)
    m = _re.search(r"[?&#]id=(\d+)", u)
    if m:
        return "id:" + m.group(1)
    return u


def find_existing_local(url: str) -> str:
    """跨歌单找同一首歌已经下载好的本地文件。

    同一首歌在多个歌单里出现很常见（比如两个歌单都收了《海阔天空》）。
    只要有一个歌单下过、文件还在，其余歌单直接复用，不用重复下载。
    """
    want = _song_key(url)
    if not want:
        return ""
    for pl in load_local():
        for song in (pl.get("songs") or []):
            if _song_key(song.get("url")) != want:
                continue
            local = str(song.get("local") or "")
            if local and Path(local).exists():
                return local
    return ""


def set_song_task(site: str, pid: str, url: str, task_id: str) -> None:
    """记下这首歌对应的下载任务 id，下载完成后靠它回填本地路径。"""
    with _STORE_LOCK:
        items = _load_for_update()
    for pl in items:
        if str(pl.get("id")) == str(pid) and pl.get("site") == site:
            for song in (pl.get("songs") or []):
                if str(song.get("url")) == str(url):
                    song["task_id"] = task_id
    save_local(items)


def set_song_local(site: str, pid: str, url: str, local: str) -> None:
    """把本地路径写到指定歌单条目上。"""
    with _STORE_LOCK:
        items = _load_for_update()
    for pl in items:
        if str(pl.get("id")) == str(pid) and pl.get("site") == site:
            for song in (pl.get("songs") or []):
                if str(song.get("url")) == str(url):
                    song["local"] = local
                    song.pop("missing", None)
                    song.pop("gave_up", None)
                    song["attempts"] = 0
    save_local(items)


def reset_song(site: str, pid: str, url: str) -> dict:
    """清掉某首歌的「已放弃重试」标记，让它重新进入自动排队。

    用户在界面上点「重试」时调用 —— 否则一首被放弃的歌永远不会再试，
    用户也没有任何办法恢复，只能删了重加。
    """
    with _STORE_LOCK:
        items = _load_for_update()
    target = next((x for x in items
                   if str(x.get("id")) == str(pid) and x.get("site") == site), None)
    if target is None:
        raise ValueError("找不到这个歌单")
    for song in (target.get("songs") or []):
        if str(song.get("url")) == str(url):
            song.pop("gave_up", None)
            song["attempts"] = 0
            song.pop("task_id", None)
            break
    save_local(items)
    return target


def remove_song(site: str, pid: str, url: str) -> dict:
    with _STORE_LOCK:
        items = _load_for_update()
        target = next((x for x in items
                       if str(x.get("id")) == str(pid) and x.get("site") == site), None)
        if target is None:
            raise ValueError("找不到这个歌单")
        target["songs"] = [s for s in (target.get("songs") or [])
                           if str(s.get("url")) != str(url)]
        save_local(items)
        return target


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
