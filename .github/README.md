# GitHub Actions

`CI` runs Ruff, the Python test suite against Home Assistant 2026.10.1 on
Python 3.14, and the frontend tests on Node.js 24. It runs on pushes and pull
requests to `main` and `dev`, and can also be started manually. Superseded
runs on the same branch are cancelled.

`Release` runs when a GitHub release or prerelease is published. It packages
the tagged integration as `king_smith.zip`, sets the packaged manifest version
from the tag (without an optional leading `v`), and attaches the ZIP to that
release. Re-running the workflow replaces the existing ZIP. It does not modify
the repository's manifest or publish a new release.
