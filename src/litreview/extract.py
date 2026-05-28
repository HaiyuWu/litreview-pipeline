"""Stage C — download PDFs, regex URLs from text, slice section bodies."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import fitz
import httpx
import yaml

from litreview.urls import (
    URL_PATTERNS, find_urls_in_text, liveness_pass, make_headers, resolve_path,
)


# ===== Module state (set by load_config) =====

CACHE: Path = Path("cache")
KEEPERS_PATH: Path = CACHE / "keepers.json"
PAPERS_DIR: Path = CACHE / "papers"
PDFS_DIR: Path = CACHE / "pdfs"
HEADERS: dict[str, str] = {}

PDF_TIMEOUT = 60.0
PDF_DELAY = 4.0  # arxiv asks for >= 3 sec between requests


# ===== URL extraction =====

def find_urls(text: str) -> dict[str, list[dict]]:
    """Return {category: [{url, context}, ...]} for all categorized URLs in text."""
    out: dict[str, list[dict]] = {"code": [], "data": [], "project": []}
    seen: dict[str, set[str]] = {"code": set(), "data": set(), "project": set()}
    for cat, url, ctx in find_urls_in_text(text, with_context=True):
        if url in seen[cat]:
            continue
        seen[cat].add(url)
        out[cat].append({"url": url, "context": ctx})
    return out


# ===== Section slicing =====

SECTION_HEAD_RE = re.compile(
    r"^\s*(?:\d+\.?\s+)?(abstract|introduction|background|related\s+work|"
    r"method(?:s|ology)?|approach|preliminaries|experiments|evaluation|"
    r"results|analysis|discussion|conclusion|conclusions|limitations)\b",
    re.I | re.M,
)


def slice_sections(text: str) -> dict[str, str]:
    """Best-effort section split. Returns {abstract, intro, method, results, conclusion}."""
    heads = [(m.start(), m.group(1).lower().strip()) for m in SECTION_HEAD_RE.finditer(text)]
    sections: dict[str, str] = {}
    for i, (pos, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        sections.setdefault(name, text[pos:end])

    def grab(*names: str, cap: int = 3500) -> str:
        for n in names:
            if n in sections:
                body = sections[n].split("\n", 1)[1] if "\n" in sections[n] else sections[n]
                return body.strip()[:cap]
        return ""

    out = {
        "abstract":   grab("abstract", cap=2200),
        "intro":      grab("introduction", cap=3500),
        "method":     grab("method", "methods", "methodology", "approach", cap=3500),
        "results":    grab("results", "experiments", "evaluation", cap=2500),
        "conclusion": grab("conclusion", "conclusions", "limitations", "discussion", cap=2200),
    }
    if not out["abstract"] and not out["intro"]:
        out["abstract"] = text[:2200].strip()
    return out


# ===== PDF naming + download + parse =====

def slugify(title: str, max_len: int = 60) -> str:
    """Slug from title's first segment (before colon) — usually the method name."""
    if not title:
        return "untitled"
    head = title.split(":", 1)[0].strip()
    if len(head) < 4 and ":" in title:
        head = title.replace(":", " ").strip()
    slug = re.sub(r"[^\w\s-]", "", head.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"


def pdf_path_for(arxiv_id: str, title: str | None = None) -> Path:
    """Canonical filename: '<slug>--<arxiv_id>.pdf' (or legacy '<arxiv_id>.pdf')."""
    if title:
        return PDFS_DIR / f"{slugify(title)}--{arxiv_id}.pdf"
    return PDFS_DIR / f"{arxiv_id}.pdf"


def find_cached_pdf(arxiv_id: str) -> Path | None:
    """Look for cached PDF under new or legacy naming. Returns None if missing."""
    for p in PDFS_DIR.glob(f"*--{arxiv_id}.pdf"):
        if p.stat().st_size > 5000:
            return p
    p = PDFS_DIR / f"{arxiv_id}.pdf"
    if p.exists() and p.stat().st_size > 5000:
        return p
    return None


def download_pdf(client: httpx.Client, arxiv_id: str, title: str | None = None) -> Path | None:
    cached = find_cached_pdf(arxiv_id)
    if cached is not None:
        return cached
    dst = pdf_path_for(arxiv_id, title)
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    try:
        with client.stream("GET", url, timeout=PDF_TIMEOUT) as r:
            if r.status_code != 200:
                print(f"    PDF {arxiv_id}: HTTP {r.status_code}", file=sys.stderr)
                return None
            with open(dst, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except Exception as e:
        print(f"    PDF {arxiv_id}: {e}", file=sys.stderr)
        return None
    if dst.stat().st_size < 5000:
        dst.unlink(missing_ok=True)
        return None
    return dst


def extract_text(pdf_path: Path) -> str:
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"    fitz fail {pdf_path.name}: {e}", file=sys.stderr)
        return ""
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(pages)


# ===== Per-paper pipeline =====

def process_paper(client: httpx.Client, entry: dict) -> dict | None:
    aid = entry["arxiv_id"]
    out_path = PAPERS_DIR / f"{aid}.meta.json"
    if out_path.exists():
        return json.loads(out_path.read_text())
    print(f"  {aid}  {entry.get('title','')[:60]}")
    pdf = download_pdf(client, aid, entry.get("title"))
    if not pdf:
        meta = {
            **entry,
            "links": {"code": [], "data": [], "project": []},
            "section_texts": {},
            "_error": "pdf_unavailable",
        }
        out_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        return meta
    text = extract_text(pdf)
    links = find_urls(text)
    liveness_pass(client, links)
    sections = slice_sections(text)
    meta = {
        **entry,
        "links": links,
        "section_texts": sections,
        "pdf_chars": len(text),
    }
    out_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return meta


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, KEEPERS_PATH, PAPERS_DIR, PDFS_DIR, HEADERS
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    KEEPERS_PATH = CACHE / "keepers.json"
    PAPERS_DIR = CACHE / "papers"
    PDFS_DIR = CACHE / "pdfs"
    HEADERS = make_headers(cfg.get("mailto"))
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)

    if not KEEPERS_PATH.exists():
        print(f"keepers.json not found at {KEEPERS_PATH}", file=sys.stderr)
        return 1

    keepers = json.loads(KEEPERS_PATH.read_text())
    if isinstance(keepers, dict) and "keepers" in keepers:
        keepers = keepers["keepers"]
    print(f"Processing {len(keepers)} keepers")

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    PDFS_DIR.mkdir(parents=True, exist_ok=True)

    summary = {"ok": 0, "no_pdf": 0, "with_code": 0, "with_data": 0}
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for i, entry in enumerate(keepers):
            time.sleep(PDF_DELAY)
            meta = process_paper(client, entry)
            if not meta:
                continue
            if meta.get("_error") == "pdf_unavailable":
                summary["no_pdf"] += 1
            else:
                summary["ok"] += 1
                if any(meta["links"]["code"]):
                    summary["with_code"] += 1
                if any(meta["links"]["data"]):
                    summary["with_data"] += 1
            if (i + 1) % 5 == 0:
                print(f"  progress: {i+1}/{len(keepers)}  {summary}")

    print(f"\nDONE  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
