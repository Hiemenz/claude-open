"""Pure text-analysis helpers: no network, no browser.

Given HTML/JS source, guess at API endpoints it references. Given an
OpenAPI/Swagger document, extract the paths it documents so discovered
endpoints can be classified as documented vs. undocumented.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

# Paths that look API-ish by convention: /api/*, /graphql, /v1/*, etc.
API_PATH_HINTS = re.compile(
    r"""["'`(=]\s*
        (
            (?:https?://[^\s"'`)]+)?
            /(?:api|graphql|rest|internal|service|v[0-9]+)(?:/[^\s"'`)<>]*)?
        )
        \s*["'`)]
    """,
    re.IGNORECASE | re.VERBOSE,
)

# fetch(...)/axios(...) calls, optionally capturing an explicit HTTP method.
FETCH_METHOD = re.compile(
    r"""(?:fetch|axios(?:\.\w+)?)\s*\(\s*["'`]([^"'`]+)["'`]
        (?:\s*,\s*\{[^{}]*?method\s*:\s*["'`](GET|POST|PUT|PATCH|DELETE)["'`])?
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

STATIC_ASSET_EXT = re.compile(
    r"\.(?:png|jpe?g|gif|svg|css|woff2?|ttf|eot|ico|map|mp4|webp)(?:$|\?)",
    re.IGNORECASE,
)


def extract_endpoints_from_text(text: str, base_url: str) -> list[tuple[str, str]]:
    """Scan HTML/JS source for strings that look like API calls.

    Returns a list of (absolute_url, http_method) pairs, deduplicated by URL.
    This is a heuristic (regex over source text), not a JS parser, so it will
    have false positives/negatives on obfuscated or dynamically-built URLs.
    """
    found: dict[str, str] = {}

    for match in API_PATH_HINTS.finditer(text):
        url = _normalize(match.group(1), base_url)
        if url:
            found.setdefault(url, "GET")

    for match in FETCH_METHOD.finditer(text):
        url = _normalize(match.group(1), base_url)
        if url:
            found[url] = (match.group(2) or found.get(url, "GET")).upper()

    return list(found.items())


def _normalize(raw: str, base_url: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith(("data:", "javascript:", "#")):
        return None
    try:
        absolute = urljoin(base_url, raw)
    except ValueError:
        return None
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None
    if STATIC_ASSET_EXT.search(parsed.path):
        return None
    return absolute


def documented_paths_from_openapi(text: str) -> set[str]:
    """Extract path templates (e.g. "/users/{id}") from an OpenAPI/Swagger doc."""
    try:
        spec = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        return set()
    return {p for p in paths if isinstance(p, str) and p.startswith("/")}


def path_matches_template(path: str, template: str) -> bool:
    """Check whether a concrete path matches an OpenAPI-style template.

    "/users/42" matches "/users/{id}" but not "/users/42/orders".
    """
    p_segs = [s for s in path.split("/") if s]
    t_segs = [s for s in template.split("/") if s]
    if len(p_segs) != len(t_segs):
        return False
    for p, t in zip(p_segs, t_segs):
        if t.startswith("{") and t.endswith("}"):
            continue
        if p != t:
            return False
    return True


def classify_undocumented(discovered_urls: set[str], documented_templates: set[str]) -> set[str]:
    """Return the subset of discovered URLs not covered by any documented template."""
    undocumented = set()
    for url in discovered_urls:
        path = urlparse(url).path
        if not any(path_matches_template(path, tmpl) for tmpl in documented_templates):
            undocumented.add(url)
    return undocumented
