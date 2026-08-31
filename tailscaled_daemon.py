#!/usr/bin/env python3
import os
import time
from pathlib import Path

BINARY = Path("/data/tailscale/bin/tailscaled")
STATE = Path("/data/tailscale/tailscaled.state")
SOCKET = Path("/data/tailscale/tailscaled.sock")

def main() -> None:
  while True:
    if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
      print(f"nap_tailscaled: waiting for {BINARY}", flush=True)
      time.sleep(30)
      continue

    if not STATE.is_file():
      print(f"nap_tailscaled: waiting for authenticated state {STATE}", flush=True)
      time.sleep(30)
      continue

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

    print("nap_tailscaled: starting Tailscale Funnel daemon", flush=True)
    os.execv(str(BINARY), args)

if __name__ == "__main__":
  main()
