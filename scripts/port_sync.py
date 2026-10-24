#!/usr/bin/env python3
"""Keep qBittorrent on the VPN's current forwarded port, with real authentication.

This sidecar shares Gluetun's network namespace. It neither controls the VPN nor
changes torrent content, categories, limits or authorization settings.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


READY = Path("/tmp/samurai-port-sync.ready")
PORT = Path("/gluetun/forwarded_port")
BASE = "http://127.0.0.1:8081"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Local API redirect refused")


class Client:
    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                  NoRedirect())

    def call(self, path: str, *, form=None):
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        headers = {"Referer": BASE + "/", "Origin": BASE}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(BASE + "/api/v2/" + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        with self.opener.open(req, timeout=5) as response:
            body = response.read(4 * 1024 * 1024)
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return body.decode("utf-8")

    def vpn_healthy(self):
        # The health server is local to this exact namespace and is unpublished.
        with self.opener.open("http://127.0.0.1:9999/", timeout=5) as response:
            response.read(128)
            return response.status == 200

    def login(self):
        response = self.call("auth/login", form={"username": self.username, "password": self.password})
        if response not in (None, "", "Ok."):
            raise RuntimeError("Authentication failed")

    def preferences(self):
        result = self.call("app/preferences")
        if not isinstance(result, dict):
            raise RuntimeError("Invalid preference response")
        return result

    def set_preferences(self, values):
        self.call("app/setPreferences", form={"json": json.dumps(values)})


def forwarded_port(path=PORT):
    try:
        if path.is_symlink():
            return None
        with path.open("r", encoding="ascii") as stream:
            value = stream.read(17).strip()
        if not value.isdecimal() or len(value) > 5:
            return None
        port = int(value)
        return port if 1 <= port <= 65535 else None
    except (OSError, UnicodeError, ValueError):
        return None


def ready_file(path=READY):
    try:
        age = time.time() - path.stat().st_mtime
        # A freshly written NTFS/test-clock mtime can round slightly ahead of
        # wall time. Accept at most one second of skew; real future/stale files
        # must still fail rather than provide a permanent healthy heartbeat.
        return -1 <= age < 60 and path.read_text(encoding="ascii").strip() == "synchronized"
    except OSError:
        return False


def clear_ready(path=READY):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def synchronize(client, *, port_path=PORT, ready_path=READY):
    """One bounded poll. On VPN/port loss bind loopback, never another NIC."""
    clear_ready(ready_path)
    try:
        healthy = client.vpn_healthy()
    except Exception:
        healthy = False
    port = forwarded_port(port_path) if healthy else None
    client.login()
    prefs = client.preferences()
    if port is None:
        if prefs.get("current_network_interface") != "lo":
            client.set_preferences({"current_network_interface": "lo"})
        if client.preferences().get("current_network_interface") != "lo":
            raise RuntimeError("Loopback hold could not be confirmed")
        return "holding"
    expected = {"current_network_interface": "tun0", "listen_port": port}
    if any(prefs.get(key) != value for key, value in expected.items()):
        client.set_preferences(expected)
    saved = client.preferences()
    if any(saved.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Port synchronization readback failed")
    ready_path.write_text("synchronized\n", encoding="ascii")
    ready_path.chmod(0o600)
    return "synchronized"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if ready_file() else 1
    try:
        username = Path("/run/secrets/qbit_username").read_text(encoding="utf-8").rstrip("\r\n")
        password = Path("/run/secrets/qbit_password").read_text(encoding="utf-8").rstrip("\r\n")
        if not username or not password:
            raise ValueError()
    except (OSError, ValueError):
        print("port-sync: private authentication files unavailable", file=sys.stderr, flush=True)
        return 1
    client = Client(username, password)
    previous = None
    while True:
        try:
            status = synchronize(client)
        except Exception:
            clear_ready()
            status = "unavailable"
        if status != previous:
            print("port-sync: " + status, flush=True)
            previous = status
        if args.once:
            return 0 if status == "synchronized" else 1
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
