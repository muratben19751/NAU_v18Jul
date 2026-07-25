"""Wiki markdown helper for the FastAPI UI.

Reads pages from the bundled ``nautilus_wiki`` copy next to this module,
strips YAML frontmatter, and — for the Karpathy-style knowledge base —
rewrites Obsidian bare-name wikilinks (``[[slug]]``, ``[[slug|Label]]``)
into markdown links so they render live inside the served HTML.

Slug resolution:
    1. Any ``.md`` in ``nautilus_wiki/wiki/**``  (basename → path)
    2. Any ``.md`` in ``nautilus_wiki/sources/**`` as fallback

An unresolved slug renders as ``**[slug]**`` (bold, visible) rather than a
dead link, so lint issues are obvious in the UI.

Wiki References
---------------
_(app-specific — outside wiki scope)_

The component that serves the wiki itself; not about its content but about its format (see the `nautilus_wiki/CLAUDE.md` schema).
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

WIKI_ROOT = Path(__file__).resolve().parent / "nautilus_wiki"

# Bare wikilink: [[slug]] or [[slug|Label]]. Rejects ] or [ inside the target/label
# so nested/broken brackets don't consume unbounded text. Multi-line targets are rejected.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")

# Fenced/inline code regions — we don't want to rewrite [[...]] inside them
# (schema-doc and tutorial pages carry wikilink syntax as prose examples).
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


# ---------------------------------------------------------------------------
# Slug index — cheap and cached; invalidated by process restart (fine for us).
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _slug_index() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    if not WIKI_ROOT.exists():
        return idx
    for p in (WIKI_ROOT / "wiki").rglob("*.md"):
        idx[p.stem] = p
    for p in (WIKI_ROOT / "sources").rglob("*.md"):
        idx.setdefault(p.stem, p)
    return idx


def resolve_slug(slug: str) -> Path | None:
    return _slug_index().get(slug)


# ---------------------------------------------------------------------------
# Page read / rewrite
# ---------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


def _rewrite_wikilinks(md: str) -> str:
    """Convert ``[[slug]]`` / ``[[slug|Label]]`` into standard markdown links.

    Skips fenced code and inline code so wikilink *syntax* embedded in prose
    examples (e.g. CLAUDE.md, tutorials) is preserved verbatim. HTML-escapes
    the label because the surrounding template renders ``|safe`` and the label
    is user-authored content.
    """

    def _sub(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if "|" in inner:
            target, raw_label = inner.split("|", 1)
            target = target.strip()
            label = raw_label.strip()
        else:
            target = inner
            label = target.replace("_", " ")
        if target.endswith(".md"):
            target = target[:-3]
        target = target.rsplit("/", 1)[-1]
        safe_label = html.escape(label, quote=False)
        path = resolve_slug(target)
        if path is None:
            return f"**[{safe_label}]**"  # visible but not clickable — a lint tell
        rel = path.relative_to(WIKI_ROOT).as_posix()
        return f"[{safe_label}](/wiki/{rel})"

    # Compute code-region spans in the ORIGINAL markdown; skip any [[...]]
    # match whose start falls inside one of those spans.
    code_spans: list[tuple[int, int]] = []
    for pat in (_FENCED_CODE_RE, _INLINE_CODE_RE):
        for m in pat.finditer(md):
            code_spans.append((m.start(), m.end()))

    def _in_code(pos: int) -> bool:
        return any(a <= pos < b for a, b in code_spans)

    out_parts: list[str] = []
    last = 0
    for m in _WIKILINK_RE.finditer(md):
        if _in_code(m.start()):
            continue
        out_parts.append(md[last : m.start()])
        out_parts.append(_sub(m))
        last = m.end()
    out_parts.append(md[last:])
    return "".join(out_parts)


def read_wiki_page(rel_path: str) -> str:
    """Read a wiki page, strip frontmatter, rewrite wikilinks, return markdown.

    ``rel_path`` is relative to ``WIKI_ROOT`` (e.g.
    ``wiki/entities/strategy_and_actor.md``). Also accepts a bare slug
    (``strategy_and_actor``) for symmetry with the CLI tools.
    """
    path = _resolve_rel(rel_path)
    if path is None:
        return f"_(Wiki page not found: `{rel_path}`)_"
    text = path.read_text(encoding="utf-8")
    body = _strip_frontmatter(text)
    return _rewrite_wikilinks(body)


def _resolve_rel(rel_path: str) -> Path | None:
    """Accept either a WIKI_ROOT-relative path or a bare slug."""
    if rel_path.endswith(".md"):
        cand = WIKI_ROOT / rel_path
        if cand.exists():
            return cand
    slug = rel_path.rsplit("/", 1)[-1]
    if slug.endswith(".md"):
        slug = slug[:-3]
    return resolve_slug(slug)


def wiki_url(rel_path: str) -> str:
    """URL served by the FastAPI wiki route (``/wiki/<rel>``).

    Falls back to a ``file://`` URL if the file cannot be resolved.
    """
    path = _resolve_rel(rel_path)
    if path is None:
        return f"file://{WIKI_ROOT / rel_path}"
    return f"/wiki/{path.relative_to(WIKI_ROOT).as_posix()}"


def wiki_link_md(rel_path: str, label: str | None = None) -> str:
    label = (
        label or rel_path.split("/")[-1].replace(".md", "").replace("_", " ").title()
    )
    return f"[📖 {label}]({wiki_url(rel_path)})"
