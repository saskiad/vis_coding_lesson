#!/usr/bin/env python3
"""Download PDFs for the links in urls.txt into ./pdfs.

Usage:  pip install requests
        python3 download_pdfs.py

Writes a report to download_report.csv. Anything that fails (paywalls,
bot-blocking, login-required) is marked FAILED so you can fetch it by hand.
"""
import csv, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import requests

HERE = Path(__file__).parent
OUT = HERE / "pdfs"
OUT.mkdir(exist_ok=True)
S = requests.Session()
S.headers["User-Agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S.headers["Accept"] = "application/pdf,text/html;q=0.9,*/*;q=0.8"


def clean(u):
    u = u.strip()
    m = re.fullmatch(r"arXiv:(\S+)", u, re.I)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    # UW library proxy -> plain publisher URL (the proxy needs your login)
    u = u.replace("www-sciencedirect-com.offcampus.lib.washington.edu", "www.sciencedirect.com")
    return u


def candidates(u):
    """Yield likely direct-PDF URLs for a landing-page URL."""
    p = urlparse(u)
    host, path = p.netloc, p.path
    base = u.split("#")[0].split("?")[0]
    if "arxiv.org" in host:
        aid = re.sub(r"^/(abs|pdf)/", "", path).removesuffix(".pdf")
        yield f"https://arxiv.org/pdf/{aid}.pdf"
    elif "biorxiv.org" in host:
        # bioRxiv pages also sit behind a bot check, so ask its API which
        # version/date exists and build the exact file link the "Download PDF"
        # button uses (.../early/YYYY/MM/DD/<id>.full.pdf).
        b = re.sub(r"\.(full|abstract)(\.pdf)?$", "", base).removesuffix(".full.pdf")
        m = re.search(r"10\.1101/(\d[\d.]*\d)(?:v(\d+))?", b)
        if m:
            rid, want = m.group(1), m.group(2)
        else:  # .../content/early/2017/09/21/134379
            rid, want = b.rstrip("/").split("/")[-1], None
        try:
            coll = S.get(f"https://api.biorxiv.org/details/biorxiv/10.1101/{rid}", timeout=30).json().get("collection", [])
        except Exception:
            coll = []
        coll.sort(key=lambda c: int(c["version"]), reverse=True)
        if want:
            coll.sort(key=lambda c: c["version"] != want)  # requested version first
        for c in coll:
            yield f"https://www.biorxiv.org/content/biorxiv/early/{c['date'].replace('-', '/')}/{rid}.full.pdf"
            yield f"https://www.biorxiv.org/content/10.1101/{rid}v{c['version']}.full.pdf"
        yield b + ".full.pdf"
    elif "plos.org" in host:
        m = re.search(r"id=([^&#]+)", u)
        if m:
            j = path.split("/")[1]
            yield f"https://journals.plos.org/{j}/article/file?id={m.group(1)}&type=printable"
    elif host == "doi.org":
        yield from candidates_from_doi(path.lstrip("/"))
    elif "eneuro.org" in host or "jneurosci.org" in host or "pnas.org" in host:
        yield base + ".full.pdf"
        yield base + ".full"
    elif "elifesciences.org" in host:
        # The article page sits behind a JavaScript bot challenge, so go to the
        # eLife API (which lists the PDF link the page's Download button uses),
        # then fall back to guessing the versioned CDN file.
        aid = path.rstrip("/").split("/")[-1]
        for accept in ("application/vnd.elife.article-vor+json", "application/vnd.elife.article-poa+json"):
            r = try_get_json(f"https://api.elifesciences.org/articles/{aid}", accept)
            if r and r.get("pdf"):
                yield r["pdf"]
                break
        for v in range(6, 0, -1):
            yield f"https://cdn.elifesciences.org/articles/{aid}/elife-{aid}-v{v}.pdf"
        return
    elif "nature.com" in host:
        yield base + ".pdf"
    elif "frontiersin.org" in host:
        yield base.removesuffix("/full") + "/pdf"
    elif "link.springer.com" in host:
        yield base.replace("/article/", "/content/pdf/").replace("/chapter/", "/content/pdf/") + ".pdf"
    elif "tandfonline.com" in host:
        yield re.sub(r"/doi/(full|abs|pdf)/", "/doi/pdf/", base)
    elif "openreview.net" in host:
        m = re.search(r"id=([^&#]+)", u)
        if m:
            yield f"https://openreview.net/pdf?id={m.group(1)}"
    elif "jstage.jst.go.jp" in host:
        yield u
    yield u  # fall back to landing page -> look for citation_pdf_url


def try_get_json(url, accept):
    try:
        r = S.get(url, headers={"Accept": accept}, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def candidates_from_doi(doi):
    if doi.startswith("10.1371/"):
        yield f"https://journals.plos.org/ploscompbiol/article/file?id={doi}&type=printable"
    if doi.startswith("10.1101/"):
        yield f"https://www.biorxiv.org/content/{doi}.full.pdf"
    yield f"https://doi.org/{doi}"


def is_pdf(r):
    return r.content[:5] == b"%PDF-"


def try_get(url):
    try:
        return S.get(url, timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        return e


def fetch(u):
    """Return (pdf_bytes, source_url) or (None, reason)."""
    reason = "no candidate worked"
    for c in candidates(u):
        r = try_get(c)
        if isinstance(r, Exception):
            reason = f"error: {r.__class__.__name__}"
            continue
        if r.status_code == 200 and is_pdf(r):
            return r.content, c
        reason = f"HTTP {r.status_code}, not a PDF at {urlparse(c).netloc}"
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            m = (re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', r.text, re.I)
                 or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url', r.text, re.I))
            if m:
                pu = urljoin(r.url, m.group(1))
                r2 = try_get(pu)
                if not isinstance(r2, Exception) and r2.status_code == 200 and is_pdf(r2):
                    return r2.content, pu
                reason = "PDF link found but blocked (likely paywall/login)"
    return None, reason


def slug(u, i):
    p = urlparse(u)
    s = unquote((p.path + ("_" + p.query if "id=" in p.query else "")).strip("/"))
    s = re.sub(r"\.(pdf|full)$", "", s)
    s = re.sub(r"[^A-Za-z0-9.]+", "_", s)[-80:].strip("_")
    return f"{i:02d}_{s}.pdf"


def main():
    urls = [clean(l) for l in (HERE / "urls.txt").read_text().splitlines() if l.strip()]
    rows = []
    for i, u in enumerate(urls, 1):
        name = slug(u, i)
        dest = OUT / name
        if dest.exists():
            status, note = "OK", "already downloaded"
        else:
            data, info = fetch(u)
            if data:
                dest.write_bytes(data)
                status, note = "OK", info
            else:
                status, note = "FAILED", info
            time.sleep(1.5)
        print(f"[{i:02d}/{len(urls)}] {status:6} {u}  ({note})", flush=True)
        rows.append([i, status, name if status == "OK" else "", u, note])
    with open(HERE / "download_report.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["#", "status", "file", "url", "note"])
        w.writerows(rows)
    ok = sum(r[1] == "OK" for r in rows)
    print(f"\nDone: {ok}/{len(rows)} downloaded. See download_report.csv for failures.")


if __name__ == "__main__":
    sys.exit(main())
