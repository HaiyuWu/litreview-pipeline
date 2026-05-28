# litreview-pipeline

A topic-agnostic literature-review pipeline. Given a research topic, it produces a structured Markdown survey of relevant papers — complete with per-paper summaries, code / data / project links, and synthesized open questions — by orchestrating OpenAlex search, PDF extraction, and Claude-driven judgment steps.

The pipeline ships with a Claude Code subagent (`litreview-builder`) that drives all eight stages from a single natural-language prompt. Every deterministic stage is also exposed as a standalone CLI command for scripting and CI.

## Overview

```
                  ┌──── deterministic stages ────┐
arxiv + OpenAlex →│ search → must-include →      │
                  │ extract → link-enrich →      │
                  │ link-clean                   │
                  └──────────────────────────────┘
                              │
                              ▼
                  ┌──── LLM judgment ────┐
                  │ B: filter (KEEP/DROP) │
                  │ D: per-paper summary  │
                  └───────────────────────┘
                              │
                              ▼
                  ┌─────────────────────┐
                  │ synthesize → <topic>-lit-review.md
                  └─────────────────────┘
```

Six deterministic stages handle search, download, URL extraction, and assembly. Two LLM-judgment stages (B: relevance filter, D: per-paper summarization) are delegated to a bundled Claude Code subagent. Users without Claude Code can run the deterministic stages manually and supply their own judgment.

## Installation

Requires `uv` ≥ 0.4 and Python ≥ 3.11.

```bash
uv tool install git+https://github.com/HaiyuWu/litreview-pipeline.git
```

The first invocation of `litreview` auto-installs the Claude Code subagent to `~/.claude/agents/litreview-builder.md`. Restart your Claude Code session once for it to be discovered.

- Opt out: `export LITREVIEW_NO_AUTO_AGENT=1`
- Force a refresh of the agent file: `litreview install-agent`

## Quick start

From within Claude Code, prompt the main agent:

```
Do a literature review on diffusion-based world models.
```

The `litreview-builder` subagent will generate a topic-specific config, drive all eight stages end-to-end, and produce `<short-name>-lit-review.md` at the working directory.

**Wall time**: ~25–45 minutes. **Token cost**: ~500K–700K (Claude calls dominate).

## Standalone CLI usage

To drive the pipeline manually, scaffold a config from the bundled example:

```bash
litreview show-example-config > configs/my-topic.yaml
# Edit queries, seeds, sections, must_include to fit your topic.
```

Then run the deterministic stages, interleaving Stage B and Stage D Claude subagent calls:

```bash
litreview search       --config configs/my-topic.yaml
# Stage B (filter): spawn a Claude subagent that reads cache/candidates.json,
# applies a KEEP/DROP rubric, and writes cache/_filter_decisions.json.
# Prompt template: src/litreview/agents/litreview-builder.md.

litreview must-include --config configs/my-topic.yaml
litreview extract      --config configs/my-topic.yaml
litreview link-enrich  --config configs/my-topic.yaml
litreview link-clean   --config configs/my-topic.yaml

# Stage D (summarize): spawn N parallel Claude subagents (5 papers each).
# Each reads cache/papers/<id>.meta.json and writes cache/papers/<id>.md.

# Hand-write cache/overview.md and cache/open_questions.md.

litreview synthesize   --config configs/my-topic.yaml
```

## Architecture

| Stage | Implementation | Output |
|---|---|---|
| A. Search        | `litreview search` (OpenAlex REST) | `cache/candidates.json` |
| A2. Must-include | `litreview must-include` (arxiv abs scrape) | mutates `cache/keepers.json` |
| B. Filter        | Claude subagent (judgment) | `cache/_filter_decisions.json` |
| C. Extract       | `litreview extract` (PyMuPDF + regex + HEAD-check) | `cache/papers/*.meta.json` |
| C2. Link enrich  | `litreview link-enrich` (arxiv abs + project page + GitHub search) | extends meta.json |
| C3. Link clean   | `litreview link-clean` (junk filter + manual overrides) | tightens meta.json |
| D. Summarize     | N parallel Claude subagents | `cache/papers/*.md` |
| E. Synthesize    | `litreview synthesize` | `<output_file>.md` |

### Design notes

**Two-tier division of labor.** Deterministic work — arxiv search, PDF download, URL extraction, HEAD-check, file assembly — runs in Python and costs no LLM tokens. LLM judgment is reserved for three irreducible tasks: relevance filtering, per-paper summarization, and link attribution (distinguishing official repos from community replicas).

**Three link-discovery routes.** PDF regex catches links present in the preprint. Arxiv abstract-page scraping recovers links added after publication. GitHub `search/repositories` finds repos whose README mentions the arxiv ID. A `cache/manual_overrides.json` file provides an escape hatch when ranking picks the wrong repo.

**Idempotent caching.** Every stage checkpoints to disk. Mid-run failures resume from where they stopped. Re-running a finished pipeline completes in seconds.

**OpenAlex bypass for recent papers.** OpenAlex occasionally has data-quality issues for very recent papers (wrong title under a DOI, missing abstract). The `must-include` stage scrapes `arxiv.org/abs` directly to bypass this.

## Per-paper entry format

```markdown
### <Paper Title>
**arxiv:** [<id>](https://arxiv.org/abs/<id>) | <Month Year> | **<Section>** | citations: <N>

**code:** <github URL or —> · **data:** <HF dataset URL or —> · **project:** <project page or —>

<2–4 paragraph summary with concrete numbers and method names>

**Limitation:** <what blocks this paper's use for the topic>
**Key idea for <topic>:** <what to borrow>
```

## Configuration

Run `litreview show-example-config` for an annotated template. Required fields:

| Field | Purpose |
|---|---|
| `topic`, `short_name`, `mailto` | Identity; `mailto` is added to OpenAlex User-Agent (polite pool) |
| `cache_dir`, `output_file` | Paths, resolved relative to the YAML file's directory |
| `queries` | 4–5 search axes, each a list of arxiv-style query strings |
| `seeds` | 4–6 known relevant arxiv IDs (citation-graph anchors) |
| `must_include` | Pinned arxiv IDs that bypass the filter |
| `sections` | 4–6 sections with `id`, `title`, `blurb` |
| `keepers_target_range` | Stage B target keeper count, e.g. `[25, 45]` |
| `include_index_table` | Optional; append a sortable index table to the output (default `true`) |

## Resource expectations

| Resource | Estimate for a typical 40–50 paper review |
|---|---|
| Wall time | 25–45 minutes |
| Anthropic tokens | 500K–700K |
| OpenAlex requests | A few hundred (polite pool, no rate-limit issues) |
| arxiv PDF downloads | One per keeper, ≥ 3 s between requests |
| GitHub API requests | Optional; rate-limited to ~10/min unauthenticated |

## Project layout

```
litreview-pipeline/
├── pyproject.toml
├── LICENSE
├── README.md
└── src/litreview/
    ├── __init__.py
    ├── cli.py                        Single CLI entry point
    ├── urls.py                       Shared URL utilities
    ├── search.py                     Stage A
    ├── must_include.py               Stage A2
    ├── extract.py                    Stage C
    ├── link_enrich.py                Stage C2
    ├── link_clean.py                 Stage C3
    ├── synthesize.py                 Stage E
    ├── agents/
    │   └── litreview-builder.md      Claude Code subagent definition
    └── examples/
        └── example-config.yaml       Annotated config template
```

## License

MIT. See [LICENSE](LICENSE).
