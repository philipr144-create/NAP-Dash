#!/usr/bin/env bash
# AGNOS root is read-only. Re-create the isolated runtime unit each boot.
set -eu
sudo -n install -m 644 /data/nap-mcp/nap-mcp.service /run/systemd/system/nap-mcp.service
sudo -n systemctl daemon-reload
sudo -n systemctl start nap-mcp.service
sudo -n install -m 644 /data/nap-mcp/nap-mcu-route.service /run/systemd/system/nap-mcu-route.service
sudo -n systemctl daemon-reload
sudo -n systemctl start nap-mcu-route.service
