"""``/wiki/*`` — serve the Karpathy-style knowledge base as browsable pages.

- ``/wiki``               → renders ``index.md`` (the catalog)
- ``/wiki/<rel_path>``    → renders any ``.md`` under ``nautilus_wiki/``
- ``/wiki/slug/<slug>``   → renders by bare Obsidian slug (same as ``[[slug]]``)

Everything goes through :func:`wiki_helper.read_wiki_page` so ``[[wikilinks]]``
are rewritten to live ``/wiki/*`` URLs and frontmatter is stripped. The
template is deliberately minimal — Obsidian remains the primary reader.

Wiki References
---------------
_(app-specific — outside wiki scope)_

Karpathy pattern frontend.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from wiki_helper import WIKI_ROOT, read_wiki_page, resolve_slug

try:
    import markdown as _md

    def render_md(txt: str) -> str:
        return _md.markdown(txt, extensions=["fenced_code", "tables", "toc"])
except Exception:  # pragma: no cover

    def render_md(txt: str) -> str:
        return f"<pre>{txt}</pre>"


router = APIRouter(prefix="/wiki")


def _render(request: Request, rel_path: str, title: str) -> HTMLResponse:
    from server import get_market_info, templates  # local import: avoid circular

    md = read_wiki_page(rel_path)
    html = render_md(md)
    ctx = {
        "active": "wiki",
        "page_title": title,
        "market": get_market_info(),
        "wiki_html": html,
        "wiki_title": title,
        "wiki_active": rel_path,
    }
    return templates.TemplateResponse(request, "wiki_page.html", ctx)


@router.get("", response_class=HTMLResponse)
async def wiki_index(request: Request):
    return _render(request, "index.md", "Wiki — Content Catalog")


@router.get("/slug/{slug}", response_class=HTMLResponse)
async def wiki_by_slug(request: Request, slug: str):
    path = resolve_slug(slug)
    if path is None:
        raise HTTPException(404, f"Unknown wiki slug: {slug}")
    rel = path.relative_to(WIKI_ROOT).as_posix()
    return _render(request, rel, slug.replace("_", " "))


@router.get("/{rel_path:path}", response_class=HTMLResponse)
async def wiki_path(request: Request, rel_path: str):
    # Only allow paths inside WIKI_ROOT — reject '..' escapes.
    candidate = (WIKI_ROOT / rel_path).resolve()
    try:
        candidate.relative_to(WIKI_ROOT.resolve())
    except ValueError:
        raise HTTPException(404, "Path escapes wiki root")
    if not candidate.exists() or candidate.suffix != ".md":
        raise HTTPException(404, f"No wiki page at {rel_path}")
    title = candidate.stem.replace("_", " ")
    return _render(request, rel_path, title)
