import kora.protocols.common
import kora.protocols.deadlines
import kora.protocols.evidence
import kora.protocols.harness
import kora.protocols.history
import kora.protocols.identity
import kora.protocols.policy
import kora.protocols.report
import kora.protocols.runtime
import kora.protocols.secret_value
import kora.protocols.secrets
import kora.protocols.vcs

INVENTORY: dict[object, tuple[str, ...]] = {
    kora.protocols.secret_value: (
        "SecretValue.reveal",
        "SecretScope.admit",
        "SecretScope.admission",
        "SecretScope.release",
        "SecretAdmission.admit",
        "SecretTransfer",
    ),
    kora.protocols.common: (
        "ProtocolContext",
        "SecretResolver.resolve",
        "ProviderProtocol.capabilities",
        "BoundaryModel",
        "Pending",
    ),
    kora.protocols.deadlines: ("ExecutionDeadline", "ScopeKind", "DEADLINE_GRACE"),
    kora.protocols.identity: (
        "ProcessIdentity.identity_key",
        "ProcessIdentitySource.current",
        "GrantBinding",
        "Handle",
        "Sha256",
        "Instant",
    ),
    kora.protocols.evidence: (
        "EvidenceScope",
        "DispatcherQuiescence",
        "TerminationReceipt",
        "NormalCompletionQuiescence",
        "DispatcherClosure",
        "PublicationOutcome",
        "ClosureEvidence",
    ),
    kora.protocols.report: ("AttemptError", "TerminalReport"),
    kora.protocols.history: (
        "GrantHistorySnapshot",
        "GrantHistoryPosition",
        "GrantHistoryPage",
        "GrantHistoryProgress",
        "ClosureSnapshot",
        "ClosurePosition",
        "EvidenceDisposition",
        "RecoveryRead",
        "RecoveryProgress",
        "RecoveryPage",
        "RecoveryEvidence",
        "chain_seed",
        "chain_step",
        "extend",
        "read_grant_history_page",
        "read_recovery_read",
    ),
    kora.protocols.runtime: (
        "RuntimeCapabilities",
        "Workload",
        "TransferItem",
        "RuntimePhase",
        "RuntimeStatus",
        "AdoptedSession",
        "RawEvent",
        "Liveness",
        "ArtifactRef",
        "ResourceRef",
        "PublicationAuthorization",
        *(
            f"Runtime.{method}"
            for method in (
                "capabilities",
                "workspace_target",
                "submit",
                "transfer_in",
                "start",
                "lookup",
                "adopt",
                "status",
                "events",
                "heartbeat",
                "transfer_out",
                "read_log",
                "read_transcript",
                "finalize",
                "cancel",
                "grant_history_status",
                "accept_grant_history",
                "recovery",
                "quiescence",
                "record_dispatcher_quiescence",
                "retire",
                "release",
            )
        ),
    ),
    kora.protocols.secrets: (
        "SecretsProvider.resolve",
        "SecretsProvider.list_names",
        "SecretsProvider.capabilities",
        "SecretsCapabilities",
    ),
    kora.protocols.vcs: (
        "WorkspaceRef",
        "WorkspaceTarget",
        "VcsCapabilities",
        "CheckoutRequest",
        "PublishRequest",
        "PlanFile",
        "PlanStep",
        "CheckoutPlan",
        "PublishPlan",
        "PublicationInput",
        "InspectionContent",
        "InspectionManifest",
        "PreparedPublication",
        "CredentialMode",
        "Binding",
        "BindingRevision",
        "CredentialRequest",
        "CredentialRights",
        "IssuedCredential",
        "Vcs.capabilities",
        "Vcs.acquire_credential",
        "Vcs.checkout_plan",
        "Vcs.publish_plan",
        "Vcs.head_sha",
    ),
    kora.protocols.policy: (
        "PolicyReason",
        "ToolCallRequest",
        "PolicyLayers",
        "PolicyDecision",
        "PolicyCapabilities",
        "Policy.capabilities",
        "Policy.validate",
        "Policy.decide",
        "PolicyClient.decide",
    ),
    kora.protocols.harness: (
        "HarnessCapabilities",
        "SessionFile",
        "Placement",
        "SessionSpec",
        "LaunchInputs",
        "LaunchEnd",
        "Launch.kill",
        "Launch.ended",
        "LaunchContext.allocate",
        "NormalisedEvent",
        "EVENT_ADAPTER",
        "RunnerCause",
        "Enforcement",
        "EventSink.emit",
        "EventSink.observe_dropped",
        "EventSink.enforcement",
        "EventSink.termination",
        "RunnerOutcome",
        "OutputCandidate",
        "EventSummary",
        "SessionResult",
        "HarnessAdapter.capabilities",
        "HarnessAdapter.check_config",
        "HarnessAdapter.build_session_spec",
        "HarnessAdapter.run",
        "HarnessAdapter.finalise",
        "HarnessAdapter.installed_version",
    ),
}
ANNOTATED: dict[object, tuple[str, ...]] = {
    kora.protocols.harness.HarnessAdapter: (
        "name",
        "config_schema",
        "egress_hosts",
        "tested_versions",
        "reserved_env",
    ),
    kora.protocols.policy.Policy: ("name", "config_schema"),
    kora.protocols.runtime.Runtime: ("name", "config_schema", "execution_domain_id"),
    kora.protocols.vcs.Vcs: ("name", "config_schema"),
    kora.protocols.harness.Launch: ("cli_path",),
    kora.protocols.harness.LaunchContext: ("placements",),
}


def resolves(owner: object, dotted: str) -> bool:
    for part in dotted.split("."):
        if not hasattr(owner, part):
            return False
        owner = getattr(owner, part)
    return True


def test_protocol_inventory() -> None:
    missing = [
        f"{module}.{name}"
        for module, names in INVENTORY.items()
        for name in names
        if not resolves(module, name)
    ]

    assert missing == []


def test_protocol_attributes_are_declared() -> None:
    missing = [
        f"{owner}.{name}"
        for owner, names in ANNOTATED.items()
        for name in names
        if name not in getattr(owner, "__annotations__", {})
    ]

    assert missing == []
