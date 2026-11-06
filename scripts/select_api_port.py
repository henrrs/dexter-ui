#!/usr/bin/env python3
"""Select an available loopback port for Dexter's local API."""

from __future__ import annotations

import socket
import sys


def is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: select_api_port.py [preferred-port]", file=sys.stderr)
        return 2

    if len(sys.argv) == 2:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("DEXTER_PORT must be an integer between 1 and 65535.", file=sys.stderr)
            return 2
        if not 1 <= port <= 65535:
            print("DEXTER_PORT must be an integer between 1 and 65535.", file=sys.stderr)
            return 2
        if not is_available(port):
            print(f"DEXTER_PORT {port} is already in use.", file=sys.stderr)
            return 1
        print(port)
        return 0

    for port in range(8000, 8101):
        if is_available(port):
            print(port)
            return 0
    print("No free Dexter API port found in 8000-8100.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
