import pytest


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    """CI/unit tests must never consume a developer's keys or contact a provider."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in unit tests")

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    for name in ("OPENAI_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY"):
        # Empty (not deleted) so a developer's populated .env cannot re-supply it via load_dotenv.
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
