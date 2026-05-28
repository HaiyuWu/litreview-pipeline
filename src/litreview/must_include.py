"""Stage A2 — inject must-include arxiv IDs into keepers.json (bypass OpenAlex)."""

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

from litreview.urls import make_headers, resolve_path


# ===== Module state =====

CACHE: Path = Path("cache")
KEEPERS_PATH: Path = CACHE / "keepers.json"
HEADERS: dict[str, str] = {}
MUST_INCLUDE: list[tuple[str, str, str]] = []

MONTHS = {
    m[:3].lower(): i for i, m in enumerate(
        ["January","February","March","April","May","June","July","August",
         "September","October","November","December"], start=1
    )
}


# ===== arxiv abs scrape =====

def fetch_arxiv_abs(client: httpx.Client, arxiv_id: str) -> dict | None:
    """Scrape title / authors / abstract / date directly from arxiv.org/abs/<id>."""
    url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        r = client.get(url, timeout=15.0)
    except Exception as e:
        print(f"  FAIL {arxiv_id}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} for {arxiv_id}", file=sys.stderr)
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    title = ""
    if (h1 := soup.find("h1", class_="title")):
        title = re.sub(r"^Title:\s*", "", h1.get_text(" ", strip=True), flags=re.I).strip()

    authors: list[str] = []
    if (au_div := soup.find("div", class_="authors")):
        au_text = re.sub(r"^Authors?:\s*", "", au_div.get_text(" ", strip=True), flags=re.I)
        authors = [x.strip() for x in re.split(r",", au_text) if x.strip()][:6]

    abstract = ""
    if (ab_block := soup.find("blockquote", class_="abstract")):
        abstract = re.sub(r"^Abstract:\s*", "", ab_block.get_text(" ", strip=True), flags=re.I).strip()

    year, month = None, None
    if (dl := soup.find("div", class_="dateline")):
        m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", dl.get_text())
        if m:
            year = int(m.group(3))
            month = MONTHS.get(m.group(2)[:3].lower())

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": year,
        "month": month,
        "abstract": abstract[:2000],
        "openalex_id": None,
        "cited_by_count": 0,
        "via": ["A0:must_include"],
    }


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, KEEPERS_PATH, HEADERS, MUST_INCLUDE
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    KEEPERS_PATH = CACHE / "keepers.json"
    HEADERS = make_headers(cfg.get("mailto"))
    MUST_INCLUDE = [
        (m["arxiv_id"], m["section"], m.get("reason", ""))
        for m in (cfg.get("must_include") or [])
    ]
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)

    if not KEEPERS_PATH.exists():
        print(f"keepers.json not found at {KEEPERS_PATH}; run Stage B first.", file=sys.stderr)
        return 1
    if not MUST_INCLUDE:
        print("no must_include entries in config; nothing to do.")
        return 0

    keepers = json.loads(KEEPERS_PATH.read_text())
    existing = {k["arxiv_id"]: k for k in keepers}
    print(f"existing keepers: {len(keepers)}  must-include: {len(MUST_INCLUDE)}")

    added = 0
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for aid, section, reason in MUST_INCLUDE:
            if aid in existing:
                print(f"  already in keepers: {aid}")
                continue
            time.sleep(1.0)
            meta = fetch_arxiv_abs(client, aid)
            if not meta or not meta.get("abstract"):
                print(f"  FAIL to fetch {aid}", file=sys.stderr)
                continue
            meta["section"] = section
            meta["reason_keep"] = reason
            keepers.append(meta)
            existing[aid] = meta
            added += 1
            print(f"  + {aid}  [{section}]  {meta['title'][:60]}")

    if added:
        keepers.sort(
            key=lambda x: ((x.get("year") or 0), (x.get("month") or 0)),
            reverse=True,
        )
        KEEPERS_PATH.write_text(json.dumps(keepers, indent=2, ensure_ascii=False))
        print(f"\nwrote {len(keepers)} keepers (+{added} new) -> {KEEPERS_PATH}")
    else:
        print("\nno changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
