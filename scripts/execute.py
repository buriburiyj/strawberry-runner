#!/usr/bin/env python3
"""strawberry-runner - scripts/execute.py

Runs exactly one dispatched task inside the **public** GitHub Actions runner of
`buriburiyj/strawberry-runner` and stores the outcome in `strawberry-server`.

Public-log rule (the whole reason this file exists)
---------------------------------------------------
Anyone on the internet can read this repository's Actions logs. So this script
prints *nothing* but `task_id`, `mode` and `exit_code`:

  * the decoded payload (URL / script / command) is never printed,
  * the result body (stdout/stderr of the task, HTTP response body) is never
    printed - it goes straight to `strawberry-server` over HTTPS,
  * `STRAWBERRY_TOKEN` is never printed, and is also stripped from the
    environment handed to the task process,
  * exception text from urllib is dropped (it can embed the target URL), only
    the exception class name and HTTP status are reported.

Environment
-----------
  TASK_ID              required  id of the task, 1..120 chars of [A-Za-z0-9._:-]
  MODE                 required  http_check | python_script | shell_cmd
  PAYLOAD_B64          required  base64 of the UTF-8 payload (max 64KB decoded)
  STRAWBERRY_URL       required  base URL, e.g. https://strawberry-server.example.workers.dev
  STRAWBERRY_TOKEN     required  bearer token of strawberry-server
  RUNNER_HTTP_TIMEOUT  optional  seconds for http_check (default 20)
  RUNNER_TASK_TIMEOUT  optional  seconds for python_script / shell_cmd (default 120)

Local flags (for testing on your own machine)
---------------------------------------------
  --dry-run         run the task and print a length-only summary; no server call,
                    so STRAWBERRY_URL / STRAWBERRY_TOKEN are not needed
  --task-id ID      override TASK_ID
  --mode MODE       override MODE
  --payload-b64 B   override PAYLOAD_B64

Exit codes of this script
-------------------------
  0  task ran with exit_code 0 and the result reached strawberry-server
  1  task ran (result stored) but the task itself exited non-zero
  2  bad input or configuration - nothing ran, nothing was sent
  3  the result could not be stored (PUT /kv or POST /logs failed)
  4  unexpected internal error

Payload shapes
--------------
  http_check     JSON {"url": "...", "method": "GET", "headers": {...}, "data": "..."}
                 or a plain URL string. Timeout 20s (RUNNER_HTTP_TIMEOUT).
  python_script  UTF-8 Python source, run with the runner's python3 in a temp
                 directory, 120s limit (RUNNER_TASK_TIMEOUT).
  shell_cmd      UTF-8 shell script, run with `bash -c`, 120s limit.

Stored result (PUT /kv/runner:<task_id>, body {"value": <result>})
-----------------------------------------------------------------
  {"task_id", "mode", "exit_code", "stdout" (<=16KB), "stderr" (<=8KB),
   "duration_ms", "finished_at", "http_status", "truncated", "timed_out"}

Log line (POST /logs): {"source": "github-runner", "event": "info"|"error",
  "level", "message", "meta": {...}, "payload": {...}}
  strawberry-server requires `source` and `event`; `event` carries the level so
  that GET /logs?source=github-runner can be read quickly. The plan's original
  {level, message, meta} body is rejected by the server (400 "event is
  required"), so the same values are also sent as top-level fields.

Python 3 standard library only.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_STDOUT_BYTES = 16 * 1024
MAX_STDERR_BYTES = 8 * 1024
DEFAULT_HTTP_TIMEOUT = 20
DEFAULT_TASK_TIMEOUT = 120
DELIVERY_TIMEOUT = 15
DELIVERY_ATTEMPTS = 2
DELIVERY_BACKOFF_S = 2.0
# 64KB of raw payload -> ~87KB of base64, safely under the ~128KB per-variable
# limit that execve/GitHub impose on environment variables.
MAX_PAYLOAD_BYTES = 64 * 1024
ALLOWED_MODES = ("http_check", "python_script", "shell_cmd")
LOG_SOURCE = "github-runner"
KEY_PREFIX = "runner:"
USER_AGENT = "strawberry-runner/1.0"
# Cloudflare's "browser integrity check" (error 1010) rejects the literal
# `Python-urllib/3.x` User-Agent, so http_check sends an identifiable one of its
# own. A payload may still override it with its own headers.User-Agent.
DEFAULT_CHECK_UA = "strawberry-runner/1.0 (+https://github.com/buriburiyj/strawberry-runner)"

_ALLOWED_TASK_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_HIDDEN_FROM_CHILD = ("STRAWBERRY_TOKEN", "STRAWBERRY_URL", "PAYLOAD_B64")


# --------------------------------------------------------------------- helpers


def _utc_now():
    """ISO-8601 UTC timestamp with milliseconds, e.g. 2026-11-18T07:12:03.412Z."""
    millis = int(time.time() * 1000) % 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%03dZ" % millis


def _env_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _arg(argv, name):
    """Return the value following `name` in argv, or None."""
    try:
        index = argv.index(name)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def _valid_task_id(task_id):
    if not task_id or len(task_id) > 120:
        return False
    return all(ch in _ALLOWED_TASK_ID_CHARS for ch in task_id)


def _clip(raw, limit):
    """Decode bytes to text, keeping at most `limit` bytes worth of characters."""
    if raw is None:
        return "", False
    if not isinstance(raw, bytes):
        raw = str(raw).encode("utf-8", "replace")
    if len(raw) <= limit:
        return raw.decode("utf-8", "replace"), False
    return raw[:limit].decode("utf-8", "replace"), True


def _child_env():
    """Environment for the task process: no server token, no raw payload."""
    env = {}
    for key, value in os.environ.items():
        if key in _HIDDEN_FROM_CHILD:
            continue
        env[key] = value
    env["STRAWBERRY_TASK_ID"] = os.environ.get("TASK_ID", "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _decode_payload(payload_b64):
    text = (payload_b64 or "").strip().replace("\r", "").replace("\n", "").replace(" ", "")
    if not text:
        raise ValueError("payload_b64 is empty")
    padding = (-len(text)) % 4
    try:
        raw = base64.b64decode(text + ("=" * padding), validate=True)
    except (ValueError, TypeError):
        raise ValueError("payload_b64 is not valid base64")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("payload is not valid UTF-8")


def _run(cmd, timeout, cwd=None):
    """Run a child process, capture clipped output, never raise on task failure."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out, out_truncated = _clip(exc.stdout, MAX_STDOUT_BYTES)
        err, err_truncated = _clip(exc.stderr, MAX_STDERR_BYTES)
        return {
            "exit_code": 124,  # same convention as coreutils `timeout`
            "stdout": out,
            "stderr": err,
            "truncated": {"stdout": out_truncated, "stderr": err_truncated},
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": "failed to start process: " + type(exc).__name__,
            "truncated": {"stdout": False, "stderr": False},
            "timed_out": False,
        }
    out, out_truncated = _clip(proc.stdout, MAX_STDOUT_BYTES)
    err, err_truncated = _clip(proc.stderr, MAX_STDERR_BYTES)
    return {
        "exit_code": int(proc.returncode),
        "stdout": out,
        "stderr": err,
        "truncated": {"stdout": out_truncated, "stderr": err_truncated},
        "timed_out": False,
    }


# ---------------------------------------------------------------------- modes


def _http_check(payload_text, timeout):
    """Fetch a URL. Payload: JSON {url, method?, headers?, data?} or a plain URL."""
    text = (payload_text or "").strip()
    if not text:
        raise ValueError("payload is empty")

    if text[0] in "{[":
        try:
            spec = json.loads(text)
        except ValueError:
            raise ValueError("payload is not valid JSON")
        if not isinstance(spec, dict):
            raise ValueError("JSON payload must be an object")
    else:
        spec = {"url": text}

    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("payload needs a non-empty \"url\"")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url must be an absolute http(s) URL")

    method = str(spec.get("method") or "GET").upper()
    if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        raise ValueError("unsupported method")

    raw_headers = spec.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ValueError("\"headers\" must be an object")
    headers = {}
    for key, value in raw_headers.items():
        headers[str(key)] = str(value)

    data = None
    if spec.get("data") is not None:
        payload_data = spec["data"]
        if isinstance(payload_data, str):
            data = payload_data.encode("utf-8")
        else:
            data = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")

    headers.setdefault("User-Agent", DEFAULT_CHECK_UA)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    status = None
    body = b""
    error_kind = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read(MAX_STDOUT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(MAX_STDOUT_BYTES + 1)
        except Exception:
            body = b""
    except (urllib.error.URLError, OSError) as exc:
        # Message dropped on purpose: it can contain the target URL.
        error_kind = type(exc).__name__

    out, out_truncated = _clip(body, MAX_STDOUT_BYTES)
    if error_kind is not None:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "request failed: " + str(error_kind),
            "truncated": {"stdout": False, "stderr": False},
            "timed_out": error_kind in ("timeout", "TimeoutError"),
            "http_status": None,
        }
    # 4xx/5xx means the check failed; 2xx/3xx means it is up.
    exit_code = 0 if status is not None and status < 400 else 1
    return {
        "exit_code": exit_code,
        "stdout": out,
        "stderr": "" if exit_code == 0 else "http_status=%s" % status,
        "truncated": {"stdout": out_truncated, "stderr": False},
        "timed_out": False,
        "http_status": status,
    }


def _python_script(payload_text, timeout):
    workdir = tempfile.mkdtemp(prefix="strawberry-task-")
    script_path = os.path.join(workdir, "task.py")
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(payload_text)
        if not payload_text.endswith("\n"):
            handle.write("\n")
    result = _run([sys.executable or "python3", script_path], timeout, cwd=workdir)
    result["http_status"] = None
    return result


def _shell_cmd(payload_text, timeout):
    result = _run(["bash", "-c", payload_text], timeout, cwd=None)
    result["http_status"] = None
    return result


# ------------------------------------------------------------------- delivery


def _send(url, method, body, token):
    """POST/PUT JSON to strawberry-server. Returns (ok, safe_error_text)."""
    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": USER_AGENT,
                "Cache-Control": "no-store",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=DELIVERY_TIMEOUT) as response:
                response.read()  # drain and discard: response bodies never reach a log
                return True, None
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            detail = "HTTP %s" % exc.code
            if 500 <= int(exc.code) < 600 and attempt < DELIVERY_ATTEMPTS:
                time.sleep(DELIVERY_BACKOFF_S)
                continue
            return False, detail
        except (urllib.error.URLError, OSError) as exc:
            # Message dropped: it can contain the URL; never the token.
            if attempt < DELIVERY_ATTEMPTS:
                time.sleep(DELIVERY_BACKOFF_S)
                continue
            return False, "network " + type(exc).__name__
        except Exception as exc:
            return False, "error " + type(exc).__name__
    return False, "unknown"


def _deliver(base_url, token, result):
    """Store the full result in KV and append a compact log line."""
    task_id = result["task_id"]
    mode = result["mode"]
    exit_code = result["exit_code"]
    level = "info" if exit_code == 0 else "error"
    message = "task %s (%s) exit=%s" % (task_id, mode, exit_code)
    meta = {"task_id": task_id, "mode": mode, "exit_code": exit_code}

    key = KEY_PREFIX + task_id
    kv_url = base_url + "/kv/" + urllib.parse.quote(key, safe=":")
    kv_body = json.dumps({"value": result}, ensure_ascii=False).encode("utf-8")
    kv_ok, kv_error = _send(kv_url, "PUT", kv_body, token)

    log_body = json.dumps(
        {
            "source": LOG_SOURCE,
            "event": level,          # required by strawberry-server (max 120 chars)
            "level": level,
            "message": message,
            "meta": meta,
            "payload": {
                "level": level,
                "message": message,
                "task_id": task_id,
                "mode": mode,
                "exit_code": exit_code,
                "duration_ms": result.get("duration_ms"),
                "timed_out": result.get("timed_out"),
                "http_status": result.get("http_status"),
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    log_ok, log_error = _send(base_url + "/logs", "POST", log_body, token)

    return kv_ok, kv_error, log_ok, log_error


# ----------------------------------------------------------------------- main


def _fail(code, message):
    sys.stderr.write("error: %s\n" % message)
    return code


def main(argv):
    dry_run = "--dry-run" in argv

    task_id = (_arg(argv, "--task-id") or os.environ.get("TASK_ID") or "").strip()
    mode = (_arg(argv, "--mode") or os.environ.get("MODE") or "").strip()
    payload_b64 = _arg(argv, "--payload-b64")
    if payload_b64 is None:
        payload_b64 = os.environ.get("PAYLOAD_B64") or ""

    base_url = (os.environ.get("STRAWBERRY_URL") or "").strip().rstrip("/")
    token = (os.environ.get("STRAWBERRY_TOKEN") or "").strip()

    if not _valid_task_id(task_id):
        return _fail(2, "TASK_ID must be 1..120 chars of A-Z a-z 0-9 . _ : -")
    if mode not in ALLOWED_MODES:
        return _fail(2, "MODE must be one of: " + ", ".join(ALLOWED_MODES))
    try:
        payload_text = _decode_payload(payload_b64)
    except ValueError as exc:
        return _fail(2, str(exc))
    if len(payload_text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return _fail(2, "payload is larger than 64KB - host large work behind a URL")

    if not dry_run:
        if not base_url.startswith(("http://", "https://")):
            return _fail(2, "STRAWBERRY_URL must be an http(s) URL")
        if not token:
            return _fail(2, "STRAWBERRY_TOKEN is empty")

    http_timeout = _env_int("RUNNER_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT)
    task_timeout = _env_int("RUNNER_TASK_TIMEOUT", DEFAULT_TASK_TIMEOUT)

    started = time.time()
    try:
        if mode == "http_check":
            outcome = _http_check(payload_text, http_timeout)
        elif mode == "python_script":
            outcome = _python_script(payload_text, task_timeout)
        else:
            outcome = _shell_cmd(payload_text, task_timeout)
    except ValueError as exc:
        return _fail(2, "bad payload: " + str(exc))
    except Exception as exc:
        return _fail(4, "unexpected error: " + type(exc).__name__)

    duration_ms = int((time.time() - started) * 1000)
    result = {
        "task_id": task_id,
        "mode": mode,
        "exit_code": outcome["exit_code"],
        "stdout": outcome["stdout"],
        "stderr": outcome["stderr"],
        "duration_ms": duration_ms,
        "finished_at": _utc_now(),
        "http_status": outcome.get("http_status"),
        "truncated": outcome.get("truncated") or {"stdout": False, "stderr": False},
        "timed_out": bool(outcome.get("timed_out")),
    }

    # The only three lines that may ever reach the public Actions log.
    sys.stdout.write("task_id=%s\n" % task_id)
    sys.stdout.write("mode=%s\n" % mode)
    sys.stdout.write("exit_code=%s\n" % result["exit_code"])

    delivery_failed = None
    if dry_run:
        sys.stdout.write(
            "dry-run: duration_ms=%s http_status=%s timed_out=%s\n"
            % (duration_ms, result["http_status"], result["timed_out"])
        )
        sys.stdout.write(
            "dry-run: stdout=%d chars (truncated=%s) stderr=%d chars (truncated=%s)\n"
            % (
                len(result["stdout"]),
                result["truncated"].get("stdout"),
                len(result["stderr"]),
                result["truncated"].get("stderr"),
            )
        )
        sys.stdout.write("dry-run: delivery skipped\n")
    else:
        kv_ok, kv_error, log_ok, log_error = _deliver(base_url, token, result)
        if not kv_ok and not log_ok:
            delivery_failed = "kv %s, logs %s" % (kv_error, log_error)
        elif not kv_ok:
            delivery_failed = "kv " + str(kv_error)
        elif not log_ok:
            delivery_failed = "logs " + str(log_error)
    sys.stdout.flush()

    if delivery_failed:
        sys.stderr.write("delivery_failed: %s\n" % delivery_failed)
        return 3
    if dry_run:
        return 0
    return 0 if result["exit_code"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
