---
name: joplin
description: >-
  Use when working with Joplin — the Data API sidecar, notebook hierarchy, note
  creation/append/read, sync status, or Loki's semantic memory which is backed
  by Joplin. Covers joplin_integration.py and semantic_memory.py. Read before
  any note-writing change; the note-lookup rule here is subtle and has
  regressed before.
---

# Joplin

## Authoritative runtime

`loki-joplin-desktop.service` — Joplin **Desktop 3.7.9** running headless under
Xvfb, `User=g2k247`, `MemoryMax=3G`, `Restart=always` (RestartSec=15). It serves
the Data API on `http://127.0.0.1:41184`. Profile:
`/home/g2k247/docker/joplin-api/data`.

Joplin **Server** is separate: containers `joplin` + `joplin-db`.

`Environment=APPDIR=` is required in the unit — AppRun's auto-detect breaks when
arguments are passed.

## The obsolete sidecar — do not resurrect

`loki-joplin-api` is the **old CLI 3.6.2 sidecar container**, superseded
2026-07-19. It is kept **stopped with `restart=no`** for rollback.

- It mounts the **same profile** as the desktop service, and two Joplin
  instances cannot share one SQLite profile — so it crash-looped in
  `JoplinDatabase.initialize` and was SIGKILLed.
- Its `Exited (137)` with `OOMKilled=false` and no memory limit is **not** an
  OOM and **not** an outage. It is intentional.
- It was removed from the `joplin` asset's container list. Monitoring it had
  manufactured a permanent false incident.

Health reports that blame it are misreading which component serves 41184.

## The note-lookup rule — this regressed once, keep it fixed

An unscoped `find_note_by_title` must search **EVERY** notebook via one
`/notes` listing, and must **not** rely on `/search`.

Two reasons, both real:

1. **Joplin's FTS index lags creation by seconds.** `/search` immediately after
   a write returns nothing.
2. **Scoping to `Loki/` hid the Boss's own notes.** A note written to
   `Personal/Officer Logs` was invisible to the next read, and `note_append`
   then created a **duplicate** in `Loki/Inbox`, splitting content across two
   notebooks.

`append_or_create(..., search_all=True)` is for "append to note X" with no
notebook named. Log writers (journals, work sessions) keep the **scoped** match
deliberately.

## Notebook tools

`notebook_list`, `notebook_tree`, `notebook_get`, `notebook_children`,
`notebook_notes`, `note_move`, `note_create(notebook=)`. Validated against
production: `Personal → Officer Logs` resolves both by full path and by bare
name.

Also: `note_search`, `note_read`, `note_append`, `list_create`,
`joplin_sync_status`.

## Memory ownership

**Joplin `Loki/Memories` is the source of truth** for explicit Boss facts.
ChromaDB `boss_memory` is a rebuildable index and is **never authoritative** —
`semantic_memory.reindex()` rebuilds it from Joplin daily and at startup.

Dedupe threshold 0.12, recall 0.62. "Forget" archives the note and drops the
embedding; it does not delete history.

Do not merge Joplin, Chroma, and the SQLite stores.

## Testing

**Never write to Joplin while testing** — notes reach the Boss's real devices
through sync. Mock `joplin_integration` calls. If a live check is genuinely
required, ask for approval and read only.

Read-only checks:

```bash
systemctl is-active loki-joplin-desktop.service
journalctl -u loki --since today | grep -i joplin
```

Sync health is parsed from the desktop log by `sync_health()`.

## Historical artifacts — leave alone

- Rollback image tag `loki/joplin-api:pre-3.7-20260719`.
- Pre-migration backup `~/backups/joplin-api-premigration-20260719-194826/`.
- The Joplin source checkout in `~/builds/` and the Dockerfile in
  `~/docker/joplin-api-candidate/` are **obsolete** — never used, since the
  cutover used the prebuilt AppImage.
- The quarantined note/folder deletion queue in
  `~/backups/joplin-api-repair-20260719-014500/` was never restored and **must
  not be**.
- `deleted_items` rows are revision-TTL housekeeping (type 13), not data loss.

## Completion criteria

Notes Loki writes are readable back from any notebook · no duplicate note
created by an append · sync status honest · the CLI sidecar left stopped · no
production note written during testing.

## Source

`joplin_integration.py` · `semantic_memory.py` · `assistant_tools.py` ·
commits `f89e503`, `40b3f5e`, `dc479a6`
