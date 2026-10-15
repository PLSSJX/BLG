"""通道 6：番剧 / 影视 PGC 接口（api.bilibili.com/pgc）。"""
from __future__ import annotations

from urllib.parse import urlencode

from ..models import Owner, Page, PlayInfo, VideoInfo
from .base import Engine, EngineUnavailable

SEASON_API = "https://api.bilibili.com/pgc/view/web/season"
PGC_PLAYURL = "https://api.bilibili.com/pgc/player/web/playurl"


class PgcBangumiEngine(Engine):
    id = "pgc_bangumi"
    name = "番剧 / 影视 PGC 接口"
    desc = "处理 ep / ss 号与番剧链接，走 PGC 专用的 season 与 playurl 接口。普通投稿视频会自动忽略此通道。"
    kind = "pgc"
    badge = "番剧专用"

    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    def handles(self, ref) -> bool:
        return ref.kind == "bangumi" and bool(ref.ep_id or ref.season_id)

    # ---------- 元数据 ----------
    def fetch_info(self, ctx, ref) -> VideoInfo:
        if not self.handles(ref):
            raise EngineUnavailable("该通道只处理番剧 / 影视（ep 或 ss 号）")
        query = f"ep_id={ref.ep_id}" if ref.ep_id else f"season_id={ref.season_id}"
        data = ctx.net.api(f"{SEASON_API}?{query}",
                           referer="https://www.bilibili.com/bangumi/")

        episodes = data.get("episodes") or []
        if not episodes and data.get("section"):
            for sec in data["section"]:
                episodes.extend(sec.get("episodes") or [])
        if not episodes:
            raise EngineUnavailable("season 接口没有返回任何剧集")

        current = None
        if ref.ep_id:
            current = next((e for e in episodes if int(e.get("id") or 0) == ref.ep_id), None)
        current = current or episodes[0]

        pages = [
            Page(
                cid=int(ep.get("cid") or 0),
                page=idx + 1,
                part=ep.get("long_title") or ep.get("title") or f"第 {idx + 1} 集",
                duration=int((ep.get("duration") or 0) // 1000 or ep.get("duration") or 0),
                from_ep=True,
            )
            for idx, ep in enumerate(episodes)
        ]
        owner = ((data.get("up_info") or {}).get("uname") and {
            "name": data["up_info"]["uname"], "mid": 0, "face": ""
        }) or {"name": data.get("staff") or "", "mid": 0, "face": ""}

        if current.get("id") and not ref.ep_id:
            ref.ep_id = int(current["id"])
        ref.season_id = int(data.get("season_id") or ref.season_id or 0)

        info = VideoInfo(
            bvid=current.get("bvid") or "",
            aid=int(current.get("aid") or 0),
            title=data.get("title") or "",
            cover=data.get("cover") or current.get("cover") or "",
            desc=data.get("evaluate") or data.get("subtitle") or "",
            duration=pages[0].duration if pages else 0,
            pubdate=int(data.get("pub_time") or 0),
            tname="番剧",
            owner=Owner(mid=int(owner.get("mid") or 0), name=owner.get("name") or "",
                        face=owner.get("face") or ""),
            pages=pages,
            stat=data.get("stat") or {},
            engine=self.id,
            kind="bangumi",
            extra={
                "season_id": data.get("season_id"),
                "season_title": data.get("season_title"),
                "episodes": [
                    {"ep_id": int(e.get("id") or 0), "title": e.get("long_title") or e.get("title") or "",
                     "cover": e.get("cover") or "", "cid": int(e.get("cid") or 0)}
                    for e in episodes
                ],
                "rating": (data.get("rating") or {}).get("score"),
            },
        )
        return info

    # ---------- 播放地址 ----------
    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:  # type: ignore[override]
        if not self.handles(ref):
            raise EngineUnavailable("该通道只处理番剧 / 影视（ep 或 ss 号）")

        ep_id = ref.ep_id
        cid = 0
        if not ep_id or not cid:
            info = self.fetch_info(ctx, ref)
            target = info.page_by_index(page) or (info.pages[0] if info.pages else None)
            if target:
                cid = target.cid
            eps = info.extra.get("episodes") or []
            picked = eps[page - 1] if 0 < page <= len(eps) else (eps[0] if eps else None)
            if picked:
                ep_id = picked["ep_id"]
                cid = picked["cid"] or cid
        if not ep_id:
            raise EngineUnavailable("未能确定 ep_id")

        params = {"ep_id": ep_id, "qn": qn or 127, "fnval": 4048, "fnver": 0, "fourk": 1}
        if cid:
            params["cid"] = cid
        try:
            payload = ctx.net.api(
                f"{PGC_PLAYURL}?{urlencode(params)}",
                referer="https://www.bilibili.com/bangumi/",
            )
        except Exception as exc:
            raise EngineUnavailable(f"PGC playurl 失败：{exc}") from None

        play = self.make_play(payload, qn, note="PGC 番剧接口")
        if not play.videos and not play.durl:
            raise EngineUnavailable("PGC 接口未返回可播放地址（该内容可能需要大会员）")
        return play
