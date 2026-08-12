"""Playwright-driven dynamic crawler.

Loads pages in a real (headless) browser so client-side JS runs, then
records every XHR/fetch call the page makes plus any API-looking strings
found in the rendered HTML and linked JS bundles. This is the only module
that touches the network/browser, kept separate from the pure logic in
analyzer.py/report.py so that logic can be unit tested without Playwright.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urldefrag, urljoin, urlparse

from .analyzer import documented_paths_from_openapi, extract_endpoints_from_text
from .models import CrawlResult, Endpoint
from .robots import RobotsChecker

DEFAULT_DOC_PATHS = (
    "/openapi.json",
    "/swagger.json",
    "/api-docs",
    "/api/openapi.json",
    "/api/swagger.json",
    "/v1/openapi.json",
    "/.well-known/openapi.json",
)

NAV_LINK = re.compile(r'href=["\']([^"\'#][^"\']*)["\']', re.IGNORECASE)
SCRIPT_SRC = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
STATIC_EXT = re.compile(
    r"\.(?:png|jpe?g|gif|svg|css|woff2?|ttf|eot|ico|pdf|zip|mp4|webp)(?:$|\?)",
    re.IGNORECASE,
)

API_RESOURCE_TYPES = {"xhr", "fetch"}


class ApiDiscoveryScraper:
    def __init__(
        self,
        start_url: str,
        *,
        max_pages: int = 15,
        same_origin_only: bool = True,
        request_delay: float = 1.0,
        respect_robots: bool = True,
        user_agent: str = "ApiDiscoveryBot/1.0 (+authorized-scan)",
        headless: bool = True,
        timeout_ms: int = 15000,
    ) -> None:
        self.start_url = start_url
        self.max_pages = max_pages
        self.same_origin_only = same_origin_only
        self.request_delay = request_delay
        self.user_agent = user_agent
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._origin = urlparse(start_url).netloc
        self._robots = RobotsChecker(user_agent) if respect_robots else None

    async def run(self) -> CrawlResult:
        # Imported lazily so the rest of the package (and its tests) work
        # without Playwright installed; only actually running a crawl needs it.
        from playwright.async_api import async_playwright

        result = CrawlResult(start_url=self.start_url)
        visited: set[str] = set()
        queue: list[str] = [self.start_url]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(user_agent=self.user_agent)

            await self._probe_doc_endpoints(context, result)

            while queue and len(visited) < self.max_pages:
                url, _ = urldefrag(queue.pop(0))
                if url in visited:
                    continue
                if self.same_origin_only and urlparse(url).netloc != self._origin:
                    continue
                if self._robots and not self._robots.allowed(url):
                    continue

                visited.add(url)
                page_endpoints = await self._load_page(context, url, result, queue)
                result.endpoints.extend(page_endpoints)
                result.pages_visited.append(url)
                await asyncio.sleep(self.request_delay)

            await browser.close()

        return result

    async def _load_page(self, context, url: str, result: CrawlResult, queue: list[str]) -> list[Endpoint]:
        page = await context.new_page()
        page_endpoints: list[Endpoint] = []

        async def handle_response(response):
            endpoint = await self._endpoint_from_response(response, url)
            if endpoint:
                page_endpoints.append(endpoint)

        page.on("response", handle_response)

        try:
            await page.goto(url, timeout=self.timeout_ms, wait_until="networkidle")
        except Exception:
            await page.close()
            return page_endpoints

        html = await page.content()
        await page.close()

        await self._extract_static(context, html, url, result)
        for link in self._extract_links(html, url):
            queue.append(link)

        return page_endpoints

    async def _endpoint_from_response(self, response, page_url: str) -> Endpoint | None:
        try:
            request = response.request
            resource_type = request.resource_type
            content_type = response.headers.get("content-type", "")
            looks_like_api = (
                resource_type in API_RESOURCE_TYPES
                or "json" in content_type.lower()
                or "graphql" in content_type.lower()
            )
            if not looks_like_api or STATIC_EXT.search(response.url):
                return None

            sample = ""
            try:
                body = await response.text()
                sample = body[:400]
            except Exception:
                pass

            return Endpoint(
                url=response.url,
                method=request.method,
                source="network",
                content_type=content_type,
                status=response.status,
                found_on_page=page_url,
                sample=sample,
            )
        except Exception:
            return None

    async def _extract_static(self, context, html: str, page_url: str, result: CrawlResult) -> None:
        for url, method in extract_endpoints_from_text(html, page_url):
            result.endpoints.append(
                Endpoint(url=url, method=method, source="static-html", found_on_page=page_url)
            )

        for match in SCRIPT_SRC.finditer(html):
            js_url = urljoin(page_url, match.group(1))
            if self.same_origin_only and urlparse(js_url).netloc != self._origin:
                continue
            try:
                resp = await context.request.get(js_url, timeout=self.timeout_ms)
                if not resp.ok:
                    continue
                js_text = await resp.text()
            except Exception:
                continue
            for url, method in extract_endpoints_from_text(js_text, js_url):
                result.endpoints.append(
                    Endpoint(url=url, method=method, source="static-js", found_on_page=page_url)
                )

    def _extract_links(self, html: str, page_url: str) -> list[str]:
        links = []
        for match in NAV_LINK.finditer(html):
            href = match.group(1)
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(page_url, href)
            if STATIC_EXT.search(absolute):
                continue
            if self.same_origin_only and urlparse(absolute).netloc != self._origin:
                continue
            links.append(urldefrag(absolute)[0])
        return links

    async def _probe_doc_endpoints(self, context, result: CrawlResult) -> None:
        origin = f"{urlparse(self.start_url).scheme}://{self._origin}"
        for path in DEFAULT_DOC_PATHS:
            doc_url = origin + path
            try:
                resp = await context.request.get(doc_url, timeout=self.timeout_ms)
                if resp.ok:
                    text = await resp.text()
                    result.documented_paths.update(documented_paths_from_openapi(text))
            except Exception:
                continue
