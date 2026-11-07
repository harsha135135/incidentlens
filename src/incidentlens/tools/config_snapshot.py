"""get_runtime_config: LedgerCore's allowlisted settings in effect during the job window, from the
startup records the collector stored (decisions D11). Configuration recorded after the window is
never presented as applying to it; a restart inside the window shows both versions."""

from __future__ import annotations

from incidentlens.tools.envelope import build_envelope, iso
from incidentlens.tools.registry import ToolContext

TOOL = "get_runtime_config"
ALLOWLIST = ("revision", "instanceId", "outboxPollerEnabled", "outboxWorkers", "outboxPollIntervalMs",
             "outboxBatchSize", "outboxDeliveryTimeoutMs", "outboxMaxAttempts", "outboxBackoffBaseMs",
             "outboxBackoffMaxMs", "consumerTarget", "consumerAuthentication", "inProcessConsumerBcryptCost",
             "poolMaximumSize", "poolMinimumIdle", "poolConnectionTimeoutMs", "databaseSessionOptions")


def get_runtime_config(context: ToolContext, arguments) -> dict:
    window = context.window
    in_effect = context.store.execute(
        "SELECT observed_at, fields FROM log_records WHERE service = %s AND record_type = 'ledgercore.config' "
        "AND observed_at < %s ORDER BY observed_at DESC LIMIT 1", (context.service, window.start)).fetchone()
    during = context.store.execute(
        "SELECT observed_at, fields FROM log_records WHERE service = %s AND record_type = 'ledgercore.config' "
        "AND observed_at >= %s AND observed_at < %s ORDER BY observed_at", (context.service, window.start, window.end)
    ).fetchall()
    versions = ([in_effect] if in_effect else []) + list(during)
    if not versions:
        return build_envelope(TOOL, {}, window, "UNAVAILABLE", "config_history", {"versions": []},
                              ["No LedgerCore startup record exists before the window end, so its configuration "
                               "for the window is unknown."], ["no configuration record"])
    listed = [{"effective_at": iso(at), "settings": {key: fields.get(key) for key in ALLOWLIST if key in fields}}
              for at, fields in versions]
    limitations = []
    if in_effect is None:
        limitations.append("No startup record before the window start: the configuration for the window's beginning is unknown.")
    if during:
        changed = sorted({key for v in listed[1:] for key, value in v["settings"].items()
                          if listed[0]["settings"].get(key) != value} - {"instanceId"}) if len(listed) > 1 else []
        limitations.append(f"LedgerCore restarted {len(during)} time(s) inside the window"
                           + (f"; settings that changed: {', '.join(changed)}." if changed else "."))
    data = {"versions": listed, "restarts_in_window": [iso(at) for at, _ in during],
            "meaning": "Allowlisted, non-secret settings from LedgerCore's startup records; one version per start."}
    target = {"revision": listed[-1]["settings"].get("revision"), "instance_id": listed[-1]["settings"].get("instanceId")}
    return build_envelope(TOOL, {}, window, "PARTIAL" if in_effect is None else "COMPLETE", "config_history", data,
                          limitations, [], observed_window={"first": listed[0]["effective_at"], "last": listed[-1]["effective_at"]},
                          target=target)
