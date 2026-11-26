#!/usr/bin/env python3
"""Watch a Docker Compose deployment and roll it back when it fails."""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def docker(args, cwd=None):
    """Every docker call goes through here (tests replace it). args start after `docker`."""
    return subprocess.run(["docker", *args], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def state_path(compose_file):
    return compose_file.resolve().parent / ".compose-guard.json"


def atomic_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def compose(args, compose_file, override=None):
    result = docker(["compose", "-f", str(compose_file), *(["-f", str(override)] if override else []), *args], cwd=compose_file.parent)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "docker compose command failed")
    return result.stdout


def containers(service, compose_file, override=None):
    raw = compose(["ps", "--format", "json", service], compose_file, override)
    try:
        rows = json.loads(raw) if raw.strip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError as e:
        raise RuntimeError("could not parse docker compose ps output") from e
    result = []
    for row in rows:
        cid = row.get("ID") or row.get("Id") or row.get("id")
        if not cid:
            continue
        inspected = json.loads(docker_checked(["inspect", cid]))[0]
        result.append(inspected)
    return result


def docker_checked(args):
    result = docker(args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "docker command failed")
    return result.stdout


def write_override(path, service, image):
    # JSON strings are valid YAML scalars and safely quote arbitrary names/images.
    path.write_text("services:\n  " + json.dumps(service) + ":\n    image: " + json.dumps(image) + "\n")


def image_of(container):
    return container.get("Config", {}).get("Image") or container.get("Image")


def inspect_ok(service, compose_file, override, baseline_restarts, url=None):
    current = containers(service, compose_file, override)
    if not current:
        return False, "no container"
    for c in current:
        state = c.get("State", {})
        if state.get("Status") != "running":
            return False, "container is not running"
        health = state.get("Health", {}).get("Status")
        if health == "unhealthy":
            return False, "healthcheck is unhealthy"
        name = c.get("Name", "").lstrip("/")
        restarts = c.get("RestartCount", 0)
        if restarts > baseline_restarts.get(name, 0):
            return False, "container restart count increased"
    if url:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if not 200 <= response.status < 300:
                    return False, "URL check returned HTTP " + str(response.status)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return False, "URL check failed: " + str(e)
    return True, "healthy"


def notify(args, message):
    if args.notify_telegram:
        token, chat = os.environ.get("CG_TELEGRAM_TOKEN"), os.environ.get("CG_TELEGRAM_CHAT")
        if not token or not chat:
            raise RuntimeError("CG_TELEGRAM_TOKEN and CG_TELEGRAM_CHAT are required")
        payload = json.dumps({"chat_id": chat, "text": message}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("Telegram notification failed")
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError("Telegram notification failed") from None
    if args.notify_webhook:
        req = urllib.request.Request(args.notify_webhook, data=json.dumps({"text": message}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("webhook notification failed")


def take_lock(path):
    lock = open(str(path) + ".lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another compose-guard operation is already running") from None
    return lock  # keep the handle open: the lock lives as long as it does


def run_deploy(args, compose_file, path):
    lock = take_lock(path)
    override = compose_file.parent / ".compose-guard-override.yml"
    old = containers(args.service, compose_file)
    if not old:
        raise RuntimeError("service has no existing container; cannot save previous image")
    previous = image_of(old[0])
    if not previous:
        raise RuntimeError("could not determine the current image")
    baseline = {c.get("Name", "").lstrip("/"): c.get("RestartCount", 0) for c in old}
    write_override(override, args.service, args.image)
    state = {"service": args.service, "previous_image": previous, "deployed_image": args.image,
             "deployed_at": datetime.now(timezone.utc).isoformat(), "result": "watching",
             "compose_file": str(compose_file), "override_file": str(override)}
    atomic_json(path, state)
    compose(["up", "-d", args.service], compose_file, override)
    deadline = time.monotonic() + args.watch
    failures = 0
    reason = "watch period completed"
    while time.monotonic() < deadline:
        ok, reason = inspect_ok(args.service, compose_file, override, baseline, args.check)
        failures = 0 if ok else failures + 1
        if failures >= args.fails:
            state["result"] = "rolling back: " + reason
            atomic_json(path, state)
            return rollback(args.service, state, path, compose_file, args)
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    state["result"] = "success"
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, state)
    notify(args, "compose-guard: " + args.service + " deployment succeeded")
    return 0


def rollback(service, state, path, compose_file, args):
    override = Path(state["override_file"])
    write_override(override, service, state["previous_image"])
    compose(["up", "-d", service], compose_file, override)
    ok, reason = inspect_ok(service, compose_file, override, {}, None)
    if ok:
        restored = containers(service, compose_file, override)
        if not restored or any(image_of(c) != state["previous_image"] for c in restored):
            ok, reason = False, "previous image is not running"
    state["result"] = "rolled back" if ok else "rollback check failed: " + reason
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, state)
    notify(args, "compose-guard: " + service + " rolled back; " + state["result"])
    return 1


def duration(value):
    match = re.fullmatch(r"(\d+)(s|m|h)", value)
    if not match:
        raise argparse.ArgumentTypeError("duration must look like 15s, 30m, or 2h")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="compose-guard")
    sub = parser.add_subparsers(dest="command", required=True)
    deploy = sub.add_parser("deploy")
    deploy.add_argument("--service", required=True)
    deploy.add_argument("--image", required=True)
    deploy.add_argument("--file", default="compose.yml")
    deploy.add_argument("--watch", type=duration, default=1800)
    deploy.add_argument("--check")
    deploy.add_argument("--interval", type=duration, default=15)
    deploy.add_argument("--fails", type=int, default=3)
    deploy.add_argument("--notify-telegram", action="store_true")
    deploy.add_argument("--notify-webhook")
    rb = sub.add_parser("rollback")
    rb.add_argument("--service", required=True)
    rb.add_argument("--file", default="compose.yml")
    rb.add_argument("--notify-telegram", action="store_true")
    rb.add_argument("--notify-webhook")
    sub.add_parser("status").add_argument("--file", default="compose.yml")
    args = parser.parse_args(argv)
    compose_file = Path(getattr(args, "file", "compose.yml")).resolve()
    path = state_path(compose_file)
    try:
        if args.command == "status":
            if not path.exists():
                print("No deployment state found")
                return 0
            print(json.dumps(json.loads(path.read_text()), indent=2))
            return 0
        if args.command == "deploy":
            if args.fails < 1 or args.interval < 1 or args.watch < 1:
                raise RuntimeError("watch, interval, and fails must be positive")
            return run_deploy(args, compose_file, path)
        lock = take_lock(path)
        if not path.exists():
            raise RuntimeError("no saved deployment state")
        state = json.loads(path.read_text())
        if state["service"] != args.service:
            raise RuntimeError("saved state belongs to service " + state["service"])
        if "--file" not in (argv if argv is not None else sys.argv[1:]):
            compose_file = Path(state["compose_file"])
        return rollback(args.service, state, path, compose_file, args)
    except (RuntimeError, OSError, KeyError, json.JSONDecodeError) as e:
        print("compose-guard: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
