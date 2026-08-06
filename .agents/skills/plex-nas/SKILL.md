---
name: plex-nas
description: >-
  Use when diagnosing Plex, Jellyfin, media playback, NAS network throughput,
  or NAS disk health from Loki. Covers the plex_* and nas_* diagnostic tools in
  nas_maint.py. Read before investigating a "media won't play" or "server is
  slow" report.
---

# Plex / Media / NAS Diagnostics

## Tools

All read-only except `plex_start`, and all routed through the restricted NAS
dispatcher where they touch the NAS (commit `90fdb30`):

| Tool | Answers |
|---|---|
| `plex_status` | Is Plex up? |
| `plex_diagnose` | Why is Plex unhealthy? |
| `plex_sessions` | Who is streaming right now? |
| `plex_playback_diagnose` | Why is playback failing or buffering? |
| `plex_start` | Start Plex (the one state-changing action) |
| `nas_network_status` | NAS link state |
| `nas_network_speed_test` | Throughput — for "it buffers" reports |
| `nas_disk_status` | Disk health and capacity |
| `nas_status` | General NAS host status |

## Diagnostic order for "media won't play"

Work outward from the client, and stop as soon as a layer explains it:

1. `plex_status` — is the server even up?
2. `plex_sessions` — is it a single session or everything?
3. `plex_playback_diagnose` — transcode vs direct play, and the specific error.
4. `nas_network_status` / `nas_network_speed_test` — buffering with a healthy
   server usually means throughput, not Plex.
5. `nas_disk_status` — a failing or full disk presents as random playback
   failures.

Do not jump to the NAS first. Most reports resolve at layer 1–3, and NAS probes
are the slowest and most invasive.

## Jellyfin

Registry asset `jellyfin` with its own runbook (`jellyfin_health`).

**`/media/nas/Sports` is a genuinely empty library** — empty since June, not a
mount failure. `jellyfin_health` treats empty media directories as *notes*, not
faults. Do not "fix" it.

Jellyfin is one of the containers watchtower auto-updates daily — if its version
changed unexpectedly, that is why (see the `container-updates` skill).

## NAS constraints apply

Everything NAS-side goes through `/usr/local/sbin/loki-nas-maint`. There is no
shell, and the dispatcher's action list is fixed. If a diagnosis needs data the
dispatcher does not expose, **report that honestly** — do not propose adding a
verb to the dispatcher to get it.

Remember the decoy: the NAS's own UGOS redis on `127.0.0.1:6379` is not
Tracearr's Redis. See the `nas-maintenance` skill.

## Reporting

Name the earliest layer that explains the symptom. A list of every probe that
came back non-green reads as many faults when there is usually one — the same
mistake the BLACK-BOXX runbook was fixed to stop making.

If a probe could not run, say the state is UNKNOWN rather than reporting the
subsystem as failed.

## Completion criteria

Root cause named at a specific layer with the evidence quoted · no dispatcher
verb added · no speculative NAS change · `plex_start` used only when Plex is
genuinely stopped and the Boss asked for it.

## Source

`nas_maint.py` · `maintenance_runbooks/` (`jellyfin_health`) ·
`docs/NAS_MAINTENANCE.md` · `config/homelab_assets.yml` (`plex`, `jellyfin`,
`ugreen-nas`)
