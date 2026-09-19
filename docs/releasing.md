# Releasing

Releases go to PyPI from GitHub Actions through trusted publishing. There is no API token in the repository, in its secrets, or on anyone's laptop: PyPI accepts an upload because it comes from `release.yml` in `yarbo-local/yarbo-local`, in the `pypi` environment, and from nowhere else.

## Once, on pypi.org

Signed in as the project owner: Account, Publishing, "Add a new pending publisher", GitHub.

| Field | Value |
|---|---|
| PyPI project name | `yarbo-local` |
| Owner | `yarbo-local` |
| Repository name | `yarbo-local` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

A pending publisher reserves nothing until the first upload; the project is created by it.

## Each release

1. Set `version` in `pyproject.toml`, commit, push, wait for CI.
2. Tag it and push the tag: `git tag v0.1.0 && git push origin v0.1.0`. The tag must equal the version with a `v` in front, or the workflow stops.
3. The workflow runs the tests, builds the wheel and the source package, installs the wheel into a clean environment and checks that the command registry is inside it, then waits.
4. Approve the `pypi` deployment on the workflow's page. Only then is anything uploaded.

A version number can never be reused on PyPI, even after deleting the release. If a release is wrong, fix it and release the next number.

## What ships

The wheel carries the library, the command registry and code tables as package data, and the Studio's static files. The source package adds the protocol directory with its redacted fixtures, the docs and the tests. `captures/` is never included: it is not in the build configuration and is ignored by git. The leak check runs as a pre-commit hook on the machine that holds the unredacted captures, which is the only place it can know what to look for.

After a release, the Home Assistant integration's `manifest.json` pins that exact version.
