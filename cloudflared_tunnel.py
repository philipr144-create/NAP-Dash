#!/usr/bin/env python3
import os
import time
from pathlib import Path

TOKEN_FILE = Path("/data/cloudflared/tunnel-token")
LOG_FILE = Path("/data/cloudflared/named-tunnel.log")
BINARIES = (
  Path("/usr/local/bin/cloudflared"),
  Path("/usr/bin/cloudflared"),
)

def main() -> None:
  LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

  while True:
    binary = next((p for p in BINARIES if p.is_file() and os.access(p, os.X_OK)), None)

    if binary is None:
      print("nap_cloudflared: cloudflared binary not found", flush=True)
      time.sleep(30)
      continue

    if not TOKEN_FILE.is_file() or TOKEN_FILE.stat().st_size < 20:
      print(f"nap_cloudflared: waiting for {TOKEN_FILE}", flush=True)
      time.sleep(30)
      continue

    args = [
      str(binary),
      "tunnel",
      "--no-autoupdate",
      "--protocol", "http2",
      "--loglevel", "info",
      "--logfile", str(LOG_FILE),
      "run",
      "--token-file", str(TOKEN_FILE),
    ]

    print("nap_cloudflared: starting named tunnel", flush=True)
    os.execv(str(binary), args)

if __name__ == "__main__":
  main()
