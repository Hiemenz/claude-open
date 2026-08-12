from api_discovery.analyzer import (
    classify_undocumented,
    documented_paths_from_openapi,
    extract_endpoints_from_text,
    path_matches_template,
)

BASE = "https://example.com/app"


def test_extract_plain_api_path_reference():
    text = 'const base = "/api/v1/users";'
    found = dict(extract_endpoints_from_text(text, BASE))
    assert found["https://example.com/api/v1/users"] == "GET"


def test_extract_fetch_call_with_explicit_method():
    text = "fetch('/api/v1/users', {method: 'POST', headers: {}})"
    found = dict(extract_endpoints_from_text(text, BASE))
    assert found["https://example.com/api/v1/users"] == "POST"


def test_extract_axios_call_defaults_to_get():
    text = "axios.get('/internal/reports/summary').then(r => r.data)"
    found = dict(extract_endpoints_from_text(text, BASE))
    assert found["https://example.com/internal/reports/summary"] == "GET"


def test_absolute_url_to_different_host_is_kept():
    text = 'fetch("https://api.example.com/v2/orders")'
    found = dict(extract_endpoints_from_text(text, BASE))
    assert "https://api.example.com/v2/orders" in found


def test_static_assets_are_excluded():
    text = 'fetch("/api/assets/logo.png")'
    found = dict(extract_endpoints_from_text(text, BASE))
    assert not found


def test_non_api_looking_paths_are_ignored():
    text = 'href="/about-us"'
    found = dict(extract_endpoints_from_text(text, BASE))
    assert not found


def test_documented_paths_from_openapi():
    spec = '{"paths": {"/users": {}, "/users/{id}": {}, "not-a-path": 1}}'
    assert documented_paths_from_openapi(spec) == {"/users", "/users/{id}"}


def test_documented_paths_from_invalid_json():
    assert documented_paths_from_openapi("not json") == set()


def test_documented_paths_from_non_dict_json():
    assert documented_paths_from_openapi("[1, 2, 3]") == set()


def test_path_matches_template():
    assert path_matches_template("/users/42", "/users/{id}")
    assert not path_matches_template("/users/42/orders", "/users/{id}")
    assert not path_matches_template("/accounts/42", "/users/{id}")
    assert path_matches_template("/users", "/users")


def test_classify_undocumented():
    discovered = {
        "https://example.com/users/42",
        "https://example.com/internal/debug",
    }
    documented = {"/users/{id}"}
    undocumented = classify_undocumented(discovered, documented)
    assert undocumented == {"https://example.com/internal/debug"}
