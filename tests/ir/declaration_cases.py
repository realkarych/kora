from kora.ir import (
    BuiltinTypeContract,
    Concurrency,
    CronTrigger,
    CustomTypeContract,
    GraphTrigger,
    InputRef,
    IrSourceLocation,
    ManualTrigger,
    OutputDecl,
    OutputTarget,
    ParameterTarget,
    ProviderRetry,
    Retention,
    RuntimeSection,
    SecretsSection,
    SensorTrigger,
    SubgraphOutput,
    TargetConfig,
    TypeBehavior,
    WebhookAuth,
    WebhookTrigger,
)
from kora.ir.record import IrRecord

DIALECT: str = "https://json-schema.org/draft/2020-12/schema"
BEHAVIOR: dict[str, object] = {
    "validation": "installed.validation.v1",
    "digest_fields": "installed.digest.v1",
    "summary": "installed.summary.v1",
}
MINIMAL: dict[type[IrRecord], dict[str, object]] = {
    IrSourceLocation: {"file": "", "line": 1},
    InputRef: {"type": "StructuredOutput"},
    OutputDecl: {"type": "StructuredOutput"},
    SubgraphOutput: {"type": "StructuredOutput", "from": "node", "output": "result"},
    TypeBehavior: BEHAVIOR,
    BuiltinTypeContract: {"builtin_revision": "unavailable"},
    CustomTypeContract: {
        "schema": {"$schema": DIALECT},
        "parent": None,
        "behavior": BEHAVIOR,
    },
    ManualTrigger: {"kind": "manual"},
    CronTrigger: {"kind": "cron", "expr": ""},
    WebhookTrigger: {
        "kind": "webhook",
        "name": "",
        "auth": {"kind": "hmac", "secret": "TOKEN"},
    },
    SensorTrigger: {
        "kind": "sensor",
        "poll": {"sensor": "http", "until": "true"},
        "interval": "0s",
    },
    GraphTrigger: {"kind": "graph", "graph": "", "on": "run_succeeded"},
    WebhookAuth: {"kind": "bearer", "secret": "TOKEN"},
    ParameterTarget: {"param": "target"},
    OutputTarget: {"from": "node", "output": "result", "path": "target"},
    TargetConfig: {"key": {"param": "target"}},
    RuntimeSection: {"runtime": "", "vcs": ""},
    Concurrency: {},
    Retention: {},
    ProviderRetry: {},
    SecretsSection: {},
}
POPULATED: dict[type[IrRecord], dict[str, object]] = {
    model: {**document, "x-meta": False} for model, document in MINIMAL.items()
}
POPULATED[InputRef].update(
    {"from": "$inputs", "output": "result", "path": "", "optional": False}
)
POPULATED[OutputDecl].update(
    {"schema": {"properties": {"first": True, "second": False}}, "collect": "result"}
)
POPULATED[CustomTypeContract] = {
    "schema": {"$schema": DIALECT, "properties": {"first": True, "second": False}},
    "parent": "custom/Parent",
    "behavior": {**BEHAVIOR, "x-meta": False},
    "x-meta": False,
}
for model in (ManualTrigger, CronTrigger, WebhookTrigger, SensorTrigger, GraphTrigger):
    POPULATED[model]["params"] = {"first": [None, False], "second": [True, ""]}
POPULATED[CronTrigger]["timezone"] = "not/a/timezone"
POPULATED[WebhookTrigger]["params_from"] = "none"
POPULATED[WebhookTrigger]["auth"] = {
    "kind": "bearer",
    "secret": "TOKEN",
    "x-meta": False,
}
POPULATED[SensorTrigger]["poll"] = {
    "sensor": "unknown",
    "until": "true",
    "config": {"first": [None, False], "second": [True, ""]},
    "x-meta": False,
}
POPULATED[TargetConfig].update(
    {"key": {**MINIMAL[OutputTarget], "x-meta": False}, "on_active": "defer"}
)
POPULATED[Concurrency].update({"max_running_nodes": 1, "max_running_runs": 2})
POPULATED[Retention].update({"transcripts": "forever", "artifact_payloads": "0001d"})
POPULATED[ProviderRetry]["max_wait"] = "0000s"
POPULATED[SecretsSection]["aliases"] = {
    "TOKEN": "secret://env/token",
    "OTHER": "secret://vault/path/key",
}
POPULATED[RuntimeSection].update(
    {
        "artifact_storage": "",
        "approval_channels": {
            "": {"enabled": False},
            "x-channel": {"data": [None, True]},
        },
        "concurrency": {"max_running_nodes": 1, "max_running_runs": 2},
        "limits": {
            "timeout": "0s",
            "max_events": 1,
            "budget_usd": 1,
            "stall_after": "0s",
        },
        "budget_headroom_pct": 0,
        "stall_after": "0s",
        "retention": {"transcripts": "forever", "artifact_payloads": "0001d"},
        "protected_paths": ["", "**/gate"],
        "infrastructure_retry": {
            "max_attempts": 1,
            "initial_interval": "0s",
            "backoff_coefficient": 1,
            "max_interval": "0s",
            "retry_on": ["timeout", "infrastructure"],
            "non_retryable": ["config_error", "cancelled"],
            "predicate": "false",
            "strategy": "fresh",
        },
        "provider_retry": {"max_wait": "0s", "x-meta": False},
    }
)
INVALID_FIELDS: dict[type[IrRecord], tuple[str, object]] = {
    IrSourceLocation: ("line", 0),
    InputRef: ("type", "Text\n"),
    OutputDecl: ("schema", False),
    SubgraphOutput: ("from", "$inputs"),
    TypeBehavior: ("summary", ""),
    BuiltinTypeContract: ("builtin_revision", ""),
    CustomTypeContract: ("schema", {"$schema": "unsupported"}),
    ManualTrigger: ("kind", "wrong"),
    CronTrigger: ("expr", False),
    WebhookTrigger: ("auth", {"kind": "wrong", "secret": "TOKEN"}),
    SensorTrigger: ("interval", "0s\n"),
    GraphTrigger: ("on", "wrong"),
    WebhookAuth: ("secret", "x-token"),
    ParameterTarget: ("param", ""),
    OutputTarget: ("path", ""),
    TargetConfig: ("key", {}),
    RuntimeSection: ("budget_headroom_pct", -1),
    Concurrency: ("max_running_nodes", 0),
    Retention: ("transcripts", "1h"),
    ProviderRetry: ("max_wait", False),
    SecretsSection: ("aliases", {"x-token": "secret://env/key"}),
}
INVALID: dict[type[IrRecord], dict[str, object]] = {
    model: {**MINIMAL[model], field: value}
    for model, (field, value) in INVALID_FIELDS.items()
}
