"""Options shared by the test suite."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("yarbo-local live site check")
    group.addoption(
        "--hosts",
        default=None,
        help="run tests/live against real robots: a subnet, an IP or a comma list",
    )
    group.addoption("--window", type=float, default=12.0, help="seconds to listen to each broker")
