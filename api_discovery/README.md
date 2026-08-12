# API Discovery Scraper

A dynamic website scraper that finds API endpoints a site's frontend calls
but doesn't publicly document. It drives a real (headless) browser so
client-side JavaScript actually runs — this catches XHR/`fetch` calls that a
plain HTML/`requests`-based scraper would never see — and also statically
scans the rendered HTML and linked JS bundles for endpoint-looking strings.

## ⚠️ Only scan sites you're authorized to test

This tool crawls multiple pages, executes JavaScript, and probes a handful
of common documentation paths (`/swagger.json`, `/openapi.json`, etc.) on
the target host. Only point it at:

- a site/app you own,
- a staging/dev environment, or
- a target covered by a pentest engagement or bug-bounty program that
  explicitly permits automated scanning.

It respects `robots.txt` by default (`--ignore-robots` to override, only if
your authorization covers that), rate-limits itself between page loads
(`--delay`, default 1s), and caps how many pages it will visit
(`--max-pages`, default 15). Running it against a site without permission
may violate that site's terms of service or the law — that's on you, not
the tool.

## How it works

1. **Crawl**: starting from the given URL, follows same-origin links
   (`--allow-cross-origin` to also follow off-domain links/scripts) up to
   `--max-pages` pages.
2. **Observe**: for every page, records every XHR/`fetch` response the page
   triggers (URL, method, status, content-type, a truncated body sample).
3. **Static scan**: also regex-scans the page's HTML and any linked JS files
   for strings that look like API paths (`/api/...`, `/graphql`,
   `fetch("...")`, `axios.get("...")`, etc.) — this surfaces endpoints that
   exist in the code but weren't actually called during the crawl.
4. **Classify**: probes common OpenAPI/Swagger doc locations; if a spec is
   found, any observed endpoint whose path doesn't match a documented path
   template is flagged `undocumented`. If no spec is found at all, every
   API-looking endpoint is flagged as an undocumented candidate (there's no
   baseline to compare against).
5. **Report**: writes a JSON or Markdown report with every endpoint found,
   where it was seen, and whether it's flagged undocumented.

This is heuristic, not exhaustive — regex-based static analysis will miss
dynamically-constructed URLs and can have false positives, and dynamic
crawling only sees what actually executes during the visit (interactions
like clicking buttons, filling forms, or paginating aren't simulated).
Treat the output as a lead list to verify manually, not a guaranteed API map.

## Install

```
pip install -r api_discovery/requirements.txt
playwright install chromium
```

## Usage

```
python -m api_discovery https://your-staging-app.example.com
```

You'll be asked to confirm you're authorized to scan the target; pass
`--yes` to skip the prompt in scripted/CI use.

Useful flags:

| Flag | Default | What it does |
|---|---|---|
| `--max-pages` | `15` | Cap on pages visited |
| `--delay` | `1.0` | Seconds between page loads |
| `--timeout` | `15000` | Per-page navigation timeout (ms) |
| `--allow-cross-origin` | off | Also follow off-domain links/scripts |
| `--ignore-robots` | off | Skip the `robots.txt` check |
| `--headed` | off | Show the browser instead of headless |
| `--format` | `json` | `json` or `markdown` report |
| `--out` | `api-discovery-report.json` | Report output path |
| `--yes` | off | Skip the authorization confirmation prompt |

Example, writing a Markdown report:

```
python -m api_discovery https://your-staging-app.example.com \
  --max-pages 30 --format markdown --out report.md --yes
```

## Development

The Playwright-driven crawler (`scraper.py`) is the only module that
touches the network or a browser. Everything else — the endpoint-extraction
regexes, OpenAPI-doc comparison, `robots.txt` matching, and report
rendering — is pure and unit-tested without a browser:

```
python -m pytest api_discovery/tests
```
