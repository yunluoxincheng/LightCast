from __future__ import annotations

import asyncio
import hashlib

import pytest

from ydlna import updater


@pytest.mark.parametrize(
    ("remote", "current", "expected"),
    [
        ("v1.2.4", "1.2.3", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.2", "1.2.3", False),
        ("invalid", "1.2.3", False),
    ],
)
def test_is_newer(remote: str, current: str, expected: bool) -> None:
    assert updater.is_newer(remote, current) is expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.1.28", "0.1.28"),
        ("0.1.28", "0.1.28"),
        ("v1.2.3-rc1", "1.2.3"),
        # 网络来源的 tag 若携带路径穿越载荷，必须被剥离，
        # 不能进入下载文件名（LightCast-Setup-<version>.exe）
        ("v1.2.3-../../evil", "1.2.3"),
        ("garbage/../../..", "0.0.0"),
        ("", "0.0.0"),
    ],
)
def test_canonical_version_strips_traversal_payload(tag: str, expected: str) -> None:
    assert updater.canonical_version(tag) == expected
    assert "/" not in expected and "\\" not in expected


def test_sha256_file(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "installer.exe"
    payload = b"trusted release artifact"
    path.write_bytes(payload)
    assert updater._sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_fetch_sha256_parses_exact_asset_name(monkeypatch) -> None:
    wanted = "a" * 64
    other = "b" * 64
    text = (
        f"{other}  LightCast-Portable-1.0.0.zip\n"
        f"{wanted.upper()}  ./LightCast-Setup-1.0.0.exe\n"
    )

    async def fetch(_url: str):  # noqa: ANN202
        return text

    monkeypatch.setattr(updater, "_fetch_direct_text", fetch)
    result = asyncio.run(
        updater._fetch_sha256(
            "https://github.com/example/SHA256SUMS.txt",
            "LightCast-Setup-1.0.0.exe",
        )
    )
    assert result == wanted


def test_fetch_sha256_rejects_missing_or_malformed_entry(monkeypatch) -> None:
    async def fetch(_url: str):  # noqa: ANN202
        return "not-a-sha  LightCast-Setup-1.0.0.exe\n"

    monkeypatch.setattr(updater, "_fetch_direct_text", fetch)
    result = asyncio.run(
        updater._fetch_sha256(
            "https://github.com/example/SHA256SUMS.txt",
            "LightCast-Setup-1.0.0.exe",
        )
    )
    assert result is None
