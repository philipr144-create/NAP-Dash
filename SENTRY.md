# NAP Sentry

Parked live viewing of the comma's front, wide-front, and cabin cameras simultaneously. The user verified all three physical feeds and browser PIN remembering on 20 October 2026.

Open NAP Dash → Sentry. The configured default PIN is `000000`; leave “Remember on this browser” checked to save it after the first successful Start. “Forget PIN” removes the saved browser value. Stop ends the session; select All cameras or a single focused view. A single focused view still receives all three streams. Remote video requires direct private/Tailscale access to the comma; a page tunnel alone does not relay media.

The runtime PIN is stored separately at `/data/nap_sentry_pin` with mode 0600 and is not included in this repository or backup. A fresh install creates the default PIN. Signaling is PIN protected and WebRTC listens only on loopback port 5002. No driving/control data bridges are enabled.

## Required integration

This installation uses `/data/openpilot/server` as a symlink to `/data/server` (this repository). Openpilot's `system/manager/process_config.py` also needs the dedicated `sentry_camera`, `sentry_stream`, `sentry_only` predicates and `nap_sentry_webrtc` process. The separate openpilot repository saves that integration. `sentry-process-config.patch` is also included here as a recovery copy. It must be applied from the openpilot repository only after `git apply --check` succeeds; do not overwrite a newer process configuration wholesale.

`server_v21.py` runs the Sentry safety monitor. `nap_sentry.py` contains the three-camera UI, PIN API, safety gate, and loopback WebRTC service. Reboot while parked after restoring or updating because the process manager caches imported Python modules. No reboot is needed merely to preserve this working version.

## Session protections

Ignition off and offroad; fresh valid device/panda/peripheral telemetry; thermal green and maximum temperature below 70 °C; 12–16 V supply, or 4.75–5.25 V with every panda reporting a disconnected harness for bench testing. Five minute absolute maximum, 20 second browser heartbeat timeout, three second lease lifetime, and five second restart cooldown. Existing device power/shutdown behavior remains intact. No motion detection or continuous recording.

## Validation

Run from this repository with the device Python environment:

```sh
/usr/local/venv/bin/python -m unittest discover -s tests -p 'test_sentry*.py'
```

Eight tests cover voltage/bench limits, ignition, temperature, telemetry freshness, lease expiry and onroad rejection, PIN authentication and permissions, fixed three-camera signaling without control bridges, browser remembering, stop/reset, and non-extending session deadlines. The device's installed aiortc/teleoprtc stack also delivered correctly labeled synthetic frames on all three tracks. The user subsequently verified the three physical feeds.

## Recovery

Keep NAP Dash and openpilot at the paired saved commits recorded with the recovery backup. To restore this feature from the saved Git history, restore `nap_sentry.py` and `server_v21.py` in NAP Dash and the Sentry integration in openpilot's `system/manager/process_config.py`, preserve the server symlink, and reboot while parked. The accompanying source backup includes these three working files without private runtime configuration. Existing unrelated settings, driving tuning, and camera recordings are outside this feature's backup.
