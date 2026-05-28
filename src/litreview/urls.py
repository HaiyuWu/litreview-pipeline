"""Shared URL utilities: regex patterns, normalization, liveness, path resolution."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

# ===== Constants =====

HEAD_TIMEOUT = 6.0
HTTP_TIMEOUT = 15.0

# Common false positives that are almost never the paper's own code/data:
JUNK_CODE_REPOS = {
    "openai/CLIP", "openai/gpt-2", "openai/gpt-3",
    "facebookresearch/fairseq", "facebookresearch/dinov2", "facebookresearch/dino",
    "huggingface/transformers", "pytorch/pytorch", "google/jax",
    "google-research/google-research",
    "lukasschwab/arxiv.py", "danielnsilva/semanticscholar",
}

# ===== Patterns =====

# Order matters: try most-specific (data) before less-specific (code).
URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("data",    re.compile(r"https?://huggingface\.co/datasets/[\w\-./]+", re.I)),
    ("data",    re.compile(r"https?://zenodo\.org/record/\d+[\w\-./]*", re.I)),
    ("data",    re.compile(r"https?://(?:www\.)?kaggle\.com/(?:datasets|c)/[\w\-./]+", re.I)),
    ("data",    re.compile(r"https?://(?:www\.)?figshare\.com/[\w\-./]+", re.I)),
    ("code",    re.compile(r"https?://github\.com/[\w\-.]+/[\w\-.]+", re.I)),
    ("code",    re.compile(r"https?://gitlab\.com/[\w\-./]+/[\w\-.]+", re.I)),
    ("code",    re.compile(r"https?://bitbucket\.org/[\w\-.]+/[\w\-.]+", re.I)),
    ("code",    re.compile(r"https?://huggingface\.co/(?!datasets/)[\w\-.]+/[\w\-.]+", re.I)),
    ("project", re.compile(r"https?://[\w\-.]+\.github\.io[\w\-./]*", re.I)),
    ("project", re.compile(r"https?://sites\.google\.com/[\w\-./]+", re.I)),
]


# ===== Normalization =====

def normalize(url: str) -> str:
    """Strip trailing punctuation and reduce repo URLs to their canonical form."""
    u = url.strip().rstrip("/").rstrip(".,;:)]")
    m = re.match(r"(https?://github\.com/[\w\-.]+/[\w\-.]+)(?:/.*)?$", u, re.I)
    if m:
        return m.group(1)
    m = re.match(r"(https?://huggingface\.co/(?:datasets/)?[\w\-.]+/[\w\-.]+)(?:/.*)?$", u, re.I)
    if m:
        return m.group(1)
    return u


def categorize(url: str) -> str | None:
    """Return 'code' / 'data' / 'project' / None for a URL."""
    for cat, pat in URL_PATTERNS:
        if pat.match(url):
            return cat
    return None


# ===== Extraction =====

def find_urls_in_text(text: str, with_context: bool = False) -> list[tuple]:
    """Find all categorized URLs in `text`.

    Returns list of (category, url) tuples by default, or
    (category, url, context_snippet) if with_context=True. JUNK_CODE_REPOS
    are filtered out.
    """
    out: list[tuple] = []
    for cat, pat in URL_PATTERNS:
        for m in pat.finditer(text):
            url = normalize(m.group(0))
            if cat == "code":
                slug = re.match(r"https?://github\.com/([\w\-.]+/[\w\-.]+)", url, re.I)
                if slug and slug.group(1) in JUNK_CODE_REPOS:
                    continue
            if with_context:
                start = max(0, m.start() - 100)
                ctx = text[start : m.end() + 50].replace("\n", " ").strip()
                out.append((cat, url, ctx))
            else:
                out.append((cat, url))
    return out


# ===== Liveness =====

def check_alive(client: httpx.Client, url: str) -> bool | None:
    """HEAD-check URL liveness with fallback to GET on 405. Returns True/False/None."""
    try:
        r = client.head(url, timeout=HEAD_TIMEOUT, follow_redirects=True)
        if r.status_code == 405:
            r = client.get(url, timeout=HEAD_TIMEOUT, follow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception:
        return None


def liveness_pass(client: httpx.Client, links: dict[str, list[dict]], workers: int = 8) -> None:
    """Mutate links in-place: set 'alive' field on every entry across all categories."""
    tasks = [(cat, i) for cat, items in links.items() for i in range(len(items))]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(check_alive, client, links[cat][i]["url"]): (cat, i)
            for (cat, i) in tasks
        }
        for f in as_completed(futs):
            cat, i = futs[f]
            links[cat][i]["alive"] = f.result()


# ===== HTTP =====

def make_headers(mailto: str | None) -> dict[str, str]:
    """Construct polite User-Agent with a contact mailto."""
    m = mailto or "anonymous@example.com"
    return {"User-Agent": f"litreview-bot/0.1 (mailto:{m})"}


# ===== Path resolution =====

def resolve_path(raw: str, base: Path) -> Path:
    """Resolve a config-supplied path with sensible defaults.

    Expands `~` (user home) and `$VAR` (env vars) first. If the result is
    absolute, returns it as-is. If relative, anchors to `base` (typically
    the YAML config file's directory) and resolves.

    Examples (assume base = /home/me/projects/foo/configs):
      ../cache-foo                  → /home/me/projects/foo/cache-foo
      /var/litreview/foo            → /var/litreview/foo
      ~/litreview-cache/foo         → /home/me/litreview-cache/foo
      $LITREVIEW_CACHE_HOME/foo     → (expanded from env) / foo
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    p = Path(expanded)
    if p.is_absolute():
        return p.resolve()
    return (base / p).resolve()
