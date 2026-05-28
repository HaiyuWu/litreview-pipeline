"""Single CLI entry point: `litreview <subcommand>`."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

from litreview import citations, extract, filter as filter_mod, link_clean, link_enrich, must_include, search, synthesize
from litreview.urls import resolve_path


# ===== Stage subcommands =====

STAGE_MODULES = {
    "search":            search,
    "must-include":      must_include,
    "enrich-citations":  citations,
    "extract":           extract,
    "link-enrich":       link_enrich,
    "link-clean":        link_clean,
    "synthesize":        synthesize,
}


def _run_stage(name: str, config_path: str) -> int:
    """Delegate to a stage module's main() by faking sys.argv."""
    original_argv = sys.argv
    try:
        sys.argv = [f"litreview-{name}", "--config", config_path]
        return STAGE_MODULES[name].main()
    finally:
        sys.argv = original_argv


# ===== Agent install =====

def _bundled_agent_path() -> Path:
    return Path(__file__).resolve().parent / "agents" / "litreview-builder.md"


def _agent_marker_for(target: Path) -> Path:
    """Hidden sidecar file that records the hash of what we last wrote.

    Used to distinguish 'file is from a previous version of us' (safe to
    overwrite on update) vs 'file has been locally modified' (don't touch).
    """
    return target.parent / f".{target.name}.installed_hash"


def _write_agent(src: Path, target: Path) -> None:
    """Write bundled agent + sidecar hash marker atomically-ish."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src_bytes = src.read_bytes()
    target.write_bytes(src_bytes)
    _agent_marker_for(target).write_text(hashlib.sha256(src_bytes).hexdigest())


def install_agent(target: str) -> int:
    """Explicit install: copy the bundled Claude Code subagent (always overwrites)."""
    src = _bundled_agent_path()
    if not src.exists():
        print(f"ERROR: bundled agent not found at {src}", file=sys.stderr)
        return 1
    dst = Path(target).expanduser().resolve()
    _write_agent(src, dst)
    print(f"Installed agent definition:")
    print(f"  source: {src}")
    print(f"  target: {dst}\n")
    print("Next: restart your Claude Code session so the new subagent is picked up.")
    print("Then ask the main agent for a lit review on any topic, e.g.:")
    print('  "do a lit review on diffusion world models"')
    return 0


def _maybe_auto_install_agent() -> None:
    """First-run convenience + auto-update agent file when the package updates.

    Three branches:
      1. Target missing             → auto-install + record hash marker.
      2. Target == bundled          → silent no-op (already current).
      3. Target != bundled          → check sidecar marker:
         3a. Marker matches target   → safe to overwrite (no user edits since
                                       our last write). Update + refresh marker.
         3b. Marker differs / missing→ user may have customized → DO NOT
                                       overwrite; print one-line stderr hint.

    Disable entirely with `LITREVIEW_NO_AUTO_AGENT=1`.
    """
    if os.environ.get("LITREVIEW_NO_AUTO_AGENT"):
        return
    if not (Path.home() / ".claude").exists():
        return
    src = _bundled_agent_path()
    if not src.exists():
        return

    target = Path.home() / ".claude" / "agents" / "litreview-builder.md"
    src_bytes = src.read_bytes()
    src_hash = hashlib.sha256(src_bytes).hexdigest()

    if not target.exists():
        action = "auto-installed"
    else:
        target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if target_hash == src_hash:
            return  # already current — silent
        marker = _agent_marker_for(target)
        marker_hash = marker.read_text().strip() if marker.exists() else None
        if marker_hash and marker_hash != target_hash:
            # User edited the file since our last install — respect their changes.
            print(
                f"[litreview] new agent version available, but {target} has been "
                f"locally modified. Run `litreview install-agent` to overwrite.",
                file=sys.stderr,
            )
            return
        action = "updated"

    try:
        _write_agent(src, target)
    except Exception as e:
        print(f"[litreview] could not {action} Claude Code subagent: {e}", file=sys.stderr)
        return

    msg_lines = [f"[litreview] {action} Claude Code subagent → {target}"]
    if action == "auto-installed":
        msg_lines.append("[litreview] restart your Claude Code session to discover it.")
    else:
        msg_lines.append("[litreview] restart your Claude Code session to pick up the new version.")
    msg_lines.append("[litreview] (set LITREVIEW_NO_AUTO_AGENT=1 to disable)")
    print("\n".join(msg_lines), file=sys.stderr)


# ===== Example config =====

def show_example_config() -> int:
    src = Path(__file__).resolve().parent / "examples" / "example-config.yaml"
    if not src.exists():
        print(f"ERROR: bundled example not found at {src}", file=sys.stderr)
        return 1
    print(src.read_text())
    return 0


# ===== PDF rename migration =====

def rename_pdfs(config_path: str) -> int:
    """Migrate legacy '<arxiv_id>.pdf' cache files to '<slug>--<arxiv_id>.pdf'.

    Idempotent: skips files already in the new format or with target existing.
    """
    import json
    import yaml
    from litreview.extract import slugify

    cfg_path = Path(config_path).resolve()
    cfg = yaml.safe_load(cfg_path.read_text())
    cache = resolve_path(cfg["cache_dir"], cfg_path.parent)
    pdfs_dir = cache / "pdfs"
    keepers_path = cache / "keepers.json"

    if not keepers_path.exists():
        print(f"keepers.json not found at {keepers_path}", file=sys.stderr)
        return 1
    if not pdfs_dir.exists():
        print(f"no pdfs/ at {pdfs_dir}; nothing to rename")
        return 0

    keepers = json.loads(keepers_path.read_text())
    summary = {"renamed": 0, "already_new": 0, "no_legacy_file": 0}
    for k in keepers:
        aid = k["arxiv_id"]
        title = k.get("title") or ""
        legacy = pdfs_dir / f"{aid}.pdf"
        new = pdfs_dir / f"{slugify(title)}--{aid}.pdf"
        if new.exists():
            summary["already_new"] += 1
            continue
        if not legacy.exists():
            summary["no_legacy_file"] += 1
            continue
        legacy.rename(new)
        summary["renamed"] += 1
        print(f"  {aid}.pdf  →  {new.name}")
    print(f"\n{summary}")
    return 0


# ===== Main =====

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="litreview",
        description=(
            "Topic-agnostic literature-review pipeline. Stages A/A2/C/C2/C3/E "
            "are deterministic and exposed as subcommands here. Stages B (filter) "
            "and D (summarize) are LLM-judgment steps, driven by a Claude Code "
            "subagent (run `litreview install-agent` to install it)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    for name in STAGE_MODULES:
        sp = sub.add_parser(name, help=f"Stage: {name.replace('-', ' ')}")
        sp.add_argument("--config", required=True,
                        help="Path to topic config YAML (see `litreview show-example-config`).")

    sp_ia = sub.add_parser("install-agent",
                           help="Copy the bundled Claude Code subagent file to ~/.claude/agents/")
    sp_ia.add_argument("--target", default="~/.claude/agents/litreview-builder.md",
                       help="Destination path for the agent file.")

    sub.add_parser("show-example-config",
                   help="Print the bundled example config YAML to stdout.")

    sp_rn = sub.add_parser("rename-pdfs",
                           help="Migrate legacy '<arxiv_id>.pdf' cache files to readable '<slug>--<arxiv_id>.pdf' naming.")
    sp_rn.add_argument("--config", required=True, help="Path to topic config YAML.")

    sp_sl = sub.add_parser("slim-candidates",
                           help="Build cache/candidates_slim.json (trimmed view fed to Stage B filter agent).")
    sp_sl.add_argument("--config", required=True)
    sp_sl.add_argument("--max-abstract-chars", type=int, default=700)

    sp_ls = sub.add_parser("list-candidates",
                           help="Print candidates to stdout in compact/slim/json format, sorted by year+citations.")
    sp_ls.add_argument("--config", required=True)
    sp_ls.add_argument("--format", choices=["compact", "slim", "json"], default="compact")
    sp_ls.add_argument("--limit", type=int, default=None, help="Show only the top-N entries.")

    sp_af = sub.add_parser("apply-filter",
                           help="Join Stage B decisions with candidate metadata → keepers.json + dropped.json.")
    sp_af.add_argument("--config", required=True)
    sp_af.add_argument("--decisions", default=None,
                       help="Path to _filter_decisions.json (default: <cache>/_filter_decisions.json).")

    # First-run convenience: idempotent agent install BEFORE argparse so
    # `litreview --help` also triggers it (argparse sys.exit on --help).
    # Skip when user is explicitly invoking install-agent — they'll do the
    # install themselves, no need for the auto-install path to chime in.
    if "install-agent" not in sys.argv[1:]:
        _maybe_auto_install_agent()

    args = parser.parse_args()

    if args.cmd == "install-agent":
        return install_agent(args.target)
    if args.cmd == "show-example-config":
        return show_example_config()
    if args.cmd == "rename-pdfs":
        return rename_pdfs(args.config)
    if args.cmd == "slim-candidates":
        filter_mod.load_config(args.config)
        return filter_mod.slim_candidates(max_abstract_chars=args.max_abstract_chars)
    if args.cmd == "list-candidates":
        filter_mod.load_config(args.config)
        return filter_mod.list_candidates(fmt=args.format, limit=args.limit)
    if args.cmd == "apply-filter":
        filter_mod.load_config(args.config)
        return filter_mod.apply_filter(decisions_path=Path(args.decisions) if args.decisions else None)
    if args.cmd in STAGE_MODULES:
        return _run_stage(args.cmd, args.config)

    parser.error(f"Unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
