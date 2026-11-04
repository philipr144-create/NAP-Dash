# Working comma 3X setup checkpoint — 2026-11-04

This checkpoint saves the owner's current NAP / Philly Fork source from the running comma 3X. It is a recoverable working configuration, not a claim of full road validation.

## Matching repositories

- OpenPilot: philipr144-create/openpilot-preap, main, `3ea2b8b2ec7501bebf72328dc5cd8cc410f76bce`.
- opendbc: philipr144-create/opendbc-preap, main, `8b26498f8bae39e005c870054600c5ef7054a215`. OpenPilot pins this exact submodule commit and tracks its main branch.
- Dashboard: this commit's main branch contains the active `/data/server` source. Existing remote files outside that source overlay have been preserved.
- Bridge support: `nap-mcp/` contains the device bridge's Python, service and shell source. Its runtime databases and credentials are excluded.

## Device layout and restoration

The dashboard package lives at `/data/server`; OpenPilot's tracked `server` symlink points there. The original `/data/NAP-Dash` checkout was older and had diverged from remote main, so publication used an isolated checkout of remote main overlaid with the running source. No checkout, pull, build, or restart was applied to the live dashboard as part of saving this checkpoint.

Restore the matching repository commits and submodules while offroad. Preserve this vehicle's existing calibration, private configuration, pairing and settings; source commits do not include those runtime values. Reuse the private setup backup where needed. The private backup also retains the earlier local-only commit history, original uncommitted diffs, old source copies and preview artifacts.

## Saved behavior

The setup includes consolidated on-road navigation/intent and speed/control displays, the guarded 0.5-second tap lane-change lead-in, current city-turn/shared speed planning and engagement changes, Tesla-primary navigation selection with comma fallback, bounded accuracy corroboration, clock-synchronization route-cache recovery, and coordinated navigation settings.

The synthetic turn-signal fix preserves navigation confirmation across normal dark flash intervals only after a matching lamp has actually been observed, with fresh feedback for the same controller/route/maneuver. Long darkness, stale feedback, hazards/opposite signals, takeover and invalid route/vehicle state still revoke authority. Manual blinker values and CAN frame generation remain unchanged.

## Validation and limits

The 14 portable synthetic-flash regression tests pass against the installed device code, including the model, real signal-feedback publisher/controller, steering executor and speed-plan consumers. Existing city-turn/lifecycle suites have 28 passing tests and 2 passing subtests, plus one unchanged pre-existing speed-ceiling release test failure. New flash-gap behavior has not been verified on a drive. GPS quality, model geometry, active control and the existing enable settings remain required. Saving this checkpoint did not enable maneuver assistance.

Runtime credentials, OAuth records, pairing keys, logs, Finder metadata, generated caches and old `.bak` copies are not published as source.
