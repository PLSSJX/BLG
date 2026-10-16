

# ---------------------------------------------------------------- NCM（网易云音乐）

_NCM_MAGIC = b"CTENFDAM"
_NCM_CORE_KEY = bytes((0x68, 0x7A, 0x48, 0x52, 0x41, 0x6D, 0x73, 0x6F,
                       0x35, 0x6B, 0x49, 0x6E, 0x62, 0x61, 0x78, 0x57))
_NCM_META_KEY = bytes((0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
                       0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28))
_NCM_KEY_PREFIX = b"neteasecloudmusic"
_NCM_META_PREFIX = b"163 key(Don't modify):"


def _ncm_build_keybox(key: bytes) -> bytearray:
    """网易的 RC4 变体：用密钥生成 256 字节 keybox。"""
    box = bytearray(range(256))
    klen = len(key)
    if klen == 0:
        raise ValueError("文件损坏：NCM 音频密钥为空")
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= klen:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return box


def _ncm_decrypt(buf: bytearray, box: bytearray, start: int) -> None:
    """就地解密 NCM 音频数据，start 为该块在音频流中的偏移。"""
    for i in range(len(buf)):
        j = (start + i + 1) & 0xFF
        buf[i] ^= box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]


def _parse_ncm(path: Path):
    """解析 NCM 头部，返回 (keybox, meta, cover, audio_offset)。"""
    size = path.stat().st_size
    with path.open("rb") as fp:
        if fp.read(8) != _NCM_MAGIC:
            raise ValueError("不是有效的 NCM 文件：文件头不是 CTENFDAM")
        fp.seek(10)
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：NCM 密钥段长度读取失败")
        key_len = _u32le(raw, 0)
        if key_len <= 0 or key_len > (1 << 20):
            raise ValueError(f"文件损坏：密钥段长度不对（{key_len}）")
        key_data = fp.read(key_len)
        if len(key_data) != key_len:
            raise ValueError("文件损坏：密钥段数据不完整")
        try:
            key_blob = _pkcs7_unpad(_aes_ecb_decrypt(_xor(key_data, 0x64), _NCM_CORE_KEY))
        except ValueError as exc:
            raise ValueError(f"文件损坏：密钥段无法解密（{exc}）") from exc
        if not key_blob.startswith(_NCM_KEY_PREFIX):
            raise ValueError("文件损坏：密钥段解密结果异常（缺少 neteasecloudmusic 前缀）")
        stream_key = key_blob[len(_NCM_KEY_PREFIX):]
        if len(stream_key) < 16:
            raise ValueError("文件损坏：音频密钥长度不足 16 字节")
        box = _ncm_build_keybox(stream_key)

        if fp.read(2) != b"\x00\x00":
            raise ValueError("文件损坏：密钥段后的分隔符不正确")
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：元数据长度读取失败")
        meta_len = _u32le(raw, 0)
        meta: dict = {}
        if 0 < meta_len <= (1 << 24):
            meta_raw = fp.read(meta_len)
            if len(meta_raw) != meta_len:
                raise ValueError("文件损坏：元数据段不完整")
            meta = _ncm_parse_meta(meta_raw)

        fp.read(4)   # CRC32
        fp.read(5)   # 版本 / 间隔
        raw = fp.read(4)
        if len(raw) < 4:
            raise ValueError("文件损坏：封面长度读取失败")
        cover_len = _u32le(raw, 0)
        cover = b""
        if 0 < cover_len <= (1 << 26):
            cover = fp.read(cover_len)
            if len(cover) != cover_len:
                raise ValueError("文件损坏：封面数据不完整")
        audio_offset = fp.tell()
        if audio_offset >= size:
            raise ValueError("文件损坏：没有音频数据")
    return box, meta, cover, audio_offset


def _ncm_parse_meta(meta_raw: bytes) -> dict:
    try:
        blob = _xor(meta_raw, 0x63)
        if blob.startswith(_NCM_META_PREFIX):
            blob = blob[len(_NCM_META_PREFIX):]
        plain = _pkcs7_unpad(_aes_ecb_decrypt(base64.b64decode(blob), _NCM_META_KEY))
        if plain.startswith(b"music:"):
            plain = plain[6:]
        data = json.loads(plain.decode("utf-8", "replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ncm_meta(meta: dict, cover: bytes) -> dict:
    out: dict = {}
    if meta.get("musicName"):
        out["title"] = str(meta["musicName"])
    artists = meta.get("artist")
    if isinstance(artists, list):
        names = []
        for item in artists:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]))
            elif isinstance(item, str):
                names.append(item)
        if names:
            out["artist"] = "/".join(names)
    if meta.get("album"):
        out["album"] = str(meta["album"])
    if meta.get("format"):
        out["format"] = str(meta["format"]).lower()
    if meta.get("bitrate"):
        out["bitrate"] = meta["bitrate"]
    if meta.get("duration"):
        out["duration"] = meta["duration"]
    if cover:
        out["cover"] = cover
    return out


# ---------------------------------------------------------------- QMC（QQ 音乐）

_QMC_STATIC_KEYS = bytes((
    0xC3, 0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52, 0xD8, 0xA1, 0x66, 0x62,
    0x9F, 0x5B, 0x09, 0x00, 0xC3, 0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E,
    0xD8, 0x92, 0x3F, 0xBC, 0x90, 0xBB, 0x74, 0x0E, 0xC3, 0x47, 0x74, 0x3D,
    0x90, 0xAA, 0x3F, 0x51, 0xD8, 0xF4, 0x11, 0x84, 0x9F, 0xDE, 0x95, 0x1D,
    0xC3, 0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9, 0xD8, 0xF0, 0xF7, 0xA0,
    0x90, 0xA1, 0xD6, 0xF3,
))

_QMC_MAGIC_Q = b"QTag"
_QMC_MAGIC_S = b"STag"
_QMC_ENC_V2 = b"QQMusic EncV2,Key:"
_QMC_V2_KEY1 = bytes((0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                      0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28))
_QMC_V2_KEY2 = bytes((0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                      0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54))
_QMC_TEA_SEED = bytes((0x69, 0x56, 0x46, 0x38, 0x2B, 0x20, 0x15, 0x0B))

_FIRST_SEGMENT = 0x80
_OTHER_SEGMENT = 0x1400


def _qmc_static_mask() -> bytes:
    """把 64 字节静态表摊平成 128 字节掩码，解密时只做取模。"""
    mask = bytearray(128)
    for i in range(128):
        index = i & 0x7F
        if index > 0x3F:
            index = (0x80 - index) & 0x3F
        mask[i] = _QMC_STATIC_KEYS[index]
    return bytes(mask)


_QMC_MASK = _qmc_static_mask()


def _tea_decrypt_block(block: bytes, key: int, rounds: int = 32,
                       delta: int = 0x9E3779B9) -> bytes:
    v0, v1 = struct.unpack(">2I", block)
    k0 = (key >> 96) & 0xFFFFFFFF
    k1 = (key >> 64) & 0xFFFFFFFF
    k2 = (key >> 32) & 0xFFFFFFFF
    k3 = key & 0xFFFFFFFF
    total = (delta * rounds) & 0xFFFFFFFF
    for _ in range(rounds):
        v1 = (v1 - ((((v0 << 4) & 0xFFFFFFFF) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3))) & 0xFFFFFFFF
        v0 = (v0 - ((((v1 << 4) & 0xFFFFFFFF) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1))) & 0xFFFFFFFF
        total = (total - delta) & 0xFFFFFFFF
    return struct.pack(">2I", v0, v1)


def _tea_decrypt_ecb(data: bytes, key: bytes, rounds: int = 32) -> bytes:
    """TEA-ECB，按 8 字节一块解密（腾讯 tc_tea 的底层）。"""
    if len(key) != 16:
        raise ValueError("TEA 密钥长度必须是 16 字节")
    if len(data) % 8:
        raise ValueError("TEA 数据长度不是 8 的整数倍")
    k = int.from_bytes(key, "big")
    return b"".join(_tea_decrypt_block(data[i:i + 8], k, rounds)
                    for i in range(0, len(data), 8))


def _tc_tea_decrypt(data: bytes, key: bytes) -> bytes:
    """腾讯 TarsCpp tc_tea（CBC，带 salt 和尾部零校验）解密。"""
    if len(data) < 16 or len(data) % 8:
        raise ValueError("tc_tea 数据长度不合法")
    first = _tea_decrypt_ecb(data[:8], key)
    pad_len = first[0] & 0x07
    out_len = len(data) - pad_len - 2 - 7 - 1
    if out_len < 0:
        raise ValueError("tc_tea 解密失败：长度推算为负")
    out = bytearray(out_len)
    iv_prev = bytearray(8)
    iv_cur = bytearray(data[:8])
    dest = bytearray(first)
    pos = 8
    idx = 1 + pad_len

    def crypt_block() -> None:
        nonlocal pos
        iv_prev[:] = iv_cur
        iv_cur[:] = data[pos:pos + 8]
        dest[:] = _tea_decrypt_ecb(bytes(x ^ y for x, y in zip(dest[:8], iv_cur)), key)
        pos += 8

    i = 1
    while i <= 2:
        if idx < 8:
            idx += 1
            i += 1
        else:
            crypt_block()
            idx = 0
    out_pos = 0
    while out_pos < out_len:
        if idx < 8:
            out[out_pos] = dest[idx] ^ iv_prev[idx]
            idx += 1
            out_pos += 1
        else:
            crypt_block()
            idx = 0
    for _ in range(1, 7):
        if idx < 8:
            if dest[idx] ^ iv_prev[idx]:
                raise ValueError("tc_tea 解密失败：尾部零校验不通过")
            idx += 1
        else:
            crypt_block()
            idx = 0
    return bytes(out)


def _qmc_v2_decode_key(raw_key: bytes) -> bytes:
    """还原 QMCv2 的 ekey：base64 解码 → EncV2 解包 → TEA 解包。"""
    ekey = bytes(raw_key).strip().rstrip(b"\x00")
    try:
        decoded = base64.b64decode(ekey, validate=False)
    except Exception as exc:
        raise ValueError("QQ 音乐密钥不是合法的 base64") from exc
    if decoded.startswith(_QMC_ENC_V2):
        blob = decoded[len(_QMC_ENC_V2):]
        try:
            stage1 = _tc_tea_decrypt(blob, _QMC_V2_KEY1)
            stage2 = _tc_tea_decrypt(stage1, _QMC_V2_KEY2)
        except ValueError as exc:
            raise ValueError(f"QQ 音乐 EncV2 密钥解包失败：{exc}") from exc
        decoded = base64.b64decode(stage2)
    if len(decoded) < 16:
        raise ValueError("QQ 音乐密钥长度不足 16 字节")
    header, body = decoded[:8], decoded[8:]
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[i * 2] = _QMC_TEA_SEED[i]
        tea_key[i * 2 + 1] = header[i]
    tail = _tea_decrypt_ecb(body, bytes(tea_key))
    return header + tail


class _QmcStaticCipher:
    """QMCv1：固定密钥表 + 文件偏移异或。"""

    name = "qmc 静态密钥表"

    def decrypt(self, buf: bytearray, offset: int) -> None:
        mask = _QMC_MASK
        step = offset % 0x7FFF
        n = len(buf)
        for start in range(0, n, 128):
            chunk = buf[start:start + 128]
            pos = step + start
            for i in range(len(chunk)):
                p = pos + i
                if p >= 0x7FFF:
                    p %= 0x7FFF
                chunk[i] ^= mask[p & 0x7F]
            buf[start:start + len(chunk)] = chunk


class _QmcMapCipher:
    """QMCv2 映射表：key256/key128 生成 128 字节掩码。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("QQ 音乐密钥为空")
        self._key = key
        self._n = len(key)
        self.name = f"qmc-v2 映射密钥（{self._n} 字节）"

    def decrypt(self, buf: bytearray, offset: int) -> None:
        key = self._key
        n = self._n
        for i in range(len(buf)):
            pos = offset + i
            if pos >= 0x7FFF:
                pos %= 0x7FFF
            idx = (pos * pos + 71214) % n
            value = key[idx]
            rot = ((idx & 7) + 4) & 7
            buf[i] ^= (((value << rot) & 0xFF) | (value >> (8 - rot)) if rot else value)


class _QmcRc4Cipher:
    """QMCv2 加固 RC4：首段查表 + 后续分段 RC4。"""

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("QQ 音乐密钥为空")
        self._key = key
        self._n = len(key)
        self.name = f"qmc-v2 加固 RC4（{self._n} 字节密钥）"
        box = bytearray(i % self._n for i in range(self._n))
        j = 0
        for i in range(self._n):
            j = (j + box[i] + key[i]) % self._n
            box[i], box[j] = box[j], box[i]
        self._box = box
        base = 1
        for value in key:
            if value == 0:
                continue
            nxt = (base * value) & 0xFFFFFFFF
            if nxt == 0 or nxt <= base:
                break
            base = nxt
        self._hash = base
        self._cache: dict = {}

    def _skip(self, seg_id: int) -> int:
        cached = self._cache.get(seg_id)
        if cached is not None:
            return cached
        seed = self._key[seg_id % self._n]
        divisor = (seg_id + 1) * seed
        value = 0 if divisor == 0 else int(self._hash / divisor * 100)
        result = value % self._n
        if len(self._cache) < 8192:
            self._cache[seg_id] = result
        return result

    def decrypt(self, buf: bytearray, offset: int) -> None:
        n = len(buf)
        if n == 0:
            return
        start = 0
        pos = offset
        if pos < _FIRST_SEGMENT:
            take = min(n, _FIRST_SEGMENT - pos)
            key = self._key
            for i in range(take):
                buf[i] ^= key[self._skip(pos + i)]
            start = take
            pos += take
        while start < n:
            seg = pos // _OTHER_SEGMENT
            skip = (pos % _OTHER_SEGMENT + self._skip(seg)) % self._n
            box = self._box[:]
            j = 0
            k = 0
            take = min(n - start, _OTHER_SEGMENT - (pos % _OTHER_SEGMENT))
            for _ in range(skip):
                j = (j + 1) % self._n
                k = (box[j] + k) % self._n
                box[j], box[k] = box[k], box[j]
            for i in range(take):
                j = (j + 1) % self._n
                k = (box[j] + k) % self._n
                box[j], box[k] = box[k], box[j]
                buf[start + i] ^= box[(box[j] + box[k]) % self._n]
            start += take
            pos += take


def _parse_qmc_tail(path: Path):
    """解析 QMC 尾部密钥区，返回 (audio_end, ekey_bytes, tag)。"""
    size = path.stat().st_size
    tail = _read_tail(path, 4096)
    if len(tail) < 8:
        raise ValueError("QQ 音乐文件过小，缺少尾部密钥区")
    magic = tail[-4:]
    if magic == _QMC_MAGIC_Q:
        meta_size = _u32be(tail, len(tail) - 8)
        end_of_meta = size - 8
        ekey_start = end_of_meta - meta_size
        if ekey_start < 0 or ekey_start >= end_of_meta:
            raise ValueError("QQ 音乐文件损坏：QTag 元数据长度不合法")
        blob = tail[len(tail) - (size - ekey_start):] if ekey_start >= size - len(tail) else b""
        if not blob:
            with path.open("rb") as fp:
                fp.seek(ekey_start)
                blob = fp.read(end_of_meta - ekey_start)
        comma = blob.find(b",")
        if comma < 0:
            raise ValueError("QQ 音乐文件损坏：QTag 中找不到密钥结束符")
        return ekey_start, blob[:comma], "QTag"
    if magic == _QMC_MAGIC_S:
        meta_size = _u32be(tail, len(tail) - 8)
        end_of_meta = size - 8
        ekey_start = end_of_meta - meta_size
        if ekey_start < 0 or ekey_start >= end_of_meta:
            raise ValueError("QQ 音乐文件损坏：STag 元数据长度不合法")
        with path.open("rb") as fp:
            fp.seek(ekey_start)
            blob = fp.read(end_of_meta - ekey_start)
        comma = blob.find(b",")
        if comma < 0:
            raise ValueError("QQ 音乐文件损坏：STag 中找不到密钥结束符")
        return ekey_start, blob[:comma], "STag"
    key_size = _u32le(tail, len(tail) - 4)
    if 0 < key_size <= 0x400:
        ekey_start = size - 4 - key_size
        if ekey_start < 0:
            raise ValueError("QQ 音乐文件损坏：尾部密钥长度超出文件大小")
        with path.open("rb") as fp:
            fp.seek(ekey_start)
            blob = fp.read(key_size)
        if len(blob) != key_size:
            raise ValueError("QQ 音乐文件损坏：尾部密钥不完整")
        return ekey_start, blob, "v1-tail"
    if magic == b"musicex\x00":
        raise ValueError("该文件是 QQ 音乐新格式（musicex），密钥不写在文件里，"
                         "需要登录客户端从服务器取回 ekey 才能解密")
    raise ValueError("无法识别的 QQ 音乐加密文件：找不到 QTag/STag/尾部密钥")
