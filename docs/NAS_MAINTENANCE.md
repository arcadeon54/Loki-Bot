# UGREEN NAS maintenance path — security caveats

Loki reaches the UGREEN NAS (`Unimatrix0001`, 192.168.1.63) through exactly one
route: a root-owned dispatcher at `/usr/local/sbin/loki-nas-maint`, invoked over
SSH via the `nas-maint` alias with a dedicated Ed25519 key. Read this before
changing anything in that path.

## The containment boundary is the sudoers rule, not the SSH key

**UGOS sets a global `ForceCommand /etc/ssh/force_command.sh` in
`/etc/ssh/sshd_config` (line 123). A global `ForceCommand` overrides any
per-key `command=""` restriction in `authorized_keys`.**

That script branches on group membership:

- **`admin` / `root` group** → `exec "$SHELL" -c "$SSH_ORIGINAL_COMMAND"`, i.e.
  arbitrary commands. The per-key forced command is bypassed entirely.
  `unimatrix_001` is in `admin`, so this is the branch it takes.
- **non-admin** → only `rsync`, `sftp-server`, `scp`; everything else is sent to
  `nologin`.

This has two consequences:

1. The `command="…"` on Loki's key is **currently inert**. It is left in place
   deliberately as fail-safe defense-in-depth — if UGOS ever drops its global
   `ForceCommand`, the key becomes confined immediately instead of silently
   staying open. **Do not treat it as today's containment.**
2. A "dedicated restricted non-admin account" does not work either: a non-admin
   user cannot run the dispatcher at all, because `sudo /usr/local/sbin/…`
   falls through to the reject branch.

**What actually contains Loki today** is `/etc/sudoers.d/loki-nas-maint`, which
enumerates six literal commands:

```
unimatrix_001 ALL=(root) NOPASSWD: /usr/local/sbin/loki-nas-maint host_status
… container_inventory / tracearr_status / tracearr_dependencies
… tracearr_recent_logs / tracearr_update_check
```

No trailing wildcard. Even if the dispatcher's own argv validation regressed,
sudo refuses anything not literally listed.

## Prohibited, permanently

- **Docker group membership** for `unimatrix_001` (or any Loki account). Docker
  socket access is root-equivalent on the NAS.
- **Wildcard docker sudo** (`NOPASSWD: /usr/bin/docker *` and similar). The
  dispatcher exists precisely so this is never needed.
- Adding a state-changing verb (restart/stop/pull/recreate/exec) to the
  dispatcher. It has none by construction; updates on the NAS are Watchtower's
  job, and any change needs an approved plan.

## Open issue: `/home/unimatrix_001` is mode 0777

The NAS home directory is world-writable, and `~/.ssh` with it. This is why
`scp`/SFTP behaves oddly and why `authorized_keys` had to be written over the
shell channel. Under default `StrictModes yes`, sshd would normally refuse a key
from a directory this loose, so UGOS is evidently relaxing that.

**This is a real security issue and is NOT fixed.** Remediation needs care:
tightening `~` or `~/.ssh` on a UGOS box can break the vendor's own file
services (Samba/SFTP shares, app-store services running as this user), so it
needs a tested change and a rollback plan rather than a quick `chmod`. Treat it
as separate work with its own verification.

## Fragility to expect

UGOS firmware updates can reset the SSH toggle, wipe `authorized_keys`, and
clear `/etc/sudoers.d`. When that happens the tools fail loudly with a precise
message naming the missing component (see `_classify_ssh_failure` in
`nas_maint.py`) — they never fall back to telling the Boss to run docker by hand.
