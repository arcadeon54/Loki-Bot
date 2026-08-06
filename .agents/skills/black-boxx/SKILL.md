---
name: black-boxx
description: >-
  Use when diagnosing or changing BLACK-BOXX, the WiFi access point on dex247 —
  the wlp2s0 AP, the wg-ap WireGuard tunnel, hostapd, dnsmasq, fwmark policy
  routing, or black-boxx-ap.service. Read before touching the AP stack or its
  runbook. The boot-race investigation is CLOSED; do not reopen it.
---

# BLACK-BOXX

## Status: healthy and settled

17/17 checks green, 0 advisories. The boot-race investigation closed 2026-08-06
(commits `8355d21`, `42380d1`). **Do not reopen it.**

## One unit owns everything

`black-boxx-ap.service` → `/usr/local/bin/black-boxx-start.sh` brings up, in
order: `wg-ap`, then hostapd and dnsmasq, then packet marking, table 100, the
fwmark policy rule, and NAT.

`hostapd.service` and `dnsmasq.service` are **masked on purpose** — the script
drives them directly. So when this one unit is dead, every downstream check
fails as a *symptom*, not as an independent fault.

## Topology

| | |
|---|---|
| AP interface | `wlp2s0`, `192.168.10.1/24` |
| Clients | `192.168.10.0/24` (DHCP `.100`–`.200`) |
| Tunnel | `wg-ap`, `100.64.145.100/32`, MTU 1420 |
| Marking | `iptables -t mangle -A PREROUTING -i wlp2s0 -j MARK --set-mark 100` (`0x64`) |
| Routing | `ip rule` fwmark 100 → table 100, priority 100; table 100 default dev wg-ap |
| NAT | MASQUERADE `-s 192.168.10.0/24 -o wg-ap` |
| Config | `/etc/wireguard/wg-ap.conf`, `/etc/hostapd/black-boxx.conf`, `/etc/dnsmasq.d/black-boxx-ap.conf` |

`canada-ap.service` is an alternate profile, `Conflicts=` with black-boxx-ap,
and is correctly disabled + inactive.

## The two known failure modes

### 1. Missing fwmark policy rule — AUTO repairable

The only condition the runbook auto-repairs. Preconditions: the rule is missing
while wg-ap, marking, table 100 and NAT are all healthy. The repair
(`restore_blackboxx_ip_rule`) is idempotent, verified by re-reading the rule
**and** confirming marked traffic resolves through the tunnel, and rolled back
if verification fails.

### 2. AP unit down — one root cause, not fourteen

`_ap_unit_down_result` returns ONE failed check (`ap_service`) with every
dependent marked `SKIPPED`. Recovery is the AUTO `restart_stateless_service`:
one bounded attempt plus a readiness wait, because the unit reporting `active`
only means the script *started*, not that the tunnel and AP actually came up.

## The boot race — fixed, and why it must stay fixed

`wg-quick@wg-ap` used to be enabled alongside `black-boxx-ap.service`. Both are
`WantedBy=multi-user.target` with no ordering between them, so at boot they
raced to `ip link add wg-ap`. The loser hit `RTNETLINK answers: File exists`,
then ran wg-quick's own error cleanup — `ip link delete dev wg-ap` — destroying
the winner's interface. wg-quick still exited 0, so systemd showed it `active`.
The AP was down for two days.

Fixed by disabling `wg-quick@wg-ap` through Loki's approval gate.
`multi-user.target.wants` now contains only `black-boxx-ap.service`.

### Disable, never stop

`systemctl stop` on a `wg-quick@` unit runs `wg-quick down`, which **deletes the
live wg-ap the AP is currently serving traffic on**. The allowlist deliberately
has no stop/down command and must never gain one.

`wg-quick@wg-ap` therefore still reads `active (exited)` while being `disabled`.
**That is correct, not a half-finished fix.** Do not "clean it up".

## Advisories, not failed checks

The runbook reports boot-ownership conflicts through `result["advisories"]` plus
an approval-tier `service_enable_disable` plan, while `healthy` stays true. A
boot-time fault is invisible in every check of the running path and must never
mark a working AP unhealthy — that would repeat the twelve-symptom problem the
diagnosis fix exists to eliminate.

Consequently, verification of such a repair hangs on the **advisory clearing**,
not on `healthy` (already true beforehand).

## Read-only checks

```bash
systemctl is-enabled black-boxx-ap.service; systemctl is-active black-boxx-ap.service
systemctl is-enabled wg-quick@wg-ap          # must stay "disabled"
ls /etc/systemd/system/multi-user.target.wants/ | grep -E 'wg-quick|black-boxx'
ip -br addr show wlp2s0; ip -br addr show wg-ap
ip rule show | grep 0x64
```

```python
import asyncio, homelab_assets, homelab_maintenance as hm
r, _ = asyncio.run(hm.run_runbook(homelab_assets.load().get('black-boxx'),
                                  allow_repairs=False))
```

## Completion criteria

17/17 checks green · `advisories` empty · `black-boxx-ap.service` enabled and
active · `wg-quick@wg-ap` disabled · only `black-boxx-ap.service` in
`multi-user.target.wants` · no reboot performed to "test".

## Source

`maintenance_runbooks/black_boxx_connectivity.py` ·
`tests/test_black_boxx_runbook.py` (33 tests) ·
`config/homelab_assets.yml` (`black-boxx` entry, including `conflicting_units`)
