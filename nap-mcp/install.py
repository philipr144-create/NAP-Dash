from pathlib import Path
import os
import subprocess
import time
root = Path('/data/nap-mcp')
hook = Path('/data/continue.sh')
original = hook.read_text()
line = '/data/nap-mcp/start.sh > /data/nap-mcp/startup.log 2>&1 &\n'
if line not in original:
    if original != '#!/usr/bin/env bash\ncd /data/openpilot\nexec ./launch_openpilot.sh\n':
        raise SystemExit('Unexpected boot hook; refusing to overwrite')
    backup = root / ('continue.sh.before-' + str(int(time.time())))
    backup.write_text(original)
    os.chmod(backup, 0o700)
    new = hook.with_name('continue.sh.nap-mcp.tmp')
    new.write_text(original.replace('cd /data/openpilot\n', line + 'cd /data/openpilot\n'))
    os.chmod(new, hook.stat().st_mode & 0o777)
    new.replace(hook)
os.chmod(root / 'start.sh', 0o700)
subprocess.run([str(root / 'start.sh')], check=True)
