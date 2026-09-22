import pytest


@pytest.fixture(autouse=True)
def isolated_run_outputs(monkeypatch, tmp_path_factory):
    """테스트 실행이 실제 outputs/local 에 결과 폴더를 만들지 않게 한다.

    개발자가 산출물 폴더를 열었을 때 테스트가 만든 mock 실행과 진짜 실행이 섞이면
    어느 것이 평가 결과인지 구분할 수 없다.
    """
    monkeypatch.setenv("RUN_OUTPUT_DIR", str(tmp_path_factory.mktemp("runs")))


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    """CI/unit tests must never consume a developer's keys or contact a provider."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in unit tests")

    import runtime.settings

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    for name in ("OPENAI_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # load_settings()가 개발자의 로컬 .env를 다시 읽어 위에서 지운 키를 되살리지 않게 한다.
    monkeypatch.setattr(runtime.settings, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
