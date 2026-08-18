#!/usr/bin/env python3
import os
import shutil
import sys

PROCESS_CONFIG_PATH = "/data/openpilot/selfdrive/manager/process_config.py"
CARSTATE_PATH = "/data/openpilot/opendbc/car/tesla/preap/carstate.py"
SERVER_PATH = "/data/openpilot/server_v21.py"

SPEED_TRIM_CODE = """
  # NAP Dashboard: Instant Speed Offset & Trim Override
  try:
    _sett = __import__('json').load(open('/data/nap_settings.json'))
    _off = 5.0 if _sett.get('speed_offset', False) else 0.0
    _trim = float(_sett.get('speed_trim', 0.0))
    ret.cruiseState.speed += ((_off + _trim) * CV.MPH_TO_MS)
  except:
    pass
"""

SERVER_CONFIG_CODE = """
# --- NAP SERVER INJECTION ---
procs += [
  NativeProcess("nap_server", "data/openpilot/server_v21.py", always=True)
]
# ----------------------------
"""

def backup_file(filepath):
    """Creates a .bak file if one doesn't already exist."""
    if not os.path.exists(filepath):
        print(f"❌ ERROR: Cannot find {filepath}")
        return False
        
    backup_path = filepath + ".bak"
    if not os.path.exists(backup_path):
        print(f"📦 Backing up {filepath} -> .bak")
        shutil.copy2(filepath, backup_path)
    return True

def restore_file(filepath):
    """Restores the original file from the .bak if it exists."""
    backup_path = filepath + ".bak"
    if os.path.exists(backup_path):
        print(f"⏪ Restoring {filepath} from backup.")
        shutil.copy2(backup_path, filepath)
        os.remove(backup_path)
    else:
        print(f"⚠️ No backup found for {filepath}")

def patch_process_config():
    """Injects the server into the boot sequence."""
    if not backup_file(PROCESS_CONFIG_PATH):
        return
        
    with open(PROCESS_CONFIG_PATH, "r") as f:
        content = f.read()
        
    if "nap_server" in content:
        print("✅ process_config.py is already patched.")
        return
        
    with open(PROCESS_CONFIG_PATH, "a") as f:
        f.write(SERVER_CONFIG_CODE)
    print("✅ Successfully injected server into process_config.py")

def patch_carstate():
    """Injects the JSON speed override logic just before `return ret`."""
    if not backup_file(CARSTATE_PATH):
        return
        
    with open(CARSTATE_PATH, "r") as f:
        lines = f.readlines()
        
    # Check if already patched
    if any("NAP Dashboard: Instant Speed Offset" in line for line in lines):
        print("✅ carstate.py is already patched.")
        return
        
    # Find the last `return ret` inside the update_preap function
    insert_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if "return ret" in lines[i]:
            insert_idx = i
            break
            
    if insert_idx == -1:
        print("❌ ERROR: Could not find 'return ret' in carstate.py.")
        return
        
    # Insert the code block
    lines.insert(insert_idx, SPEED_TRIM_CODE)
    
    with open(CARSTATE_PATH, "w") as f:
        f.writelines(lines)
    print("✅ Successfully injected speed override logic into carstate.py")

def install():
    print("Starting NAP Web Dashboard Installation...\n")
    if os.path.exists(SERVER_PATH):
        os.chmod(SERVER_PATH, 0o755)
        print(f"✅ Made {SERVER_PATH} executable.")
    else:
        print(f"❌ ERROR: Server script missing at {SERVER_PATH}")
        print("Please download server_v21.py to /data/openpilot/ first.")
        return
        
    patch_process_config()
    patch_carstate()
    
    print("\n--- INSTALL COMPLETE ---")
    print("Please reboot your comma device to apply changes.")
    print("To uninstall later, run: python3 install_nap.py --restore")

def uninstall():
    print("Starting NAP Web Dashboard Uninstallation...\n")
    restore_file(PROCESS_CONFIG_PATH)
    restore_file(CARSTATE_PATH)
    print("\n--- UNINSTALL COMPLETE ---")
    print("Please reboot your comma device.")

if __name__ == "__main__":
    if "--restore" in sys.argv:
        uninstall()
    else:
        install()
