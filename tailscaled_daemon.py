#!/usr/bin/env python3
"""Keep Tailscale healthy while the comma changes network interfaces."""

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

BINARY = Path(os.environ.get("NAP_TAILSCALED_BINARY", "/data/tailscale/bin/tailscaled"))
CLI = Path(os.environ.get("NAP_TAILSCALE_CLI", "/data/tailscale/bin/tailscale"))
STATE = Path(os.environ.get("NAP_TAILSCALE_STATE", "/data/tailscale/tailscaled.state"))
SOCKET = Path(os.environ.get("NAP_TAILSCALE_SOCKET", "/data/tailscale/tailscaled.sock"))
STATUS_FILE = Path(os.environ.get("NAP_TAILSCALE_WATCHDOG_STATUS", "/data/tailscale/watchdog_status.json"))

CHECK_SECONDS = float(os.environ.get("NAP_TAILSCALE_CHECK_SECONDS", "5"))
ROUTE_STABLE_CHECKS = int(os.environ.get("NAP_TAILSCALE_ROUTE_STABLE_CHECKS", "2"))
HEALTH_SECONDS = float(os.environ.get("NAP_TAILSCALE_HEALTH_SECONDS", "30"))
STARTUP_GRACE_SECONDS = float(os.environ.get("NAP_TAILSCALE_STARTUP_GRACE_SECONDS", "45"))
RESTART_COOLDOWN_SECONDS = float(os.environ.get("NAP_TAILSCALE_RESTART_COOLDOWN_SECONDS", "30"))
HEALTH_FAILURE_LIMIT = int(os.environ.get("NAP_TAILSCALE_HEALTH_FAILURE_LIMIT", "2"))

# Probe only the existing public dashboard; never change Funnel or authentication.
def _read_private_public_url():
    try:
        return Path(
            "/data/nap_settings/nap_tailscale_public_url"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""

PUBLIC_URL = _read_private_public_url()


def http_ok(url):
  try:
    result = subprocess.run(
      ["curl", "--connect-timeout", "3", "--max-time", "5", "--silent",
       "--output", "/dev/null", "--write-out", "%{http_code}", url],
      capture_output=True, text=True, timeout=7, check=False)
    return result.returncode == 0 and result.stdout.strip() == "200"
  except (OSError, subprocess.TimeoutExpired):
    return False


class FunnelHealth:
  """Three failed public probes, working local server and internet, bounded retries."""
  def __init__(self):
    self.failures = 0
    self.successes = 0
    self.next_restart = 0.0
    self.cooldown = 300.0
    self.state = "not checked"

  def check(self, now, eligible, probe=http_ok):
    if not eligible:
      self.failures = self.successes = 0
      self.state = "waiting for backend and route"
      return False
    if not probe("http://127.0.0.1:7070/"):
      self.failures = self.successes = 0
      self.state = "local web server unavailable; no Tailscale restart"
      return False
    if probe(PUBLIC_URL):
      self.failures = 0
      self.successes += 1
      self.state = "public HTTPS reachable"
      if self.successes >= 3:
        self.cooldown = 300.0
      return False
    self.successes = 0
    # An internet outage is not evidence that tailscaled needs restarting.
    if not probe("https://example.com/"):
      self.failures = 0
      self.state = "internet probe unavailable; waiting"
      return False
    self.failures += 1
    self.state = "public HTTPS failed (%d/3)" % self.failures
    if self.failures < 3 or now < self.next_restart:
      return False
    self.failures = 0
    self.next_restart = now + self.cooldown
    self.cooldown = min(1800.0, self.cooldown * 2)
    self.state = "recovering public HTTPS"
    return True


FUNNEL = FunnelHealth()
STOP = threading.Event()


def command_output(args, timeout=5):
  try:
    return subprocess.run(
      args, capture_output=True, text=True, timeout=timeout, check=False
    ).stdout.strip()
  except Exception:
    return ""


def route_signature():
  """Return the interface, gateway and source address used for internet traffic."""
  output = command_output(["ip", "route", "get", "1.1.1.1"])
  if output:
    dev = re.search(r"\bdev\s+(\S+)", output)
    via = re.search(r"\bvia\s+(\S+)", output)
    src = re.search(r"\bsrc\s+(\S+)", output)
    if dev:
      return {
        "interface": dev.group(1),
        "gateway": via.group(1) if via else "",
        "source": src.group(1) if src else "",
      }

  # BusyBox/minimal images may not provide `ip`. /proc/net/route still tells us
  # which interface owns the default route.
  try:
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
      fields = line.split()
      if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 0x1:
        return {"interface": fields[0], "gateway": fields[2], "source": ""}
  except Exception:
    pass

  # An interface can remain marked "up" while it has no internet route. Do not
  # treat that as connectivity or the health checker would restart in a loop in
  # a dead zone.
  return None


def backend_state():
  if not CLI.is_file() or not SOCKET.exists():
    return "unavailable"
  output = command_output([str(CLI), f"--socket={SOCKET}", "status", "--json"], timeout=10)
  try:
    return str(json.loads(output).get("BackendState", "unknown"))
  except Exception:
    return "unavailable"


def write_status(child, route, state, reason, restarts):
  payload = {
    "updated_at": time.time(),
    "funnel_health": FUNNEL.state,
    "funnel_retry_after_mono": FUNNEL.next_restart,
    "pid": child.pid if child and child.poll() is None else None,
    "route": route,
    "backend_state": state,
    "last_restart_reason": reason,
    "restart_count": restarts,
  }
  try:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    os.replace(temporary, STATUS_FILE)
  except Exception:
    pass


def start_child():
  try:
    SOCKET.unlink()
  except FileNotFoundError:
    pass
  args = [
    str(BINARY),
    "--tun=userspace-networking",
    f"--state={STATE}",
    f"--socket={SOCKET}",
  ]
  return subprocess.Popen(args)


def stop_child(child):
  if not child or child.poll() is not None:
    return
  child.terminate()
  try:
    child.wait(timeout=10)
  except subprocess.TimeoutExpired:
    child.kill()
    child.wait(timeout=5)


def request_stop(signum, frame):
  del signum, frame
  STOP.set()


def main():
  signal.signal(signal.SIGTERM, request_stop)
  signal.signal(signal.SIGINT, request_stop)
  child = None
  applied_route = None
  candidate_route = None
  candidate_count = 0
  started_at = 0.0
  last_restart = 0.0
  last_health_check = 0.0
  health_failures = 0
  restart_count = 0
  last_reason = "initial start"
  last_backend_state = "starting"

  while not STOP.is_set():
    if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
      print(f"nap_tailscaled: waiting for executable {BINARY}", flush=True)
      STOP.wait(30)
      continue
    if not STATE.is_file():
      print(f"nap_tailscaled: waiting for authenticated state {STATE}", flush=True)
      STOP.wait(30)
      continue

    now = time.monotonic()
    current_route = route_signature()
    if child is None or child.poll() is not None:
      if child is not None:
        last_reason = f"tailscaled exited ({child.returncode})"
        restart_count += 1
      print(f"nap_tailscaled: starting ({last_reason})", flush=True)
      child = start_child()
      started_at = now
      last_restart = now
      applied_route = current_route
      candidate_route = None
      candidate_count = 0
      health_failures = 0
      last_backend_state = "starting"

    restart_reason = None
    if current_route and current_route != applied_route:
      if current_route == candidate_route:
        candidate_count += 1
      else:
        candidate_route = current_route
        candidate_count = 1
      if candidate_count >= max(1, ROUTE_STABLE_CHECKS):
        restart_reason = "default route changed"
    else:
      candidate_route = None
      candidate_count = 0

    if now - last_health_check >= HEALTH_SECONDS:
      last_health_check = now
      last_backend_state = backend_state()
      healthy = last_backend_state == "Running"
      if healthy or current_route is None or now - started_at < STARTUP_GRACE_SECONDS:
        health_failures = 0
      else:
        health_failures += 1
        if health_failures >= max(1, HEALTH_FAILURE_LIMIT):
          restart_reason = f"backend unhealthy ({last_backend_state})"

      if FUNNEL.check(time.monotonic(), healthy and current_route is not None
                      and now - started_at >= STARTUP_GRACE_SECONDS):
        restart_reason = "public HTTPS unreachable with local server and internet working"

    if restart_reason and now - last_restart >= RESTART_COOLDOWN_SECONDS:
      print(
        f"nap_tailscaled: {restart_reason}; route {applied_route} -> {current_route}",
        flush=True,
      )
      stop_child(child)
      child = None
      applied_route = current_route
      last_reason = restart_reason
      restart_count += 1
      write_status(child, current_route, last_backend_state, last_reason, restart_count)
      continue

    write_status(child, current_route, last_backend_state, last_reason, restart_count)
    STOP.wait(CHECK_SECONDS)

  stop_child(child)
  write_status(None, route_signature(), "stopped", "manager requested stop", restart_count)


if __name__ == "__main__":
  main()
