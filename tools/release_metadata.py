"""Parse LightCast release metadata and classify GitHub release state.

This module deliberately uses only the Python standard library so the same
logic can run in pytest and in GitHub Actions before application dependencies
are installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import sys


_VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_VERSION_RE = re.compile(rf"^{_VERSION_PATTERN}$", re.ASCII)
_HEADING_RE = re.compile(
    rf"^## \[(?P<version>{_VERSION_PATTERN})\] - (?P<date>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$",
    re.ASCII,
)


class ReleaseMetadataError(ValueError):
    """Raised when CHANGELOG release metadata is missing or malformed."""


class ReleaseState(str, Enum):
    NEW = "new"
    RECOVER_RELEASE = "recover_release"
    RECOVER_DRAFT = "recover_draft"
    ALREADY_RELEASED = "already_released"
    CONFLICT = "conflict"


class ReleaseStatus(str, Enum):
    MISSING = "missing"
    DRAFT = "draft"
    PUBLISHED = "published"
    PRERELEASE = "prerelease"


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    notes: str

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def validate_version(version: str) -> str:
    """Return a stable SemVer version or raise a user-facing error."""
    if not _VERSION_RE.fullmatch(version):
        raise ReleaseMetadataError(
            f"Invalid stable version {version!r}; expected X.Y.Z without a v prefix or prerelease suffix"
        )
    return version


def _release_headings(text: str) -> list[tuple[int, int, str]]:
    """Return bracketed level-two headings outside fenced code blocks."""
    headings: list[tuple[int, int, str]] = []
    offset = 0
    fence: str | None = None

    for line_with_ending in text.splitlines(keepends=True):
        line = line_with_ending.rstrip("\r\n")
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        elif fence is None and line.startswith("## ["):
            headings.append((offset, offset + len(line), line))
        offset += len(line_with_ending)

    return headings


def _metadata_from_heading(
    text: str,
    headings: list[tuple[int, int, str]],
    heading_index: int,
) -> ReleaseMetadata:
    start, _, heading = headings[heading_index]
    match = _HEADING_RE.fullmatch(heading)
    if match is None:
        raise ReleaseMetadataError(
            f"Malformed CHANGELOG release heading {heading!r}; expected '## [X.Y.Z] - YYYY-MM-DD'"
        )

    end = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(text)
    notes = text[start:end].strip()
    body = text[start + len(heading) : end].strip()
    if not body:
        raise ReleaseMetadataError(
            f"CHANGELOG section for {match.group('version')} has no release notes"
        )

    return ReleaseMetadata(version=match.group("version"), notes=notes)


def extract_latest_release(text: str) -> ReleaseMetadata:
    """Extract the first real release section from CHANGELOG text."""
    headings = _release_headings(text)
    if not headings:
        raise ReleaseMetadataError(
            "CHANGELOG has no release heading; expected '## [X.Y.Z] - YYYY-MM-DD'"
        )
    return _metadata_from_heading(text, headings, 0)


def extract_release(text: str, version: str) -> ReleaseMetadata:
    """Extract one exact stable release section from CHANGELOG text."""
    validate_version(version)
    headings = _release_headings(text)
    for index, (_, _, heading) in enumerate(headings):
        match = _HEADING_RE.fullmatch(heading)
        if match is not None and match.group("version") == version:
            return _metadata_from_heading(text, headings, index)
    raise ReleaseMetadataError(
        f"CHANGELOG has no section for {version}; expected '## [{version}] - YYYY-MM-DD'"
    )


def classify_release_state(
    *,
    tag_sha: str | None,
    current_sha: str,
    release_status: ReleaseStatus | str,
) -> ReleaseState:
    """Classify whether a release is new, recoverable, complete, or conflicting."""
    if not current_sha:
        raise ValueError("current_sha must not be empty")
    try:
        status = ReleaseStatus(release_status)
    except ValueError as exc:
        raise ValueError(f"unknown release status: {release_status!r}") from exc

    if not tag_sha:
        return (
            ReleaseState.NEW
            if status is ReleaseStatus.MISSING
            else ReleaseState.CONFLICT
        )
    if tag_sha != current_sha:
        return ReleaseState.CONFLICT
    if status is ReleaseStatus.PUBLISHED:
        return ReleaseState.ALREADY_RELEASED
    if status is ReleaseStatus.DRAFT:
        return ReleaseState.RECOVER_DRAFT
    if status is ReleaseStatus.MISSING:
        return ReleaseState.RECOVER_RELEASE
    return ReleaseState.CONFLICT


def _write_github_output(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"GitHub output {key!r} must be a single line")
            output.write(f"{key}={value}\n")


def _command_changelog(args: argparse.Namespace) -> None:
    text = args.changelog.read_text(encoding="utf-8")
    metadata = (
        extract_release(text, args.version)
        if args.version is not None
        else extract_latest_release(text)
    )
    if args.notes_out is not None:
        args.notes_out.write_text(metadata.notes + "\n", encoding="utf-8", newline="\n")
    _write_github_output(
        args.github_output,
        {"version": metadata.version, "tag": metadata.tag},
    )
    print(metadata.version)


def _command_state(args: argparse.Namespace) -> None:
    state = classify_release_state(
        tag_sha=args.tag_sha or None,
        current_sha=args.current_sha,
        release_status=args.release_status,
    )
    _write_github_output(args.github_output, {"release_state": state.value})
    print(state.value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    changelog = subparsers.add_parser("changelog", help="validate and extract CHANGELOG metadata")
    changelog.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    changelog.add_argument("--version", help="extract this version instead of the latest section")
    changelog.add_argument("--notes-out", type=Path)
    changelog.add_argument("--github-output", type=Path)
    changelog.set_defaults(handler=_command_changelog)

    state = subparsers.add_parser("state", help="classify existing tag and Release state")
    state.add_argument("--tag-sha", default="")
    state.add_argument("--current-sha", required=True)
    state.add_argument(
        "--release-status",
        choices=[status.value for status in ReleaseStatus],
        default=ReleaseStatus.MISSING.value,
    )
    state.add_argument("--github-output", type=Path)
    state.set_defaults(handler=_command_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (OSError, ReleaseMetadataError, ValueError) as exc:
        print(f"release metadata error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
