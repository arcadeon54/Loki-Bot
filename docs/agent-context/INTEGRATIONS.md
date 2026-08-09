# Integrations

Configuration variable **names** only. Values live in `.env` and are never
reproduced in documentation, prompts, logs, or commit messages.

## Chat surfaces

| Integration | Vars | State |
|---|---|---|
| Discord | `DISCORD_TOKEN`, `OWNER_USER_ID`, `CREW_USER_IDS`, `HA_NOTIFY_CHANNEL_ID`, `JOBSITE_CHANNEL_ID`, `MAINTENANCE_OPS_CHANNEL_ID` | DONE |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID` | DONE (text/media); voice messages UNFINISHED |
| Duplicate-link guard | `DUPLICATE_LINK_DETECTION_ENABLED`, `DUPLICATE_LINK_EXCLUDED_CHANNEL_IDS`, `DUPLICATE_LINK_RETENTION_DAYS`, `DUPLICATE_LINK_ESCALATION_WINDOW_DAYS`, `DUPLICATE_LINK_WARNING_DELETE_AFTER`, `DUPLICATE_LINK_CAPTION_MAX_CHARS` | DONE |

Telegram pairing is single-owner via `telegram_state.json`. **Deleting that file
re-opens first-come pairing** — treat it as a credential.

## Models

| | Vars |
|---|---|
| Primary | `OPENAI_API_KEY`, `OPENAI_MODEL` (gpt-5.1) |
| Fallback / Groq | `FALLBACK_LLM_URL`, `FALLBACK_LLM_MODEL`, `FALLBACK_LLM_API_KEY` |
| Local | `LOCAL_LLM_URL`, `LOCAL_LLM_MODEL`, `LLM_PROVIDER` |
| Vision | `GEMINI_API_KEY` (`google.generativeai` is EOL — migration planned) |
| Voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` |

Routing table is `routing.json`. Settled — do not re-litigate.

## Home Assistant

`HA_URL`, `HA_TOKEN`, `HA_WEBHOOK_PORT`, `PRESENCE_BOSS_ENTITY`,
`PRESENCE_ROOMMATE_ENTITY`, `PRESENCE_BOSS_NOTIFY`, `PRESENCE_ROOMMATE_NOTIFY`,
`PRESENCE_COOLDOWN_MINUTES`.

HA automations POST `{title, message}` to Loki's `/ha-notify` webhook.
`ha_integration.get_smart_notification()` rewrites through Groq using
`personality.HA_NOTIFICATION` — **except** the four Boss presence transitions,
which bypass it. Before changing any HA notification wording, check whether the
text originates in the HA automation or in Loki's rewriter:
`GET {HA_URL}/api/config/automation/config/{id}` shows the real payload.

## Joplin

`JOPLIN_API_URL` (`http://127.0.0.1:41184`), `JOPLIN_API_TOKEN`, `LOKI_NOTEBOOK`.
Served by `loki-joplin-desktop.service`. See the `joplin` skill.

## Storage / search / media

| Integration | Vars |
|---|---|
| ChromaDB | `CHROMADB_HOST`, `CHROMADB_PORT` (:8100) |
| SearXNG | `SEARXNG_URL` (:8083) |
| Tavily | `TAVILY_API_KEY` |
| Jellyfin | `JELLYFIN_URL`, `JELLYFIN_API_KEY` |
| Overseerr/Jellyseerr | `SEERR_URL`, `SEERR_API_KEY` |
| Cobalt (downloads) | `COBALT_URL`, `DOWNLOAD_DIR` |
| Nextcloud (private DM delivery) | `NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_PASS`, `NEXTCLOUD_PUBLIC_BASE_URL`, `NEXTCLOUD_SHARE_EXPIRY_DAYS`, `NEXTCLOUD_KEEP_CLEARS_EXPIRY` |

### Nextcloud: two URLs, never interchangeable

`NEXTCLOUD_URL` is the **internal** endpoint Loki talks to (`192.168.1.63:8082`,
the NAS). `NEXTCLOUD_PUBLIC_BASE_URL` is the **public** origin a recipient's
browser opens (`https://cloud.ivn-group.cc`, via nginx-proxy-manager). Confusing
them is what made this feature ship dead links.

**Using the OCS response's own `url` field is not sufficient.** This server has
no `overwritehost`/`overwrite.cli.url` set behind the proxy, so it reports
`https://192.168.1.63:8082/s/<token>` in its own API output. Loki therefore
takes the *token* from the API — never invents one — and re-bases only the
origin onto the public base. If Nextcloud's config is ever corrected, the
returned URL is already public and the re-basing becomes a no-op.

Public links are read-only (`permissions=1`, `publicUpload=false`), scoped to a
single file or to that request's own batch folder, and expire after
`NEXTCLOUD_SHARE_EXPIRY_DAYS` (default 3 = 72 h; `0` disables). "Keep" clears
the expiry so the link persists, matching the historical behaviour; set
`NEXTCLOUD_KEEP_CLEARS_EXPIRY=false` to leave the expiry in place.

**`.env` is the only source of configuration** — `loki.service` sets no
`EnvironmentFile`, so the process starts with a bare environment.
`load_dotenv()` therefore MUST run before any project module is imported;
several read `os.getenv` at import time and would otherwise be pinned to their
hard-coded fallbacks forever. Pinned by `tests/test_nextcloud_share.py`.

## Hermes escalation (razr)

`HERMES_WORKER_URL`, `HERMES_WORKER_TOKEN`, plus guard tuning:
`HERMES_GUARD_FAILURE_THRESHOLD`, `HERMES_GUARD_COOLDOWN_SECS`,
`HERMES_GUARD_COOLDOWN_MAX_SECS`, `HERMES_GUARD_BILLING_COOLDOWN_SECS`,
`HERMES_MAX_REQUESTS_PER_HOUR`, `HERMES_MAX_REQUESTS_PER_DAY`,
`HERMES_MAX_SPEND_USD_PER_DAY`.

The OpenRouter key lives on **razr**, not dex247.

## Work tracking

`WORK_PERSON_ENTITY`, `WORK_HOURLY_RATE`, `WORK_CONFIRM_MINUTES`,
`WORK_MOVE_THRESHOLD_M`, `GOOGLE_SHEETS_CONFIG_ENTRY`.
Sessions land in SQLite + Joplin + Sheets. **The Sheets half is broken**
(`sheets_ok` 2/15) — SQLite and Joplin are fine, so no data is lost.

## Career-Ops (razr)

`CAREER_OPS_API_URL`, `CAREER_OPS_TOKEN` (bridge on razr, `agy -p` worker).
State in gitignored `career_ops_state.json`.

## Monitoring

`MONITOR_SUMMARY_INTERVAL_SECS`. Homelab monitor polls every 300s.

## Not integrations, but adjacent

- **skillkit** at `/home/g2k247/skillkit` — mirrored into Loki as 29 `skill_*`
  tools by `skill_bridge.py`.
- **watchtower** on dex247 — an *unmanaged* auto-updater currently conflicting
  with Loki's approval gate. See `COMPLETED_WORK.md`.
