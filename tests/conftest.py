"""Session-wide safety guard: tests run without AWS variables or live sockets."""
from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(scope="session", autouse=True)
def no_aws_credentials_or_network():
    """Remove AWS env settings and prohibit socket creation/connection in tests."""
    saved_aws = {key: value for key, value in os.environ.items() if key.startswith("AWS_")}
    for key in saved_aws:
        os.environ.pop(key, None)
    original_socket = socket.socket
    original_create_connection = socket.create_connection

    def is_loopback(address):
        host = address[0] if isinstance(address, tuple) and address else ""
        return host in {"127.0.0.1", "::1", "localhost"}

    class OfflineSocket(original_socket):
        """Permit local event-loop sockets while prohibiting external connections."""
        def connect(self, *args, **kwargs):
            if args and is_loopback(args[0]):
                return super().connect(*args, **kwargs)
            raise AssertionError("external network connections are disabled in the test suite")

        def connect_ex(self, *args, **kwargs):
            if args and is_loopback(args[0]):
                return super().connect_ex(*args, **kwargs)
            raise AssertionError("external network connections are disabled in the test suite")

    def blocked_connection(*args, **kwargs):
        address = args[0] if args else kwargs.get("address")
        if is_loopback(address):
            return original_create_connection(*args, **kwargs)
        raise AssertionError("external network connections are disabled in the test suite")

    socket.socket = OfflineSocket
    socket.create_connection = blocked_connection
    try:
        yield
    finally:
        socket.socket = original_socket
        socket.create_connection = original_create_connection
        for key in [key for key in os.environ if key.startswith("AWS_")]:
            os.environ.pop(key, None)
        os.environ.update(saved_aws)
