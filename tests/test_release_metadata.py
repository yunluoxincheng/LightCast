from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from tools.release_metadata import (
    ReleaseMetadataError,
    ReleaseState,
    classify_release_state,
    extract_latest_release,
    extract_release,
    validate_version,
)


def test_latest_release_ignores_format_example_and_stops_at_next_section() -> None:
    changelog = """# 更新日志

```markdown
## [版本号] - 日期
```

## [1.2.3] - 2026-08-11

### 修复
- 完整正文

## [1.2.2] - 2026-08-10

- 旧正文
"""

    metadata = extract_latest_release(changelog)

    assert metadata.version == "1.2.3"
    assert metadata.tag == "v1.2.3"
    assert metadata.notes == "## [1.2.3] - 2026-08-11\n\n### 修复\n- 完整正文"
    assert "旧正文" not in metadata.notes


def test_extract_exact_release_for_manual_tag() -> None:
    changelog = """## [1.2.3] - 2026-08-11

- 新正文

## [1.2.2] - 2026-08-10

- 目标正文
"""

    metadata = extract_release(changelog, "1.2.2")

    assert metadata.version == "1.2.2"
    assert metadata.notes.endswith("- 目标正文")


@pytest.mark.parametrize(
    "changelog",
    [
        "# no releases\n",
        "## [1.2] - 2026-08-11\n\n- notes\n",
        "## [v1.2.3] - 2026-08-11\n\n- notes\n",
        "## [1.2.3-rc.1] - 2026-08-11\n\n- notes\n",
        "## [01.2.3] - 2026-08-11\n\n- notes\n",
        "## [1.2.3] 2026-08-11\n\n- notes\n",
        "## [1.2.3] - 2026-08-11\n\n   \n",
    ],
)
def test_latest_release_rejects_missing_malformed_or_empty_sections(changelog: str) -> None:
    with pytest.raises(ReleaseMetadataError):
        extract_latest_release(changelog)


@pytest.mark.parametrize(
    "version",
    [
        "v1.2.3",
        "1.2",
        "1.2.3.4",
        "1.2.3-rc.1",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3;echo-owned",
        "9" * 5000,
        "１.２.３",
    ],
)
def test_validate_version_rejects_non_stable_or_unsafe_text(version: str) -> None:
    with pytest.raises(ReleaseMetadataError):
        validate_version(version)


@pytest.mark.parametrize("version", ["0.1.24", "1.0.0", "10.20.300"])
def test_validate_version_accepts_stable_semver(version: str) -> None:
    assert validate_version(version) == version


@pytest.mark.parametrize(
    ("tag_sha", "release_exists", "expected"),
    [
        (None, False, ReleaseState.NEW),
        ("abc123", False, ReleaseState.RECOVER_RELEASE),
        ("abc123", True, ReleaseState.ALREADY_RELEASED),
        ("older", False, ReleaseState.CONFLICT),
        ("older", True, ReleaseState.CONFLICT),
    ],
)
def test_classify_release_state(
    tag_sha: str | None,
    release_exists: bool,
    expected: ReleaseState,
) -> None:
    assert (
        classify_release_state(
            tag_sha=tag_sha,
            current_sha="abc123",
            release_exists=release_exists,
        )
        is expected
    )


def test_changelog_cli_writes_notes_and_single_line_outputs(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    notes = tmp_path / "notes.md"
    output = tmp_path / "github-output.txt"
    changelog.write_text(
        "## [0.1.24] - 2026-08-11\n\n### 新增\n- 自动发布\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/release_metadata.py",
            "changelog",
            "--changelog",
            str(changelog),
            "--notes-out",
            str(notes),
            "--github-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert notes.read_text(encoding="utf-8") == (
        "## [0.1.24] - 2026-08-11\n\n### 新增\n- 自动发布\n"
    )
    assert output.read_text(encoding="utf-8") == "version=0.1.24\ntag=v0.1.24\n"
