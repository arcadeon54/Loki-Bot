# Claude History Import

Institutional knowledge carried over from Claude Code sessions (2026-07-19 →
2026-08-06), so that no Claude conversation remains load-bearing.

**This is historical context — precedence rank 6.** Live production state,
current code, and `CURRENT_HANDOFF.md` all outrank it. Where this file
disagrees with the running system, the running system is right and this file is
stale.

---

## Traps already paid for

Each of these cost real time or real damage. Do not rediscover them.

### The twelve-symptom report

BLACK-BOXX monitoring reported twelve subsystems failing simultaneously. They
were not twelve faults — `black-boxx-ap.service` owns the whole stack, and when
it dies every downstream probe fails as a symptom. A flat list of twelve names
told the Boss nothing and pushed a fabricated "unknown condition" escalation to
Hermes. Fix: probe the executor first, name the earliest failure in dependency
order as root cause, mark dependents `SKIPPED` (never "failed" — that asserts
evidence never gathered).

### The 297-job billing storm

Escalation closed the incident and started a cooldown, so a fault that never
resolved minted a new incident and a new billed Hermes job every cycle. Fix:
incidents stay active until verified recovered health.

### Legacy task rows re-announcing to Telegram

302 autonomous `hermes_escalation` rows already carried `channel_id =
"tg:739041549"` from before the ops feed existed. They are long-lived (a
`paused_quota` row never finishes), so every restart re-announced them. Routing
on channel id was insufficient; autonomy had to be decided by requester
identity, plus an idempotent data repair re-addressing legacy rows on connect.

### The Joplin note that vanished

An unscoped `find_note_by_title` scoped to `Loki/` meant a note written to
`Personal/Officer Logs` was invisible to the next read, and `note_append` then
created a duplicate in `Loki/Inbox`, splitting content across two notebooks.
Joplin's FTS index also lags creation by seconds, so `/search` is unreliable
immediately after a write — list `/notes` instead.

### The obsolete container that manufactured a permanent incident

`loki-joplin-api` (CLI sidecar) mounts the SAME profile as the desktop service.
Two Joplin instances cannot share one SQLite profile, so it crash-looped and was
SIGKILLed. Its `Exited (137)` with `OOMKilled=false` is **not** an OOM and **not**
an outage — it is intentional. Health reports flagging it were misreading which
component serves 41184.

### The test suite that deleted production data

A new test file importing `loki_bot` sorted alphabetically before
`test_homelab_lifecycle.py`, so that file's writes landed on the real
`homelab_incidents.db` and `config/homelab_lifecycle.yml`, destroying the live
`ivn-site` decommission record. Recovered from git history + journalctl + the
Joplin "Homelab Incidents" note, with explicit sign-off before writing back.
Mocked NAS update tests separately rewrote the production pinned digest with a
fixture value.

### The false-premise detour

Duplicate social links were reported broken. An earlier session misdiagnosed it
as a missing "Hell Yeah Films forwarding" feature — a channel that never
existed — and built a parallel subsystem instead of repairing the real guard.
Corrected mid-session. **Verify the premise against live evidence before
building anything.**

### The rewriter that mangled correct text

HA already sends several messages fully formatted in Loki's voice. The Groq
rewriter turned `🏠 - Welcome home, Boss - 🏠` into narrated sentences. The
wording was never wrong in HA; it was wrong in Loki.

### The digest that never matches

`docker manifest inspect` digests do NOT match local image digests (platform vs
index). Compare `docker image inspect .Id`/RepoDigest against
`docker buildx imagetools inspect`'s `Digest:` line.

### The major release that almost shipped

`tracearr_update` always targeted "latest stable", and v2.0.0 landed upstream
hours before an approved "v1.5.0" run — which would have silently applied a
major release. Version pinning exists because of this.

### Small ones worth keeping

- `du` on the Immich upload library times out; use `df`.
- Splatting a result dict into a helper that also takes `ok` collides at call
  time — filter at the call site.
- A test that swaps `hm._registry` must restore it in `tearDown`, or every later
  test module inherits the fake homelab.
- `/media/nas/Sports` is a genuinely empty Jellyfin library, not a mount failure.
- Guard SQLite tables use an autocommit connection (`isolation_level=None`); a
  lingering transaction there deadlocks the maintenance controller.

## Working relationship notes

- The Boss wants **completion**, not audits. Finish the requested objective;
  fix defects found in that path and keep going.
- Report faithfully. If something did not happen, say so plainly with evidence —
  a claimed success that the system contradicts is worse than a failure.
- Approval means approval for that specific action, not a standing grant.
- "Tests pass" has repeatedly not meant "it works". Verify live.

## Source

Distilled from the Claude Code project memory at
`~/.claude/projects/-home-g2k247-loki-bot/memory/` (16 files) plus session
transcripts. That directory is Claude-specific and is **not** a dependency of
this repository — everything load-bearing has been copied here.
