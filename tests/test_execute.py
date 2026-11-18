#!/usr/bin/env python3
"""Local end-to-end tests for scripts/execute.py.

Runs the real `scripts/execute.py` as a subprocess against a mock
`strawberry-server` built on Python's `http.server`. The mock copies the live
server's rules closely enough to catch a wrong request shape:

  PUT  /kv/<key>   needs `Authorization: Bearer <token>` and a JSON object with
                   a `value` field                        -> 201 / 400 / 401
  POST /logs       needs `source` and `event` (<=120 chars) -> 201 / 400 / 401
                   (the live server rejects a body without `event` with
                    400 invalid_field "event is required.")
  GET  /kv/<key>   read back what the runner stored

Also checks the public-log rule: nothing but task_id / mode / exit_code and no
payload, response body or token may ever appear on the runner's stdout/stderr.

Usage:
    python3 tests/test_execute.py            # all checks
    python3 tests/test_execute.py -v         # show each check's detail

Standard library only, no network access needed.
"""

import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "execute.py")

TOKEN = "test-token-2f9c8b7a"
MARKER = "MARKER_7f3a_should_never_be_public"

CHECKS = []          # filled by run(); (name, ok, detail)
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


# --------------------------------------------------------------- mock server


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, token=TOKEN):
        super().__init__(("127.0.0.1", 0), Handler)
        self.token = token
        self.kv = {}
        self.logs = []
        self.requests = []

    @property
    def base_url(self):
        host, port = self.server_address[:2]
        return "http://%s:%d" % (host, port)

    def reset(self):
        self.kv = {}
        self.logs = []
        self.requests = []

    def handle_error(self, request, client_address):
        # A client that gave up mid-response (the timeout cases) is expected here.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output clean
        pass

    # -- helpers
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, status, text):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        return (self.headers.get("Authorization") or "") == "Bearer " + self.server.token

    def _record(self, body):
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "body": body.decode("utf-8", "replace"),
            }
        )

    # -- routes
    def do_PUT(self):
        body = self._read_body()
        self._record(body)
        parsed = urllib.parse.urlsplit(self.path)
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if not parsed.path.startswith("/kv/"):
            return self._json(404, {"ok": False, "error": "not_found"})
        key = urllib.parse.unquote(parsed.path[len("/kv/"):])
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            return self._json(400, {"ok": False, "error": "invalid_json"})
        if not isinstance(payload, dict) or "value" not in payload:
            return self._json(400, {"ok": False, "error": "invalid_request"})
        created = key not in self.server.kv
        self.server.kv[key] = payload["value"]
        return self._json(200 if not created else 201, {"ok": True, "key": key, "created": created})

    def do_POST(self):
        body = self._read_body()
        self._record(body)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api":  # a third-party API being checked, not the store
            return self._json(200, {"ok": True, "body_len": len(body)})
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        if parsed.path == "/logs":
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                return self._json(400, {"ok": False, "error": "invalid_json"})
            if not isinstance(payload, dict):
                return self._json(400, {"ok": False, "error": "invalid_json"})
            source = payload.get("source")
            event = payload.get("event")
            if not isinstance(source, str) or not source.strip():
                return self._json(400, {"ok": False, "error": "invalid_field", "message": "source is required."})
            if not isinstance(event, str) or not event.strip():
                return self._json(400, {"ok": False, "error": "invalid_field", "message": "event is required."})
            if len(source) > 120 or len(event) > 120:
                return self._json(400, {"ok": False, "error": "invalid_field", "message": "too long"})
            self.server.logs.append(payload)
            return self._json(201, {"ok": True, "id": len(self.server.logs)})
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_GET(self):
        self._record(b"")
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/echo":
            return self._text(200, "hello from mock " + "x" * 40)
        if parsed.path == "/slow":
            time.sleep(3)
            return self._text(200, "slow")
        if parsed.path == "/missing":
            return self._text(404, "nope")
        if parsed.path == "/big":
            return self._text(200, "B" * 40000)
        if parsed.path.startswith("/kv/"):
            if not self._authorized():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            key = urllib.parse.unquote(parsed.path[len("/kv/"):])
            if key not in self.server.kv:
                return self._json(404, {"ok": False, "error": "not_found"})
            return self._json(200, {"ok": True, "key": key, "value": self.server.kv[key]})
        return self._json(404, {"ok": False, "error": "not_found"})


# ------------------------------------------------------------------ harness


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _base_env():
    env = {}
    for key, value in os.environ.items():
        if key in ("STRAWBERRY_URL", "STRAWBERRY_TOKEN", "TASK_ID", "MODE", "PAYLOAD_B64",
                   "RUNNER_HTTP_TIMEOUT", "RUNNER_TASK_TIMEOUT"):
            continue
        env[key] = value
    return env


def run_runner(server=None, task_id="t-1", mode="http_check", payload="", args=(),
               timeout=None, token=None):
    """Run scripts/execute.py in a subprocess; return (returncode, stdout, stderr, env)."""
    from subprocess import run as subproc_run

    env = _base_env()
    if server is not None:
        env["STRAWBERRY_URL"] = server.base_url
        env["STRAWBERRY_TOKEN"] = token if token is not None else TOKEN
    if task_id is not None:
        env["TASK_ID"] = task_id
    if mode is not None:
        env["MODE"] = mode
    if payload is not None:
        env["PAYLOAD_B64"] = b64encode(payload.encode("utf-8")).decode("ascii")
    if timeout is not None:
        env["RUNNER_HTTP_TIMEOUT"] = str(timeout[0])
        env["RUNNER_TASK_TIMEOUT"] = str(timeout[1])
    proc = subproc_run(
        [sys.executable, SCRIPT] + list(args),
        env=env, capture_output=True, timeout=120,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"), env


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), "" if condition else detail))


def _stdout_lines(stdout):
    return [line for line in stdout.splitlines() if line.strip()]


def _no_leak(stdout, stderr, extra=()):
    blob = stdout + "\n" + stderr
    for needle in (MARKER, TOKEN) + tuple(extra):
        if needle and needle in blob:
            return False
    return True


# ------------------------------------------------------------------- checks


def test_http_check_success(server):
    server.reset()
    payload = json.dumps({"url": server.base_url + "/echo", "headers": {"X-Trace": MARKER}})
    code, out, err, _ = run_runner(server, "check-ok", "http_check", payload)
    check("http_check: script exit 0", code == 0, "exit=%s stderr=%s" % (code, err))
    lines = _stdout_lines(out)
    check("http_check: stdout is exactly task_id/mode/exit_code",
          lines == ["task_id=check-ok", "mode=http_check", "exit_code=0"], lines)
    check("http_check: public logs carry no payload or token", _no_leak(out, err))
    stored = server.kv.get("runner:check-ok")
    check("http_check: PUT /kv/runner:<task_id> stored", isinstance(stored, dict), server.kv)
    if isinstance(stored, dict):
        check("http_check: stored stdout is the response body",
              "hello from mock" in stored.get("stdout", ""), stored.get("stdout"))
        check("http_check: stored fields complete",
              set(stored) >= {"task_id", "mode", "exit_code", "stdout", "stderr",
                              "duration_ms", "finished_at"},
              sorted(stored))
        check("http_check: http_status recorded", stored.get("http_status") == 200, stored.get("http_status"))
        check("http_check: finished_at is ISO-8601 UTC",
              isinstance(stored.get("finished_at"), str)
              and stored["finished_at"].endswith("Z")
              and len(stored["finished_at"]) == 24
              and stored["finished_at"][10] == "T",
              stored.get("finished_at"))
        check("http_check: duration_ms is a number",
              isinstance(stored.get("duration_ms"), int) and stored["duration_ms"] >= 0,
              stored.get("duration_ms"))
    check("http_check: one log line appended", len(server.logs) == 1, len(server.logs))
    if server.logs:
        log = server.logs[0]
        check("logs: source is github-runner", log.get("source") == "github-runner", log.get("source"))
        check("logs: event carries the level", log.get("event") == "info", log.get("event"))
        check("logs: message names task/mode/exit",
              log.get("message") == "task check-ok (http_check) exit=0", log.get("message"))
        meta = log.get("meta") or {}
        check("logs: meta has task_id/mode/exit_code",
              meta.get("task_id") == "check-ok" and meta.get("mode") == "http_check"
              and meta.get("exit_code") == 0, meta)
    echo = [r for r in server.requests if r["path"] == "/echo"]
    check("http_check: sends an identifiable User-Agent (not Python-urllib)",
          echo and "strawberry-runner" in (echo[0]["user_agent"] or ""), echo[:1])
    put = [r for r in server.requests if r["method"] == "PUT"]
    check("http_check: bearer token sent on PUT",
          put and put[0]["authorization"] == "Bearer " + TOKEN, put[:1])


def test_http_check_404(server):
    server.reset()
    code, out, err, _ = run_runner(server, "check-404", "http_check", server.base_url + "/missing")
    stored = server.kv.get("runner:check-404") or {}
    check("http_check 404: script exit 1", code == 1, code)
    check("http_check 404: stored exit_code 1", stored.get("exit_code") == 1, stored.get("exit_code"))
    check("http_check 404: http_status 404", stored.get("http_status") == 404, stored.get("http_status"))
    check("http_check 404: stdout carries the body", "nope" in stored.get("stdout", ""), stored.get("stdout"))
    check("http_check 404: log level is error",
          server.logs and server.logs[0].get("event") == "error", server.logs)
    check("http_check 404: exit_code printed after the summary lines",
          _stdout_lines(out)[-1] == "exit_code=1", _stdout_lines(out))


def test_http_check_network_error(server):
    server.reset()
    dead = "http://127.0.0.1:%d/never" % _closed_port()
    code, out, err, _ = run_runner(server, "check-dead", "http_check", dead)
    stored = server.kv.get("runner:check-dead") or {}
    check("http_check refused: script exit 1", code == 1, code)
    check("http_check refused: generic stderr only",
          "request failed" in stored.get("stderr", ""), stored.get("stderr"))
    check("http_check refused: target URL not stored",
          "127.0.0.1" not in stored.get("stderr", ""), stored.get("stderr"))


def test_http_check_timeout(server):
    server.reset()
    code, out, err, _ = run_runner(server, "check-slow", "http_check",
                                  server.base_url + "/slow", timeout=(1, 120))
    stored = server.kv.get("runner:check-slow") or {}
    check("http_check timeout: exit_code 1", stored.get("exit_code") == 1, stored.get("exit_code"))
    check("http_check timeout: timed_out flag set", stored.get("timed_out") is True, stored.get("timed_out"))


def test_http_check_post_json(server):
    server.reset()
    payload = json.dumps({"url": server.base_url + "/api", "method": "POST", "data": {"hello": MARKER}})
    code, out, err, _ = run_runner(server, "check-post", "http_check", payload)
    stored = server.kv.get("runner:check-post") or {}
    check("http_check POST: exit 0", code == 0 and stored.get("exit_code") == 0, (code, stored.get("exit_code")))
    check("http_check POST: body reached the mock", '"body_len"' in stored.get("stdout", ""), stored.get("stdout"))
    check("http_check POST: no leak of payload", _no_leak(out, err))


def test_http_check_bad_payload(server):
    server.reset()
    code, out, err, _ = run_runner(server, "check-bad", "http_check", "ftp://example.com/file")
    check("http_check bad scheme: refused with exit 2", code == 2, (code, err))
    check("http_check bad scheme: nothing sent to the server", server.requests == [], server.requests)

    server.reset()
    code, out, err, _ = run_runner(server, "check-bad2", "http_check", "{not json")
    check("http_check bad json: refused with exit 2", code == 2, (code, err))
    check("http_check bad json: nothing sent to the server", server.requests == [], server.requests)


def test_python_script(server):
    server.reset()
    script = "import sys\nprint('hello stdout')\nprint('hello stderr', file=sys.stderr)\n"
    code, out, err, _ = run_runner(server, "py-ok", "python_script", script)
    stored = server.kv.get("runner:py-ok") or {}
    check("python_script: script exit 0", code == 0, (code, err))
    check("python_script: stdout captured", stored.get("stdout", "").strip() == "hello stdout", stored.get("stdout"))
    check("python_script: stderr captured", stored.get("stderr", "").strip() == "hello stderr", stored.get("stderr"))
    check("python_script: task output not printed to the public log", "hello stdout" not in out + err)


def test_python_script_nonzero(server):
    server.reset()
    code, out, err, _ = run_runner(server, "py-exit7", "python_script", "raise SystemExit(7)\n")
    stored = server.kv.get("runner:py-exit7") or {}
    check("python_script: non-zero task exit propagates to the result",
          stored.get("exit_code") == 7, stored.get("exit_code"))
    check("python_script: non-zero task exit makes the job fail", code == 1, code)
    check("python_script: log level error", server.logs and server.logs[0].get("event") == "error", server.logs)


def test_python_script_timeout(server):
    server.reset()
    code, out, err, _ = run_runner(server, "py-slow", "python_script",
                                  "import time\ntime.sleep(10)\n", timeout=(20, 1))
    stored = server.kv.get("runner:py-slow") or {}
    check("python_script timeout: exit_code 124", stored.get("exit_code") == 124, stored.get("exit_code"))
    check("python_script timeout: timed_out flag", stored.get("timed_out") is True, stored.get("timed_out"))


def test_python_secret_env(server):
    server.reset()
    script = (
        "import os\n"
        "print('token=' + str(os.environ.get('STRAWBERRY_TOKEN')))\n"
        "print('url=' + str(os.environ.get('STRAWBERRY_URL')))\n"
    )
    code, out, err, _ = run_runner(server, "py-env", "python_script", script)
    stored = server.kv.get("runner:py-env") or {}
    body = stored.get("stdout", "")
    check("python_script: token hidden from the task process",
          "token=None" in body and TOKEN not in body, body)
    check("python_script: server URL hidden from the task process",
          "url=None" in body, body)


def test_shell_cmd(server):
    server.reset()
    code, out, err, _ = run_runner(server, "sh-ok", "shell_cmd",
                                  "echo shell-out; echo shell-err >&2; exit 3")
    stored = server.kv.get("runner:sh-ok") or {}
    check("shell_cmd: stdout captured", stored.get("stdout", "").strip() == "shell-out", stored.get("stdout"))
    check("shell_cmd: stderr captured", stored.get("stderr", "").strip() == "shell-err", stored.get("stderr"))
    check("shell_cmd: exit code 3 stored", stored.get("exit_code") == 3, stored.get("exit_code"))
    check("shell_cmd: task output not printed publicly", "shell-out" not in out + err)

    server.reset()
    code, out, err, _ = run_runner(server, "sh-env", "shell_cmd",
                                  'echo "tok=${STRAWBERRY_TOKEN:-none}"')
    stored = server.kv.get("runner:sh-env") or {}
    check("shell_cmd: token hidden from bash", "tok=none" in stored.get("stdout", ""), stored.get("stdout"))


def test_shell_timeout(server):
    server.reset()
    code, out, err, _ = run_runner(server, "sh-slow", "shell_cmd", "sleep 10", timeout=(20, 1))
    stored = server.kv.get("runner:sh-slow") or {}
    check("shell_cmd timeout: exit_code 124", stored.get("exit_code") == 124, stored.get("exit_code"))


def test_truncation(server):
    server.reset()
    code, out, err, _ = run_runner(server, "trunc", "python_script",
                                  "print('A' * 40000)\nimport sys\nprint('B' * 40000, file=sys.stderr)\n")
    stored = server.kv.get("runner:trunc") or {}
    check("truncation: stdout clipped to 16KB", len(stored.get("stdout", "")) == 16 * 1024,
          len(stored.get("stdout", "")))
    check("truncation: stderr clipped to 8KB", len(stored.get("stderr", "")) == 8 * 1024,
          len(stored.get("stderr", "")))
    check("truncation: flags recorded",
          stored.get("truncated") == {"stdout": True, "stderr": True}, stored.get("truncated"))

    server.reset()
    code, out, err, _ = run_runner(server, "big-http", "http_check", server.base_url + "/big")
    stored = server.kv.get("runner:big-http") or {}
    check("truncation: http body clipped to 16KB", len(stored.get("stdout", "")) == 16 * 1024,
          len(stored.get("stdout", "")))


def test_bad_input(server):
    server.reset()
    code, out, err, _ = run_runner(server, "bad id/with slash", "http_check", "https://example.com")
    check("bad task_id (slash): refused with exit 2", code == 2, (code, err))
    check("bad task_id: nothing sent", server.requests == [], server.requests)

    server.reset()
    code, out, err, _ = run_runner(server, "x" * 121, "http_check", "https://example.com")
    check("bad task_id (>120 chars): refused with exit 2", code == 2, (code, err))

    server.reset()
    code, out, err, _ = run_runner(server, "bad-mode", "rm -rf", "x")
    check("bad mode: refused with exit 2", code == 2, (code, err))
    check("bad mode: nothing sent", server.requests == [], server.requests)

    server.reset()
    code, out, err, env = run_runner(server, "bad-b64", "http_check", None)
    env["PAYLOAD_B64"] = "!!!not-base64!!!"
    from subprocess import run as subproc_run
    proc = subproc_run([sys.executable, SCRIPT], env=env, capture_output=True, timeout=60)
    check("bad base64: refused with exit 2", proc.returncode == 2, proc.returncode)
    check("bad base64: nothing sent", server.requests == [], server.requests)

    server.reset()
    code, out, err, _ = run_runner(server, "empty-payload", "http_check", "")
    check("empty payload: refused with exit 2", code == 2, (code, err))

    server.reset()
    big = json.dumps({"url": "https://example.com", "pad": "x" * (70 * 1024)})
    code, out, err, _ = run_runner(server, "huge-payload", "http_check", big)
    check("payload over 64KB: refused with exit 2", code == 2, (code, err))


def test_delivery_failure(server):
    server.reset()
    code, out, err, _ = run_runner(server, "auth-fail", "shell_cmd", "echo " + MARKER, token="wrong-token")
    check("delivery 401: script exit 3", code == 3, (code, err))
    check("delivery 401: stderr names HTTP 401", "delivery_failed" in err and "401" in err, err)
    check("delivery 401: no payload leak", MARKER not in out + err, out + err)
    check("delivery 401: still prints the three summary lines",
          _stdout_lines(out)[:3] == ["task_id=auth-fail", "mode=shell_cmd", "exit_code=0"], _stdout_lines(out))

    server.reset()
    dead_env = _base_env()
    dead_env.update({
        "STRAWBERRY_URL": "http://127.0.0.1:%d" % _closed_port(),
        "STRAWBERRY_TOKEN": TOKEN,
        "TASK_ID": "dead-server",
        "MODE": "shell_cmd",
        "PAYLOAD_B64": b64encode(b"echo " + MARKER.encode()).decode(),
    })
    from subprocess import run as subproc_run
    started = time.time()
    proc = subproc_run([sys.executable, SCRIPT], env=dead_env, capture_output=True, timeout=120)
    elapsed = time.time() - started
    err = proc.stderr.decode("utf-8", "replace")
    check("delivery unreachable: script exit 3", proc.returncode == 3, proc.returncode)
    check("delivery unreachable: retried before giving up", elapsed >= 1.5, "%.2fs" % elapsed)
    check("delivery unreachable: no payload leak", MARKER not in err, err)


def test_dry_run(server):
    server.reset()
    payload = "print('%s')\n" % MARKER
    code, out, err, _ = run_runner(None, "dry-1", "python_script", payload, args=("--dry-run",))
    check("dry-run: exit 0 without server config", code == 0, (code, err))
    lines = _stdout_lines(out)
    check("dry-run: three summary lines still printed",
          lines[:3] == ["task_id=dry-1", "mode=python_script", "exit_code=0"], lines)
    check("dry-run: redacted summary present",
          any(line.startswith("dry-run: stdout=") for line in lines), lines)
    check("dry-run: says delivery was skipped",
          any("delivery skipped" in line for line in lines), lines)
    check("dry-run: no payload or token in output", _no_leak(out, err), out + err)
    check("dry-run: no request to any server", server.requests == [], server.requests)


def test_workflow_file():
    path = os.path.join(ROOT, ".github", "workflows", "run-task.yml")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    check("workflow: exists", os.path.exists(path))
    active = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    check("workflow: only workflow_dispatch trigger",
          "\n  workflow_dispatch:" in active
          and not any(("\n  " + bad) in active for bad in
                      ("pull_request:", "issues:", "push:", "repository_dispatch:",
                       "schedule:", "workflow_call:", "release:")),
          active.split("on:", 1)[-1][:200])
    check("workflow: permissions contents: read", "permissions:\n  contents: read" in text, "")
    check("workflow: three modes offered",
          all(m in text for m in ("http_check", "python_script", "shell_cmd")), "")
    check("workflow: secrets passed as env",
          "STRAWBERRY_URL: ${{ secrets.STRAWBERRY_URL }}" in text
          and "STRAWBERRY_TOKEN: ${{ secrets.STRAWBERRY_TOKEN }}" in text, "")
    check("workflow: payload passed through env, not inline interpolation",
          "PAYLOAD_B64: ${{ inputs.payload_b64 }}" in text
          and "run: python3 scripts/execute.py" in text, "")
    check("workflow: no echo/set -x of inputs",
          "echo ${{" not in text and "set -x" not in text, "")


def main():
    server = MockServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tests = [
        test_http_check_success,
        test_http_check_404,
        test_http_check_network_error,
        test_http_check_timeout,
        test_http_check_post_json,
        test_http_check_bad_payload,
        test_python_script,
        test_python_script_nonzero,
        test_python_script_timeout,
        test_python_secret_env,
        test_shell_cmd,
        test_shell_timeout,
        test_truncation,
        test_bad_input,
        test_delivery_failure,
        test_dry_run,
    ]
    try:
        for func in tests:
            start = len(CHECKS)
            try:
                func(server)
            except Exception:
                check(func.__name__ + " raised", False, traceback.format_exc(limit=3))
            for name, ok, detail in CHECKS[start:]:
                print("%s %s" % ("ok  " if ok else "FAIL", name))
                if not ok and detail:
                    print("      -> %s" % str(detail)[:400])
        test_workflow_file()
        for name, ok, detail in CHECKS[-7:]:
            print("%s %s" % ("ok  " if ok else "FAIL", name))
            if not ok and detail:
                print("      -> %s" % str(detail)[:400])
    finally:
        server.shutdown()
        server.server_close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print("\n%d/%d checks passed, %d failed" % (passed, len(CHECKS), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
