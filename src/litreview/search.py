"""Stage A — multi-axis OpenAlex search for lit-review candidates."""

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
CANDIDATES_PATH: Path = CACHE / "candidates.json"
HEADERS: dict[str, str] = {}
MAILTO: str = ""
QUERIES: dict[str, list[str]] = {}
SEEDS: dict[str, str] = {}

PER_QUERY_RESULTS = 75   # OpenAlex max is 200
DELAY = 0.5              # polite-pool friendly
HTTP_TIMEOUT = 25.0

DOI_ARXIV_RE = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.I)
URL_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)

WORK_FIELDS = (
    "id,doi,ids,title,abstract_inverted_index,publication_year,"
    "publication_date,authorships,primary_location,locations,"
    "cited_by_count,referenced_works"
)


# ===== OpenAlex record helpers =====

def reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex stores abstracts as inverted index {word: [pos, ...]}; rebuild it."""
    if not inv:
        return ""
    pos2word = {p: w for w, positions in inv.items() for p in (positions or [])}
    return " ".join(pos2word[i] for i in sorted(pos2word.keys()))


def extract_arxiv_id(work: dict) -> str | None:
    """Find an arxiv ID via DOI, then landing-page URL, then locations[]."""
    for doi_field in [(work.get("doi") or "").lower(),
                      ((work.get("ids") or {}).get("doi") or "").lower()]:
        if doi_field:
            m = DOI_ARXIV_RE.search(doi_field)
            if m:
                return m.group(1)
    for loc in [work.get("primary_location") or {}] + (work.get("locations") or []):
        m = URL_ARXIV_RE.search(loc.get("landing_page_url") or "")
        if m:
            return m.group(1)
    return None


def work_to_record(work: dict) -> dict | None:
    """Project an OpenAlex Work into our normalized candidate dict."""
    aid = extract_arxiv_id(work)
    if not aid:
        return None
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    if not abstract or len(abstract) < 60:
        return None
    pub_date = work.get("publication_date") or ""
    year, month = None, None
    if pub_date:
        try:
            parts = pub_date.split("-")
            year = int(parts[0])
            if len(parts) > 1:
                month = int(parts[1])
        except Exception:
            pass
    year = year or work.get("publication_year")
    authors = [
        ag["author"]["display_name"]
        for ag in (work.get("authorships") or [])[:6]
        if (ag.get("author") or {}).get("display_name")
    ]
    return {
        "arxiv_id": aid,
        "title": (work.get("title") or "").strip(),
        "authors": authors,
        "year": year,
        "month": month,
        "abstract": abstract[:2000],
        "openalex_id": work.get("id"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "via": [],
    }


def _merge(bucket: dict[str, dict], item: dict, via: str) -> None:
    """Add `item` to bucket (or merge into existing entry, accumulating `via` tags)."""
    aid = item["arxiv_id"]
    if aid in bucket:
        if via not in bucket[aid]["via"]:
            bucket[aid]["via"].append(via)
        for k in ("title", "abstract", "year", "month"):
            if not bucket[aid].get(k) and item.get(k):
                bucket[aid][k] = item[k]
        if not bucket[aid].get("authors") and item.get("authors"):
            bucket[aid]["authors"] = item["authors"]
    else:
        item["via"] = [via]
        bucket[aid] = item


# ===== OpenAlex API calls =====

def openalex_search(client: httpx.Client, query: str, per_page: int) -> list[dict]:
    """One keyword search → list of arxiv-IDed candidate records."""
    params = {
        "search": query,
        "per_page": per_page,
        "mailto": MAILTO,
        "filter": "publication_year:>2017",
        "select": WORK_FIELDS,
    }
    r = client.get("https://api.openalex.org/works", params=params, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        print(f"    OpenAlex HTTP {r.status_code} for {query!r}", file=sys.stderr)
        return []
    return [rec for w in r.json().get("results", []) if (rec := work_to_record(w))]


def openalex_work_by_arxiv(client: httpx.Client, arxiv_id: str) -> dict | None:
    """Fetch a single work by its arXiv DOI (with case-fallback)."""
    for doi_case in (f"10.48550/arXiv.{arxiv_id}", f"10.48550/arxiv.{arxiv_id}"):
        try:
            r = client.get(
                f"https://api.openalex.org/works/doi:{doi_case}",
                params={"mailto": MAILTO}, timeout=HTTP_TIMEOUT,
            )
        except Exception as e:
            print(f"  OA fetch {arxiv_id}: {e}", file=sys.stderr)
            continue
        if r.status_code == 200:
            return r.json()
    print(f"  OA fetch {arxiv_id}: not found", file=sys.stderr)
    return None


# ===== Citation-graph expansion =====

def citation_graph_from_seeds(client: httpx.Client, bucket: dict[str, dict]) -> int:
    """For each seed: pull its references + top-30 cited-by papers into the bucket."""
    added = 0
    for sid, sname in SEEDS.items():
        print(f"  seed {sid}  ({sname})")
        time.sleep(DELAY)
        work = openalex_work_by_arxiv(client, sid)
        if not work:
            continue

        # references (papers this seed cites)
        ref_ids = list(work.get("referenced_works") or [])
        for i in range(0, len(ref_ids), 50):
            batch = ref_ids[i : i + 50]
            if not batch:
                continue
            time.sleep(DELAY)
            try:
                r = client.get(
                    "https://api.openalex.org/works",
                    params={
                        "filter": "openalex:" + "|".join(b.split("/")[-1] for b in batch),
                        "per_page": 50, "mailto": MAILTO, "select": WORK_FIELDS,
                    },
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code != 200:
                    print(f"    refs batch HTTP {r.status_code}", file=sys.stderr)
                    continue
                for w in r.json().get("results", []):
                    rec = work_to_record(w)
                    if rec:
                        if rec["arxiv_id"] not in bucket:
                            added += 1
                        _merge(bucket, rec, f"A5:ref:{sid}")
            except Exception as e:
                print(f"    refs batch fail: {e}", file=sys.stderr)

        # cited-by (top-30 papers that cite this seed)
        oa_id = (work.get("id") or "").split("/")[-1]
        if oa_id:
            time.sleep(DELAY)
            try:
                r = client.get(
                    "https://api.openalex.org/works",
                    params={
                        "filter": f"cites:{oa_id}", "per_page": 30,
                        "sort": "cited_by_count:desc",
                        "mailto": MAILTO, "select": WORK_FIELDS,
                    },
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code == 200:
                    for w in r.json().get("results", []):
                        rec = work_to_record(w)
                        if rec:
                            if rec["arxiv_id"] not in bucket:
                                added += 1
                            _merge(bucket, rec, f"A5:citedby:{sid}")
                else:
                    print(f"    cites HTTP {r.status_code}", file=sys.stderr)
            except Exception as e:
                print(f"    cites fail: {e}", file=sys.stderr)
    return added


def ensure_seeds_in_bucket(client: httpx.Client, bucket: dict[str, dict]) -> None:
    """Inject any seed papers that keyword search didn't already surface."""
    for sid, name in SEEDS.items():
        if sid in bucket:
            continue
        time.sleep(DELAY)
        work = openalex_work_by_arxiv(client, sid)
        if not work:
            continue
        rec = work_to_record(work)
        if rec:
            _merge(bucket, rec, "A0:seed_paper")
            print(f"  injected seed {sid} ({name})")


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global MAILTO, HEADERS, CACHE, CANDIDATES_PATH, QUERIES, SEEDS
    MAILTO = cfg.get("mailto") or "anonymous@example.com"
    HEADERS = make_headers(MAILTO)
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    CANDIDATES_PATH = CACHE / "candidates.json"
    QUERIES = cfg["queries"]
    SEEDS = {str(k): v for k, v in (cfg.get("seeds") or {}).items()}
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)
    print(f"loaded config: {args.config}  (cache: {CACHE})")

    CACHE.mkdir(parents=True, exist_ok=True)
    bucket: dict[str, dict] = {}

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for axis, queries in QUERIES.items():
            print(f"\n=== {axis} ===")
            for q in queries:
                time.sleep(DELAY)
                print(f"  q: {q!r}")
                try:
                    results = openalex_search(client, q, PER_QUERY_RESULTS)
                except Exception as e:
                    print(f"    fail: {e}", file=sys.stderr)
                    continue
                for r in results:
                    _merge(bucket, r, f"{axis}:{q[:36]}")
                print(f"    +{len(results)} arxiv-IDed (unique total: {len(bucket)})")

        print(f"\n=== A5_CitationGraph (OpenAlex) ===")
        added = citation_graph_from_seeds(client, bucket)
        print(f"  +{added} new from citation graph (total: {len(bucket)})")

        print(f"\n=== Ensuring {len(SEEDS)} known seeds are present ===")
        ensure_seeds_in_bucket(client, bucket)

    final = [it for it in bucket.values() if it.get("abstract") and len(it["abstract"]) > 60]
    final.sort(
        key=lambda x: ((x.get("year") or 0), (x.get("month") or 0), x.get("cited_by_count") or 0),
        reverse=True,
    )

    CANDIDATES_PATH.write_text(json.dumps(final, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(final)} candidates -> {CANDIDATES_PATH}")
    print(f"  (dropped {len(bucket) - len(final)} entries with no abstract)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
