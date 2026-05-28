"""Stage C2 — find code/data/project URLs beyond what was in the PDF.

Three discovery routes layered for robustness:
  1. arxiv.org/abs/<id> HTML — anchors (catches code links added after the PDF)
  2. project-page crawl — chase *.github.io URLs the PDF mentioned
  3. GitHub search by title — verify hit's README contains the arxiv ID
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

from litreview.urls import (
    JUNK_CODE_REPOS, categorize, check_alive, find_urls_in_text,
    HTTP_TIMEOUT, make_headers, normalize, resolve_path,
)


# ===== Module state =====

CACHE: Path = Path("cache")
KEEPERS_PATH: Path = CACHE / "keepers.json"
PAPERS_DIR: Path = CACHE / "papers"
HEADERS: dict[str, str] = {}
GH_HEADERS: dict[str, str] = {}

DELAY = 1.0  # politeness between requests


# ===== Route 1: arxiv abs page =====

def discover_from_arxiv_abs(client: httpx.Client, arxiv_id: str) -> list[tuple[str, str, str]]:
    """Scrape anchors + abstract text on arxiv.org/abs/<id> for code/data/project URLs."""
    try:
        r = client.get(f"https://arxiv.org/abs/{arxiv_id}", timeout=HTTP_TIMEOUT)
    except Exception as e:
        print(f"    arxiv-abs fail: {e}", file=sys.stderr)
        return []
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    hits: list[tuple[str, str, str]] = []
    # abstract text
    ab = soup.find("blockquote", class_="abstract")
    if ab:
        for cat, url in find_urls_in_text(ab.get_text(" ", strip=True)):
            hits.append((cat, url, "arxiv_abs_abstract"))
    # all anchors on the page
    for a in soup.find_all("a", href=True):
        cat = categorize(normalize(a["href"]))
        if cat:
            hits.append((cat, normalize(a["href"]), "arxiv_abs_anchor"))
    return hits


# ===== Route 2: project-page crawl =====

def discover_from_project_pages(client: httpx.Client, existing_links: dict) -> list[tuple[str, str, str]]:
    """For each known *.github.io URL, fetch it and harvest more code/data anchors."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for link_entry in existing_links.get("project", []):
        url = link_entry.get("url")
        if not url or url in seen or not link_entry.get("alive"):
            continue
        seen.add(url)
        try:
            r = client.get(url, timeout=HTTP_TIMEOUT)
        except Exception as e:
            print(f"    project-page fail: {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for cat, url2 in find_urls_in_text(soup.get_text(" ", strip=True)):
            out.append((cat, url2, f"project_page:{url}"))
        for a in soup.find_all("a", href=True):
            cat = categorize(normalize(a["href"]))
            if cat:
                out.append((cat, normalize(a["href"]), f"project_page_anchor:{url}"))
    return out


# ===== Route 3: GitHub search by title =====

def discover_from_github_search(client: httpx.Client, title: str, arxiv_id: str) -> list[tuple[str, str, str]]:
    """Search github by title; accept only repos whose README mentions the arxiv ID."""
    if not title:
        return []
    q_title = title.split(":")[0].strip()
    if len(q_title) < 6:
        q_title = title.split(".")[0].strip()
    q = f'"{q_title}" arxiv'
    try:
        r = client.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "per_page": 5, "sort": "stars", "order": "desc"},
            headers=GH_HEADERS,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print(f"    github-search fail: {e}", file=sys.stderr)
        return []
    if r.status_code == 403:
        print("    github-search rate limited (403)", file=sys.stderr)
        return []
    if r.status_code != 200:
        return []
    hits: list[tuple[str, str, str]] = []
    for item in r.json().get("items", [])[:5]:
        full_name = item.get("full_name", "")
        if full_name in JUNK_CODE_REPOS:
            continue
        time.sleep(DELAY)
        try:
            rr = client.get(
                f"https://api.github.com/repos/{full_name}/readme",
                headers=GH_HEADERS, timeout=HTTP_TIMEOUT,
            )
        except Exception:
            continue
        if rr.status_code != 200:
            continue
        download_url = (rr.json() or {}).get("download_url")
        if not download_url:
            continue
        time.sleep(DELAY)
        try:
            rrr = client.get(download_url, timeout=HTTP_TIMEOUT)
        except Exception:
            continue
        if rrr.status_code != 200:
            continue
        if re.search(rf"\b{re.escape(arxiv_id)}\b", rrr.text):
            hits.append(("code", normalize(item.get("html_url")), "github_search:readme_verified"))
            print(f"    github-search verified: {full_name}")
            break  # only accept top match per title
    return hits


# ===== Per-paper enrichment =====

def enrich_paper(client: httpx.Client, meta_path: Path, do_github: bool = True) -> dict:
    meta = json.loads(meta_path.read_text())
    aid = meta["arxiv_id"]
    title = meta.get("title") or ""
    existing_links = meta.get("links") or {"code": [], "data": [], "project": []}
    print(f"  {aid}  {title[:60]}")

    known: set[tuple[str, str]] = {
        (cat, e["url"])
        for cat in ("code", "data", "project")
        for e in existing_links.get(cat, [])
    }

    candidates: list[tuple[str, str, str]] = []
    time.sleep(DELAY)
    candidates.extend(discover_from_arxiv_abs(client, aid))

    has_alive_code = any(e.get("alive") for e in existing_links.get("code", []))
    has_alive_proj = any(e.get("alive") for e in existing_links.get("project", []))

    if has_alive_proj:
        candidates.extend(discover_from_project_pages(client, existing_links))

    if do_github and not has_alive_code:
        time.sleep(DELAY)
        candidates.extend(discover_from_github_search(client, title, aid))

    additions = 0
    seen = set(known)
    for cat, url, src in candidates:
        if cat not in ("code", "data", "project") or (cat, url) in seen:
            continue
        seen.add((cat, url))
        existing_links.setdefault(cat, []).append({
            "url": url, "source": src, "alive": check_alive(client, url),
        })
        additions += 1
        print(f"    + [{cat}] {url}  ({src})")

    meta["links"] = existing_links
    if additions > 0:
        meta["_enriched"] = True
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return {"arxiv_id": aid, "additions": additions}


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, KEEPERS_PATH, PAPERS_DIR, HEADERS, GH_HEADERS
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    KEEPERS_PATH = CACHE / "keepers.json"
    PAPERS_DIR = CACHE / "papers"
    HEADERS = make_headers(cfg.get("mailto"))
    GH_HEADERS = {**HEADERS, "Accept": "application/vnd.github+json"}
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)

    keepers = json.loads(KEEPERS_PATH.read_text())
    print(f"enriching links for {len(keepers)} keepers")

    summary = {"papers": 0, "with_additions": 0, "total_additions": 0}
    enriched_ids: list[str] = []
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for k in keepers:
            meta_path = PAPERS_DIR / f"{k['arxiv_id']}.meta.json"
            if not meta_path.exists():
                print(f"  skip (no meta): {k['arxiv_id']}")
                continue
            res = enrich_paper(client, meta_path, do_github=True)
            summary["papers"] += 1
            if res["additions"] > 0:
                summary["with_additions"] += 1
                summary["total_additions"] += res["additions"]
                enriched_ids.append(res["arxiv_id"])

    (CACHE / "_enriched_ids.json").write_text(json.dumps(enriched_ids, indent=2))
    print(f"\nDONE  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
