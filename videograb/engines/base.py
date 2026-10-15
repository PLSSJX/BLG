"""抓取通道基类与 DASH / durl 解析helpers。"""
from __future__ import annotations

from ..models import Durl, PlayInfo, Stream
from ..quality import QUALITY_TABLE, best as best_qn, short as q_short, weight as q_weight


class EngineUnavailable(Exception):
    """通道在当前环境下不可用（缺依赖、缺登录态等）。"""


class Engine:
    id = "base"
    name = "基础通道"
    desc = ""
    kind = "api"            # api | web | app | tv | pgc | thirdparty | external
    badge = ""
    needs: list[str] = []   # cookie / external

    # ---------- 生命周期 ----------
    def available(self, ctx) -> tuple[bool, str]:
        return True, ""

    def fetch_info(self, ctx, ref):
        raise NotImplementedError

    def fetch_play(self, ctx, ref, page, qn: int = 0) -> PlayInfo:
        raise NotImplementedError

    # ---------- 公共工具 ----------
    @staticmethod
    def require_video_ref(ref) -> None:
        if ref.kind != "video" or not (ref.bvid or ref.aid):
            raise EngineUnavailable("该通道只处理普通投稿视频")

    @staticmethod
    def ident(ref) -> str:
        return f"bvid={ref.bvid}" if ref.bvid else f"aid={ref.aid}"

    @staticmethod
    def page_cid(info, ref, page) -> int:
        target = info.page_by_index(page)
        if target and target.cid:
            return target.cid
        return info.cid

    def make_play(self, payload: dict, qn: int, note: str = "") -> PlayInfo:
        """把任意通道返回的 playurl 结构统一成 PlayInfo。"""
        play = PlayInfo(
            engine=self.id,
            engine_name=self.name,
            note=note,
            duration=int(payload.get("timelength") or 0) // 1000
            or int(payload.get("duration") or 0),
        )

        dash = payload.get("dash") or {}
        if dash:
            play.is_dash = True
            play.duration = int(dash.get("duration") or play.duration)
            play.videos = self._parse_video_streams(dash, play.duration)
            play.audios = self._parse_audio_streams(dash, play.duration)

        durl = payload.get("durl") or []
        if durl:
            play.is_dash = play.is_dash and False
            for idx, item in enumerate(durl):
                play.durl.append(Durl(
                    order=int(item.get("order") or idx + 1),
                    url=item.get("url") or "",
                    size=int(item.get("size") or 0),
                    length=int(item.get("length") or 0) // 1000,
                    backups=list(item.get("backup_url") or []),
                ))
            if not play.duration and play.durl:
                play.duration = max(d.length for d in play.durl)

        formats = payload.get("support_formats") or []
        play.support_formats = [
            {
                "quality": int(f.get("quality") or 0),
                "label": f.get("new_description") or f.get("display_desc") or q_short(int(f.get("quality") or 0)),
                "superscript": f.get("superscript") or "",
                "format": f.get("format") or "",
            }
            for f in formats
        ]

        # 接口的 accept_quality 会"虚报"：未登录时它可能声称支持 1080P+，
        # 而 dash.video 里其实只下发了 480P。所以真实可选项一律以实际流为准，
        # 接口声明的那份另外保留成 declared_quality 供界面提示。
        declared = [int(q) for q in (payload.get("accept_quality") or [])]
        if not declared and play.support_formats:
            declared = [f["quality"] for f in play.support_formats]
        actual: list[int] = []
        if play.videos:
            actual = sorted({v.qn for v in play.videos if v.qn}, key=q_weight, reverse=True)
        elif play.durl:
            actual = [int(payload.get("quality") or qn or 64)]

        play.accept_quality = [q for q in actual if q in QUALITY_TABLE] or actual
        play.accept_desc = [
            QUALITY_TABLE.get(q, ("", str(q), 0))[0] for q in play.accept_quality
        ]
        play.declared_quality = [q for q in declared if q in QUALITY_TABLE]
        # 接口说支持、但实际没下发的那几档，界面会标注成"需登录 / 未下发"
        play.phantom_quality = [q for q in play.declared_quality if q not in play.accept_quality]
        return play

    @staticmethod
    def _parse_video_streams(dash: dict, duration: int) -> list[Stream]:
        out: list[Stream] = []
        for item in dash.get("video") or []:
            bandwidth = int(item.get("bandwidth") or 0)
            # B 站的 DASH 不返回 size，只有码率。这里必须把"估算值"和
            # "真实值"分开：估算值一旦被下载器当成真实文件长度用，
            # 分块边界就会错位，最后一块永远对不上。
            size = int(item.get("size") or 0)
            hint = bandwidth * duration // 8 if duration else 0
            qn = int(item.get("id") or 0)
            out.append(Stream(
                id=qn, qn=qn,
                label=q_short(qn),
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                codecs=item.get("codecs") or "",
                bandwidth=bandwidth,
                size=size, size_hint=hint,
                url=item.get("baseUrl") or item.get("base_url") or "",
                backups=list(item.get("backupUrl") or item.get("backup_url") or []),
                mime=item.get("mimeType") or item.get("mime_type") or "video/mp4",
                fps=float(item.get("frameRate") or item.get("frame_rate") or 0) or 0.0,
                kind="video",
            ))
        out.sort(key=lambda s: (s.height, s.bandwidth), reverse=True)
        return out

    @staticmethod
    def _parse_audio_streams(dash: dict, duration: int) -> list[Stream]:
        out: list[Stream] = []
        sources: list[tuple[str, list]] = []
        if dash.get("audio"):
            sources.append(("normal", list(dash["audio"])))
        if isinstance(dash.get("dolby"), dict) and dash["dolby"].get("audio"):
            sources.append(("dolby", list(dash["dolby"]["audio"])))
        if isinstance(dash.get("flac"), dict) and dash["flac"].get("audio"):
            flac = dash["flac"]["audio"]
            sources.append(("flac", flac if isinstance(flac, list) else [flac]))

        extra_label = {"normal": "", "dolby": "杜比全景声", "flac": "Hi-Res 无损"}
        for tag, items in sources:
            for item in items:
                if not isinstance(item, dict):
                    continue
                bandwidth = int(item.get("bandwidth") or 0)
                size = int(item.get("size") or 0)
                hint = bandwidth * duration // 8 if duration else 0
                aid = int(item.get("id") or 0)
                out.append(Stream(
                    id=aid, qn=aid,
                    label=extra_label[tag] or f"音频 {aid}",
                    codecs=item.get("codecs") or "",
                    bandwidth=bandwidth, size=size, size_hint=hint,
                    url=item.get("baseUrl") or item.get("base_url") or "",
                    backups=list(item.get("backupUrl") or item.get("backup_url") or []),
                    mime=item.get("mimeType") or item.get("mime_type") or "audio/mp4",
                    kind="audio", audio_quality=aid,
                    note=extra_label[tag],
                ))
        out.sort(key=lambda s: s.bandwidth, reverse=True)
        return out

    @staticmethod
    def pick_stream(streams: list[Stream], qn: int) -> Stream | None:
        """按用户要求挑流：指定 qn 优先，否则给最高的。"""
        if not streams:
            return None
        if qn:
            exact = [s for s in streams if s.qn == qn]
            if exact:
                return max(exact, key=lambda s: (s.height, s.bandwidth))
            # 退而求其次：不高于目标画质的最高一档
            lower = [s for s in streams if q_weight(s.qn) <= q_weight(qn)]
            if lower:
                return max(lower, key=lambda s: (s.height, s.bandwidth))
        return max(streams, key=lambda s: (s.height, s.bandwidth))


def dash_payload_from(data: dict) -> dict:
    """部分接口把 dash 直接放在 data 下，部分是 data.dash，统一一下。"""
    if not isinstance(data, dict):
        return {}
    if "dash" in data or "durl" in data:
        return data
    for key in ("playurl", "play_url", "video_info"):
        inner = data.get(key)
        if isinstance(inner, dict) and ("dash" in inner or "durl" in inner):
            return inner
    return data
