"""
black_boxx_connectivity — the known BLACK-BOXX "no internet" diagnosis.

BLACK-BOXX is the wireless AP on dex247: clients on wlp2s0 (192.168.10.0/24)
are policy-routed through the wg-ap WireGuard tunnel via fwmark 100 → table
100. The known failure mode is the policy rule (fwmark 100 lookup 100 prio
100) disappearing while everything else stays healthy; the ONLY automatic
repair this runbook performs is restoring that predeclared rule — idempotent,
verified afterwards, and rolled back if verification fails.
"""

import asyncio
import time

NAME = "black_boxx_connectivity"

HANDSHAKE_FRESH_SECS = 300

# Bounded wait for the AP stack to come up after a restart: the script brings
# up wg-ap, hostapd, dnsmasq and the routing in sequence, so the interface is
# not there the instant systemd returns.
READY_DEADLINE_SECS = 45
READY_POLL_SECS = 5

OK, FAILED, SKIPPED, UNAVAILABLE = "ok", "failed", "skipped", "unavailable"


def _mark_hex(mark: int) -> str:
    return f"0x{mark:x}"


async def _probe(ops, name: str, **params):
    """Run one allowlisted command, distinguishing a real command failure from
    the command being unrunnable at all.

    Returns (rc, out, transport_ok). A refused/missing/unspawnable command is
    Loki's own problem — reporting it as a dead subsystem is how one broken
    executor turns into twelve fabricated hardware faults."""
    try:
        rc, out = await ops.run(name, **params)
        return rc, out, True
    except Exception as e:                     # PolicyError, OSError, ...
        return None, f"{type(e).__name__}: {e}", False


async def run(asset: dict, ops) -> dict:
    net = asset.get("network") or {}
    wl = net["wireless_interface"]
    wg = net["vpn_interface"]
    mark = int(net["routing_mark"])
    table = int(net["routing_table"])
    prio = int(net["rule_priority"])
    subnet = net["client_subnet"]
    ap_unit = (asset.get("systemd") or {}).get("ap_unit", "")
    checks = []

    def add(name, ok, detail="", status=None):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400],
                       "status": status or (OK if ok else FAILED)})
        return bool(ok)

    def skip(name, why):
        """A check that was NOT run because a prerequisite is already down.
        Reporting it as 'failed' would claim evidence this run never gathered."""
        checks.append({"name": name, "ok": False, "detail": why[:400],
                       "status": SKIPPED})

    # 0. Can we execute anything at all? Every check below reads host state
    #    through the same executor, so if THIS fails the answer is "Loki
    #    cannot see BLACK-BOXX", not "BLACK-BOXX is entirely dead".
    #    The probe doubles as the AP-unit read when one is declared, so it
    #    costs no extra command and consumes no check's scripted output.
    probe_cmd, probe_args = (("systemctl_is_active", {"unit": ap_unit})
                             if ap_unit else ("ip_forward", {}))
    rc, out, transport_ok = await _probe(ops, probe_cmd, **probe_args)
    if not transport_ok:
        add("diagnostic_transport", False, out, status=UNAVAILABLE)
        return {"checks": checks, "healthy": False, "repair": None,
                "repair_result": None, "escalate": False,
                "diagnosis": (
                    "Diagnostic transport failed — Loki could not run its "
                    "read-only inspection commands on dex247, so BLACK-BOXX's "
                    "actual state is UNKNOWN. This is a Loki/executor problem, "
                    f"not a proven AP fault: {str(out)[:200]}")}
    add("diagnostic_transport", True, "inspection commands runnable")

    # 0b. The unit that owns the whole stack. When it is dead, everything
    #     below is a symptom of one cause — record that instead of gathering
    #     twelve dependent failures and calling them twelve faults.
    ap_unit_ok = True
    if ap_unit:
        state = (out or "").strip()
        ap_unit_ok = rc == 0 and state == "active"
        add("ap_service", ap_unit_ok, f"{ap_unit}: {state or 'unknown'}")
        if not ap_unit_ok:
            for name in ("ap_interface", "hostapd_process", "dnsmasq_process",
                         "dhcp_leases", "vpn_interface", "vpn_handshake",
                         "vpn_traffic", "ip_forwarding", "packet_mark",
                         "routing_table", "policy_rule", "nat_masquerade",
                         "marked_route_lookup", "tunnel_internet",
                         "dns_resolution"):
                skip(name, f"not checked — {ap_unit} is {state}; it owns this")
            return await _ap_unit_down_result(ops, checks, ap_unit, state, net)

    # 1. AP interface + address
    rc, out = await ops.run("ip_addr", iface=wl)
    ap_ok = add("ap_interface", rc == 0 and "UP" in out
                and str(net.get("ap_address", "")) in out, out)

    # 2/3. hostapd + dnsmasq — actual processes (they run outside systemd here)
    rc, out = await ops.run("pgrep_hostapd")
    hostapd_ok = add("hostapd_process",
                     rc == 0 and str(net.get("hostapd_conf", "hostapd")) in out, out)
    rc, out = await ops.run("pgrep_dnsmasq")
    dnsmasq_ok = add("dnsmasq_process",
                     rc == 0 and str(net.get("dnsmasq_conf", "dnsmasq")) in out, out)

    # 4. DHCP lease metadata (count + newest age only — never MACs/hostnames)
    meta = await ops.lease_meta(net.get("dhcp_leases_file", ""))
    add("dhcp_leases", meta.get("ok", False),
        f"{meta.get('count', 0)} lease(s), newest {meta.get('newest_age_secs', '?')}s old"
        if meta.get("ok") else meta.get("error", "unreadable"))

    # 5. wg-ap interface, handshake freshness, traffic counters
    rc, out = await ops.run("ip_addr", iface=wg)
    wg_if_ok = add("vpn_interface", rc == 0 and wg in out, out)
    rc, out = await ops.run("wg_handshake", iface=wg)
    hs_age = None
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0:
                hs_age = max(0, int(time.time()) - int(parts[1]))
    hs_ok = add("vpn_handshake", hs_age is not None and hs_age < HANDSHAKE_FRESH_SECS,
                f"last handshake {hs_age}s ago" if hs_age is not None
                else "no completed handshake")
    rc, out = await ops.run("wg_transfer", iface=wg)
    add("vpn_traffic", rc == 0 and any(p.isdigit() and int(p) > 0
                                       for p in out.split()), out)
    wg_ok = wg_if_ok and hs_ok

    # 6. IP forwarding
    rc, out = await ops.run("ip_forward")
    add("ip_forwarding", rc == 0 and out.strip() == "1", out)

    # 7. Packet mark on AP ingress (mangle PREROUTING)
    rc, out = await ops.run("iptables_mangle")
    mark_ok = add("packet_mark",
                  rc == 0 and f"-i {wl}" in out
                  and f"--set-xmark {_mark_hex(mark)}" in out,
                  "MARK rule present" if rc == 0 else out)

    # 8. Routing table default through the tunnel
    rc, out = await ops.run("ip_route_table", num=table)
    table_ok = add("routing_table",
                   rc == 0 and "default" in out and f"dev {wg}" in out, out)

    # 9. Policy rule: fwmark <mark> lookup <table>  (the known failure point)
    rc, out = await ops.run("ip_rule_show")
    rule_ok = add("policy_rule",
                  rc == 0 and _mark_hex(mark) in out and f"lookup {table}" in out,
                  "rule present" if rc == 0 and _mark_hex(mark) in out
                  else "fwmark rule MISSING")

    # 10. NAT for the client subnet out the tunnel
    rc, out = await ops.run("iptables_nat")
    nat_ok = add("nat_masquerade",
                 rc == 0 and f"-s {subnet}" in out and f"-o {wg}" in out
                 and "MASQUERADE" in out,
                 "MASQUERADE present" if rc == 0 else out)

    # 11. Marked route lookup resolves through the tunnel
    rc, out = await ops.run("ip_route_get_marked", probe_ip="1.1.1.1", num=mark)
    marked_ok = add("marked_route_lookup", rc == 0 and f"dev {wg}" in out, out)

    # 12. Internet + DNS through the tunnel (safe probes)
    rc, out = await ops.run("ping_iface", iface=wg, probe_ip="1.1.1.1")
    tunnel_net_ok = add("tunnel_internet", rc == 0, out.splitlines()[-1] if out else "")
    dns_ok = await ops.dns_check("one.one.one.one")
    add("dns_resolution", dns_ok, "resolver answered" if dns_ok else "DNS lookup failed")

    healthy = all(c["ok"] for c in checks)
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}

    if healthy:
        result["diagnosis"] = ("BLACK-BOXX path fully healthy: AP up, tunnel "
                               "handshaking, marking/routing/NAT all in place, "
                               "internet reachable through the tunnel.")
        return result

    # The ONE known auto-repairable state: policy rule missing while the
    # tunnel, packet marking, routing table and NAT are all healthy.
    if (not rule_ok) and wg_ok and mark_ok and table_ok and nat_ok:
        plan = {
            "action": "restore_blackboxx_ip_rule",
            "description": (f"restore policy rule: ip rule add fwmark {mark} "
                            f"table {table} priority {prio}"),
            "commands": [{"name": "ip_rule_add_fwmark",
                          "params": {"num": mark, "num2": table, "num3": prio}}],
            "rollback": [{"name": "ip_rule_del_fwmark",
                          "params": {"num": mark, "num2": table, "num3": prio}}],
        }
        result["repair"] = plan
        result["diagnosis"] = (
            "Known failure: the fwmark policy rule is missing while wg-ap, "
            "packet marking, table routing and NAT are all healthy — AP clients "
            "have no route to the tunnel.")
        if ops.auto_repair_allowed:
            result["repair_result"] = await _apply_rule_repair(
                ops, plan, mark, table, wg)
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    # Unknown condition. Name the EARLIEST failure in dependency order as the
    # likely root cause — a flat list of every failed check reads as a dozen
    # separate faults and tells the Boss (and Hermes) nothing about where to
    # start. The full per-check detail is still in `checks`.
    failed = [c["name"] for c in checks if not c["ok"] and c["status"] == FAILED]
    order = ("ap_service", "ap_interface", "vpn_interface", "vpn_handshake",
             "hostapd_process", "dnsmasq_process", "ip_forwarding",
             "packet_mark", "routing_table", "policy_rule", "nat_masquerade",
             "marked_route_lookup", "tunnel_internet", "dns_resolution")
    root = next((n for n in order if n in failed), failed[0] if failed else "unknown")
    downstream = [n for n in failed if n != root]
    result["diagnosis"] = (
        f"BLACK-BOXX is unhealthy outside the known auto-repairable condition. "
        f"Earliest failure in dependency order: {root}"
        + (f" (with {len(downstream)} later check(s) also failing, likely "
           f"downstream of it: {', '.join(downstream[:6])}"
           + ("…" if len(downstream) > 6 else "") + ")" if downstream else "")
        + ".")
    result["escalate"] = True
    return result


async def _ap_unit_down_result(ops, checks, ap_unit: str, state: str, net) -> dict:
    """The AP unit owns wg-ap, hostapd, dnsmasq and the routing, so a dead unit
    is ONE fault, not fourteen. Restarting a stopped/crashed stateless service
    is the existing AUTO tier (restart_stateless_service) — bounded to a single
    attempt, then verified."""
    plan = {
        "action": "restart_stateless_service",
        "description": f"restart {ap_unit} to bring the BLACK-BOXX stack back up",
        "commands": [{"name": "systemctl_restart_unit", "params": {"unit": ap_unit}}],
        "rollback": [],
    }
    result = {"checks": checks, "healthy": False, "repair": plan,
              "repair_result": None, "escalate": False,
              "diagnosis": (
                  f"Root cause: {ap_unit} is {state}. That unit owns the whole "
                  f"BLACK-BOXX stack (wg-ap, hostapd, dnsmasq, marking, table, "
                  f"rule, NAT), so every downstream check is a SYMPTOM of this "
                  f"one failure, not an independent fault.")}
    if not ops.auto_repair_allowed:
        return result
    if await ops.attempted("restart_stateless_service", ap_unit):
        result["diagnosis"] += (" Already restarted once recently and still "
                                "down — escalating rather than looping.")
        result["escalate"] = True
        return result
    result["repair_result"] = await _restart_ap_and_verify(ops, ap_unit, net)
    result["escalate"] = not result["repair_result"]["ok"]
    return result


async def _restart_ap_and_verify(ops, ap_unit: str, net) -> dict:
    """One bounded restart, a readiness wait, then real verification — the unit
    reporting 'active' only means the script started, not that the tunnel and
    AP actually came up."""
    steps = []
    rc, out, ok = await _probe(ops, "systemctl_restart_unit", unit=ap_unit)
    steps.append(f"systemctl restart {ap_unit} → rc={rc}")
    if not ok:
        return {"ok": False, "steps": steps + [str(out)[:200]], "verified": False}
    await ops.record_attempt("restart_stateless_service", ap_unit)

    wg, wl = net["vpn_interface"], net["wireless_interface"]
    deadline = time.time() + READY_DEADLINE_SECS
    attempts = 0
    while True:
        attempts += 1
        rc, out, _ = await _probe(ops, "systemctl_is_active", unit=ap_unit)
        unit_up = rc == 0 and (out or "").strip() == "active"
        rc, out, _ = await _probe(ops, "ip_addr", iface=wg)
        wg_up = rc == 0 and wg in (out or "")
        rc, out, _ = await _probe(ops, "ip_addr", iface=wl)
        ap_up = rc == 0 and "UP" in (out or "")
        if unit_up and wg_up and ap_up:
            steps.append(f"ready after {attempts} check(s): unit active, "
                         f"{wg} present, {wl} up")
            return {"ok": True, "steps": steps, "verified": True,
                    "readiness": {"attempts": attempts}}
        if time.time() >= deadline:
            steps.append(f"NOT ready within {READY_DEADLINE_SECS}s "
                         f"(unit_active={unit_up} {wg}={wg_up} {wl}={ap_up})")
            return {"ok": False, "steps": steps, "verified": False,
                    "readiness": {"attempts": attempts}}
        await asyncio.sleep(READY_POLL_SECS)


async def _apply_rule_repair(ops, plan, mark, table, wg) -> dict:
    """Idempotent + verified + reversible restore of the policy rule."""
    steps = []
    # Idempotency: re-check immediately before acting; another actor (or a
    # previous attempt) may have already restored it.
    rc, out = await ops.run("ip_rule_show")
    if rc == 0 and _mark_hex(mark) in out and f"lookup {table}" in out:
        return {"ok": True, "steps": ["rule already present — nothing to do"],
                "verified": True}

    cmd = plan["commands"][0]
    rc, out = await ops.run(cmd["name"], **cmd["params"])
    steps.append(f"ip rule add → rc={rc}")
    if rc != 0 and "File exists" not in out:      # "File exists" = idempotent no-op
        return {"ok": False, "steps": steps + [out[:200]], "verified": False}
    await ops.record_attempt("restore_blackboxx_ip_rule", "rule re-added")

    # Verify: rule visible AND marked traffic resolves through the tunnel.
    rc, out = await ops.run("ip_rule_show")
    rule_back = rc == 0 and _mark_hex(mark) in out and f"lookup {table}" in out
    rc, out = await ops.run("ip_route_get_marked", probe_ip="1.1.1.1", num=mark)
    route_ok = rc == 0 and f"dev {wg}" in out
    if rule_back and route_ok:
        steps.append("verified: rule present, marked traffic routes via tunnel")
        return {"ok": True, "steps": steps, "verified": True}

    # Verification failed — revert to the pre-repair state and escalate.
    rb = plan["rollback"][0]
    rc, out = await ops.run(rb["name"], **rb["params"])
    steps.append(f"verification FAILED — rolled back (rc={rc})")
    return {"ok": False, "steps": steps, "verified": False, "rolled_back": True}
