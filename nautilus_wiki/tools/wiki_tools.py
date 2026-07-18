"""wiki_tools — Karpathy-style operations on the Nautilus wiki.

Usage (from any dir):
    python nautilus_wiki/tools/wiki_tools.py <cmd> [args]

Commands
--------
    index               Rebuild index.md from frontmatter (summary/type).
    backlinks           Refresh "## Referenced by" sections on every wiki/ page.
    lint                Print a health report (orphans / broken links / stale / missing summary / stub gaps).
    lint --write        Same, but also emit lint/YYYY-MM-DD_health.md (--date overrides; default today).
    search "query"      Naive full-text search across wiki/**/*.md and sources/**.md, ranked by term frequency.
    resolve <slug>      Print the absolute path a bare [[slug]] resolves to.
    stub  <slug>  <kind> "Title"   Create a stub page. kind ∈ {entity, concept, synthesis, tutorial}.
                        For kind=tutorial, `tutorial_` is auto-prefixed to slug if missing.
    show  <slug>        Print a wiki page body (frontmatter stripped).

Design notes
------------
- No third-party deps. Parsing is deliberately dumb; the schema is the contract.
- Frontmatter is YAML-lite: key: value or key: [a, b, c]. Lists in block form OK.
- Bare-name wikilinks: `[[slug]]` → wiki/**/(slug).md unique match (falls back to sources/(slug).md).
- Never edits sources/. Never edits pages whose `status: frozen` frontmatter is set.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

WIKI_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = WIKI_ROOT / "wiki"
SOURCES_DIR = WIKI_ROOT / "sources"
LINT_DIR = WIKI_ROOT / "lint"
INDEX_FILE = WIKI_ROOT / "index.md"

LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_SEP = "---"


# ---------------------------------------------------------------------------
# Frontmatter / wiki-page primitives
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith(FRONTMATTER_SEP):
        return {}, text
    end = text.find("\n" + FRONTMATTER_SEP, len(FRONTMATTER_SEP))
    if end == -1:
        return {}, text
    fm_raw = text[len(FRONTMATTER_SEP) : end].strip("\n")
    body = text[end + len("\n" + FRONTMATTER_SEP) :].lstrip("\n")
    return _parse_yaml_lite(fm_raw), body


def _parse_yaml_lite(txt: str) -> dict:
    """Extremely small YAML subset: scalars + inline/block lists + block scalars.

    Enough for the fields we use: title/type/status/last_updated/sources/summary.
    Block scalars (``summary: >-`` + girintili devam satırları) tek satıra
    katlanır — CLAUDE.md şablonu bu formu kullanır; desteklenmezse ``backlinks``
    yeniden yazarken özet içeriği sessizce kaybolur.
    """
    out: dict = {}
    current_key: str | None = None
    block_key: str | None = None  # ">-" / "|" devam satırlarını toplayan anahtar
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key is not None:
            block_key = None
            out.setdefault(current_key, []).append(
                line[4:].strip().strip('"').strip("'")
            )
            continue
        if block_key is not None and line.startswith("  "):
            prev = out.get(block_key, "")
            out[block_key] = (prev + " " + line.strip()).strip()
            continue
        if ":" in line and not line.startswith(" "):
            block_key = None
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if v == "":
                current_key = k
                out[k] = []
            elif v in (">", ">-", "|", "|-"):
                out[k] = ""
                block_key = k
                current_key = None
            elif v.startswith("[") and v.endswith("]"):
                items = [
                    x.strip().strip('"').strip("'")
                    for x in v[1:-1].split(",")
                    if x.strip()
                ]
                out[k] = items
                current_key = None
            else:
                out[k] = v.strip('"').strip("'")
                current_key = None
    return out


def _dump_yaml_lite(fm: dict) -> str:
    lines: list[str] = []
    for k, v in fm.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _write_page(path: Path, frontmatter: dict, body: str) -> None:
    fm_txt = _dump_yaml_lite(frontmatter)
    path.write_text(f"---\n{fm_txt}\n---\n\n{body.lstrip()}", encoding="utf-8")


def _all_wiki_pages() -> list[Path]:
    return sorted(WIKI_DIR.rglob("*.md"))


def _all_source_pages() -> list[Path]:
    return sorted(SOURCES_DIR.rglob("*.md"))


def _stem_index() -> dict[str, Path]:
    """Map bare slug → absolute path across wiki/ + sources/."""
    idx: dict[str, Path] = {}
    for p in _all_wiki_pages():
        idx[p.stem] = p
    for p in _all_source_pages():
        idx.setdefault(p.stem, p)
    return idx


def _bare_targets(text: str) -> list[str]:
    """Extract bare wikilink targets from body text.

    Skips fenced code blocks and inline code to avoid rewriting sample syntax
    that appears in schema-doc or tutorial pages.
    """
    out = []
    stripped = _strip_code_regions(text)
    for m in LINK_RE.finditer(stripped):
        inner = m.group(1)
        target = inner.split("|", 1)[0].strip()
        if target.endswith(".md"):
            target = target[:-3]
        out.append(target.rsplit("/", 1)[-1])
    return out


_BACKLINK_BLOCK_RE = re.compile(
    r"\n?<!-- BACKLINKS:BEGIN -->[\s\S]*?<!-- BACKLINKS:END -->\n?",
)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _strip_backlinks(body: str) -> str:
    """Return body with the (single) auto-generated backlinks block removed."""
    return _BACKLINK_BLOCK_RE.sub("", body, count=1)


def _strip_code_regions(text: str) -> str:
    """Return text with fenced ``` blocks and inline `code` replaced by whitespace, so
    wikilink scanning ignores syntax examples embedded in code."""
    text = _FENCED_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return text


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_resolve(slug: str) -> int:
    idx = _stem_index()
    if slug in idx:
        print(idx[slug])
        return 0
    print(f"(unresolved) {slug}", file=sys.stderr)
    return 1


def cmd_show(slug: str) -> int:
    idx = _stem_index()
    p = idx.get(slug)
    if p is None:
        print(f"(not found) {slug}", file=sys.stderr)
        return 1
    _, body = _split_frontmatter(p.read_text(encoding="utf-8"))
    print(body)
    return 0


def cmd_search(query: str, limit: int = 15) -> int:
    """Naive TF ranking. Case-insensitive, whole-word bonus."""
    terms = [t.lower() for t in re.findall(r"\w+", query) if t.strip()]
    if not terms:
        print("(empty query)", file=sys.stderr)
        return 1
    scored: list[tuple[float, Path, str]] = []
    for p in _all_wiki_pages() + _all_source_pages():
        txt = p.read_text(encoding="utf-8").lower()
        score = 0.0
        for t in terms:
            score += txt.count(t)
            score += 3 * len(re.findall(rf"\b{re.escape(t)}\b", txt))
        if score:
            _, body = _split_frontmatter(p.read_text(encoding="utf-8"))
            snippet = body.strip().splitlines()[0] if body.strip() else ""
            scored.append((score, p, snippet[:120]))
    scored.sort(reverse=True)
    for score, p, snippet in scored[:limit]:
        rel = p.relative_to(WIKI_ROOT)
        print(f"{score:6.1f}  {rel}")
        if snippet:
            print(f"        {snippet}")
    if not scored:
        print("(no matches)")
    return 0


def cmd_index() -> int:
    """Rebuild index.md from every page's frontmatter."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in _all_wiki_pages():
        section = p.parent.name  # entities | concepts | synthesis | tutorials
        groups[section].append(p)
    for p in _all_source_pages():
        groups["sources"].append(p)

    section_order = ["sources", "entities", "concepts", "synthesis", "tutorials"]
    section_titles = {
        "sources": "Kaynaklar (immutable)",
        "entities": "Entities (somut bileşenler)",
        "concepts": "Concepts (soyut fikirler)",
        "synthesis": "Synthesis (karşılaştırmalar & rehberler)",
        "tutorials": "Tutorials (resmi öğreticiler)",
    }

    lines: list[str] = ["# NautilusTrader Wiki — İçerik Kataloğu", ""]
    lines.append(
        "Bu sayfa `tools/wiki_tools.py index` tarafından her sayfanın frontmatter'ından yeniden üretilir. Elle düzenlemeyin.\n"
    )

    for key in section_order:
        pages = sorted(groups.get(key, []), key=lambda p: p.stem)
        if not pages:
            continue
        lines.append(f"## {section_titles[key]}")
        for p in pages:
            fm, _ = _split_frontmatter(p.read_text(encoding="utf-8"))
            title = fm.get("title") or p.stem.replace("_", " ").title()
            summary = fm.get("summary") or ""
            status = fm.get("status")
            rel = p.relative_to(WIKI_ROOT)
            slug = p.stem
            badge = " *(stub)*" if status == "stub" else ""
            if summary:
                lines.append(f"- [[{slug}|{title}]]{badge} — {summary}  (`{rel}`)")
            else:
                lines.append(f"- [[{slug}|{title}]]{badge}  (`{rel}`)")
        lines.append("")

    INDEX_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {INDEX_FILE}")
    return 0


def cmd_backlinks() -> int:
    """Recompute and inject a '## Referenced by' section into every wiki page.

    Immutable inputs: sources/. Backlink section is idempotent: bounded by BEGIN/END markers.
    IMPORTANT: When computing incoming edges, we strip the existing BACKLINKS block
    from each page's body first — otherwise every listed backlink becomes self-reinforcing
    (page X lists page Y as a backer forever, even after Y removes its outgoing [[X]]).
    """
    incoming: dict[str, set[str]] = defaultdict(set)
    for p in _all_wiki_pages():
        text = p.read_text(encoding="utf-8")
        _, body = _split_frontmatter(text)
        body = _strip_backlinks(body)
        for tgt in _bare_targets(body):
            incoming[tgt].add(p.stem)

    updated = 0
    idx = _stem_index()
    for slug, refs in incoming.items():
        page = idx.get(slug)
        if not page or page.parent.name not in {
            "entities",
            "concepts",
            "synthesis",
            "tutorials",
        }:
            continue
        text = page.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        # strip any prior backlinks section
        body = _strip_backlinks(body)
        ordered = sorted(refs - {slug})
        if not ordered:
            new_body = body.rstrip() + "\n"
        else:
            lines = ["", "<!-- BACKLINKS:BEGIN -->", "## Referenced by", ""]
            for r in ordered:
                lines.append(f"- [[{r}]]")
            lines.append("<!-- BACKLINKS:END -->")
            new_body = body.rstrip() + "\n" + "\n".join(lines) + "\n"
        if new_body != body:
            _write_page(page, fm, new_body)
            updated += 1

    # Also handle pages with ZERO incoming — strip any stale backlink section they might carry.
    linked = set(incoming.keys())
    for p in _all_wiki_pages():
        if p.stem in linked:
            continue
        text = p.read_text(encoding="utf-8")
        if "BACKLINKS:BEGIN" not in text:
            continue
        fm, body = _split_frontmatter(text)
        body = _strip_backlinks(body)
        _write_page(p, fm, body)
        updated += 1

    print(f"Backlinks refreshed on {updated} page(s).")
    return 0


def cmd_lint(write: bool = False, date_iso: str | None = None) -> int:
    """Return a health report."""
    report: dict[str, list] = {
        "broken_links": [],
        "orphans": [],
        "missing_summary": [],
        "missing_frontmatter": [],
        "stale": [],  # last_updated > 180 days is impossible to compute deterministically w/o Date; leave empty
        "stubs": [],
    }
    idx = _stem_index()
    stems = set(idx.keys())

    incoming: dict[str, set[str]] = defaultdict(set)
    for p in _all_wiki_pages():
        text = p.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        # Do NOT count the auto-generated backlink block as real incoming edges.
        body_no_backlinks = _strip_backlinks(body)
        rel = str(p.relative_to(WIKI_ROOT))
        if not fm:
            report["missing_frontmatter"].append(rel)
        else:
            if not fm.get("summary"):
                report["missing_summary"].append(rel)
            if fm.get("status") == "stub":
                report["stubs"].append(rel)
        for tgt in _bare_targets(body_no_backlinks):
            if tgt not in stems:
                report["broken_links"].append(f"{rel} → [[{tgt}]]")
            incoming[tgt].add(p.stem)

    # orphans: wiki/ pages nothing links to (except tutorials root & synthesis root)
    for p in _all_wiki_pages():
        if p.stem not in incoming:
            report["orphans"].append(str(p.relative_to(WIKI_ROOT)))

    # print
    for key, items in report.items():
        print(f"# {key} ({len(items)})")
        for i in items:
            print(f"  - {i}")
        print()

    if write:
        LINT_DIR.mkdir(parents=True, exist_ok=True)
        if date_iso:
            stamp = date_iso
        else:
            import datetime as _dt

            stamp = _dt.date.today().isoformat()
        out = LINT_DIR / f"{stamp}_health.md"
        lines = [f"# Wiki Health Report — {stamp}", ""]
        for key, items in report.items():
            lines.append(f"## {key} ({len(items)})")
            for i in items:
                lines.append(f"- {i}")
            lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"Wrote {out}")
    return 0 if not report["broken_links"] else 2


def cmd_stub(slug: str, kind: str, title: str) -> int:
    """Create a minimal stub page. Refuses to overwrite.

    For ``kind == 'tutorial'``, the ``tutorial_`` prefix is auto-applied to the
    slug if not already present, so it matches the naming convention documented
    in CLAUDE.md.
    """
    if kind not in {"entity", "concept", "synthesis", "tutorial"}:
        print(f"invalid kind {kind}", file=sys.stderr)
        return 1
    if kind == "tutorial" and not slug.startswith("tutorial_"):
        slug = f"tutorial_{slug}"
    subdir = {
        "entity": "entities",
        "concept": "concepts",
        "synthesis": "synthesis",
        "tutorial": "tutorials",
    }[kind]
    target = WIKI_DIR / subdir / f"{slug}.md"
    if target.exists():
        print(f"exists: {target}", file=sys.stderr)
        return 1
    body = (
        "\nDoldurulacak. Bilinen kaynaklar için `sources/` ve ilgili wiki sayfalarına bakın.\n\n"
        "## Bilinen boşluklar\n\n"
        "- (Bu bölüm, sayfa doldurulunca güncellenmeli.)\n"
    )
    _write_page(
        target,
        {
            "title": title,
            "type": kind,
            "status": "stub",
            "sources": [],
            "last_updated": "TODO",
        },
        body,
    )
    print(f"Created {target}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="wiki_tools")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("index")
    sub.add_parser("backlinks")
    lp = sub.add_parser("lint")
    lp.add_argument("--write", action="store_true")
    lp.add_argument("--date", default=None)
    sp = sub.add_parser("search")
    sp.add_argument("query", nargs="+")
    rp = sub.add_parser("resolve")
    rp.add_argument("slug")
    xp = sub.add_parser("show")
    xp.add_argument("slug")
    stp = sub.add_parser("stub")
    stp.add_argument("slug")
    stp.add_argument("kind")
    stp.add_argument("title")

    args = ap.parse_args(argv)
    if args.cmd == "index":
        return cmd_index()
    if args.cmd == "backlinks":
        return cmd_backlinks()
    if args.cmd == "lint":
        return cmd_lint(write=args.write, date_iso=args.date)
    if args.cmd == "search":
        return cmd_search(" ".join(args.query))
    if args.cmd == "resolve":
        return cmd_resolve(args.slug)
    if args.cmd == "show":
        return cmd_show(args.slug)
    if args.cmd == "stub":
        return cmd_stub(args.slug, args.kind, args.title)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
