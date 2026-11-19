"""Select the stable or next numbered prerelease tag for a branch push."""

from argparse import ArgumentParser
import json
from pathlib import Path
import re
from subprocess import check_output
import sys

ROOT = Path(__file__).resolve().parents[2]


def select_tag(version: str, branch: str, tags: set[str], head_tags: set[str]) -> str:
    """Allocate a tag once per commit and preserve tags from earlier releases."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("The manifest version must be a stable x.y.z version.")

    if branch == "main":
        versions = (version, f"v{version}")
        return next(
            (tag for tag in versions if tag in head_tags),
            "" if tags.intersection(versions) else version,
        )

    pattern = re.compile(rf"v?{re.escape(version)}-dev\.([1-9][0-9]*)")
    previews = [
        (int(match[1]), tag) for tag in tags if (match := pattern.fullmatch(tag))
    ]
    existing = max(
        ((number, tag) for number, tag in previews if tag in head_tags),
        default=None,
    )
    if existing:
        return existing[1]
    next_number = max((number for number, _ in previews), default=0) + 1
    return f"{version}-dev.{next_number}"


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("branch", help="Name of the branch that received a push")
    options = parser.parse_args()
    manifest = ROOT / "custom_components" / "king_smith" / "manifest.json"
    version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    tags = set(check_output(["git", "tag", "--list"], cwd=ROOT, text=True).splitlines())
    head_tags = set(
        check_output(
            ["git", "tag", "--points-at", "HEAD"], cwd=ROOT, text=True
        ).splitlines()
    )
    sys.stdout.write(f"{select_tag(version, options.branch, tags, head_tags)}\n")
