"""
maintenance_runbooks — deterministic diagnosis/repair procedures, one module
per known problem class. A runbook:

  - runs ONLY allowlisted commands through the ops facade it is given,
  - never invents actions: any repair it proposes is a named action from
    maintenance_policy.ACTION_TIERS with a locked command plan,
  - performs a repair itself only when ops.auto_repair_allowed is set AND the
    action's tier is AUTO and its exact preconditions hold,
  - reports everything as structured checks so unknown failures can be
    escalated as a bounded diagnostic bundle.

Registration is by module name — the asset registry's `runbook:` field names
the module here. Adding a runbook = adding a module + listing it in RUNBOOKS.
"""

from . import (black_boxx_connectivity, jellyfin_health, immich_status,
               joplin_sync, loki_interfaces, stateless_container)

RUNBOOKS = {
    black_boxx_connectivity.NAME: black_boxx_connectivity.run,
    jellyfin_health.NAME: jellyfin_health.run,
    immich_status.NAME: immich_status.run,
    joplin_sync.NAME: joplin_sync.run,
    loki_interfaces.NAME: loki_interfaces.run,
    stateless_container.NAME: stateless_container.run,
}
