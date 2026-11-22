"""In-process, read-only Docker Engine API gateway.

This module replaces the external read-only `socket-proxy` sidecar. It
speaks the Docker Engine API directly over the mounted (read-only)
/var/run/docker.sock, but only ever issues the hard-coded read endpoints
below — the same allow-list the socket-proxy container enforced — so the
monitor can list containers and read stats but cannot create, start,
stop, kill, exec, build, or otherwise mutate anything on the host daemon.

The Engine API is plain HTTP/1.1 over a unix socket, so the transport is
a few dozen lines of stdlib socket code; no third-party client (docker-py,
requests) is required. Unversioned paths are used, which the daemon
accepts by defaulting to its own API version, so nothing is pinned to a
specific daemon version.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
from urllib.parse import quote, urlencode

# The Engine API endpoint. The shipped compose file mounts the host socket
# here, read-only. Hard-coded (no env var): the monitor only ever reads.
SOCKET_PATH = "/var/run/docker.sock"

# Per-request timeout in seconds (matches the socket-proxy client timeout).
_TIMEOUT = 10

# Hard-coded read-only allow-list (GET only). Anything not matching one of
# these is rejected before a single byte is sent to the daemon.
#
# Image refs accept the Docker shapes: name[:tag], domain[:port]/name[/path...]
# [:tag], with an optional @sha256:digest, or a bare sha256 digest.
_IMG_REF = (
    r"sha256:[0-9a-f]{64}"
    r"|"
    r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?"
)
_ALLOWED = (
    re.compile(r"^/_ping$"),
    re.compile(r"^/version$"),
    re.compile(r"^/containers/json$"),
    re.compile(r"^/containers/[0-9a-f]+/json$"),
    re.compile(r"^/containers/[0-9a-f]+/stats$"),
    re.compile(r"^/images/(?:%s)/json$" % _IMG_REF),
)


class GatewayError(Exception):
    """Raised when the daemon is unreachable or an endpoint is disallowed."""


def _is_allowed(path: str) -> bool:
    return any(pattern.match(path) for pattern in _ALLOWED)


def _decode_chunked(data: bytes) -> bytes:
    """Decode an HTTP/1.1 `Transfer-Encoding: chunked` body.

    The Engine daemon returns list/stats responses chunked (a single
    `<size>\\r\\n<data>\\r\\n0\\r\\n\\r\\n` frame); strip the framing so the
    caller gets the bare payload.
    """
    out = bytearray()
    i = 0
    total = len(data)
    while i < total:
        line_end = data.index(b"\r\n", i)
        size = int(data[i:line_end].split(b";", 1)[0].strip(), 16)
        i = line_end + 2
        if size == 0:
            break  # terminating chunk; ignore any trailer headers
        out += data[i : i + size]
        i += size + 2  # skip the data and its trailing CRLF
    return bytes(out)


def _http_get(path: str, params: dict | None = None) -> bytes:
    """Issue a GET over the unix socket and return the response body.

    Raises GatewayError for a disallowed path, a connection/timeout
    failure, or a non-2xx daemon status.
    """
    if not _is_allowed(path):
        raise GatewayError(f"endpoint not allowed by read-only gateway: {path}")
    if params:
        path = f"{path}?{urlencode(params)}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_TIMEOUT)
        sock.connect(SOCKET_PATH)
        sock.sendall(request)
        raw = b""
        while True:  # Connection: close, so the body ends at EOF
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    except (OSError, socket.error) as exc:
        raise GatewayError(f"cannot reach Docker daemon: {exc}") from exc
    finally:
        sock.close()

    head_blob, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise GatewayError(f"malformed daemon response: {raw[:200]!r}")
    status_line = head_blob.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) != 3:
        raise GatewayError(f"bad daemon status line: {status_line!r}")
    status = int(parts[1])

    headers = {}
    for line in head_blob.split(b"\r\n")[1:]:
        key, colon, value = line.partition(b":")
        if colon:
            headers[key.decode("ascii", "replace").strip().lower()] = (
                value.decode("ascii", "replace").strip()
            )

    try:
        if headers.get("transfer-encoding") == "chunked":
            body = _decode_chunked(body)
        else:
            length = headers.get("content-length")
            if length:
                body = body[: int(length)]
    except (ValueError, IndexError) as exc:
        raise GatewayError(f"malformed daemon response body: {exc}") from exc
    if headers.get("content-encoding") == "gzip" and body:
        try:
            body = gzip.decompress(body)
        except OSError:
            pass

    if not 200 <= status < 300:
        raise GatewayError(f"daemon returned HTTP {status} for {path}: {body[:200]!r}")
    return body


def ping() -> bool:
    """Return True if the daemon answers /_ping."""
    _http_get("/_ping")
    return True


def list_containers() -> list[dict]:
    """All containers (the `docker ps` rows), newest first."""
    return json.loads(_http_get("/containers/json", {"all": "true"}))


def inspect_container(container_id: str) -> dict:
    """Full container inspect (State dict incl. ExitCode, Config, …)."""
    return json.loads(_http_get(f"/containers/{container_id}/json"))


def container_stats(container_id: str) -> dict:
    """One non-streaming stats sample (precpu_stats + cpu_stats + memory)."""
    return json.loads(
        _http_get(f"/containers/{container_id}/stats", {"stream": "false"})
    )


def inspect_image(ref: str) -> dict:
    """Image inspect for a name:tag / name@digest / sha256 ref."""
    # Preserve / : @ so registry paths keep their slashes in the URL path.
    return json.loads(_http_get(f"/images/{quote(ref, safe='/:@')}/json"))
