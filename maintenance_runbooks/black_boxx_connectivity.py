"""
black_boxx_connectivity — the known BLACK-BOXX "no internet" diagnosis.

BLACK-BOXX is the wireless AP on dex247: clients on wlp2s0 (192.168.10.0/24)
are policy-routed through the wg-ap WireGuard tunnel via fwmark 100 → table
100. The known failure mode is the policy rule (fwmark 100 lookup 100 prio
100) disappearing while everything else stays healthy; the ONLY automatic
repair this runbook performs is restoring that predeclared rule — idempotent,
verified afterwards, and rolled back if verification fails.
"""

import time

NAME = "black_boxx_connectivity"

HANDSHAKE_FRESH_SECS = 300


def _mark_hex(mark: int) -> str:
    return f"0x{mark:x}"


async def run(asset: dict, ops) -> dict:
    net = asset.get("network") or {}
    wl = net["wireless_interface"]
    wg = net["vpn_interface"]
    mark = int(net["routing_mark"])
    table = int(net["routing_table"])
    prio = int(net["rule_priority"])
    subnet = net["client_subnet"]
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

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

    result["diagnosis"] = (
        "BLACK-BOXX is unhealthy in a way outside the known auto-repairable "
        "condition: " + "; ".join(c["name"] for c in checks if not c["ok"]))
    result["escalate"] = True
    return result


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
