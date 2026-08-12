import json

from api_discovery.models import CrawlResult, Endpoint
from api_discovery.report import to_json, to_markdown


def _sample_result() -> CrawlResult:
    result = CrawlResult(start_url="https://example.com")
    result.pages_visited = ["https://example.com", "https://example.com/dashboard"]
    result.documented_paths = {"/users/{id}"}
    result.endpoints = [
        Endpoint(
            url="https://example.com/api/users/42",
            method="GET",
            source="network",
            content_type="application/json",
            status=200,
            found_on_page="https://example.com/dashboard",
            sample='{"id": 42}',
        ),
        Endpoint(
            url="https://example.com/internal/debug",
            method="GET",
            source="static-js",
            found_on_page="https://example.com",
        ),
    ]
    return result


def test_to_json_marks_undocumented_flag():
    result = _sample_result()
    undocumented = {"https://example.com/internal/debug"}

    payload = json.loads(to_json(result, undocumented))

    assert payload["start_url"] == "https://example.com"
    assert payload["pages_visited"] == sorted(result.pages_visited)
    by_url = {e["url"]: e for e in payload["endpoints"]}
    assert by_url["https://example.com/internal/debug"]["undocumented"] is True
    assert by_url["https://example.com/api/users/42"]["undocumented"] is False


def test_to_markdown_lists_undocumented_section():
    result = _sample_result()
    undocumented = {"https://example.com/internal/debug"}

    markdown = to_markdown(result, undocumented)

    assert "## Undocumented candidates" in markdown
    assert "`https://example.com/internal/debug`" in markdown
    assert "Pages crawled: 2" in markdown


def test_to_markdown_handles_no_undocumented_endpoints():
    result = _sample_result()
    markdown = to_markdown(result, undocumented=set())
    assert "_None found" in markdown
