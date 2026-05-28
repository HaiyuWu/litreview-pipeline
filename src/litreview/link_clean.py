"""Stage C3 — filter junk URLs from enriched link sets + apply manual overrides."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from litreview.urls import resolve_path


# ===== Module state =====

CACHE: Path = Path("cache")
PAPERS_DIR: Path = CACHE / "papers"
KEEPERS_PATH: Path = CACHE / "keepers.json"
OVERRIDES_PATH: Path = CACHE / "manual_overrides.json"


# ===== Junk filter =====

# Always-skip URL patterns (arxiv page furniture, generic tool repos, etc.).
JUNK_URL_RE = [
    re.compile(r"^https?://huggingface\.co/docs/", re.I),
    re.compile(r"^https?://huggingface\.co/spaces/Qwen$", re.I),
    re.compile(r"^https?://github\.com/arxiv-vanity", re.I),
    re.compile(r"^https?://github\.com/openai/CLIP$", re.I),
    re.compile(r"^https?://github\.com/huggingface/transformers$", re.I),
    re.compile(r"^https?://github\.com/pytorch/pytorch$", re.I),
    re.compile(r"^https?://github\.com/facebookresearch/dinov2$", re.I),
]


def is_junk(url: str) -> bool:
    return any(p.match(url) for p in JUNK_URL_RE)


def clean_links(links: dict) -> dict:
    """Drop junk URLs and dedupe by (cat, url) inside a meta.json's `links` dict."""
    out = {"code": [], "data": [], "project": []}
    seen: set[tuple[str, str]] = set()
    for cat in ("code", "data", "project"):
        for entry in links.get(cat, []) or []:
            url = entry.get("url") or ""
            if is_junk(url) or (cat, url) in seen:
                continue
            seen.add((cat, url))
            out[cat].append(entry)
    return out


# ===== Manual overrides =====

def apply_overrides(meta: dict, overrides: dict) -> bool:
    """Inject user-curated link(s) for this paper if specified. Returns True on change."""
    aid = meta["arxiv_id"]
    if aid not in overrides:
        return False
    over = overrides[aid]
    changed = False
    for cat in ("code", "data", "project"):
        url = over.get(cat)
        if not url:
            continue
        meta["links"].setdefault(cat, [])
        meta["links"][cat] = [e for e in meta["links"][cat] if e.get("url") != url]
        meta["links"][cat].insert(0, {
            "url": url,
            "source": "manual_override",
            "alive": True,
            "_curator_note": "user-confirmed official URL",
        })
        changed = True
    if changed:
        meta["_has_manual_override"] = True
    return changed


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, KEEPERS_PATH, PAPERS_DIR, OVERRIDES_PATH
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    KEEPERS_PATH = CACHE / "keepers.json"
    PAPERS_DIR = CACHE / "papers"
    OVERRIDES_PATH = CACHE / "manual_overrides.json"
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)

    keepers = json.loads(KEEPERS_PATH.read_text())
    overrides: dict = {}
    if OVERRIDES_PATH.exists():
        loaded = json.loads(OVERRIDES_PATH.read_text())
        overrides = {k: v for k, v in loaded.items() if not k.startswith("_")}

    summary = {"papers": 0, "junk_removed": 0, "overrides_applied": 0}
    affected_ids: list[str] = []
    for k in keepers:
        aid = k["arxiv_id"]
        meta_path = PAPERS_DIR / f"{aid}.meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        before_n = sum(len(meta["links"].get(c, [])) for c in ("code", "data", "project"))
        meta["links"] = clean_links(meta["links"])
        after_n = sum(len(meta["links"].get(c, [])) for c in ("code", "data", "project"))
        junk = before_n - after_n
        ov_changed = apply_overrides(meta, overrides)
        if junk or ov_changed:
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            affected_ids.append(aid)
            summary["junk_removed"] += junk
            if ov_changed:
                summary["overrides_applied"] += 1
        summary["papers"] += 1

    (CACHE / "_cleaned_ids.json").write_text(json.dumps(affected_ids, indent=2))
    print(f"DONE  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
