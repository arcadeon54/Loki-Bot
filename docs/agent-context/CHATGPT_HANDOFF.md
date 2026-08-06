# ChatGPT Handoff

A compact continuity block for a brand-new ChatGPT session with no history.
Paste the block below verbatim, then ask your question.

---

```
CONTEXT: Loki homelab project.

Loki is a production personal AI assistant for Kavaris ("Boss"), running on a
Linux host called dex247 under account g2k247. The repository at
/home/g2k247/loki-bot IS the deployment: systemd unit loki.service runs
venv/bin/python loki_bot.py directly from that working tree, so any edit there
is live after the next restart. Not containerized. Branch master, 46 commits
ahead of origin, nothing pushed on purpose.

SURFACES: Discord (public persona + serious DMs), Telegram @Leauxki_Bot (always
serious), Home Assistant notifications, voice. Roommate Ammiel = "Rob",
crew-level user.

MACHINES:
- dex247 — runs Loki, the BLACK-BOXX WiFi AP, most Docker services.
- razr — headless, SSH only. Hosts Hermes (escalation agent, OpenRouter-backed),
  the Career-Ops bridge, and a browser research worker. Tailscale razr-1.
- UGREEN NAS (192.168.1.63) — storage plus Tracearr. Loki has NO shell there;
  it uses a root-owned dispatcher /usr/local/sbin/loki-nas-maint exposing six
  read-only actions.

ARCHITECTURE: loki_bot.py is a ~6,600-line monolith entrypoint with satellite
modules (tools.py registry, personality.py owns all tone, telegram_interface,
ha_integration, joplin_integration, semantic_memory, homelab_maintenance,
task_supervisor, draft_approval, nas_maint, hermes_guard, and others). A
separate repo /home/g2k247/skillkit owns the operational brain (planner,
playbooks, verification); Loki is just a caller.

SAFETY MODEL: Maintenance shell commands are built from fixed argv templates in
maintenance_policy.py with registry-validated parameters and no shell. Every
action has a fixed tier — AUTO, APPROVAL, MANUAL — declared in code, never
decided by a model. Consequential tools are marked in the tool registry and go
through a durable draft-and-approve gate with payload hashing before they can
run. Hermes only diagnoses and proposes; it never executes.

MEMORY OWNERSHIP: Joplin notebook Loki/Memories is the source of truth for
explicit facts. ChromaDB boss_memory is a rebuildable index, never
authoritative. SQLite holds conversation history, work sessions, incidents,
drafts and tasks. These systems are deliberately not merged.

RECENTLY COMPLETED (all local commits, verified live):
- BLACK-BOXX diagnosis fixed so one dead unit reports one root cause instead of
  twelve symptom failures (8355d21), and the wg-quick@wg-ap boot race removed so
  black-boxx-ap.service is the sole boot-time owner of the wg-ap tunnel
  (42380d1). AP currently healthy, 17/17 checks.
- Hermes/OpenRouter circuit breaker with per-hour, per-day and spend budgets
  (51fda47).
- Maintenance incident dedupe plus a Discord ops feed, replacing Telegram spam
  (daf150e).
- Tracearr updated to v1.5.0 through an approval-gated, digest-pinned, backed-up
  and verified path (b075780, 251807b).
- Joplin note read-back fix (dc479a6) and presence notification passthrough
  (ede172d).

KNOWN UNFINISHED: Google Sheets work-session export is broken (SQLite and Joplin
halves fine, no data loss); a weekly Discord export returns 403 and needs a
Discord-side permission; watchtower auto-updates every container daily and
bypasses Loki's approval gate; Tracearr restarts repeatedly with an unproven
cause and deliberately no auto-repair; Telegram voice messages are ignored.

DO NOT REOPEN: BLACK-BOXX, the Tracearr v1.5.0 path, the obsolete Joplin CLI
sidecar, maintenance notification amplification, the Hermes guard, or the
gluetun/qBittorrent pairing.

RULES I EXPECT YOU TO FOLLOW: never suggest pushing git without my explicit
say-so; never suggest restarting loki.service, containers, or migrations
without approval; never print or ask me to paste secrets (variable names only);
prefer fixing a defect and continuing over stopping to report it; treat live
system state as outranking any document; and remember that passing tests is not
the same as done.

The authoritative durable context lives in the repo at docs/agent-context/ and
AGENTS.md. If my question depends on current state, ask me to run a specific
read-only command rather than guessing.
```

---

## Keeping this current

Refresh the "recently completed" and "known unfinished" paragraphs whenever
`COMPLETED_WORK.md` changes materially. Everything above the fold — machines,
architecture, safety model, memory ownership — is stable and rarely needs
editing.
