# Changelog

## 0.1.3 - 2026-11-06

Production release of the integration previously published as `0.1.3-dev.5`.

- Restore manual setup and include the WalkingPad command-lock, cadence,
  polling, model-discovery and offline-handling improvements from development.
- Record persistent walking sessions and export their original JSON and FIT
  files, with restart recovery and recording-quality explanations.
- Review sessions through Companion App notifications before uploading to
  Garmin Connect; uncertain uploads require an explicit reviewed retry.
- Browse sessions, compare saved and decoded FIT metrics, download files,
  and approve uploads in the bundled Home Assistant administrator panel.
- Version the phone review blueprint as **1.0.0**, with its changelog and
  compatibility notes in the description. Inputs, defaults, triggers and
  actions match the unversioned blueprint from `0.1.3-dev.5`; existing
  automations need no input changes. Blueprint imports and update checks
  follow production `main`.
- Keep one CI workflow for Ruff and Python/frontend tests, plus the release
  workflow for the integration ZIP. Remove duplicate checks and the template
  HACS/Hassfest validation workflow.

Install integration `0.1.3` from this fork's HACS repository, restart Home
Assistant, then import or re-import the blueprint separately from the
[production source](https://github.com/s-bed/king_smith/blob/main/blueprints/automation/king_smith/review_walkingpad_session.yaml).
Keep the existing blueprint filename when updating. Copies imported from
a fixed prerelease tag must be re-imported once to follow production updates.

Automated checks cover the recorder, FIT encoding/decoding, Home Assistant
APIs, blueprint templates and frontend behavior. Physical treadmill recording
and Garmin's displayed interpretation still require live validation.
