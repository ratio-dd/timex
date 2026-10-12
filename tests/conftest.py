import os
import socket

import pytest
os.environ['MODEL_PROVIDER'] = 'fake'
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Even live-provider tests use in-memory HTTP; never call a paid API."""
    def blocked(*args, **kwargs):
        raise AssertionError('Tests must not open network connections')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
