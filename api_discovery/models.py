from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    """A single API call observed or inferred while scanning a site."""

    url: str
    method: str = "GET"
    source: str = "network"  # "network" | "static-html" | "static-js"
    content_type: str = ""
    status: int | None = None
    found_on_page: str = ""
    sample: str = ""  # truncated response body or surrounding code, for context


@dataclass
class CrawlResult:
    start_url: str
    pages_visited: list[str] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    documented_paths: set[str] = field(default_factory=set)
