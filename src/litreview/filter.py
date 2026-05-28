"""Stage B helpers — slim candidates, list them, apply filter decisions.

These three operations used to be ad-hoc Python written by the orchestrating
agent at runtime. That was a smell: every new topic re-derived the same JSON
manipulation. Now they live here as deterministic CLI subcommands so the
agent never needs to write code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from litreview.urls import resolve_path


# ===== Module state =====

CACHE: Path = Path("cache")


def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    return cfg


# ===== slim-candidates =====

def slim_candidates(max_abstract_chars: int = 700) -> int:
    """Write candidates_slim.json — trimmed view for the Stage B filter subagent."""
    src = CACHE / "candidates.json"
    dst = CACHE / "candidates_slim.json"
    if not src.exists():
        print(f"ERROR: {src} not found; run `litreview search` first.", file=sys.stderr)
        return 1
    data = json.loads(src.read_text())
    slim = [
        {
            "arxiv_id": d["arxiv_id"],
            "title": d.get("title", ""),
            "year": d.get("year"),
            "cited_by": d.get("cited_by_count", 0),
            "via": d.get("via", []),
            "abstract": (d.get("abstract", "") or "")[:max_abstract_chars].rstrip(),
        }
        for d in data
    ]
    dst.write_text(json.dumps(slim, indent=2, ensure_ascii=False))
    print(f"wrote {len(slim)} slim candidates -> {dst}")
    return 0


# ===== list-candidates =====

def list_candidates(fmt: str = "compact", limit: int | None = None) -> int:
    """Print candidates to stdout in one of three formats, sorted by year/cited desc."""
    src = CACHE / "candidates.json"
    if not src.exists():
        print(f"ERROR: {src} not found; run `litreview search` first.", file=sys.stderr)
        return 1
    data = json.loads(src.read_text())
    data.sort(key=lambda d: (-(d.get("year") or 0), -(d.get("cited_by_count") or 0)))
    if limit:
        data = data[:limit]

    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if fmt == "slim":
        slim = [
            {
                "arxiv_id": d["arxiv_id"],
                "title": d.get("title", ""),
                "year": d.get("year"),
                "cited_by": d.get("cited_by_count", 0),
                "abstract": (d.get("abstract", "") or "")[:700],
            }
            for d in data
        ]
        print(json.dumps(slim, indent=2, ensure_ascii=False))
        return 0

    # compact (default) — one line per candidate
    for d in data:
        via = ",".join(v.split(":", 1)[0] for v in (d.get("via") or [])[:3])
        abst = (d.get("abstract", "") or "").replace("\n", " ")[:200]
        print(
            f"[{d['arxiv_id']}] y={d.get('year')} c={d.get('cited_by_count', 0):>4} "
            f"{(d.get('title') or '')[:90]} | via:{via} | {abst}"
        )
    return 0


# ===== apply-filter =====

def apply_filter(decisions_path: Path | None = None) -> int:
    """Join Stage B decisions with candidate metadata → keepers.json + dropped.json."""
    candidates_path = CACHE / "candidates.json"
    decisions_path = decisions_path or (CACHE / "_filter_decisions.json")
    keepers_path = CACHE / "keepers.json"
    dropped_path = CACHE / "dropped.json"

    if not candidates_path.exists():
        print(f"ERROR: {candidates_path} not found.", file=sys.stderr)
        return 1
    if not decisions_path.exists():
        print(f"ERROR: {decisions_path} not found; run the Stage B filter agent first.",
              file=sys.stderr)
        return 1

    cands = {d["arxiv_id"]: d for d in json.loads(candidates_path.read_text())}
    dec = json.loads(decisions_path.read_text())

    keepers: list[dict] = []
    for k in dec.get("keep", []):
        aid = k["arxiv_id"]
        if aid not in cands:
            print(f"  warn: keeper {aid} not in candidates", file=sys.stderr)
            continue
        entry = dict(cands[aid])
        entry["section"] = k.get("section")
        entry["reason_keep"] = k.get("reason", "")
        keepers.append(entry)

    dropped = [
        {
            "arxiv_id": d["arxiv_id"],
            "title": cands.get(d["arxiv_id"], {}).get("title", ""),
            "reason_drop": d.get("reason", ""),
        }
        for d in dec.get("drop", [])
    ]

    keepers_path.write_text(json.dumps(keepers, indent=2, ensure_ascii=False))
    dropped_path.write_text(json.dumps(dropped, indent=2, ensure_ascii=False))

    # Distribution by section for quick sanity check.
    from collections import Counter
    by_sec = Counter(k.get("section") for k in keepers)
    print(f"keepers: {len(keepers)}  dropped: {len(dropped)}")
    print(f"  by section: {dict(by_sec)}")
    return 0


# ===== CLI entry points =====

def main_slim() -> int:
    """Entry point for `litreview slim-candidates`."""
    ap = argparse.ArgumentParser(description=slim_candidates.__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--max-abstract-chars", type=int, default=700,
                    help="Trim abstracts to this many chars (default: 700).")
    args = ap.parse_args()
    load_config(args.config)
    return slim_candidates(max_abstract_chars=args.max_abstract_chars)


def main_list() -> int:
    """Entry point for `litreview list-candidates`."""
    ap = argparse.ArgumentParser(description=list_candidates.__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--format", choices=["compact", "slim", "json"], default="compact")
    ap.add_argument("--limit", type=int, default=None, help="Show only the top-N entries.")
    args = ap.parse_args()
    load_config(args.config)
    return list_candidates(fmt=args.format, limit=args.limit)


def main_apply() -> int:
    """Entry point for `litreview apply-filter`."""
    ap = argparse.ArgumentParser(description=apply_filter.__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--decisions", default=None,
                    help="Path to _filter_decisions.json (default: <cache>/_filter_decisions.json).")
    args = ap.parse_args()
    load_config(args.config)
    return apply_filter(decisions_path=Path(args.decisions) if args.decisions else None)
