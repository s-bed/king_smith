# GitHub Actions

`CI` runs Ruff, the Python test suite against Home Assistant 2026.10.1 on
Python 3.14, and the frontend tests on Node.js 24. It runs on pushes and pull
requests to `main` and `dev`, and can also be started manually. Superseded
runs on the same branch are cancelled.

`Release` tags and publishes a stable release when a new manifest version
reaches `main`. Pushes to `dev` and branches under `codex/` publish numbered
prereleases such as `0.1.4-dev.1` for the version in `manifest.json`.

The workflow packages `king_smith.zip` with the tag as its manifest version
and includes the corresponding section of `CHANGELOG.md` in the release body.
HACS uses that body for the Home Assistant update dialog. Use a heading such as
`## 0.1.4` or `## 0.1.4 - YYYY-MM-DD`. A prerelease uses its own section when
present and otherwise uses the stable version's section.

Pushing a version tag manually also starts a release. Publishing a release
through GitHub attaches the ZIP and adds the changelog to any existing notes.
