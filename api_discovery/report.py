from __future__ import annotations

import json
from datetime import datetime, timezone

from .models import CrawlResult


def to_json(result: CrawlResult, undocumented: set[str]) -> str:
    payload = {
        "start_url": result.start_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pages_visited": sorted(result.pages_visited),
        "documented_paths": sorted(result.documented_paths),
        "endpoints": [
            {
                "url": e.url,
                "method": e.method,
                "source": e.source,
                "content_type": e.content_type,
                "status": e.status,
                "found_on_page": e.found_on_page,
                "sample": e.sample,
                "undocumented": e.url in undocumented,
            }
            for e in sorted(result.endpoints, key=lambda e: e.url)
        ],
    }
    return json.dumps(payload, indent=2)


def to_markdown(result: CrawlResult, undocumented: set[str]) -> str:
    lines = [f"# API discovery report for {result.start_url}", ""]
    lines.append(f"- Pages crawled: {len(result.pages_visited)}")
    lines.append(f"- Endpoints observed: {len(result.endpoints)}")
    lines.append(f"- Undocumented candidates: {len(undocumented)}")
    lines.append("")
    lines.append("## Undocumented candidates")
    lines.append("")
    if not undocumented:
        lines.append("_None found (or no documentation baseline was available for comparison)._")
    for url in sorted(undocumented):
        lines.append(f"- `{url}`")
    lines.append("")
    lines.append("## All observed endpoints")
    lines.append("")
    lines.append("| Method | URL | Content-Type | Status | Source | Found on |")
    lines.append("|---|---|---|---|---|---|")
    for e in sorted(result.endpoints, key=lambda e: e.url):
        lines.append(
            f"| {e.method} | `{e.url}` | {e.content_type} | {e.status or ''} | {e.source} | {e.found_on_page} |"
        )
    return "\n".join(lines) + "\n"
