# GitHub Actions

`CI` runs Ruff, the Python test suite against Home Assistant 2026.10.1 on
Python 3.14, and the frontend tests on Node.js 24. It runs on pushes and pull
requests to `main` and `dev`, and can also be started manually. Superseded
runs on the same branch are cancelled.

`Release` runs when a GitHub release or prerelease is published. It packages
the tagged integration as `king_smith.zip`, sets the packaged manifest version
from the tag (without an optional leading `v`), and attaches the ZIP to that
release. It also includes the matching section of `CHANGELOG.md` in the GitHub
release body so HACS can display the changes in Home Assistant's update dialog.
Use a heading such as `## 0.1.4` or `## 0.1.4 - YYYY-MM-DD`. Prereleases use
their own section when present and otherwise use the stable version's section.
Re-running the workflow replaces the ZIP and preserves existing release text.
