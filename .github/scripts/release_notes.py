"""Add the tagged changelog to GitHub release notes for Home Assistant updates."""

from argparse import ArgumentParser
from pathlib import Path
import re

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def release_notes(tag: str, changelog: str, existing: str) -> str:
    """Preserve authored release text and include the corresponding changes once."""
    version = tag.removeprefix("v")
    sections = {}
    for section in re.split(r"(?m)^## ", changelog)[1:]:
        heading, _, text = section.partition("\n")
        if text.strip():
            sections[heading.split(maxsplit=1)[0]] = text.strip()
    notes = sections.get(version) or sections.get(version.partition("-")[0])
    if not notes or notes in existing:
        return existing
    if not existing.strip():
        return notes + "\n"
    return f"{existing.rstrip()}\n\n## Changelog\n\n{notes}\n"


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("existing", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    options.output.write_text(
        release_notes(options.tag, CHANGELOG.read_text(encoding="utf-8"), options.existing.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
