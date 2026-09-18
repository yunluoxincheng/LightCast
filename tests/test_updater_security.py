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
    ("remote", "current", "expected"),
    [
        # M9：同数字段的正式版必须新于其预发布，rc 用户才能收到正式版
        ("1.2.3", "1.2.3-rc1", True),
        ("v1.2.3", "1.2.3-rc1", True),
        ("1.2.3-rc1", "1.2.3", False),
        # 预发布之间按后缀逐段比较
        ("1.2.3-rc2", "1.2.3-rc1", True),
        ("1.2.3-rc1", "1.2.3-rc2", False),
        # 点分数字段按数值而非字典序比较：2 < 11
        ("1.2.3-rc.11", "1.2.3-rc.2", True),
        ("1.2.3-rc.2", "1.2.3-rc.11", False),
        # SemVer：数字标识符 < 字母数字标识符；不得抛 TypeError
        ("1.2.3-beta.1", "1.2.3-alpha.2", True),
        ("1.2.3-alpha", "1.2.3-beta", False),
        ("1.2.3-1", "1.2.3-rc", False),
        ("1.2.3-rc", "1.2.3-1", True),
        # 更高的数字段无视预发布属性
        ("1.2.3-rc99", "1.2.4", False),
        ("1.2.4-rc1", "1.2.3", True),
        # SemVer 标识符区分大小写（ASCII 字典序）
        ("1.2.3-rc", "1.2.3-RC", True),
        ("1.2.3-RC", "1.2.3-rc", False),
        # 构建元数据（+build）不影响比较
        ("1.2.3+build.5", "1.2.3", False),
        ("1.2.4+build.5", "1.2.3", True),
        # 非法输入：远端非法一律不提示更新；本地非法沿用旧 (0,0,0) 语义
        ("garbage", "1.2.3-rc1", False),
        ("1.2.3-rc1", "garbage", True),
        ("garbage", "0.0.0-rc1", False),
        ("0.0.0-rc1", "garbage", False),
        ("0.0.0", "garbage", False),
        ("garbage", "garbage", False),
        ("1.2.3", "garbage", True),
        # 超长纯数字预发布标识不触发 int 转换位数上限，且仍按数值比较
        ("1.2.3-" + "9" * 5000, "1.2.3", False),
        ("1.2.4", "1.2.3-" + "9" * 5000, True),
        # 65 位 vs 66 位：位数多者数值大，不会因超长被降级成字符串比较
        ("1.2.3-" + "9" * 65, "1.2.3-" + "1" * 66, False),
        ("1.2.3-" + "1" * 66, "1.2.3-" + "9" * 65, True),
        # SemVer：任意长度的数字标识都低于字母数字标识
        ("1.2.3-" + "9" * 65, "1.2.3-1a", False),
        ("1.2.3-1a", "1.2.3-" + "9" * 65, True),
        # 严格 SemVer 解析：版本后携带垃圾字符视为无法解析的远端 → False
        ("1.2.4whatever", "1.2.3", False),
    ],
)
def test_is_newer_with_prerelease_suffix(
    remote: str, current: str, expected: bool
) -> None:
    assert updater.is_newer(remote, current) is expected


def test_parse_version_contract_unchanged_for_prerelease() -> None:
    """parse_version 只出数字段（canonical_version 依赖它做安全文件名）。"""
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("v1.2.3-rc1") == (1, 2, 3)
    assert updater.parse_version("1.2.3-../../evil") == (1, 2, 3)
    assert updater.parse_version("garbage") == (0, 0, 0)


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


def test_validated_download_dest_rejects_directory_escape(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """写盘前必须校验最终路径仍在下载目录内（防 ../ 逃逸）。"""
    monkeypatch.setattr(updater, "download_dir", lambda: tmp_path)

    inside = tmp_path / "LightCast-Setup-0.1.28.exe"
    assert updater._validated_download_dest(inside) == inside

    # 相对分量逃逸：resolve 后父目录不再是下载目录，必须拦截
    with pytest.raises(ValueError):
        updater._validated_download_dest(tmp_path / ".." / "outside.exe")
    # 绝对路径逃逸
    with pytest.raises(ValueError):
        updater._validated_download_dest(tmp_path.parent / "evil.exe")
    # 目录内的相对分量（resolve 后仍在下载目录内）合法
    ok = updater._validated_download_dest(tmp_path / "sub" / ".." / "inside.exe")
    assert ok == (tmp_path / "inside.exe").resolve()


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


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class _FakeGetContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """替身 aiohttp.ClientSession：check_for_update 的唯一网络接缝。"""

    def __init__(self, response: _FakeResponse, **_kwargs: object) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def get(self, *_args: object, **_kwargs: object) -> _FakeGetContext:
        return _FakeGetContext(self._response)


def _release_payload(tag: str) -> dict:
    return {
        "tag_name": tag,
        "body": "release notes",
        "published_at": "2026-09-18T00:00:00Z",
        "assets": [
            {
                "name": f"LightCast-Setup-{tag.lstrip('v')}.exe",
                "browser_download_url": "https://example.com/setup.exe",
            },
            {
                "name": f"LightCast-Portable-{tag.lstrip('v')}.zip",
                "browser_download_url": "https://example.com/portable.zip",
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://example.com/sums.txt",
            },
        ],
    }


def _run_check(monkeypatch, tag: str, local_version: str):  # noqa: ANN001, ANN202
    monkeypatch.setattr(
        updater.aiohttp, "ClientSession", lambda **kw: _FakeSession(_FakeResponse(_release_payload(tag)))
    )
    monkeypatch.setattr(updater, "__version__", local_version)
    return asyncio.run(updater.check_for_update())


def test_check_for_update_offers_stable_to_prerelease_user(monkeypatch) -> None:
    """M9 真实调用链：本地 0.1.31-rc1 + 远端 v0.1.31 → 提示更新。"""
    info = _run_check(monkeypatch, "v0.1.31", "0.1.31-rc1")
    assert info is not None
    assert info.tag == "v0.1.31"
    assert info.version == "0.1.31"


def test_check_for_update_ignores_older_or_equal_release(monkeypatch) -> None:
    assert _run_check(monkeypatch, "v0.1.30", "0.1.31-rc1") is None
    assert _run_check(monkeypatch, "v0.1.31", "0.1.31") is None


def test_check_for_update_rejects_unparseable_remote_tag(monkeypatch) -> None:
    """远端 tag 垃圾时必须显式失败，不能折叠成 0.0.0 走更新流。"""
    with pytest.raises(RuntimeError):
        _run_check(monkeypatch, "not-a-version", "0.1.31")
