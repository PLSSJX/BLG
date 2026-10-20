# -*- coding: utf-8 -*-
"""文献检索：把几个有公开 API 的学术源聚合成一个结果列表。

为什么只接这几个：国家哲社文献中心、PubScholar、OpenSign、ChinaXiv、Socolar、
WorldLib、OALib、谷歌学术、百度学术都**没有公开 API**，只能爬网页。爬虫随时会
被改版和反爬打挂，这种东西放进应用里，坏的时候用户只会看到"搜不出东西"。

这里接的都是官方 REST 接口，不需要密钥：
  OpenAlex        4.7 亿作品，覆盖全学科，还带开放获取链接
  Crossref        出版社交付的元数据，DOI 最权威
  DOAJ            经过质量审核的开放获取期刊
  PubMed          NCBI E-utilities，生物医学首选
  Semantic Scholar 语义检索，摘要更全（无密钥时限速较严）
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "MakiseToolkit/3.5 (+https://github.com/PLSSJX/BLG)"
# 下载 PDF 时用真实浏览器 UA：MDPI、Wiley 这类出版商会直接 403 掉
# 非浏览器 UA，用工具名去请求等于自找失败。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TIMEOUT = 12


class PaperError(Exception):
    pass


def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        # 把 HTTP 状态码和原因带出来，前端才能说清楚是哪一源挂了
        raise PaperError(str(exc)[:160]) from None


def _json(url: str, timeout: int = TIMEOUT) -> dict:
    raw = _get(url, timeout)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise PaperError("返回的不是 JSON，接口可能变了") from None


def _clean(text) -> str:
    """任何东西都安全转成干净字符串。

    各个源的字段类型并不统一（DOAJ 的 identifier 是字典列表，Crossref 的
    title 是数组），统一在这里收口，别让一个源的结构差异炸掉整次检索。
    """
    if isinstance(text, (list, tuple)):
        text = ", ".join(_clean(x) for x in text if x is not None)
    elif isinstance(text, dict):
        text = text.get("name") or text.get("title") or ""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _item(**kw) -> dict:
    """统一的结果结构。缺的字段给空串，前端不用到处判空。"""
    return {
        "title": _clean(kw.get("title")),
        "authors": _clean(kw.get("authors")),
        "year": _clean(kw.get("year")),
        "venue": _clean(kw.get("venue")),
        "doi": _clean(kw.get("doi")),
        "url": _clean(kw.get("url")),
        "pdf": _clean(kw.get("pdf")),
        "abstract": _clean(kw.get("abstract"))[:400],
        "source": kw.get("source", ""),
        "oa": bool(kw.get("oa")),
    }


# ------------------------------------------------------------------ OpenAlex
def search_openalex(query: str, limit: int) -> list:
    url = ("https://api.openalex.org/works?search=" + urllib.parse.quote(query)
           + "&per-page=" + str(limit) + "&mailto=contact@example.com")
    data = _json(url)
    out = []
    for w in data.get("results") or []:
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name") or ""
        names = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
        best = w.get("best_oa_location") or {}
        out.append(_item(
            title=w.get("display_name"),
            authors=", ".join([n for n in names if n][:6]),
            year=w.get("publication_year"),
            venue=src,
            doi=(w.get("doi") or "").replace("https://doi.org/", ""),
            url=w.get("doi") or w.get("id") or "",
            pdf=best.get("pdf_url") or (w.get("open_access") or {}).get("oa_url") or "",
            abstract="",          # 摘要要额外请求，省掉
            source="openalex",
            oa=bool((w.get("open_access") or {}).get("is_oa")),
        ))
    return out


# ------------------------------------------------------------------ Crossref
def search_crossref(query: str, limit: int) -> list:
    url = ("https://api.crossref.org/works?query=" + urllib.parse.quote(query)
           + "&rows=" + str(limit) + "&select=title,author,issued,container-title,DOI,URL,abstract,link")
    data = _json(url)
    out = []
    for w in ((data.get("message") or {}).get("items") or []):
        titles = w.get("title") or []
        authors = []
        for a in (w.get("author") or [])[:6]:
            authors.append(((a.get("given") or "") + " " + (a.get("family") or "")).strip())
        year = ""
        parts = (w.get("issued") or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
        pdf = ""
        for link in (w.get("link") or []):
            if (link.get("content-type") or "").startswith("application/pdf"):
                pdf = link.get("URL") or ""
                break
        out.append(_item(
            title=titles[0] if titles else "",
            authors=", ".join([x for x in authors if x]),
            year=year,
            venue=(w.get("container-title") or [""])[0],
            doi=w.get("DOI"),
            url=w.get("URL") or (("https://doi.org/" + (w.get("DOI") or "")) if w.get("DOI") else ""),
            pdf=pdf,
            abstract=re.sub(r"<[^>]+>", "", w.get("abstract") or ""),
            source="crossref",
            oa=bool(pdf),
        ))
    return out


# ------------------------------------------------------------------ DOAJ
def search_doaj(query: str, limit: int) -> list:
    url = ("https://doaj.org/api/search/articles/" + urllib.parse.quote(query)
           + "?pageSize=" + str(limit))
    data = _json(url)
    out = []
    for r in data.get("results") or []:
        b = r.get("bibjson") or {}
        links = b.get("link") or []
        pdf = ""
        page = ""
        for ln in links:
            t = (ln.get("type") or "").lower()
            if t == "fulltext" or t.endswith("pdf"):
                pdf = pdf or ln.get("url") or ""
            page = page or ln.get("url") or ""
        out.append(_item(
            title=b.get("title"),
            authors=", ".join([(a.get("name") or "") for a in (b.get("author") or [])[:6]]),
            year=b.get("year"),
            venue=(b.get("journal") or {}).get("title") or "",
            # bibjson.identifier 是 **字典列表**（{type, id}），不是字符串列表。
            # 之前直接 ", ".join() 会抛 TypeError，整个 DOAJ 源就此失败、计数为 0。
            doi=_doaj_doi(b),
            url=page or pdf,
            pdf=pdf,
            abstract=b.get("abstract") or "",
            source="doaj",
            oa=True,
        ))
    return out


def _doaj_doi(bib: dict) -> str:
    """从 bibjson.identifier 里挑出 DOI。字段长这样：[{"type":"doi","id":"10.xxx"}]"""
    for ident in (bib.get("identifier") or []):
        if isinstance(ident, dict) and (ident.get("type") or "").lower() == "doi":
            return str(ident.get("id") or "")
    return ""


# ------------------------------------------------------------------ PubMed
def search_pubmed(query: str, limit: int) -> list:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    ids = _json(base + "esearch.fcgi?db=pubmed&retmode=json&retmax=" + str(limit)
                + "&term=" + urllib.parse.quote(query))
    idlist = ((ids.get("esearchresult") or {}).get("idlist") or [])
    if not idlist:
        return []
    summ = _json(base + "esummary.fcgi?db=pubmed&retmode=json&id=" + ",".join(idlist))
    result = summ.get("result") or {}
    out = []
    for pid in idlist:
        w = result.get(pid) or {}
        if not w:
            continue
        doi = ""
        for aid in (w.get("articleids") or []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value") or ""
                break
        out.append(_item(
            title=w.get("title"),
            authors=", ".join([(a.get("name") or "") for a in (w.get("authors") or [])[:6]]),
            year=(w.get("pubdate") or "")[:4],
            venue=w.get("fulljournalname") or w.get("source") or "",
            doi=doi,
            url="https://pubmed.ncbi.nlm.nih.gov/" + pid + "/",
            pdf="",
            abstract="",
            source="pubmed",
            oa=False,
        ))
    return out


# ------------------------------------------------------------------ Semantic Scholar
def search_semantic(query: str, limit: int) -> list:
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query=" + urllib.parse.quote(query)
           + "&limit=" + str(limit)
           + "&fields=title,authors,year,venue,externalIds,openAccessPdf,url,abstract")
    data = _json(url)
    out = []
    for w in data.get("data") or []:
        ext = w.get("externalIds") or {}
        oa = w.get("openAccessPdf") or {}
        out.append(_item(
            title=w.get("title"),
            authors=", ".join([(a.get("name") or "") for a in (w.get("authors") or [])[:6]]),
            year=w.get("year"),
            venue=w.get("venue") or "",
            doi=ext.get("DOI") or "",
            url=w.get("url") or "",
            pdf=oa.get("url") or "",
            abstract=w.get("abstract") or "",
            source="semantic",
            oa=bool(oa.get("url")),
        ))
    return out


SOURCES = {
    "openalex": ("OpenAlex", search_openalex),
    "crossref": ("Crossref", search_crossref),
    "doaj": ("DOAJ", search_doaj),
    "pubmed": ("PubMed", search_pubmed),
    "semantic": ("Semantic Scholar", search_semantic),
}


def search(query: str, sources: list | None = None, limit: int = 10,
           year_from: int = 0, year_to: int = 0, oa_only: bool = False,
           sort: str = "relevance") -> dict:
    """并发查多个源。单个源失败不影响其它源，但**必须把失败原因报出来**。"""
    query = _clean(query)
    if not query:
        raise PaperError("请输入要检索的关键词")
    picked = [s for s in (sources or ["openalex", "crossref", "doaj"]) if s in SOURCES]
    if not picked:
        raise PaperError("至少选一个文献源")
    limit = max(3, min(int(limit or 10), 25))
    year_from = max(0, int(year_from or 0))
    year_to = max(0, int(year_to or 0))
    if year_from and year_to and year_from > year_to:
        year_from, year_to = year_to, year_from
    # 有筛选时多抓一些，否则过滤完可能剩不下几条
    fetch_limit = limit * 2 if (year_from or year_to or oa_only) else limit

    items: list = []
    errors: list = []
    counts: dict = {}

    def run(sid: str) -> tuple:
        name, fn = SOURCES[sid]
        try:
            got = fn(query, fetch_limit)
            return sid, name, got, None
        except Exception as exc:
            return sid, name, [], str(exc)[:200]

    with ThreadPoolExecutor(max_workers=len(picked)) as pool:
        for sid, name, got, err in pool.map(run, picked):
            counts[name] = len(got)
            items.extend(got)
            if err:
                errors.append({"source": name, "error": err})
                try:
                    from .log import log_error
                    log_error("papers:" + sid, err, "query=" + query[:60])
                except Exception:
                    pass

    # DOI 去重：同一个 DOI 只留第一条，但把"它出现在哪几个源"记下来
    seen: dict = {}
    for it in items:
        key = (it["doi"] or "").lower() or (it["title"] or "").lower()
        if not key:
            continue
        if key in seen:
            seen[key].setdefault("also", []).append(it["source"])
        else:
            it.setdefault("also", [])
            seen[key] = it
    merged = list(seen.values())

    # 筛选：年份区间 + 仅开放获取
    before = len(merged)
    if year_from or year_to or oa_only:
        kept = []
        for it in merged:
            y = int(re.sub(r"\D", "", it["year"]) or 0)
            if year_from and y and y < year_from:
                continue
            if year_to and y and y > year_to:
                continue
            if oa_only and not (it["oa"] or it["pdf"]):
                continue
            kept.append(it)
        merged = kept

    # 排序：用户可以自己选，默认"有全文的靠前"。
    # 相关度这一档我们拿不到各源的分数（接口不统一），所以退化成"按源内原序"，
    # 前端会把这一点说清楚，不假装是精确相关度。
    def year_of(x) -> int:
        return int(re.sub(r"\D", "", x["year"]) or 0)

    if sort == "newest":
        merged.sort(key=lambda x: (-year_of(x), 0 if x["pdf"] else 1))
    elif sort == "oldest":
        merged.sort(key=lambda x: (year_of(x) or 9999, 0 if x["pdf"] else 1))
    elif sort == "oa":
        merged.sort(key=lambda x: (0 if (x["oa"] or x["pdf"]) else 1, -year_of(x)))
    else:      # relevance
        merged.sort(key=lambda x: (0 if x["pdf"] else 1, -year_of(x)))
    return {"query": query, "items": merged[:limit * 3], "errors": errors,
            "counts": counts, "total": len(merged), "filtered_out": before - len(merged)}


# ---------------------------------------------------------------- 下载全文
MAX_PDF_BYTES = 40 * 1024 * 1024      # 单个 PDF 上限 40MB


def _host_is_safe(host: str) -> bool:
    """拒绝内网/回环/链路本地地址。

    这个接口的 URL 来自外部搜索结果 —— 不加限制的话，任何人都能借它去探测
    本机或内网服务（SSRF）。这里先把主机名解析成 IP 再判断，域名指向内网也拦得住。
    """
    import ipaddress
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _find_pdf_in_html(html: bytes, base) -> str:
    """从落地页 HTML 里找出真正的 PDF 地址。

    优先 <meta name="citation_pdf_url">（学术出版商的标准做法），
    其次任意指向 .pdf 的链接。相对地址按 base 解析。
    """
    import urllib.parse
    text = html[:400000].decode("utf-8", "replace")

    m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', text, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+\.pdf[^"\']*)["\'][^>]+name=["\']citation_pdf_url', text, re.I)
    if m:
        return urllib.parse.urljoin(base.geturl(), m.group(1).replace("&amp;", "&"))

    for href in re.findall(r'href=["\']([^"\']+)["\']', text):
        if ".pdf" in href.lower():
            full = urllib.parse.urljoin(base.geturl(), href.replace("&amp;", "&"))
            if full.startswith(("http://", "https://")):
                return full
    return ""


def download_pdf(url: str, dest_dir, name: str = "") -> dict:
    """把开放获取的 PDF 抓下来存到本地。

    只接受 http/https、只接受 PDF（看文件头）、有大小上限。
    """
    import urllib.parse
    from pathlib import Path

    url = _clean(url)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise PaperError("只支持 http / https 链接")
    if not parsed.hostname or not _host_is_safe(parsed.hostname):
        raise PaperError("这个链接指向内网或本机地址，出于安全考虑不下载")

    origin = parsed.scheme + "://" + (parsed.hostname or "")
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "Referer": origin + "/",
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            length = resp.headers.get("Content-Length")
            if length and int(length) > MAX_PDF_BYTES:
                raise PaperError("文件太大（" + str(round(int(length) / 1048576)) + " MB），已拒绝")
            chunks, total = [], 0
            while True:
                block = resp.read(262144)
                if not block:
                    break
                total += len(block)
                if total > MAX_PDF_BYTES:
                    raise PaperError("文件超过 40MB 上限，已中止")
                chunks.append(block)
            data = b"".join(chunks)
    except PaperError:
        raise
    except Exception as exc:
        msg = str(exc)
        if "403" in msg or "401" in msg:
            # MDPI、Wiley、Elsevier 这批会用 Cloudflare 之类挡住程序访问，
            # 换 UA 也没用（要跑 JS 挑战）。这不是我们的 bug，说清楚比硬试有用。
            raise PaperError("该出版商禁止程序直接下载，请点标题打开原文页手动下载") from None
        raise PaperError("下载失败：" + msg[:140]) from None

    # 很多出版商给的是一个 HTML 落地页，真正的 PDF 藏在 <meta name="citation_pdf_url">
    # 或某个 .pdf 链接里。这里尝试把真实 PDF 地址挖出来再抓一次。
    if not data.startswith(b"%PDF-") and data.lstrip()[:1] == b"<":
        found = _find_pdf_in_html(data, parsed)
        if found and found != url:
            try:
                req2 = urllib.request.Request(found, headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                    "Referer": url})
                with urllib.request.urlopen(req2, timeout=40) as resp2:
                    data = resp2.read(MAX_PDF_BYTES + 1)
                if len(data) > MAX_PDF_BYTES:
                    raise PaperError("文件超过 40MB 上限，已中止")
            except PaperError:
                raise
            except Exception as exc:
                raise PaperError("页面里找到 PDF 地址但抓取失败：" + str(exc)[:120]) from None

    if not data.startswith(b"%PDF-"):
        # 很多出版商返回的是登录页/HTML 而不是 PDF，必须拦住
        raise PaperError("拿到的不是 PDF（可能是登录页或跳转页），请到原文页手动下载")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", _clean(name) or "paper")[:80]
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    path = dest / safe
    index = 2
    while path.exists():
        path = dest / (Path(safe).stem + " (" + str(index) + ").pdf")
        index += 1
    path.write_bytes(data)
    return {"path": str(path), "size": len(data), "name": path.name}
