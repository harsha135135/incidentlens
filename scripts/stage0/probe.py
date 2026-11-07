"""Stage 0 probe that runs inside the OpenShell sandbox. Standard library only.

Each check prints one JSON line with what was attempted, what happened and what the policy
requires. A check passes when the observed result matches the expectation; the smoke script
reads these lines and fails if any check does not pass.
"""

import json
import os
import socket
import subprocess
import urllib.error
import urllib.request

GATEWAY_HOST = os.environ.get("STAGE0_GATEWAY_HOST", "host.openshell.internal")
TOKEN = os.environ.get("STAGE0_CAPABILITY", "")


def http_get(url, token=None, timeout=5):
    """Returns (status, detail). status is None when no HTTP response arrived."""
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(200).decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(200).decode(errors="replace")
    except (urllib.error.URLError, OSError) as error:
        return None, f"{type(error).__name__}: {error}"


def tcp_connect(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except OSError as error:
        return False, f"{type(error).__name__}: {error}"


def report(check, expectation, passed, detail):
    print(json.dumps({"check": check, "expect": expectation, "pass": passed, "detail": detail}), flush=True)


def main():
    ping = f"http://{GATEWAY_HOST}:8089/internal/v1/ping"

    status, detail = http_get(ping, TOKEN)
    report("gateway_valid_capability", "HTTP 200 from 8089", status == 200, f"status={status} {detail}")

    status, detail = http_get(ping, "not-the-capability")
    report("gateway_invalid_capability", "HTTP 401 from 8089", status == 401, f"status={status} {detail}")

    # The proxy may answer a denied request itself (for example 403) instead of resetting the
    # connection. A denial is any result that is not a response from the target listener.
    status, detail = http_get(f"http://{GATEWAY_HOST}:8088/internal/v1/ping", TOKEN)
    reached = status is not None and '"listener"' in detail
    report("public_port_8088_denied", "no response from the 8088 listener", not reached, f"status={status} {detail}")

    status, detail = http_get("https://example.com/")
    reached = status is not None and "Example Domain" in detail
    report("internet_denied", "example.com unreachable", not reached, f"status={status} {detail}")

    for port in (5432, 55432, 55433, 17670):
        connected, detail = tcp_connect(GATEWAY_HOST, port)
        report(f"tcp_{port}_denied", "TCP connect refused or blocked", not connected, detail)

    # OpenShell matches the connecting binary *or any parent process*, so a child of this
    # allowed python3 inherits its network permission. Recorded here as an observation;
    # the separate check that bash alone is denied runs outside python3 (see the smoke script).
    result = subprocess.run(
        ["bash", "-c", f"exec 3<>/dev/tcp/{GATEWAY_HOST}/8089 && echo connected"],
        capture_output=True, text=True, timeout=10,
    )
    inherited = "connected" in result.stdout
    print(json.dumps({"observation": "child_of_allowed_binary_inherits_access", "value": inherited}), flush=True)

    exposed = [path for path in ("/Users", "/var/run/docker.sock", "/root/.ssh", "/home/ubuntu/.ssh")
               if os.path.exists(path)]
    report("host_paths_absent", "no host home, Docker socket or SSH keys", not exposed, f"present={exposed}")

    leaked = sorted(name for name in os.environ
                    if any(word in name for word in ("API_KEY", "SECRET", "PASSWORD", "AWS_", "ANTHROPIC")))
    report("no_secret_env", "no credential-like environment variables", not leaked, f"names={leaked}")


if __name__ == "__main__":
    main()
