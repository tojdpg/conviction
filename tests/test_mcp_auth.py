"""MCP client authentication for Conviction's local HTTP bridge."""

from pathlib import Path

import mcp_server


class _Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload or {}
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _RecordingClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.requests.append(("get", url, kwargs))
        return _Response({"ok": True}, "summary")

    def post(self, url, **kwargs):
        self.requests.append(("post", url, kwargs))
        return _Response({"ok": True})


def test_missing_auth_file_keeps_client_unauthenticated(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVICTION_AUTH_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setattr(mcp_server.httpx, "Client", _RecordingClient)
    _RecordingClient.instances.clear()

    with mcp_server._client():
        pass

    assert _RecordingClient.instances[0].kwargs == {"timeout": 30}


def test_auth_file_passes_basic_credentials_to_client(monkeypatch, tmp_path):
    auth_file = Path(tmp_path) / "conviction.env"
    auth_file.write_text(
        "# private runtime credentials\n"
        "CONVICTION_AUTH_USERNAME=discord-agent\n"
        "CONVICTION_AUTH_PASSWORD=private-password\n"
    )
    monkeypatch.setenv("CONVICTION_AUTH_FILE", str(auth_file))
    monkeypatch.setattr(mcp_server.httpx, "Client", _RecordingClient)
    _RecordingClient.instances.clear()

    with mcp_server._client():
        pass

    assert _RecordingClient.instances[0].kwargs == {
        "timeout": 30,
        "auth": ("discord-agent", "private-password"),
    }


def test_get_config_and_post_thesis_use_central_authenticated_client(monkeypatch, tmp_path):
    auth_file = Path(tmp_path) / "conviction.env"
    auth_file.write_text(
        "CONVICTION_AUTH_USERNAME=discord-agent\n"
        "CONVICTION_AUTH_PASSWORD=private-password\n"
    )
    monkeypatch.setenv("CONVICTION_AUTH_FILE", str(auth_file))
    monkeypatch.setattr(mcp_server.httpx, "Client", _RecordingClient)
    _RecordingClient.instances.clear()

    assert mcp_server.get_config() == {"ok": True}
    assert mcp_server.post_thesis("NVDA", "wait", "Needs a pullback.", "wu") == {"ok": True}

    assert [client.kwargs["auth"] for client in _RecordingClient.instances] == [
        ("discord-agent", "private-password"),
        ("discord-agent", "private-password"),
    ]
    assert _RecordingClient.instances[0].requests == [
        ("get", f"{mcp_server.BASE_URL}/api/config", {"params": {}}),
    ]
    assert _RecordingClient.instances[1].requests == [
        ("post", f"{mcp_server.BASE_URL}/api/thesis", {
            "json": {
                "ticker": "NVDA",
                "verdict": "wait",
                "rationale": "Needs a pullback.",
                "author": "wu",
            },
        }),
    ]
