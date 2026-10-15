# Bilibili 抓取通道实测报告

- 主机: Windows, 工作区 D:\BLD, 探测脚本目录 D:\BLD\biligrab\.probe\
- Python: 3.12.14 (仅标准库 urllib/json/hashlib/struct/gzip/re)
- 测试时间: 2026-10-15 (UTC+8 晚)
- 网络: api.bilibili.com / www.bilibili.com / *.bilivideo.com 直连可用
- 样本 A: BV1GJ411x7h7 (aid=80433022, cid=137649199, 213s, 2019 年老视频)
- 样本 B: BV1Bjp966EA8 (源 2160x3840 竖屏 4K, 新热门, cid=42680649924)
- **全流程不带任何 Cookie** (nav 返回 code=-101 未登录)
- 所有请求带 Chrome 124 桌面 UA + Referer https://www.bilibili.com/, 超时 20s (第三方 15s)

---

## 0. 通道总表

| 通道 | 是否可用 | 实际最高可用画质 | 关键限制 |
|---|---|---|---|
| web-wbi (/x/player/wbi/playurl) | 可用, w_rid 被接受 | dash id=32 -> **852x480 (480P)** | 未登录 dash >480P 不发流; 需正确 mixin_key |
| web-legacy (/x/player/playurl) | 可用 (无需签名) | dash id=32 -> **852x480** | 与 wbi 版结果逐字段一致; 无签名也不拦 |
| web-legacy-durl (fnval=1&qn=64) | 可用 | **1280x720 (720P) 单文件 MP4** | 单文件渐进 MP4, moov 在前; accept_quality 只有 [64,16] |
| app-api (appkey=27eb53fc9058f8c3) | 可用, sign 被接受 | dash 852x480 / durl 720P | 无需 access_key; 结果与 web 完全相同 |
| tv-api (appkey=4409e2ce8ffd12b8) | **不可用** | - | 两个 host 全部 code=-400 "请求错误", 与参数/签名无关 |
| html-scrape __playinfo__ | **已废弃** | - | 桌面页只有 if (window.__playinfo__) 守卫, 无内联 JSON |
| html-scrape __INITIAL_STATE__ | 可用 | 无播放地址 (仅元数据) | 必须 gzip 解压; 内含 defaultWbiKey 可直接算 mixin_key |
| pgc-bangumi (pgc/player/web/playurl) | 可用 | dash 852x480; durl 仅 360P 试看 | 付费番剧只能拿免费试看档 |
| 第三方解析 (7 个指定 + 6 个额外) | **全部失效** | - | 只有 cenguigui 的 search action 活着 (纯元数据) |

---

## 1. web-wbi — 可用, w_rid 被接受

### 1.1 取 wbi key
`
GET https://api.bilibili.com/x/web-interface/nav
-> HTTP 200, code=-101 (账号未登录), message="账号未登录"
   data.wbi_img.img_url = https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png
   data.wbi_img.sub_url = https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png
   img_key = 7cd084941338484aae1ad9425b84077c
   sub_key = 4932caff0ff746eab6f01bf08b70ac45
   mixin_key(按 mixinKeyEncTab 重排取前 32 位) = ea1db124af3c7062474693fa704f4ff8
`
即使 code=-101, wbi_img 字段依然返回, **不需要登录**。

### 1.2 请求
`
GET https://api.bilibili.com/x/player/wbi/playurl
  ?bvid=BV1GJ411x7h7&cid=137649199&fnval=4048&fourk=1&qn=127&wts=1792070361
  &w_rid=517596a9f459e7dc014503fa7c31e28f
-> HTTP 200 | code=0 | message=OK | w_rid **被接受**
`
(query 按 key 排序 urlencode + mixin_key 做 md5, 与上表值一致)

### 1.3 返回
- dash: 有;  durl: 无
- quality=64  accept_quality=[112,80,64,32,16]
  accept_description=["高清 1080P+","高清 1080P","高清 720P","清晰 480P","流畅 360P"]
  support_formats=[[112,"1080P 高码率"],[80,"1080P 高清"],[64,"720P 准高清"],[32,"480P 标清"],[16,"360P 流畅"]]
- **实际 dash.video 里只有 id=32 和 id=16 —— accept_quality 是"视频存在这些档", 不是"你能拿到这些档"**
- dash.duration=213, dash.minBufferTime=1.5, dash.type=null
- dash keys = ["audio","dolby","duration","flac","minBufferTime","min_buffer_time","video"]
- dash.dolby = {"type":0,"audio":null};  dash.flac = null

dash.video 真实 id 列表 (6 条 = 2 个画质 x 3 个编码):

| id | 分辨率 | codecs | bandwidth | frameRate |
|---|---|---|---|---|
| 32 | 852x480 | hev1.1.6.L120.90 | 218550 | 25 |
| 32 | 852x480 | avc1.64001F | 786766 | 25 |
| 32 | 852x480 | av01.0.08M.08.0.110.01.01.01.0 | 184371 | 25 |
| 16 | 640x360 | hev1.1.6.L120.90 | 153220 | 25 |
| 16 | 640x360 | avc1.64001E | 350253 | 25 |
| 16 | 640x360 | av01.0.08M.08.0.110.01.01.01.0 | 155867 | 25 |

dash.audio: id=30232 (mp4a.40.2, bw=102931) / id=30216 (mp4a.40.5, 43962) / id=30280 (mp4a.40.2, 203786)

URL 前缀: https://cn-sh-fx-01-0{1,2,3}.bilivideo.com/upgcxcode/99/91/137649199/137649199_da2...m4s?...

dash.video[0] 完整字段名:
`
SegmentBase, backupUrl, backup_url, bandwidth, baseUrl, base_url, codecid, codecs,
frameRate, frame_rate, height, id, mimeType, mime_type, sar, segment_base,
startWithSap, start_with_sap, width
`
样例: SegmentBase={"Initialization":"0-1085","indexRange":"1086-1633"}
      mimeType="video/mp4", codecid=7, startWithSap=1, sar="1:1"

---

## 2. web-legacy — 可用

`
GET https://api.bilibili.com/x/player/playurl?bvid=BV1GJ411x7h7&cid=137649199&qn=127&fnval=4048&fourk=1
-> HTTP 200 | code=0 | message=OK | dash=有 | durl=无
   quality=64  accept_quality=[112,80,64,32,16]
   dash.video ids = [32, 16]  (与 web-wbi 逐字段一致, 只有 CDN 主机名不同)
`
**结论: 老接口不需要 w_rid, 返回内容与 WBI 接口等价。**

---

## 3. web-legacy-durl (fnval=1&qn=64) — 可用, 单文件 MP4

`
GET https://api.bilibili.com/x/player/playurl?bvid=BV1GJ411x7h7&cid=137649199&qn=64&fnval=1&fourk=1
-> HTTP 200 | code=0 | message=OK | dash=无 | durl=有
   quality=64  accept_quality=[64,16]
   timelength=213000
   durl[0].order=1
   durl[0].size=51973319          (51.97 MB)
   durl[0].length=213000
   durl[0].url = https://cn-sh-fx-01-0x.bilivideo.com/upgcxcode/99/91/137649199/137649199_da2...mp4?...
`
qn=127 请求时 durl 路径同样只给 quality=64 (accept_quality 仍是 [64,16]), 即 durl 通道上限 720P。

### 3.1 实测这个 MP4 的真实分辨率 (拉 3 x 64KB 头, 拼出完整 moov)
`
chunk1 Range: bytes=0-65535       -> 206, Content-Range: bytes 0-65535/51973319
chunk2 Range: bytes=65536-131071  -> 206, Content-Range: bytes 65536-131071/51973319
chunk3 Range: bytes=131072-196607 -> 206, Content-Range: bytes 131072-196607/51973319
前 16 字节 ASCII: "... ftypisom...."

box 序列 (moov 在前 = faststart):
/ftyp                                    32
/moov                                    163199
  /moov/mvhd                             108
  /moov/trak                             85435      <- 视频轨
    /moov/trak/tkhd                      92         w=1280.0 h=720.0
    /moov/trak/edts/elst                 40
    /moov/trak/mdia/mdhd                 32         timescale=16000 duration=3395840
    /moov/trak/mdia/hdlr                 45
    /moov/trak/mdia/minf/vmhd            20
    /moov/trak/mdia/minf/dinf/dref       28
    /moov/trak/mdia/minf/stbl
      stsd 170 -> [avc1, entry=154, 1280x720]
      stts 24 / stss 188 / ctts 42232 / stsc 40 / stsz 21244 / stco 21232
  /moov/trak                             77494      <- 音频轨
    /moov/trak/tkhd                      92         w=0 h=0
    /moov/trak/mdia/mdhd                 32         timescale=48000 duration=10190824
    /moov/trak/mdia/minf/smhd            16
    /moov/trak/mdia/minf/stbl
      stsd 103 -> [mp4a, entry=87]
      stts 48 / stsc 15940 / stsz 39828 / stco 21232 / sgpd 26 / sbgp 28
  /moov/udta/meta                        146
/free                                    8
/mdat                                    51810080
`
**实测: 1280x720 (720P), H.264 avc1, 音频 mp4a, 单文件, moov 在 mdat 之前 -> 可边下边播, 不需要合并器。**

---

## 4. app-api — 签名被接受

`
appkey = 27eb53fc9058f8c3
appsec = c2ed53a74eeefe3cf99fbd01d8c9c375
参数按 key 排序 urlencode + appsec -> md5 -> sign   (含 ts, 不含 access_key)

GET https://api.bilibili.com/x/player/playurl?<sorted params>&sign=<md5>

fnval=4048 -> HTTP 200 code=0 OK  dash=有 durl=无
   quality=64 accept_quality=[112,80,64,32,16]
   V id=32 852x480 hev1.1.6.L120.90 bw=218550
   V id=32 852x480 avc1.64001F        bw=786766
   V id=32 852x480 av01.0.08M.08...   bw=184371
   V id=16 640x360 hev1.1.6.L120.90   bw=153220
   V id=16 640x360 avc1.64001E        bw=350253
   V id=16 640x360 av01.0.08M.08...   bw=155867

fnval=1    -> HTTP 200 code=0 OK  dash=无 durl=有
   quality=64 accept_quality=[64,16]
   D size=51973319 length=213000
     url=https://cn-sh-fx-01-03.bilivideo.com/upgcxcode/99/91/137649199/137649199_da...mp4
`
**结论: 不带 access_key 也能用, 结果与 web 通道逐字段相同 (同一份 CDN 资源)。**

---

## 5. tv-api — 完全不可用

测试矩阵 (全部组合):

| host | 参数变体 | HTTP | code | message |
|---|---|---|---|---|
| api.snm0516.aisee.tv | 无参数 | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | 只有 appkey | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | appkey+ts 无 sign | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | appkey+ts+sign | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | 全参数正确 sign (cid/avid/qn/fnval/fnver/fourk/platform/mobi_app) | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | 全参数但 sign = "0"*32 (故意错) | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | + access_key=invalid | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | ep_id=285145 fnval=4048 | 200 | -400 | 请求错误 |
| api.snm0516.aisee.tv | fnval=1 qn=64 (想要 durl) | 200 | -400 | 请求错误 |
| api.bilibili.com | 同上全部 8 种变体 | 200 | -400 | 请求错误 |

补充对照 (证明 host 与签名方案本身没问题):
`
GET https://api.snm0516.aisee.tv/x/v2/rank?appkey=4409e2ce8ffd12b8&ts=..&sign=..
 -> HTTP 200 code=0 (正常返回排行榜数据)          <-- host 活着, 签名可用
POST https://api.snm0516.aisee.tv/x/tv/playurl   -> HTTP 405 Method Not Allowed  <-- 路由存在
GET  https://api.snm0516.aisee.tv/x/tv/playurl2  -> HTTP 404                       <-- 路径不是通配
`
**关键: /x/tv/playurl 对"无参数"和"完整正确签名"返回完全相同的 -400。不是缺参数/错签名, 而是该路由在网关层被无条件拒绝。匿名 TV 通道已死, 拿不到 durl。**
(fnval=1 的 durl 请求也因此无法验证 —— 请求根本没到业务逻辑。)

---

## 6. html-scrape — 一半可用

### 6.1 坑: 必须 gzip 解压
第一次请求返回 47259 字节乱码 —— 因为 Content-Encoding: gzip 而服务端**无视** Accept-Encoding: identity 仍返回 gzip; 直接 .decode('utf-8') 得到垃圾。

| UA | Accept-Encoding | Content-Encoding | 原始字节 | 解压后 | __playinfo__ | __INITIAL_STATE__ |
|---|---|---|---|---|---|---|
| Chrome 桌面 | gzip, deflate | gzip | 50664 | 162244 | 2 | 16 |
| Chrome 桌面 | identity | **gzip** (被无视) | 49860 | 160775 | 2 | 16 |
| Chrome 桌面 | br | **br** | 49863 | stdlib 无法解 | 0 | 0 |
| iPhone Safari | gzip, deflate | gzip | 24749 | 103907 | 0 | 1 |
| iPhone Safari | identity | 无 | 108880 | 明文 | 0 | 1 |

-> **必须显式带 Accept-Encoding: gzip, deflate, 并用 gzip magic (1f 8b) 判断解压。不要用 br —— 标准库解不了。**

### 6.2 window.__playinfo__ — 已废弃 (正则能命中但无数据)
解压后的 162KB 桌面 HTML 里 __playinfo__ 出现 2 次, 均在同一段守卫代码里:
`
viewInfo: getPlayerViewInfo(window.__INITIAL_STATE__),
i18n: { locale: lang }
}
if (window.__playinfo__) {            // <-- 只是判断, 页面上从未赋值
  primarySetting.prefetch = { playUrl: window.__playinfo__ }
}
`
统计: "dash" 出现 **0** 次, durl 出现 **0** 次, baseUrl 出现 **0** 次, playUrl 1 次。
**结论: 桌面播放页不再内联 __playinfo__ JSON, 抓该变量拿不到任何播放地址。此通道作废。**

### 6.3 window.__INITIAL_STATE__ — 命中 (但只有元数据)
`
window.__INITIAL_STATE__ = {...}   JSON 长度 80116, 顶层 key 60+ 个
  bvid=BV1GJ411x7h7  cid=137649199  aid=80433022
  videoData.title="【官方 MV】Never Gonna Give You Up - Rick Astley"
  videoData.duration=213
`
里面没有 dash/durl/baseUrl —— **不能用来取流地址, 只能取元数据。**

### 6.4 意外收获: HTML 内嵌 wbi key (可直接用)
`
__INITIAL_STATE__.defaultWbiKey = {
  "wbiImgKey": "2590160e9f5142d4a501feda0490f3bd",
  "wbiSubKey": "34ba9c5c4a824b368e9c053be34016bd"
}

mixin_key = 360959a3495d40ba026e4f813fdc45a2   (与 nav 拿到的 ea1db124... 不同)
用它签名请求 wbi/playurl -> HTTP 200 code=0 message=OK  有效
`
**可以完全跳过 /x/web-interface/nav, 直接从视频页 HTML 里抠 wbi key。**

---

## 7. pgc-bangumi — 可用 (画质受限)

注意 PGC 接口返回字段是 result 而不是 data (第一次按 data 解析拿到空)。

`
GET https://api.bilibili.com/pgc/view/web/season?season_id=26801
-> HTTP 200 code=0 message=success
   result.title="鬼灭之刃"  result.season_id=26801  episodes=26
   第 1 集: ep_id=267851  cid=125463156  aid=48487753
season_id=37138 -> code=-404 "啥都木有"
`

| 请求 | HTTP | code | dash | durl | quality |
|---|---|---|---|---|---|
| pgc/player/web/playurl?ep_id=267851&fnval=4048&qn=80&fourk=1 | 200 | 0 | 有 | 无 | 32 |
| pgc/player/web/playurl?ep_id=267851&fnval=4048&qn=127&fourk=1 | 200 | 0 | 有 | 无 | 32 |
| pgc/player/web/playurl?ep_id=267851&fnval=1&qn=64&fourk=1 | 200 | 0 | 无 | 有 | **16** |
| pgc/player/web/playurl?cid=..&ep_id=..&fnval=4048&qn=80 | 200 | 0 | 有 | 无 | 32 |
| pgc/player/web/playurl?avid=..&cid=..&ep_id=..&fnval=4048 | 200 | 0 | 有 | 无 | 32 |
| **pgc/player/api**/playurl?ep_id=..&fnval=4048&qn=80 | 200 | **-400** | 无 | 无 | - |

dash.video (只到 480P):
`
id=32 852x480 avc1.64001F                        bw=481817
id=32 852x480 hvc1.1.6.L120.90                   bw=292719
id=32 852x480 av01.0.08M.08.0.110.01.01.01.0     bw=242425
id=16 640x360 hvc1.1.6.L120.90                   bw=205561
id=16 640x360 avc1.64001E                        bw=274413
id=16 640x360 av01.0.08M.08.0.110.01.01.01.0     bw=171322
`
durl (fnval=1): size=61277053, length=1425213, quality=16 (360P 试看档)
url 前缀 https://cn-sh-fx-01-01.bilivideo.com/upgcxcode/56/31/125463156/1254631...

**付费番剧未登录只能拿 480P dash / 360P durl 试看, 与普通视频一样卡在 480P。**

---

## 8. 第三方解析接口 — 全部失效

指定 7 个:

| 接口 | HTTP | 结果 | 判定 |
|---|---|---|---|
| api.injahow.cn/bparse/?av=BV1GJ411x7h7&p=1&q=1080&format=mp4 | 200 | {"code":500,"data":null,"message":"请求异常"} (49B) | 失效 |
| api.injahow.cn/bparse/?bv=BV1GJ411x7h7&p=1&q=1080&format=mp4 | 200 | {"code":1,"data":null,"message":"清晰度受限，需要账号授权请求"} (77B) | 要账号 |
| jx.xmflv.com/?url=https://www.bilibili.com/video/BV1GJ411x7h7 | 200 | HTML 3201B, title="江湖路远，后会无期" | 已关站 |
| api.xingzhige.com/API/bilibili/?url=BV1GJ411x7h7 | 200 | HTML 1550B, title="404" | 失效 |
| api.52vmy.cn/api/video/bilibili?url=BV1GJ411x7h7 | **522** | 0 字节, 10.29s 超时 (Cloudflare) | 失效 |
| tenapi.cn/v2/bilibilivideo?url=... | **502** | HTML 6376B, title="tenapi.cn : 502: Bad gateway" | 失效 |
| api.pearktrue.cn/api/bilibili/?url=... | - | URLError: [Errno 11001] getaddrinfo failed | DNS 不存在 |

额外尝试 6 个:

| 接口 | HTTP | 结果 | 判定 |
|---|---|---|---|
| api.oioweb.cn/api/bilibili/?url=... | 404 | nginx 404 | 失效 |
| api.leafone.cn/api/bilibili?url=... | - | getaddrinfo failed | DNS |
| api.cenguigui.cn/api/bilibili/?url=... | 200 | 返回接口文档 HTML (39279B), 不是数据 | 路径错 |
| api.cenguigui.cn/api/bilibili/**bilibili.php**?action=search&query=... | 200 | {"code":200,"message":"搜索解析成功","data":[{bvid,title,artist,pic,duration,play_count...}]} | **仅元数据** |
| api.cenguigui.cn/api/bilibili/bilibili.php?action=media&bvid=..&quality=high | 200 | {"code":500,"error":"服务器错误: 获取CID失败"} | 服务端坏 |
| api.suolink.cn/api/bilibili/?url=... | 404 | Spring 404 JSON | 失效 |
| api.mtyqx.cn/api/bilibili/?url=... | 404 | nginx 404 | 失效 |
| api.kxzjoker.cn / api.hellobit.top / api.duanzi.icu | - | getaddrinfo failed | DNS |

### 唯一活着的: cenguigui search
`
GET https://api.cenguigui.cn/api/bilibili/bilibili.php?action=search&query=<kw>&page=1&limit=3
-> 200  {"code":200,"message":"搜索解析成功","data":[
     {"id":"BV1GJ411x7h7","aid":80433022,"bvid":"BV1GJ411x7h7",
      "artist":"索尼音乐中国","title":"【官方 MV】Never Gonna Give You Up - Rick Astley",
      "tag":"...","album":"BV1GJ411x7h7",
      "pic":"https://i1.hdslb.com/bfs/archive/5242...jpg",
      "duration":"3:33","play_count":107255104,
      "link":"http://www.bilibili.com/video/av80433022"}]}
`
**只返回元数据, 没有任何视频/音频地址。不能用于取流。**
其它 action: rank_categories / rank_list 存在; media 坏了; 无参返回 {"code":400,"error":"参数错误","available_actions":[...]}。

**结论: 第三方解析通道在本机 0 个可用于取流。**

---

## 9. Range / fMP4 实测 (核心)

样本: dash.video avc1 id=32 852x480 (baseUrl), dash.audio id=30280 mp4a.40.2

### 9.1 Range 可用性
| 请求 | HTTP | 字节 | Content-Range |
|---|---|---|---|
| 不带 Range | **200** | 20872903 (全量!) | 无 |
| Range: bytes=0-65535 视频 | **206** | 65536 | bytes 0-65535/20872903 |
| Range: bytes=0-1023 | **206** | 1024 | bytes 0-1023/20872903 |
| Range: bytes=1000000-1000255 (中间) | **206** | 256 | bytes 1000000-1000255/20872903 |
| Range: bytes=99999999- (越界) | **416** | 0 | 无 |
| Range: bytes=0-65535 音频 | **206** | 65536 | bytes 0-65535/5408198 |

- **206 可用**; 响应头**没有 Accept-Ranges**, 但服务端确实支持任意 Range。
- Content-Length 正确回填为切片长度。
- 越界返回 416 (不是 200 回退全量)。
- **不带 Range 会直接吐 20MB 全量**, 分片下载必须显式带 Range。

### 9.2 防盗链 / Referer / UA 矩阵 (对同一个 CDN URL)
| 请求头 | HTTP | 说明 |
|---|---|---|
| Referer: https://www.bilibili.com/ + Chrome UA | **206** | 正常 |
| Referer: https://www.bilibili.com (无尾斜杠) | **200** | 也接受 |
| **完全不带 Referer** | **403** | 552B HTML "403 Forbidden" |
| Referer: https://example.com/ | **403** | 必须 bilibili 域 |
| UA 留空 | **403** | 150B |
| UA: curl/8.4.0 | **403** | 必须是浏览器 UA |
| 无 Origin 且无 Referer | **403** | - |

**-> CDN 同时校验 Referer(必须是 bilibili 域) 和 UA(必须是浏览器)。两者缺一即 403。**
URL 里已带 deadline= 与 upsig=, 是时效签名; 同一 URL 短时间内重复请求仍返回 206 (实测有效)。

### 9.3 fMP4 box 结构 (原始序列)

**视频流 ...137649199_da2-1-30032.m4s 前 64KB:**
`
前 16 字节 ASCII: "...$ftypiso5...."
前 64 字节 hex:
000000246674797069736f35000000016176633169736f3564736d736d73697864617368
00000008667265650000004a6672656549736f4d656469612046696c

box 序列:
ftyp  size=36      major_brand=iso5  compatible=[avc1, iso5, dash]
free  size=8
free  size=74
moov  size=894
  mvhd size=108
  mvex size=72
    mehd size=16
    trex size=32
    trep size=16
  trak size=552
    tkhd size=92      (末尾 8 字节 = 852.0 x 480.0)
    edts size=48  ->  elst size=40
    mdia size=404
      mdhd size=32    timescale=16000  duration=0   <-- moov 里 duration 是 0, 用 sidx
      hdlr size=45
      minf size=319
        vmhd size=20
        dinf size=36 -> dref size=28
        stbl size=255
          stsd size=179 -> [avc1, entry_size=163, 852x480]
          stts size=16
          stsc size=16
          stsz size=20
          stco size=16
  udta size=154
    meta size=146
sidx  size=536
moof  size=1088
  mfhd size=16
  traf size=1064
    tfhd size=16
    tfdt size=16
    trun size=1024
mdat  size=348053   (被 64KB 截断)
`

**音频流 ...137649199_da2-1-30280.m4s 前 64KB:**
`
前 16 字节 ASCII: "...$ftypiso5...."   (与视频完全同构)
box 序列:
ftyp  size=36
free  size=8
free  size=74
moov  size=816
  mvhd size=108
  mvex size=72  ->  mehd 16 / trex 32 / trep 16
  trak size=474
    tkhd size=92      (w=0 h=0, 音频轨)
    edts size=36 -> elst size=28
    mdia size=338
      mdhd size=32    timescale=48000  duration=0
      hdlr size=45
      minf size=253
        smhd size=16                    <-- 音频用 smhd (视频是 vmhd)
        dinf size=36 -> dref size=28
        stbl size=193
          stsd size=91  -> [mp4a, entry_size=75]
          stts size=16
          stsc size=16
          stsz size=20
          stco size=16
          sgpd size=26
  udta size=154
    meta size=146
sidx  size=548
moof  size=1984
  mfhd size=16
  traf size=1960
    tfhd size=16
    tfdt size=16
    sbgp size=28                        <-- 音频多一个 sbgp
    trun size=1892
mdat  size=130772  (被 64KB 截断)
`

### 9.4 box 顺序结论 (给自写合并器用)
**视频和音频的序列完全一致:**
`
ftyp -> free -> free -> moov -> sidx -> moof -> mdat -> (moof -> mdat)*
`
- 两者都是标准 **fMP4 / CMAF (major_brand=iso5, compatible=avc1,iso5,dash)**, 文件头确实是 ftyp。
- **moov 在文件最前面且完整落在前 ~1KB 内** (894B / 816B) -> 一个 64KB 的 Range 请求就能同时拿到 init segment + sidx + 第一个 moof, 不需要 moov-at-end 的二次回源。
- moov 内**没有 stts/stsz 的实际样本表** (全是 size=16/20 的空表) -> 样本信息全在 moof/traf/trun 里, 这是纯分片结构。
- sidx (536B / 548B) 描述后续分片偏移 -> **可以用 sidx 直接做分片级 Range 拉取, 不必顺序下载**。
- mdhd duration = 0 -> 时长看 sidx 或接口的 dash.duration (=213)。
- mdat 是**长度前缀 NAL (AVCC)**, 视频 mdat 首个样本实测:
  00 00 00 02 | 09 f0 (len=2, AUD) -> 00 00 00 22 | 67 64 00 1f ac ca (len=34, SPS 0x67)。
  即 **4 字节大端长度 + NAL**, 直接串接即可, 无需转 Annex-B。
- 音频是 mp4a (AAC), trun 里有 sbgp -> 合并时把音频 traf 的 trun 原样保留即可。
- **自写合并器只需: 取视频 ftyp+moov 一次, 取音频 ftyp+moov 一次, 然后按时间轴交错拼接 (moof+mdat) 片段, 并重写每个 moof/traf/tfhd 的 base_data_offset 或改用 default-base-is-moof 标志。** 因为 sidx 的存在, 分片边界是自描述的。

---

## 10. 实际最高可用画质 (不带 Cookie)

### 10.1 热门老视频 BV1GJ411x7h7 (原始 1080P+)
| 路径 | 接口返回 accept_quality | dash.video 真实 id / 分辨率 | 实测文件分辨率 |
|---|---|---|---|
| dash (web-wbi / web-legacy / app-api, fnval=4048) | [112,80,64,32,16] | **id=32 852x480** 和 id=16 640x360 | moov/tkhd = **852x480**, stsd avc1 852x480 |
| **durl (fnval=1, qn=64 或 127)** | [64,16] | - | moov/tkhd = **1280x720**, stsd avc1 1280x720 |

**-> 不带 Cookie 的实际上限: dash 480P (852x480); 但走 fnval=1 的 durl 路径可以拿到 720P (1280x720) 单文件 MP4。**
accept_quality 里的 112/80 是**假的** —— 请求 qn=127 也没用, dash 里根本不发。

### 10.2 较新的大分辨率视频 BV1Bjp966EA8 (源 2160x3840 竖屏 4K)
| 路径 | 返回 | 实测 |
|---|---|---|
| dash fnval=4048 | quality=64, accept_quality=[**120**,112,80,64,32,16] | dash.video ids=[16,32], 分辨率只有 [360x640, **480x852**] |
| durl fnval=1 qn=127 | quality=64, accept_quality=[64,16] | moov/tkhd = **720x1280**, stsd avc1 720x1280, moov size=697573 |

**-> 越新的 4K 视频, 未登录的上限依然是 480P (dash) / 720P (durl)。accept_quality 会列出 120(4K)/112(1080P+)/80(1080P), 但一条都不发。**

### 10.3 一句话
**未登录 + 无 Cookie: dash 通道硬上限 480P (id=32)。想要 720P 必须走 fnval=1 的 durl 单文件 MP4。1080P/1080P+/4K 一律需要 SESSDATA 登录 Cookie。**

---

## 11. 坑清单

1. **CDN 防盗链是双重的**: 缺 Referer 或 Referer 非 bilibili 域 -> **403**; 缺浏览器 UA 或 UA 是 curl/空 -> **403**。必须同时带 Referer: https://www.bilibili.com/ 和 Chrome 桌面 UA。没有 Referer 时返回 552 字节 HTML 403。
2. **Accept-Ranges 响应头不存在**, 但 Range 确实生效 (206)。不要靠这个头判断, 直接试 206。
3. **不带 Range 会拉全量** (实测 20872903 字节)。分片/探测务必显式带 Range: bytes=0-N。
4. **越界 Range 返回 416**, 不是 200 回退全量。
5. **dash 的 moov 里 mdhd.duration = 0** —— 时长必须从 sidx 或接口 dash.duration 读取, 别信 moov。
6. **dash 的 mdat 是 AVCC 长度前缀 NAL, 不是 Annex-B**, 不要加 start code。
7. **html 页面必须 gzip 解压**: 服务端无视 Accept-Encoding: identity 仍返回 gzip; 且不要声明 br, 标准库解不了 brotli。用 1f 8b magic 判断。
8. **window.__playinfo__ 在桌面播放页已废弃**, 抓它必然拿不到播放地址 (页面只有 if (window.__playinfo__) 守卫)。改用官方 API 或 __INITIAL_STATE__.defaultWbiKey。
9. **__INITIAL_STATE__ 里有可直接用的 wbi key** (defaultWbiKey.wbiImgKey / wbiSubKey), 可省掉一次 nav 请求。实测该 key 签名的 wbi/playurl 返回 code=0。
10. **accept_quality / accept_description 完全不可信** —— 它列的是"该视频存在的档位", 不是"你被授权拿到的档位"。唯一可信的是 dash.video[].id 和 width/height。
11. **quality 字段也撒谎**: 未登录时 dash 返回 quality=64 (720P), 但 dash 里一条 720P 都没有 (只有 32/16)。别用 quality 判断。
12. **PGC 接口用 result 不用 data**, 按 data 解析会静默拿到空。
13. **pgc/player/api/playurl 返回 -400**, 只有 pgc/player/web/playurl 可用。
14. **TV API 已死**: /x/tv/playurl 对任何输入 (含无参数) 都返回 -400 请求错误。别在这条路上浪费时间。同 host 的 /x/v2/rank 反而正常。
15. **web-wbi 和 web-legacy 结果完全一致** —— 老接口不校验 w_rid, 可以先不实现 WBI 签名。省事时直接用 /x/player/playurl。
16. **durl 单文件 MP4 是 faststart (moov 在前)**, 非常适合边下边播; 但 moov 有 160KB~700KB, 读元数据要 3~11 个 64KB Range 请求。
17. **URL 有时效签名** (deadline= / upsig=), 但 backupUrl 数组里有多条备用 CDN (upos-sz-mirrorcoso1 / upos-sz-mirrorcos 等); 主 URL 挂了要轮询 backupUrl。
18. **第三方解析全线死亡** —— 免费公开 bilibili 解析接口在本机实测 0 个可用于取流。不要再依赖它们。
19. api.pearktrue.cn / api.leafone.cn / api.kxzjoker.cn / api.hellobit.top / api.duanzi.icu **DNS 都解析不了** (Errno 11001) —— 可能是本机 DNS 屏蔽, 未必是站点已死, 但本机不可用。
20. api.bilibili.com 和 www.bilibili.com 都能直连, 无需代理。

---

## 12. 原始证据文件

| 文件 | 内容 |
|---|---|
| _ids.json | aid / cid / img_key / sub_key / mixin_key |
| ch123.json | web-wbi, web-legacy, web-legacy-durl 全量字段 |
| tv_variants.json | TV api 16 种参数变体结果 |
| ch7_pgc.json | pgc season + playurl 全量 |
| ch6_html.json / ch6_final.json | html-scrape 结果 |
| html_desktop_gzip_deflate.html | 解压后的 162KB 桌面页 (可复查) |
| ch8_thirdparty.json / ch15_cenguigui.json / ch16_cenguigui_media.json | 第三方接口 |
| ch9_newer.json | 新视频候选 + dash 探测 |
| ch10_range.json | Range + fMP4 box 结构 |
| ch11_range_matrix.json | Referer/UA/Range 矩阵 |
| ch12_mp4struct.json | dash moov 逐 box |
| ch13_durl_headers.json | durl MP4 moov 逐 box (720P 证据) |
| ch14_final.json | app-api 证据 + dash 字段样例 + 额外第三方 |
| log18.txt .. log23.txt | 各脚本完整 stdout |

脚本: common.py, p01_ids.py, p02_channels123.py, p05_tv_variants.py, p06_tv_diag.py, p07_tv_final.py,
p08_pgc.py, p09_html.py, p10_newer.py, p11_html_raw.py, p12_html_enc.py, p13_html_final.py,
p14_html_search.py, p15_thirdparty.py, p16_range.py, p17_matrix.py, p18_mp4struct.py,
p19_durl_head.py, p20_final.py, p21_reasons.py, p22_cenguigui.py, p23_cg_media.py
