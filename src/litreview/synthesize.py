"""Stage E — assemble Overview + section bodies + optional index table + Open Questions."""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

import yaml

from litreview.urls import resolve_path


# ===== Module state =====

CACHE: Path = Path("cache")
OUT: Path = Path("./lit-review.md")
SECTIONS: list[dict] = []
TOPIC: str = ""
SHORT_NAME: str = ""
INCLUDE_INDEX_TABLE: bool = True
INCLUDE_TOC: bool = True

MONTH_SHORT = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}


# ===== Body assembly =====

def assemble_body() -> tuple[str, list[dict]]:
    """Group per-paper .md files by section (in config order), sort within section."""
    keepers = json.loads((CACHE / "keepers.json").read_text())
    papers_dir = CACHE / "papers"

    buckets: dict[str, list[dict]] = {s["id"]: [] for s in SECTIONS}
    for k in keepers:
        sec = k.get("section") if k.get("section") in buckets else SECTIONS[0]["id"]
        md_path = papers_dir / f"{k['arxiv_id']}.md"
        if not md_path.exists():
            print(f"  MISSING .md for {k['arxiv_id']}; skipping in body")
            continue
        buckets[sec].append({
            "arxiv_id": k["arxiv_id"],
            "title": k.get("title", ""),
            "year": k.get("year") or 0,
            "month": k.get("month") or 0,
            "cited": k.get("cited_by_count") or 0,
            "body": md_path.read_text().strip(),
        })

    parts: list[str] = []
    flat: list[dict] = []
    for s in SECTIONS:
        papers = sorted(buckets[s["id"]], key=lambda x: (-x["year"], -x["month"], -x["cited"]))
        parts.append(f"\n## {s['title']}\n")
        parts.append(s["blurb"].strip())
        parts.append("")
        for p in papers:
            parts.extend(["", "---", "", p["body"]])
            flat.append({**p, "section": s["id"]})
        parts.append("")
    return "\n".join(parts), flat


# ===== Index table =====

def _extract_link(body: str, label: str) -> str | None:
    """Find '**label:** url' in a per-paper .md body."""
    pat = re.compile(rf'\*\*{label}:\*\*\s*([^·\n]+?)\s*(?:·|\n)', re.I)
    m = pat.search(body)
    if not m:
        return None
    val = m.group(1).strip().rstrip(".,;:")
    return None if val in ("—", "-", "None", "") else val


def _short_label(url: str | None) -> str:
    """Make a compact markdown link for an index-table cell."""
    if not url:
        return "—"
    if (m := re.match(r"\[([^\]]+)\]\(([^)]+)\)", url)):
        url = m.group(2)
    if (m := re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)):
        return f"[{m.group(1)}/{m.group(2)}]({url})"
    if (m := re.match(r"https?://huggingface\.co/(?:datasets/)?([^/]+)/([^/?#]+)", url)):
        return f"[HF:{m.group(1)}/{m.group(2)}]({url})"
    if (m := re.match(r"https?://([^/]+\.github\.io)(/.*)?", url)):
        return f"[{m.group(1)}]({url})"
    if (m := re.match(r"https?://([^/]+)", url)):
        return f"[{m.group(1)}]({url})"
    return f"[{url[:40]}]({url})"


def build_index_table(flat: list[dict]) -> str:
    rows = [
        "| arxiv | date | section | title | code | data | project |",
        "|:------|:-----|:--------|:------|:-----|:-----|:--------|",
    ]
    for p in flat:
        aid = p["arxiv_id"]
        body = p["body"]
        date = f"{MONTH_SHORT.get(p['month'], '?')} {p['year']}" if p["year"] else "?"
        title_short = (p.get("title", "") or "")[:65].replace("|", "/")
        rows.append(
            f"| [{aid}](https://arxiv.org/abs/{aid}) | {date} | {p['section']} | "
            f"{title_short} | {_short_label(_extract_link(body, 'code'))} | "
            f"{_short_label(_extract_link(body, 'data'))} | "
            f"{_short_label(_extract_link(body, 'project'))} |"
        )
    return "\n".join(rows)


# ===== Table of contents =====

def _gh_anchor(header: str) -> str:
    """GitHub's heading-anchor slug: lowercase, drop punctuation, each space → one hyphen.

    Critically, multiple consecutive spaces become multiple consecutive hyphens
    (GitHub does NOT collapse them). So `## Foo / Bar` → `#foo--bar` (double).
    """
    a = header.lower()
    a = re.sub(r"[^\w\s-]", "", a)   # keep word chars / whitespace / hyphens
    a = re.sub(r"\s", "-", a)        # 1:1 space → hyphen (no collapsing)
    return a


def build_toc(*sources: str) -> str:
    """Collect all H2 headers across the provided markdown chunks, build linked TOC."""
    headers: list[str] = []
    seen: set[str] = set()
    for chunk in sources:
        for m in re.finditer(r"^##\s+(.+?)\s*$", chunk, re.M):
            h = m.group(1).strip()
            if h not in seen:
                seen.add(h)
                headers.append(h)
    if not headers:
        return ""
    lines = ["## Table of contents", ""]
    for h in headers:
        lines.append(f"- [{h}](#{_gh_anchor(h)})")
    return "\n".join(lines) + "\n"


# ===== Optional overview + open-questions blocks =====
# When cache/overview.md or cache/open_questions.md are missing, the
# placeholders below match the canonical document shape used by every
# litreview run — see agents/litreview-builder.md (Step 7) for the spec.

def _count_keepers() -> int:
    p = CACHE / "keepers.json"
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()))
    except Exception:
        return 0


def load_overview() -> str:
    """Read cache/overview.md, or return the canonical 3-line callout placeholder."""
    p = CACHE / "overview.md"
    if p.exists():
        return p.read_text()
    today = datetime.date.today().isoformat()
    return (
        f"# {SHORT_NAME} — Literature Review\n\n"
        f"> A working lit review for the **{SHORT_NAME}** project.\n"
        f"> **{_count_keepers()} papers**, {len(SECTIONS)} sections, "
        f"code+data links extracted where available.\n"
        f"> Last regenerated: {today}.\n\n"
        f"---\n"
    )


def load_open_questions() -> str:
    """Read cache/open_questions.md, or return a canonical-shape placeholder."""
    p = CACHE / "open_questions.md"
    if p.exists():
        return p.read_text()
    return (
        f"\n\n---\n\n## Open questions for {SHORT_NAME}\n\n"
        f"_(No `cache/open_questions.md` was provided — placeholder. The "
        f"litreview-builder agent or the curator should synthesize 5–8 concrete "
        f"questions across the kept papers, each tied to specific arxiv IDs "
        f"and a next step.)_\n"
    )


# ===== Config + main =====

def load_config(path: str) -> dict:
    cfg_path = Path(path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    global CACHE, OUT, SECTIONS, TOPIC, SHORT_NAME, INCLUDE_INDEX_TABLE, INCLUDE_TOC
    CACHE = resolve_path(cfg["cache_dir"], cfg_path.parent)
    OUT = resolve_path(cfg["output_file"], cfg_path.parent)
    SECTIONS = cfg["sections"]
    TOPIC = cfg.get("topic", "(topic unspecified)")
    SHORT_NAME = cfg.get("short_name", "lit-review")
    INCLUDE_INDEX_TABLE = bool(cfg.get("include_index_table", True))
    INCLUDE_TOC = bool(cfg.get("include_toc", True))
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/www-jepa.yaml")
    args = ap.parse_args()
    load_config(args.config)

    body, flat = assemble_body()
    overview = load_overview()
    open_q = load_open_questions()
    index_block = ""
    if INCLUDE_INDEX_TABLE:
        index_block = "\n\n---\n\n## Index table\n\n" + build_index_table(flat)

    parts: list[str] = [overview]
    if INCLUDE_TOC:
        # Build TOC from H2 headers in body + index + open_q (after overview = the doc nav).
        toc = build_toc(body, index_block, open_q)
        if toc:
            parts.append("\n" + toc)
    parts.append(body.strip())
    if index_block:
        parts.append(index_block)
    parts.append(open_q)

    final = "\n".join(parts)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(final)
    print(f"wrote {OUT}  ({len(final):,} chars / {len(flat)} papers / {len(SECTIONS)} sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
