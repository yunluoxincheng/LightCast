from __future__ import annotations

import asyncio

import pytest

from ydlna.player._url_guard import (
    SSRFBlockedError,
    SSRFSafeConnector,
    UrlBlockedError,
    _filter_resolved_hosts,
    make_session,
    safe_get,
    validate_upstream_url,
)


@pytest.mark.parametrize("scheme", ["file", "ftp", "data", "edl", "javascript"])
def test_only_http_and_https_are_allowed(scheme: str) -> None:
    with pytest.raises(UrlBlockedError):
        validate_upstream_url(f"{scheme}://example.com/media")


@pytest.mark.parametrize(
    "url",
    ["http://[::1", "http:///missing-host", "http://", "http://example.com:99999"],
)
def test_malformed_urls_are_reported_as_policy_errors(url: str) -> None:
    with pytest.raises(UrlBlockedError):
        validate_upstream_url(url)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "[::1]",
        "169.254.169.254",
        "[fe80::1]",
        "0.0.0.0",
        "[::]",
        "224.0.0.1",
        "[ff02::1]",
        "[::ffff:127.0.0.1]",
        "100.64.0.1",
        "198.18.0.1",
    ],
)
def test_always_blocked_literal_addresses(host: str) -> None:
    with pytest.raises(UrlBlockedError):
        validate_upstream_url(f"http://{host}/media", allow_intranet=True)


@pytest.mark.parametrize("host", ["10.0.0.8", "172.16.0.8", "192.168.1.8", "[fd00::8]"])
def test_private_literals_follow_intranet_setting(host: str) -> None:
    url = f"http://{host}/media"
    assert validate_upstream_url(url, allow_intranet=True) == url
    with pytest.raises(UrlBlockedError):
        validate_upstream_url(url, allow_intranet=False)


def test_resolved_candidates_are_filtered_before_connect() -> None:
    hosts = [
        {"host": "127.0.0.1", "port": 80},
        {"host": "93.184.216.34", "port": 80},
    ]
    assert _filter_resolved_hosts(
        "example.test", hosts, allow_intranet=False
    ) == [hosts[1]]

    private = [{"host": "192.168.1.20", "port": 8080}]
    assert _filter_resolved_hosts(
        "phone.local", private, allow_intranet=True
    ) == private
    with pytest.raises(SSRFBlockedError):
        _filter_resolved_hosts(
            "phone.local", private, allow_intranet=False
        )

    with pytest.raises(SSRFBlockedError):
        _filter_resolved_hosts(
            "blocked.test", [hosts[0]], allow_intranet=True
        )


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:1/secret", "http://localhost:1/secret"]
)
def test_connector_blocks_resolved_target_before_tcp_connect(url: str) -> None:
    async def scenario() -> None:
        async with make_session(allow_intranet=True) as session:
            with pytest.raises(UrlBlockedError):
                await safe_get(session, url)

    asyncio.run(scenario())


class _FakeResponse:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"Location": location} if location is not None else {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.request_headers: list[dict | None] = []

    async def get(self, url: str, **kwargs):  # noqa: ANN003, ANN201
        self.urls.append(url)
        headers = kwargs.get("headers")
        self.request_headers.append(dict(headers) if headers is not None else None)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_redirect_target_is_validated_before_second_request() -> None:
    first = _FakeResponse(302, "http://127.0.0.1/admin")
    session = _FakeSession([first])

    async def scenario() -> None:
        with pytest.raises(UrlBlockedError):
            await safe_get(session, "https://public.example/media")  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert first.closed
    assert session.urls == ["https://public.example/media"]


def test_connector_policy_error_is_not_downgraded_to_network_failure() -> None:
    session = _FakeSession(
        [SSRFBlockedError("rebinding.example", "解析结果指向回环地址")]
    )

    async def scenario() -> None:
        with pytest.raises(UrlBlockedError, match="回环"):
            await safe_get(session, "https://rebinding.example/media")  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_cross_origin_redirect_strips_sensitive_headers() -> None:
    session = _FakeSession(
        [
            _FakeResponse(302, "https://cdn.example/video"),
            _FakeResponse(200),
        ]
    )

    async def scenario() -> None:
        response = await safe_get(  # type: ignore[arg-type]
            session,
            "https://origin.example/video",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "Range": "bytes=0-1023",
            },
        )
        assert response.status == 200

    asyncio.run(scenario())
    assert session.request_headers[0] == {
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "Range": "bytes=0-1023",
    }
    assert session.request_headers[1] == {"Range": "bytes=0-1023"}


def test_connector_does_not_replace_resolver_method_per_request() -> None:
    async def scenario() -> None:
        connector = SSRFSafeConnector(allow_intranet=True)
        try:
            assert "_resolve_host" not in connector.__dict__
        finally:
            await connector.close()

    asyncio.run(scenario())
