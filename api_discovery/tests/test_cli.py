from api_discovery.cli import build_parser


def test_defaults():
    args = build_parser().parse_args(["https://example.com"])
    assert args.url == "https://example.com"
    assert args.max_pages == 15
    assert args.delay == 1.0
    assert args.format == "json"
    assert args.allow_cross_origin is False
    assert args.ignore_robots is False
    assert args.headed is False
    assert args.yes is False


def test_overrides():
    args = build_parser().parse_args(
        [
            "https://example.com",
            "--max-pages",
            "5",
            "--delay",
            "0.5",
            "--allow-cross-origin",
            "--ignore-robots",
            "--headed",
            "--format",
            "markdown",
            "--out",
            "report.md",
            "--yes",
        ]
    )
    assert args.max_pages == 5
    assert args.delay == 0.5
    assert args.allow_cross_origin is True
    assert args.ignore_robots is True
    assert args.headed is True
    assert args.format == "markdown"
    assert args.out == "report.md"
    assert args.yes is True
