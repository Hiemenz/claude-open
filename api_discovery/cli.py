from __future__ import annotations

import argparse
import asyncio
import sys

from .analyzer import classify_undocumented
from .report import to_json, to_markdown
from .scraper import ApiDiscoveryScraper

DISCLAIMER = (
    "This tool actively crawls and probes the target site in a real browser.\n"
    "Only run it against sites you own or are explicitly authorized to test\n"
    "(your own app, a staging environment, or an engagement/program that\n"
    "permits automated scanning). Scraping third-party sites without\n"
    "permission may violate their terms of service or the law.\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api-discovery",
        description="Dynamically crawl a website and surface undocumented API endpoints it calls.",
    )
    parser.add_argument("url", help="Start URL, e.g. https://example.com")
    parser.add_argument("--max-pages", type=int, default=15, help="Maximum number of pages to visit")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between page loads")
    parser.add_argument("--timeout", type=int, default=15000, help="Per-page navigation timeout in ms")
    parser.add_argument(
        "--allow-cross-origin", action="store_true", help="Also follow links/scripts off the start domain"
    )
    parser.add_argument("--ignore-robots", action="store_true", help="Do not consult robots.txt before crawling")
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    parser.add_argument("--user-agent", default="ApiDiscoveryBot/1.0 (+authorized-scan)")
    parser.add_argument("--out", default="api-discovery-report.json", help="Report output path")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--yes", action="store_true", help="Skip the authorization confirmation prompt")
    return parser


async def _run(args: argparse.Namespace) -> int:
    scraper = ApiDiscoveryScraper(
        args.url,
        max_pages=args.max_pages,
        same_origin_only=not args.allow_cross_origin,
        request_delay=args.delay,
        respect_robots=not args.ignore_robots,
        user_agent=args.user_agent,
        headless=not args.headed,
        timeout_ms=args.timeout,
    )
    result = await scraper.run()

    discovered_urls = {e.url for e in result.endpoints}
    undocumented = (
        classify_undocumented(discovered_urls, result.documented_paths)
        if result.documented_paths
        else discovered_urls
    )

    body = to_json(result, undocumented) if args.format == "json" else to_markdown(result, undocumented)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(body)

    print(
        f"Crawled {len(result.pages_visited)} page(s), observed {len(result.endpoints)} endpoint(s), "
        f"{len(undocumented)} flagged as undocumented."
    )
    print(f"Report written to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    print(DISCLAIMER)
    if not args.yes:
        reply = input(f"Confirm you are authorized to scan {args.url} [y/N]: ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return 1

    try:
        return asyncio.run(_run(args))
    except ImportError:
        print(
            "Playwright is required to run a crawl. Install with:\n"
            "  pip install -r api_discovery/requirements.txt\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
