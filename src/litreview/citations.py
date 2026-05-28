"""Stage A3 — enrich `cited_by_count` via Semantic Scholar (closer to Google Scholar)."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

from litreview.urls import make_headers, resolve_path


# ===== Module state =====

CACHE: Path = Path("cache")
PAPERS_DIR: Path = CACHE / "papers"
HEADERS: dict[str, str] = {}
CITATION_SOURCE: str = "semanticscholar"

S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = "citationCount"
BATCH_SIZE = 100   # S2 accepts up to 500; 100 keeps responses small + retryable
S2_DELAY = 1.0     # polite gap between batches


# ===== S2 batch lookup =====

def fetch_s2_citations(client: httpx.Client, arxiv_ids: list[str]) -> dict[str, int | None]:
    """{arxiv_id: citation_count}. None if S2 has no record or batch failed."""
    out: dict[str, int | None] = {aid: None for aid in arxiv_ids}
    for i in range(0, len(arxiv_ids), BATCH_SIZE):
        batch = arxiv_ids[i : i + BATCH_SIZE]
        s2_ids = [f"ARXIV:{aid}" for aid in batch]
        time.sleep(S2_DELAY)
        try:
            r = client.post(
                S2_BATCH_URL,
                params={"fields": S2_FIELDS},
                json={"ids": s2_ids},
                timeout=30.0,
            )
        except Exception as e:
            print(f"  S2 batch {i}-{i+len(batch)} fail: {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            print(f"  S2 batch HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            continue
        try:
            records = r.json()
        except Exception as e:
            print(f"  S2 batch parse fail: {e}", file=sys.stderr)
            continue
        for aid, rec in zip(batch, records):
            if rec is not None:
                out[aid] = rec.get("citationCount")
        print(f"  S2 batch {i+1}-{i+len(batch)}/{len(arxiv_ids)}: "
              f"{sum(1 for aid in batch if out[aid] is not None)} hit")
    return out


def enrich_records(records: list[dict], citations: dict[str, int | None]) -> int:
    """Replace `cited_by_count` in-place where S2 has a value. Returns # records updated."""
    updated = 0
    for rec in records:
        aid = rec.get("arxiv_id")
        s2_cite = citations.get(aid)
        if s2_cite is None:
            continue
        if rec.get("cited_by_count") == s2_cite:
            continue
        rec["cited_by_count_openalex"] = rec.get("cited_by_count")
        rec["cited_by_count"] = s2_cite
        rec["citation_source"] = "semanticscholar"
        updated += 1
    return updated


def patch_md_citations(records: list[dict]) -> int:
    """Patch the `citations: N` line in each per-paper .md to match the updated count."""
    patched = 0
    if not PAPERS_DIR.exists():
        return 0
    for rec in records:
        if rec.get("citation_source") != "semanticscholar":
            continue
        md = PAPERS_DIR / f"{rec['arxiv_id']}.md"
        if not md.exists():
            continue
        text = md.read_text()
        new_text = re.sub(r"(citations:)\s*\d+", rf"\1 {rec['cited_by_count']}", text, count=1)
        if new_text != text:
            md.write_text(new_text)
            patched += 1
    return patched


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, PAPERS_DIR, HEADERS, CITATION_SOURCE
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    PAPERS_DIR = CACHE / "papers"
    HEADERS = make_headers(cfg.get("mailto"))
    CITATION_SOURCE = cfg.get("citation_source", "semanticscholar")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    ap.add_argument(
        "--scope", choices=["keepers", "candidates", "both"], default="keepers",
        help="Which cache files to update (default: keepers).",
    )
    args = ap.parse_args()
    load_config(args.config)

    if CITATION_SOURCE == "openalex":
        print("citation_source = 'openalex'; nothing to do.")
        return 0

    targets: list[Path] = []
    if args.scope in ("keepers", "both"):
        p = CACHE / "keepers.json"
        if p.exists():
            targets.append(p)
    if args.scope in ("candidates", "both"):
        p = CACHE / "candidates.json"
        if p.exists():
            targets.append(p)

    if not targets:
        print(f"no target files in {CACHE}; run search/filter first.", file=sys.stderr)
        return 1

    # Collect unique arxiv IDs across all target files.
    file_records: list[tuple[Path, list[dict]]] = []
    all_ids: set[str] = set()
    for p in targets:
        recs = json.loads(p.read_text())
        file_records.append((p, recs))
        for r in recs:
            if r.get("arxiv_id"):
                all_ids.add(r["arxiv_id"])

    if not all_ids:
        print("no arxiv_ids found across target files.")
        return 0

    print(f"Looking up Semantic Scholar citations for {len(all_ids)} unique arxiv IDs...")
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        citations = fetch_s2_citations(client, sorted(all_ids))

    hit = sum(1 for v in citations.values() if v is not None)
    print(f"\nS2 returned counts for {hit}/{len(all_ids)} papers")

    # Apply per-file.
    updated_recs: list[dict] = []
    for path, recs in file_records:
        n = enrich_records(recs, citations)
        path.write_text(json.dumps(recs, indent=2, ensure_ascii=False))
        print(f"  {path.name}: replaced {n} cited_by_count fields")
        updated_recs.extend(recs)

    # Patch per-paper .md "citations: N" lines.
    n_md = patch_md_citations(updated_recs)
    print(f"  patched {n_md} per-paper .md citation headers")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
