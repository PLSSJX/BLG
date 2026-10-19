"""bigjpg 图片放大（网页免费通道）。

**不需要登录，也不需要 API key。** 走的是 bigjpg 官网前端自己用的那套接口：

  1. POST /api/sign_upload   form 编码 {filename, filetype}
                             -> {url, fileurl, object, storage}
  2. PUT  <url>              相同 Content-Type 把文件本体传上去
  3. POST /task              form 编码 {conf: <JSON 字符串>}
                             conf = {x2, style, noise, file_name,
                                     files_size, file_height, file_width,
                                     input: fileurl, storage}
                             -> {status: "ok", info: <fid>, time: <分钟>}
  4. GET  /free?fids=["fid"] -> {fid: [status, url, size]}
  5. GET  /retry?fids=["fid"] 重试失败任务

注意第 3 步：**整个 JSON 是塞在一个叫 conf 的表单字段里的**。
铺平成多个表单字段会直接 400 —— 这是复刻时最容易踩的坑。

如果设置里填了 API key，也可以走官方付费接口（/api/img_upload + /api/task/），
但默认走免费通道。

重要：本模块会把用户的图片上传到 bigjpg 的服务器。界面必须已经明确告知。
"""
from __future__ import annotations

import json
import mimetypes
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://bigjpg.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0")

# 网页版只跟踪这两个语言 cookie，登录态完全不需要
_BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": BASE,
    "Referer": BASE + "/zh",
    "User-Agent": UA,
    "Cookie": "bigjpg_lng=zh",
}

STYLES = {"art": "卡通插画", "photo": "照片"}
NOISES = {"-1": "无", "0": "低", "1": "中", "2": "高", "3": "最高"}
SCALES = {"1": "2 倍", "2": "4 倍", "3": "8 倍", "4": "16 倍"}
STATUS_TEXT = {"new": "排队中", "process": "处理中", "success": "已完成", "error": "失败"}

ERRORS = {
    "valid_api_key_required": "API key 缺失或无效（付费接口才需要）。",
    "requires_vip": "bigjpg 拒绝了这次转换。免费额度可能已经用完。",
    "month_limit": "本月转换次数已用完。",
    "param_error": "参数超出范围。",
    "input_url_error": "图片地址无效。",
    "oversize": "图片超过了 bigjpg 的尺寸上限。",
}

MAX_BYTES = 20 * 1024 * 1024
ALLOWED = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class BigjpgError(RuntimeError):
    pass


def _explain(code: str) -> str:
    return ERRORS.get(code, "bigjpg 返回错误：" + (str(code) or "未知"))


def _image_size(data: bytes) -> tuple:
    """读出图片宽高。后端只用标准库，不能靠 PIL。

    返回 (宽, 高)；认不出来就返回 (0, 0) —— bigjpg 会自己校验。
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if data[:2] == b"\xff\xd8":                       # JPEG：扫 SOFn
            index = 2
            while index < len(data) - 9:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    h, w = struct.unpack(">HH", data[index + 5:index + 9])
                    return int(w), int(h)
                length = struct.unpack(">H", data[index + 2:index + 4])[0]
                index += 2 + length
        if data[:2] == b"BM":                             # BMP
            w, h = struct.unpack("<ii", data[18:26])
            return int(abs(w)), int(abs(h))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return w, h
    except Exception:
        pass
    return 0, 0


def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, timeout: int = 90) -> dict:
    hdrs = dict(_BASE_HEADERS)
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            text = ""
        raise BigjpgError("bigjpg 返回 HTTP %s：%s" % (exc.code, text[:160] or "（无内容）")) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BigjpgError("连接 bigjpg 失败：" + str(exc)[:120]) from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise BigjpgError("bigjpg 返回了无法解析的内容：" + raw[:120].decode("utf-8", "replace")) from None


def describe() -> dict:
    return {"styles": STYLES, "noises": NOISES, "scales": SCALES,
            "status": STATUS_TEXT, "errors": ERRORS,
            "max_bytes": MAX_BYTES, "allowed": sorted(ALLOWED), "free": True}


def upload_image(key: str, path: Path) -> dict:
    """上传一张本地图片，返回 {fileurl, storage}。

    免费通道用 /api/sign_upload（form 编码，不需要 key）。
    如果给了 key 且 sign_upload 失败，再退到付费的 /api/img_upload。
    """
    path = Path(path)
    if not path.is_file():
        raise BigjpgError("文件不存在：" + str(path))
    ext = path.suffix.lower()
    if ext not in ALLOWED:
        raise BigjpgError("不支持的格式 " + ext + "，只接受 " + "、".join(sorted(ALLOWED)))
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise BigjpgError("图片过大（%.1f MB），上限 20 MB" % (len(data) / 1048576))

    filetype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    form = urllib.parse.urlencode({"filename": path.name, "filetype": filetype}).encode()
    signed = _request(BASE + "/api/sign_upload", method="POST", data=form,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    if signed.get("status") not in (None, "ok") or not signed.get("url"):
        raise BigjpgError("申请上传地址失败：" + _explain(str(signed.get("status"))))

    upload_url = signed["url"]
    req = urllib.request.Request(upload_url, data=data, method="PUT",
                                 headers={"Content-Type": filetype, "User-Agent": UA,
                                          "Content-Length": str(len(data))})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise BigjpgError("上传到 bigjpg 存储失败（HTTP %s）" % exc.code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BigjpgError("上传到 bigjpg 存储失败：" + str(exc)[:120]) from None

    return {"fileurl": signed.get("fileurl") or "",
            "storage": signed.get("storage") or "oss",
            "file_name": path.name,
            "file_size": len(data),
            "width": _image_size(data)[0],
            "height": _image_size(data)[1]}


def create_task(key: str, *, style: str, noise: str, x2: str, upload: dict) -> dict:
    """创建放大任务。免费通道：POST /task，body 是 conf=<JSON>。"""
    if style not in STYLES:
        raise BigjpgError("style 只能是 art 或 photo")
    if noise not in NOISES:
        raise BigjpgError("noise 只能取 -1 到 3")
    if x2 not in SCALES:
        raise BigjpgError("x2 只能取 1 到 4")

    conf = {
        "x2": str(x2), "style": style, "noise": str(noise),
        "file_name": upload.get("file_name") or "",
        "files_size": upload.get("file_size") or 0,
        "file_height": upload.get("height") or 0,
        "file_width": upload.get("width") or 0,
        "input": upload.get("fileurl") or "",
        "storage": upload.get("storage") or "oss",
    }
    payload = urllib.parse.urlencode({"conf": json.dumps(conf, separators=(",", ":"))}).encode()
    info = _request(BASE + "/task", method="POST", data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    if info.get("status") != "ok" or not info.get("info"):
        raise BigjpgError(_explain(str(info.get("status"))))
    return {"tid": str(info["info"]),
            "minute": info.get("time"),
            "remaining": info.get("remaining_api_calls")}


def query_tasks(tids: list) -> dict:
    """查询任务。返回 {tid: {status, url, size}}。

    官方返回的是 {tid: [status, url, size]} —— 数组，不是对象。
    """
    ids = [str(t) for t in tids if str(t).strip()][:100]
    if not ids:
        return {}
    raw = _request(BASE + "/free?fids=" + urllib.parse.quote(json.dumps(ids)))
    out: dict = {}
    for tid, value in (raw or {}).items():
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            out[str(tid)] = {"status": str(value[0]),
                             "url": str(value[1] or ""),
                             "size": str(value[2] if len(value) > 2 else "")}
        elif isinstance(value, dict):
            out[str(tid)] = {"status": str(value.get("status") or ""),
                             "url": value.get("url") or "",
                             "size": str(value.get("size") or "")}
    return out


def retry_tasks(tids: list) -> dict:
    ids = [str(t) for t in tids if str(t).strip()][:100]
    if not ids:
        raise BigjpgError("没有要重试的任务")
    return _request(BASE + "/retry?fids=" + urllib.parse.quote(json.dumps(ids)))
