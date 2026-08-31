#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
import py_compile
import shutil

repo = Path(__file__).resolve().parent
source = repo / "tailscaled_daemon.py"
target = Path("/data/server/tailscaled_daemon.py")
process_config = Path("/data/openpilot/system/manager/process_config.py")
backup_dir = Path("/data/tailscale/backups")

server_line = '  PythonProcess("server_v21", "server.server_v21", always_run),\n'
cloudflare_line = '  PythonProcess("nap_cloudflared", "server.cloudflared_tunnel", always_run),\n'
tailscale_line = '  PythonProcess("nap_tailscaled", "server.tailscaled_daemon", always_run),\n'

for required in (
  source,
  process_config,
  Path("/data/tailscale/bin/tailscaled"),
  Path("/data/tailscale/tailscaled.state"),
):
  if not required.exists():
    raise SystemExit(f"ERROR: Missing {required}")

target.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source, target)

text = process_config.read_text()
updated = text.replace(cloudflare_line, "")

if tailscale_line not in updated:
  if updated.count(server_line) != 1:
    raise SystemExit("ERROR: Could not uniquely locate server_v21 entry")
  updated = updated.replace(server_line, server_line + tailscale_line, 1)

if updated != text:
  backup_dir.mkdir(parents=True, exist_ok=True)
  stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
  backup = backup_dir / f"process_config.py.pre_tailscale_{stamp}.bak"
  shutil.copy2(process_config, backup)
  process_config.write_text(updated)
  print(f"Patched: {process_config}")
  print(f"Backup:  {backup}")
else:
  print("process_config.py already configured for Tailscale")

py_compile.compile(str(source), doraise=True)
py_compile.compile(str(target), doraise=True)
py_compile.compile(str(process_config), doraise=True)

print(f"Installed: {target}")
print("Compilation passed.")
