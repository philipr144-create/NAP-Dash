# NAP Sentry V2

Parked security for NAP Dash on comma 3: motion events from any camera and grouped front/wide-front/cabin evidence, plus simultaneous front/wide-front/cabin WebRTC viewing. No object recognition, neural inference, tracking, pre-event buffer, driving parameters, or shutdown overrides.

## Use

Open **NAP Dash → Sentry**. Use the existing PIN (`000000` by user request). The existing “Remember on this browser” setting is preserved. Connect displays actual sensor, storage, and safety status.

- **Resume monitoring / Pause until next drive:** Control parked monitoring. Automatic offroad arming is enabled by default. After a safety-stabilization delay (normally ten seconds, sixty after a safety interruption), monitoring starts without a browser or a manual Arm request and continues while powered, offroad, and safe. There is no automatic-mode 30-minute timeout. Resume monitoring starts it explicitly; Pause until next drive is remembered across Dash/device restarts and clears when driving/ignition is observed again. The auto-arm checkbox can disable the feature persistently. Legacy timed manual sessions remain available when automatic arming is disabled.
- **Live view / Stop live view:** Connect all three cameras on demand. Five minute absolute limit, 20 second lost-heartbeat timeout. Ending live view leaves an independently armed monitor running. Disarm stops all security activity.
- **Snapshot:** Save a low-resolution grayscale front-camera image without requiring streaming or video encoding.
- **Manual record / Stop recording:** Save grouped front, wide-front, and cabin MP4s. Default duration/tail is 30 seconds; maximum clip duration is 120 seconds. Ongoing motion extends the tail within the maximum; trigger cooldown defaults to 15 seconds.
- **Motion detection:** Toggle simple frame differences while armed. No neural models.
- **Events:** Play clips, inspect thumbnails, protect/unprotect events, or delete individual events. Unprotected finalized evidence expires after 24 hours. Cleanup runs at startup and at least once per minute while Dash is running, even if monitoring is paused or disabled. Protected evidence is exempt. The quota may remove oldest unprotected evidence sooner. Older-event pagination covers the full bounded history. Protect prevents automatic retention deletion, but explicit Delete still removes the selected event.
- **Settings:** Disarm and stop live viewing before saving. Conservative limits constrain all configurable values.

Remote live media requires direct private/Tailscale connectivity to the comma. An HTTPS page tunnel alone does not relay WebRTC media. All API/evidence routes require the PIN. The WebRTC listener is loopback-only at `127.0.0.1:5002`. No unauthenticated public camera or recording endpoint is added.

## Resource states

`nap_sentry.py` coordinates short volatile resource leases and safety checks inside the existing Dash server. `sentry_common.py` provides validated configuration, leases, and bounded storage. `sentry_page.py` provides the native Dash interface.

- **Disarmed:** No Sentry worker, Sentry encoder, Sentry WebRTC, or security camera demand. Legitimate driver view or normal onroad camera activity remains independent.
- **Armed monitoring:** `camerad` plus `server.sentry_worker`. The worker samples existing VisionIPC NV12 luma at 1–2 fps, downsamples before processing, blurs, compensates median brightness shifts, rejects isolated pixel noise, and requires consecutive motion confirmation. No encoding runs merely to monitor motion.
- **Recording:** `server.sentry_encoder` execs the existing hardware `encoderd --sentry`, with a parked-only bounded profile. The worker waits for a keyframe, writes all three camera H264 streams, then remuxes with the existing `ffmpeg_full` using stream copy. No software video re-encoding.
- **Viewing:** The same parked encoder also provides all three live tracks through the existing teleoprtc/aiortc infrastructure. Recording and viewing share it.
- **Suspended:** Invalid/stale safety data, ignition/onroad state, shutdown/reboot request, high temperature, invalid power, or worker/camera failure removes security demand. No automatic restart occurs while conditions remain unsafe. Safe thermal/power recovery is delayed and uses temperature hysteresis; camera/worker faults are latched for the parking period.

The camera hardware still captures its native streams. Lower motion-analysis rate reduces analysis work, not camera hardware power. The existing stream encoder architecture starts all three hardware encoding tracks when recording or viewing, and evidence preserves all three under one event. This implementation does not add separate full-resolution capture or image conversion.

## Safety and storage defaults

- Valid device/panda/peripheral messages no older than three seconds; known panda; ignition off and `deviceState.started == false`.
- Existing `IsOnroad`, `DoShutdown`, and `DoReboot` requests are respected, never changed.
- Existing hardware thermal status must remain green. At 70 °C device max, analysis reduces to 1 fps; at 75 °C security suspends. Configurable suspension is capped at 75 °C, matching the installed openpilot offroad danger threshold; hardwared remains unchanged. Discovered sensor readings are displayed; gating uses openpilot's `deviceState.maxTempC` and thermal status. Some thermal-zone entries are virtual/control values rather than the temperature used by hardwared.
- Supply 12–16 V, or the verified bench exception of 4.75–5.25 V only with every panda reporting a disconnected vehicle harness. Upstream battery voltage behind regulated power is not inferred from a 5 V reading.
- Security lease lifetime three seconds. Failed/stalled Dash monitoring releases demand. The manager independently rejects Sentry predicates when started/onroad.
- `/data/nap_sentry/events` is separate from driving logs. Default allowance 512 MB, free-space reserve 1 GB, hard event-count limit 1,000. Oldest unprotected finalized events are removed first. Active clips cannot be deleted. If protected files or low free space prevent pruning, new recording is refused/stopped.
- Per-clip byte cap is the smaller of 128 MB and half the security allowance. Clip lengths and repeated trigger frequency are bounded. No encoded pre-event footage is retained; the subscriber drains unused packets.
- Metadata is atomic JSON; active recordings use `.partial.h264`. Interrupted clips are remuxed on the next worker start when decodable, or marked failed; orphan partial files are removed only inside the security evidence directory. Finalization timeout is bounded to fit manager shutdown.

Configuration lives at `/data/nap_sentry/config.json`; pause/fault intent is in `/data/nap_sentry/policy.json`; runtime PIN at `/data/nap_sentry_pin` (0600). Neither is tracked. Events, credentials, and private camera images are not source artifacts.

## Integration and recovery

`/data/openpilot/server` points to `/data/server` (NAP Dash). The openpilot repository contains two scoped changes: dedicated Sentry process conditions in `system/manager/process_config.py`, and an explicitly separate `--sentry` encoder profile in `system/loggerd/encoderd.cc`. Normal driving encoding and `--stream` paths retain their behavior. The shared `notcar` predicate is unchanged.

The process-configuration and encoder patches alongside this document are recovery references. Apply only after `git apply --check` succeeds; do not overwrite newer customized files blindly. Build the encoder using the existing environment:

```sh
cd /data/openpilot
PATH=/usr/local/venv/bin:$PATH scons -j2 system/loggerd/encoderd
```

Reboot while parked after source restoration or updates because the manager caches imported modules. Never restore the V1 encoder binary while V2 recording/viewing is active.

An exact pre-V2 runtime backup, including the original encoder binary, is in `/data/nap_backups/sentry-v2-20261020/`. To return to the already working V1 system, Disarm in Dash, then restore these backed-up files to their matching locations and reboot while parked:

```sh
cp /data/nap_backups/sentry-v2-20261020/nap_sentry.py /data/server/nap_sentry.py
cp /data/nap_backups/sentry-v2-20261020/server_v21.py /data/server/server_v21.py
cp /data/nap_backups/sentry-v2-20261020/process_config.py /data/openpilot/system/manager/process_config.py
cp /data/nap_backups/sentry-v2-20261020/encoderd.cc /data/openpilot/system/loggerd/encoderd.cc
cp /data/nap_backups/sentry-v2-20261020/encoderd /data/openpilot/system/loggerd/encoderd
```

V2 helper modules and stored evidence may remain; V1 does not invoke them. This rollback preserves driving logs, the PIN, and unrelated customizations. Removing Sentry entirely requires restoring the pre-Sentry manager/server versions instead, as documented in the earlier recovery archive.

## Validation

Run the consolidated suite in NAP Dash:

```sh
cd /data/server
PYTHONPATH=/data/openpilot /usr/local/venv/bin/python -m unittest discover -s tests -p 'test_sentry*.py'
```

Thirty-nine regression tests cover motion/brightness/noise, power/thermal/freshness gates, malformed and separate resource leases, authentication including evidence, bounded/non-extending deadlines, recording tail extension, keyframe start, quota retention/protection, free-space rejection, path validation, interrupted recordings, and browser PIN remembering.

Actual comma bench checks also passed: low-rate camera sampling without encoder; real user hand movement triggered a finalized front-camera clip (313 decoded frames, 640×360/10 fps, about 2 MB); real snapshot; manual clip; simultaneous physical three-camera WebRTC frames; heartbeat/disconnect cleanup preserving arming; browser event playback; private thumbnails/video and range requests; protect/unprotect/delete; decodable interrupted H264 recovery; and disarm resource release.

Short bench input-power samples (including a final 58.7-second monitoring sample) were approximately 1.8 W disarmed, 3.7–4.2 W monitoring, 4.3 W recording, and 4.6 W viewing with an on-device validation client. The local viewing client adds CPU/decoding load, so that figure is not a remote-phone benchmark. These are preliminary indoor samples, not summer-heat, battery endurance, or vehicle-runtime claims. See the delivered measurement report for sample durations and limits.

Still required before relying on unattended vehicle security: in-car voltage characterization, ignition transition with the physical vehicle, summer/soak thermal behavior, realistic lighting/motion false-trigger tuning, long-duration parked power/endurance, and normal driving camera/encoder transition confirmation. Thermal and low-power guard logic was tested on-device with controlled test inputs; the physical device was not intentionally overheated or undervolted.

## Automatic offroad and 24-hour history update

Auto-arm is enabled by default (`auto_arm: 1`). Automatic sessions renew only a short watchdog lease, so background monitoring has no parking-duration countdown. The live-view limit remains five minutes. The monitoring/live indicator says **While offroad** for automatic operation. Existing power shutdown behavior is not changed; Sentry cannot monitor or erase files while the device is powered off. Expired evidence is cleaned when Dash next runs. Motion detection and stored event video cover all three cameras; snapshots remain front-only.

The rolling history stores events, not continuous video. Protected events remain until explicitly deleted. The 512 MB default allowance, 1 GB free-space reserve, 1,000-event cap, and all clip limits remain, so retaining every event for a full day depends on available space.

To roll back this update to manual/timed V2, restore `nap_sentry.py`, `sentry_common.py`, and `sentry_page.py` from `/data/nap_backups/sentry-autoarm-20261020/`, restore the backed-up `config.json` to `/data/nap_sentry/config.json`, and reboot while parked. No encoder, manager, driving, or boot UI changes are part of this update.

## Grouped camera events

Motion analysis samples front, wide front, and cabin independently. A trigger from any view starts the same bounded event on all three hardware H264 streams. Default sensitivity is 10 luma levels, 0.8% spatially supported change, and two confirmations, with a short background comparison for small/dark moving objects. Uniform exposure jumps and isolated sensor noise remain suppressed; passing headlights without a visible object are not guaranteed triggers. Darkness and distance still limit detection.

Each event includes per-camera keyframe timestamps, frame counts, offsets, and availability. Dash presents three labeled views with one Play/Pause and seek timeline; offsets use camera capture timestamps. Missing/failed cameras are labeled and do not discard usable evidence. Old front-only clips remain playable. Thumbnails and snapshots remain front-only. There is no continuous recording or pre-event footage.

The motion activity tile reports each camera score; its tooltip includes the threshold and last detection. Temperature, ignition, voltage, quotas, and 24-hour retention apply to the grouped event. Three video tracks consume more event storage than the previous front-only clips.
