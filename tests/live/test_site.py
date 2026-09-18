"""The site check as pytest, for people who would rather read ``pytest -v``.

    uv run pytest tests/live --hosts 192.168.50.0/24 -v

Skipped unless ``--hosts`` is given, so CI never touches a network. It runs exactly
what ``yarbo-local sitecheck`` runs, once, then reports each check as a test. It only
listens and sends verified reads. The report and log land in the current directory,
with serials and addresses replaced by labels and no position written anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from yarbo_local import sitecheck


@pytest.fixture(scope="session")
def site(request: pytest.FixtureRequest) -> tuple[sitecheck.Survey, dict[str, sitecheck.Finding]]:
    hosts = request.config.getoption("--hosts")
    if not hosts:
        pytest.skip("pass --hosts SUBNET_OR_IPS to check a real site")
    survey, findings = sitecheck.run(
        hosts, window=request.config.getoption("--window"), out_dir=Path.cwd()
    )
    return survey, {finding.check: finding for finding in findings}


@pytest.mark.parametrize("check", sitecheck.CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
def test_site(
    site: tuple[sitecheck.Survey, dict[str, sitecheck.Finding]],
    check: Callable[[sitecheck.Survey], sitecheck.Finding],
) -> None:
    survey, _ = site
    finding = check(survey)
    text = survey.labels.scrub(finding.render())
    if finding.status == "skip":
        pytest.skip(text)
    assert finding.ok, "\n" + text
    print(text)
