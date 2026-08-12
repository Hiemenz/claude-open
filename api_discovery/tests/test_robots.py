from api_discovery.robots import robots_allows_text


def test_robots_allows_by_default():
    body = "User-agent: *\nDisallow: /admin\n"
    assert robots_allows_text(body, "ApiDiscoveryBot", "https://example.com/products")


def test_robots_disallows_matching_path():
    body = "User-agent: *\nDisallow: /admin\n"
    assert not robots_allows_text(body, "ApiDiscoveryBot", "https://example.com/admin/panel")


def test_robots_specific_user_agent_rule():
    body = "User-agent: ApiDiscoveryBot\nDisallow: /private\n\nUser-agent: *\nDisallow:\n"
    assert not robots_allows_text(body, "ApiDiscoveryBot", "https://example.com/private/data")
    assert robots_allows_text(body, "SomeOtherBot", "https://example.com/private/data")
