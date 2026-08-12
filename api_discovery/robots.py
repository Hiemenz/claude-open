from __future__ import annotations

import urllib.robotparser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def robots_allows_text(body: str, user_agent: str, url: str) -> bool:
    """Pure helper: does a robots.txt body (as text) allow fetching url?"""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    return parser.can_fetch(user_agent, url)


class RobotsChecker:
    """Fetches and caches robots.txt per origin, defaulting to "allowed" on error."""

    def __init__(self, user_agent: str, fetch_timeout: float = 5.0) -> None:
        self.user_agent = user_agent
        self.fetch_timeout = fetch_timeout
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._parsers.get(origin)
        if parser is None:
            parser = self._fetch_parser(origin)
            self._parsers[origin] = parser
        return parser.can_fetch(self.user_agent, url)

    def _fetch_parser(self, origin: str) -> urllib.robotparser.RobotFileParser:
        parser = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(origin + "/", "robots.txt")
        parser.set_url(robots_url)
        try:
            request = Request(robots_url, headers={"User-Agent": self.user_agent})
            with urlopen(request, timeout=self.fetch_timeout) as resp:  # noqa: S310 (http/https only, url derived from target origin)
                body = resp.read().decode("utf-8", errors="replace")
            parser.parse(body.splitlines())
        except Exception:
            parser.allow_all = True
        return parser
