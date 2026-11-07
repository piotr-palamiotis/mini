"""pytest plugin for tool tests (loaded with -p mini_pytest): every test runs inside its own tmp_path, the way
the harness runs a tool inside its working folder, so relative input and output paths mean the same in both."""
import pytest


@pytest.fixture(autouse=True)
def _mini_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
