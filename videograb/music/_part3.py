

# ---------------------------------------------------------------- NCM（网易云音乐）

_NCM_MAGIC = b"CTENFDAM"
_NCM_CORE_KEY = bytes((0x68, 0x7A, 0x48, 0x52, 0x41, 0x6D, 0x73, 0x6F,
                       0x35, 0x6B, 0x49, 0x6E, 0x62, 0x61, 0x78, 0x57))
_NCM_META_KEY = bytes((0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
                       0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28))
_NCM_KEY_PREFIX = b"neteasecloudmusic"
_NCM_META_PREFIX = b"163 key(Don't modify):"


def _ncm_build_keybox(key: bytes) -> bytearray:
    """网易的 RC4 变体：用音频密钥生成 256 字节 keybox。"""
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


def _ncm_meta_pick(meta: dict, cover: bytes) -> dict:
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
    for key in ("bitrate", "duration"):
        if meta.get(key):
            out[key] = meta[key]
    if cover:
        out["cover"] = cover
    return out


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
            raise ValueError("文件损坏：密钥段解密结果缺少 neteasecloudmusic 前缀")
        stream_key = key_blob[len(_NCM_KEY_PREFIX):]
        if len(stream_key) < 16:
            raise ValueError("文件损坏：音频密钥不足 16 字节")
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
        fp.read(5)   # 版本与间隔
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
_QMC_MAGIC_M = b"musicex\x00"
_QMC_ENC_V2 = b"QQMusic EncV2,Key:"
_QMC_V2_KEY1 = bytes((0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                      0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28))
_QMC_V2_KEY2 = bytes((0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                      0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54))
_QMC_TEA_SEED = bytes((0x69, 0x56, 0x46, 0x38, 0x2B, 0x20, 0x15, 0x0B))

_FIRST_SEGMENT = 0x80
_OTHER_SEGMENT = 0x1400


def _qmc_make_mask() -> bytes:
    """把 64 字节静态表摊平成 128 字节掩码，解密时只做取模。"""
    mask = bytearray(128)
    for i in range(128):
        index = i & 0x7F
        if index > 0x3F:
            index = (0x80 - index) & 0x3F
        mask[i] = _QMC_STATIC_KEYS[index]
    return bytes(mask)


_QMC_MASK = _qmc_make_mask()


def _tea_decrypt_block(block: bytes, key: int, rounds: int = 32,
                       delta: int = 0x9E3779B9) -> bytes:
    v0, v1 = struct.unpack(">2I", block)
    k0 = (key >> 96) & 0xFFFFFFFF
    k1 = (key >> 64) & 0xFFFFFFFF
    k2 = (key >> 32) & 0xFFFFFFFF
    k3 = key & 0xFFFFFFFF
    total = (delta * (rounds // 2)) & 0xFFFFFFFF
    for _ in range(rounds // 2):
        v1 = (v1 - ((((v0 << 4) & 0xFFFFFFFF) + k2) ^ ((v0 + total) & 0xFFFFFFFF) ^
                    (((v0 >> 5) + k3) & 0xFFFFFFFF))) & 0xFFFFFFFF
        v0 = (v0 - ((((v1 << 4) & 0xFFFFFFFF) + k0) ^ ((v1 + total) & 0xFFFFFFFF) ^
                    (((v1 >> 5) + k1) & 0xFFFFFFFF))) & 0xFFFFFFFF
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
    """腾讯 TarsCpp tc_tea（CBC + salt + 尾部零校验）解密。"""
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

    skip = 0
    while skip < 2:
        if idx < 8:
            idx += 1
            skip += 1
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
    for _ in range(7):
        if idx == 8:
            crypt_block()
            idx = 0
        if dest[idx] ^ iv_prev[idx]:
            raise ValueError("tc_tea 解密失败：尾部零校验不通过")
        idx += 1
    return bytes(out)


def _qmc_v2_decode_key(raw_key: bytes) -> bytes:
    """还原 QMCv2 的 ekey：base64 → EncV2 解包 → TEA 解包。"""
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
    return header + _tea_decrypt_ecb(body, bytes(tea_key))


class _QmcStaticCipher:
    """QMCv1：固定密钥表 + 文件偏移异或。"""

    name = "qmc 固定密钥表"

    def decrypt(self, buf: bytearray, offset: int) -> None:
        mask = _QMC_MASK
        n = len(buf)
        base = offset % 0x7FFF
        for start in range(0, n, 128):
            chunk = buf[start:start + 128]
            for i in range(len(chunk)):
                p = base + start + i
                if p >= 0x7FFF:
                    p %= 0x7FFF
                chunk[i] ^= mask[p & 0x7F]
            buf[start:start + len(chunk)] = chunk


class _QmcMapCipher:
    """QMCv2 映射表：256/128 字节密钥 → 128 字节掩码。"""

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
            if rot:
                buf[i] ^= (((value << rot) & 0xFF) | (value >> (8 - rot)))
            else:
                buf[i] ^= value


class _QmcRc4Cipher:
    """QMCv2 加固 RC4：首 128 字节查表 + 之后按 5120 字节分段 RC4。"""

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
        if len(self._cache) < 4096:
            self._cache[seg_id] = result
        return result

    def decrypt(self, buf: bytearray, offset: int) -> None:
        n = len(buf)
        if n == 0:
            return
        key = self._key
        start = 0
        pos = offset
        if pos < _FIRST_SEGMENT:
            take = min(n, _FIRST_SEGMENT - pos)
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


def _parse_qmc(path: Path):
    """解析 QMC 尾部密钥区，返回 (audio_end, ekey, tag)。"""
    size = path.stat().st_size
    tail = _read_tail(path, 65536)
    if len(tail) < 8:
        raise ValueError("QQ 音乐文件太小，缺少尾部密钥区")
    magic = tail[-4:]
    if magic == _QMC_MAGIC_Q or magic == _QMC_MAGIC_S:
        meta_size = _u32be(tail, len(tail) - 8)
        end_of_meta = size - 8
        ekey_start = end_of_meta - meta_size
        if ekey_start < 0 or ekey_start >= end_of_meta:
            raise ValueError("QQ 音乐文件损坏：尾部元数据长度不合法")
        with path.open("rb") as fp:
            fp.seek(ekey_start)
            blob = fp.read(end_of_meta - ekey_start)
        comma = blob.find(b",")
        if comma < 0:
            raise ValueError("QQ 音乐文件损坏：尾部找不到密钥结束符（逗号）")
        tag = "QTag" if magic == _QMC_MAGIC_Q else "STag"
        return ekey_start, blob[:comma], tag
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
        return ekey_start, blob, ""
    if magic == _QMC_MAGIC_M:
        raise ValueError("这是 QQ 音乐新格式（musicex）：密钥不写在文件里，"
                         "需要登录客户端从服务器取回 ekey 才能解密")
    raise ValueError("无法识别的 QQ 音乐加密文件：找不到 QTag/STag/尾部密钥")


# ---------------------------------------------------------------- 酷狗 KGM / KGMA / VPR

_KGM_MAGIC = bytes((0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                    0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14))
_VPR_MAGIC = bytes((0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
                    0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31))
_VPR_MASK_DIFF = bytes((0x25, 0xDF, 0xE8, 0xA6, 0x75, 0x1E, 0x75, 0x0E, 0x2F,
                        0x80, 0xF3, 0x2D, 0xB8, 0xB6, 0xE3, 0x11, 0x00))

_KGM_MASK_PRE = bytes.fromhex(
    "b8d53db2e9af788c8333715176a0cd37" "2f3e358da9be98b7e78c22ce5a61df68"
    "6989fea5b6dea977fcc8bdbde56d3e5a" "36ef694ebee1e9661cf3d902b6f2129b"
    "44d06fb93589b6466d73820669c1edd7" "85c230dfa262be792d62623d0d7ebe48"
    "892302a0e4d57551320253fd163a213b" "160fc3b2bbb3e2ba3a3d13ecf6014584"
    "a5700f93490c64cd31d5cc4c07019e00" "1a2390bf881e3baba63ec47347107e3b"
    "5ebce30084ff09d4e0890f5b58704ffb" "65d85c531bd3c8c6bfef98b0504f0fea"
    "e583588c282c8467cdd09e47db2750ca" "f46363e8977f1b4b0cc2c1214ccc58f5"
    "9452a3f3d3e068f40023f35e0a7b93dd" "ab12b213e884d7a79f0f324c551d0436"
    "52dc03f3f94e42e93d61ef7cb6b39350")
_KGM_TABLE1 = bytes.fromhex(
    "00000000000000000000000000000000" "00012101" "61012101" "e1012101" "61012101"
    "d2230202" "42420202" "c2c20202" "42420202"
    "d3d30203" "63436303" "e3c3e303" "63436303"
    "94b49465" "04040404" "84848484" "04040404"
    "95959595" "04052505" "e585a585" "e5052505"
    "d6b696b6" "d6270606" "c6c68686" "c6c60606"
    "d7d79797" "d7d70607" "e7c7e787" "e7c7e707"
    "18381878" "183818e9" "08080808" "08080808"
    "19191919" "19191919" "08092909" "69092909"
    "da3a1a3a" "5a3a1a3a" "da2b0a0a" "4a4a0a0a"
    "dbdb1b1b" "5b5b1b1b" "dbdb0a0b" "6b4b6b0b"
    "9cbc9c7c" "1c3c1c7c" "9cbc9c6d" "0c0c0c0c"
    "9d9d9d9d" "1d1d1d1d" "9d9d9d9d" "0c0d2d0d"
    "debe9ebe" "de3e1e3e" "debe9ebe" "de2f0e0e"
    "dfdf9f9f" "dfdf1f1f" "dfdf9f9f" "dfdf0e0f"
    "00200060" "002000e0" "00200060" "002000f1")
_KGM_TABLE2 = bytes.fromhex(
    "00000000000000000000000000000000" "00012301" "67012301" "ef012301" "67012301"
    "df210202" "46460202" "cece0202" "46460202"
    "dede0203" "65476503" "edcfed03" "65476503"
    "9dbf9d63" "04040404" "8c8c8c8c" "04040404"
    "9c9c9c9c" "04052705" "eb8daf8d" "eb052705"
    "dbbd9fbd" "db250606" "caca8e8e" "caca0606"
    "dada9e9e" "dada0607" "e9cbe98f" "e9cbe907"
    "193b197f" "193b19e7" "08080808" "08080808"
    "18181818" "18181818" "08092b09" "6f092b09"
    "d7391b39" "5f391b39" "d7290a0a" "4e4e0a0a"
    "d6d61a1a" "5e5e1a1a" "d6d60a0b" "6d4f6d0b"
    "95b7957b" "1d3f1d7b" "95b7956b" "0c0c0c0c"
    "94949494" "1c1c1c1c" "94949494" "0c0d2f0d"
    "d3b597b5" "d33d1f3d" "d3b597b5" "d32d0e0e"
    "d2d29696" "d2d21e1e" "d2d29696" "d2d20e0f"
    "00220066" "002200ee" "00220066" "002200fe")

# 每字节操作合并成一张 256 项查表：v -> table1[k ^ v] ^ (v ^ ((v & 0x0F) << 4))
_KGM_NIBBLE = bytes((b ^ ((b & 0x0F) << 4)) & 0xFF for b in range(256))


def _kgm_get_mask(pos: int) -> int:
    offset = pos >> 4
    value = 0
    while offset >= 0x11:
        value ^= _KGM_TABLE1[offset % 272]
        offset >>= 4
        value ^= _KGM_TABLE2[offset % 272]
        offset >>= 4
    return _KGM_MASK_PRE[pos % 272] ^ value


class _KgmCipher:
    """酷狗 KGM/KGMA/VPR：文件里的 16 字节 slot key + 静态掩码表。"""

    def __init__(self, key: bytes, is_vpr: bool):
        if len(key) != 16:
            raise ValueError("酷狗密钥长度不是 16 字节")
        self._key = key
        self._vpr = is_vpr
        self.name = "酷狗 VPR" if is_vpr else "酷狗 KGM"
        self._tables = tuple(bytes(_KGM_TABLE1[k ^ v] ^ _KGM_NIBBLE[v] for v in range(256))
                             for k in key)
        self._mask_cache: dict = {}

    def _mask_row(self, phase: int) -> bytes:
        """同一相位（pos % 272 相同）下掩码只随 pos//272 变化，按需缓存。"""
        row = self._mask_cache.get(phase)
        if row is None:
            row = bytes(_kgm_get_mask(i * 272 + phase) for i in range(256))
            if len(self._mask_cache) < 512:
                self._mask_cache[phase] = row
        return row

    def decrypt(self, buf: bytearray, offset: int) -> None:
        tables = self._tables
        n = len(buf)
        for i in range(n):
            pos = offset + i
            buf[i] ^= tables[pos & 15][buf[i]] ^ _kgm_get_mask(pos)
        if self._vpr:
            diff = _VPR_MASK_DIFF
            for i in range(n):
                buf[i] ^= diff[(offset + i) % 17]


# ---------------------------------------------------------------- KWM（酷我音乐）

_KWM_ROOT = b"MoOtOiTvINGwd2E6n0E1i7L5t2IoOoNk"


def _parse_kwm(path: Path):
    """解析酷我头部，返回 (mask, audio_offset)。"""
    size = path.stat().st_size
    with path.open("rb") as fp:
        head = fp.read(0x400)
        if len(head) < 0x20:
            raise ValueError("酷我文件太小，不是有效的 KWM")
        if not (head.startswith(b"yeelion") or head.startswith(b"yeelion-kuwo")):
            raise ValueError("不是有效的酷我加密文件：文件头不是 yeelion")
        seed = _u32le(head, 0x10)
        text = str(seed).encode("ascii")[:0x20]
        mask = bytearray(0x20)
        mask[:len(text)] = text
        for i in range(0x20):
            mask[i] ^= _KWM_ROOT[i]
        audio_offset = 0x400
        if audio_offset >= size:
            raise ValueError("酷我文件损坏：没有音频数据")
    return bytes(mask), audio_offset


# ---------------------------------------------------------------- 对外接口

def _ext(path) -> str:
    try:
        name = os.fspath(path)
    except TypeError:
        name = str(path)
    return os.path.splitext(str(name))[1].lower()


def _head_magic(head: bytes) -> str:
    if head[:8] == _NCM_MAGIC:
        return "ncm"
    if head[:16] == _KGM_MAGIC:
        return "kgm"
    if head[:16] == _VPR_MAGIC:
        return "vpr"
    if head[:8] == b"yeelion":
        return "kwm"
    return ""


def is_encrypted(path) -> bool:
    """按扩展名 + 文件头判断能不能处理这个文件。"""
    p = Path(path)
    if not p.is_file():
        return False
    if _ext(p) not in ENCRYPTED_EXTENSIONS:
        return False
    try:
        head = _read_head(p, 16)
    except OSError:
        return False
    if _head_magic(head):
        return True
    tail = b""
    try:
        tail = _read_tail(p, 4096)
    except OSError:
        pass
    if tail[-4:] in (_QMC_MAGIC_Q, _QMC_MAGIC_S):
        return True
    if _head_magic(tail[-16:]):
        return True
    ext = _ext(p)
    if ext.startswith(".qmc") or ext == ".tkm":
        return True
    key_size = _u32le(tail + b"\x00" * 4, len(tail) - 4) if len(tail) >= 4 else 0
    return 0 < key_size <= 0x400


def sniff(path) -> str:
    """返回 'ncm' / 'qmc' / 'kgm' / 'kwm' / ''（空串表示未知）。"""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        head = _read_head(p, 16)
    except OSError:
        return ""
    magic = _head_magic(head)
    if magic in ("ncm", "kwm"):
        return magic
    if magic in ("kgm", "vpr"):
        return "kgm"
    tail = b""
    try:
        tail = _read_tail(p, 4096)
    except OSError:
        pass
    if tail[-4:] in (_QMC_MAGIC_Q, _QMC_MAGIC_S):
        return "qmc"
    if _head_magic(tail[-16:]) in ("kgm", "vpr"):
        return "kgm"
    if _head_magic(tail[-8:]):
        return "ncm" if tail[-8:] == _NCM_MAGIC else "kwm"
    key_size = _u32le(tail + b"\x00" * 4, len(tail) - 4) if len(tail) >= 4 else 0
    if 0 < key_size <= 0x400:
        return "qmc"
    return ""


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return path


def _notify(progress, done: int, total: int, note: str) -> None:
    if progress is None:
        return
    try:
        progress(done, total, note)
    except TypeError:
        try:
            progress(done, total)
        except TypeError:
            try:
                progress(done / total if total else 0.0)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass


def _dest_path(src: Path, dest_dir, stem: str, suffix: str) -> Path:
    out_dir = Path(dest_dir) if dest_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return _unique_path(out_dir / f"{stem}{suffix}")


class _Job:
    """一次解密任务：统一的流式读写 + 产物校验。"""

    def __init__(self, src: Path, cipher, start: int, end: int, *, name: str):
        self.src = src
        self.cipher = cipher
        self.start = start
        self.end = end
        self.name = name
        self.total = max(0, end - start)

    def run(self, out_path: Path, progress) -> tuple:
        """边解密边写文件，返回 (格式, 已写字节数)。"""
        fmt = ""
        written = 0
        with self.src.open("rb") as fp, out_path.open("wb") as out:
            fp.seek(self.start)
            remaining = self.total
            buf = bytearray(min(_CHUNK, remaining) if remaining else 0)
            while remaining > 0:
                want = min(_CHUNK, remaining)
                chunk = fp.read(want)
                if not chunk:
                    raise ValueError("文件损坏：音频数据提前结束")
                view = bytearray(chunk)
                self.cipher.decrypt(view, written)
                out.write(view)
                if not fmt:
                    fmt = _probe_format(bytes(view[:64]))
                written += len(view)
                remaining -= len(view)
                _notify(progress, written, self.total, self.name)
        return fmt, written


def decrypt(path, dest_dir=None, progress=None) -> dict:
    """就地解密一个平台加密音频，返回产物信息（失败抛 ValueError）。

    返回 dict 的键：ok / output(Path) / format / meta / platform / note。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"文件不存在：{src}")
    platform = sniff(src)
    fmt = ""
    meta: dict = {}
    note = ""
    out_path: Path | None = None

    if platform == "ncm":
        box, raw_meta, cover, audio_offset = _parse_ncm(src)
        meta = _ncm_meta_pick(raw_meta, cover)
        cipher = _ncm_box_cipher(box)
        name = "网易云 NCM"
        job = _Job(src, cipher, audio_offset, src.stat().st_size, name=name)
        hint = str(meta.get("format") or "")
    elif platform == "qmc":
        audio_end, ekey, tag = _parse_qmc(src)
        if tag or _u32le(_read_tail(src, 4), 0) <= 0x400:
            key = _qmc_v2_decode_key(ekey)
            if len(key) > 300:
                cipher = _QmcRc4Cipher(key[16:])
            else:
                cipher = _QmcMapCipher(key[16:] if len(key) > 128 else key)
        else:
            cipher = _QmcStaticCipher()
        name = f"QQ 音乐 {tag or 'QMCv1'}"
        job = _Job(src, cipher, 0, audio_end, name=name)
        hint = ""
    elif platform == "kgm":
        with src.open("rb") as fp:
            head = fp.read(0x2C)
        if len(head) < 0x2C:
            raise ValueError("酷狗文件太小，头部不完整")
        if head[:16] == _VPR_MAGIC:
            is_vpr = True
        elif head[:16] == _KGM_MAGIC:
            is_vpr = False
        else:
            raise ValueError("不是有效的酷狗加密文件：文件头不匹配")
        header_len = _u32le(head, 0x10)
        if header_len < 0x2C or header_len > src.stat().st_size:
            raise ValueError(f"酷狗文件损坏：头部长度不合法（{header_len}）")
        cipher = _KgmCipher(head[0x1C:0x2C], is_vpr)
        name = "酷狗 VPR" if is_vpr else "酷狗 KGM"
        job = _Job(src, cipher, header_len, src.stat().st_size, name=name)
        hint = ""
    elif platform == "kwm":
        mask, audio_offset = _parse_kwm(src)
        cipher = _KwmCipher(mask)
        name = "酷我 KWM"
        job = _Job(src, cipher, audio_offset, src.stat().st_size, name=name)
        hint = ""
    else:
        raise ValueError("不是本模块能识别的加密音频文件"
                         "（支持 .ncm / .qmc* / .mflac / .mgg / .kgm / .kgma / .vpr / .kwm）")

    stem = src.stem
    suffix = _FORMAT_EXT.get(hint, "") if hint in _FORMAT_EXT else ""
    out_path = _dest_path(src, dest_dir, stem, suffix or ".tmp")
    try:
        fmt, written = job.run(out_path, progress)
        if not fmt:
            head = _read_head(out_path, 4096)
            fmt = _probe_format(head)
        if not fmt:
            raise ValueError("解密结果不是已知的音频格式（文件头既不是 flac/mp3/ogg/m4a），"
                            "密钥可能不匹配，已放弃输出")
        if suffix and suffix != _FORMAT_EXT[fmt]:
            out_path.unlink(missing_ok=True)
            out_path = _dest_path(src, dest_dir, stem, _FORMAT_EXT[fmt])
            with src.open("rb") as fp, out_path.open("wb") as out:
                fp.seek(job.start)
                left = job.total
                while left > 0:
                    chunk = fp.read(min(_CHUNK, left))
                    if not chunk:
                        break
                    view = bytearray(chunk)
                    job.cipher.decrypt(view, job.total - left)
                    out.write(view)
                    left -= len(view)
        elif out_path.suffix == ".tmp":
            final = _dest_path(src, dest_dir, stem, _FORMAT_EXT[fmt])
            out_path.replace(final)
            out_path = final
    except BaseException:
        try:
            if out_path is not None and out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        raise

    if not meta.get("format"):
        meta["format"] = fmt
    note = f"{name}：已还原为 {fmt.upper()}（{written} 字节）"
    _notify(progress, written, written, "完成")
    return {
        "ok": True,
        "output": out_path,
        "format": fmt,
        "meta": meta,
        "platform": {"ncm": "netease", "qmc": "qmc", "kgm": "kugou", "kwm": "kuwo"}[platform],
        "note": note,
    }


def _ncm_box_cipher(box: bytearray):
    """把 keybox 包装成和别的 cipher 一样的接口。"""

    class _Cipher:
        name = "网易云 NCM RC4 变体"

        def decrypt(self, buf: bytearray, offset: int) -> None:
            for i in range(len(buf)):
                j = (offset + i + 1) & 0xFF
                buf[i] ^= box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]

    return _Cipher()


class _KwmCipher:
    """酷我 KWM：与 32 字节掩码循环异或。"""

    name = "酷我 KWM"

    def __init__(self, mask: bytes):
        if len(mask) != 0x20:
            raise ValueError("酷我掩码长度不是 32 字节")
        self._mask = mask

    def decrypt(self, buf: bytearray, offset: int) -> None:
        mask = self._mask
        for i in range(len(buf)):
            buf[i] ^= mask[(offset + i) & 0x1F]
