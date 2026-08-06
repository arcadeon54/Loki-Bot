# Secrets Policy

## Where secrets live

- `/home/g2k247/loki-bot/.env` — live secrets, gitignored.
- `.env.bak*` — **also live secrets**, gitignored, on disk. Treat identically.
- `~/.gemini/` — Antigravity OAuth tokens (keyring with file fallback).
- `~/.ssh/` — SSH keys for razr and the NAS dispatcher.
- `telegram_state.json` — single-owner pairing. Deleting it re-opens first-come
  pairing, so treat it as a credential.
- The OpenRouter key for Hermes lives on **razr**, not on dex247.

## Rules

- **Never print a secret value.** Variable names only; values as `REDACTED`.
- Never write a secret into project documentation, a commit message, a prompt, a
  log line, or a command argument.
- Never paste a token into a chat surface to "test" it.
- Never echo `.env` or grep it in a way that prints values. To list which
  variables exist, read `.env.example`, which is the names-only reference.
- Never commit `.env`, `.env.bak*`, private keys, or credential files.
- Never disable TLS verification or fall back to plain HTTP for a production
  service.

Reading `.env` and friends is in the **DENY** tier and is blocked by
`.agents/hooks.json`.

## In code

- No hardcoded secrets, API keys, tokens, or passwords.
- Read configuration from the environment, never from a literal.
- Parameterized queries only — never string concatenation into SQL.
- Validate and sanitize all external input: user messages, API responses, file
  contents, and anything a model produced.

## In logs

`tools.py` redacts credentials from the tool-call log, and content-bearing tools
set `redact_log` so their arguments and results are never written at all. When
adding a tool that carries note bodies, memories, or anything the Boss might
have dictated, set `redact_log=True`.

The maintenance controller has its own `redact()` applied to check details,
diagnoses, and escalation bundles before anything is stored or sent to Hermes.
Use it for any new field that leaves the process.

## If a secret is exposed

Say so immediately and precisely — which secret, where it landed, and when.
Do not quietly rotate, do not minimise, and do not bury it in a summary.
Rotation is the Boss's call.
