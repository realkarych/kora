from importlib.util import find_spec
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kora.conformance.lifetime import (
        LifetimeError as LifetimeError,
    )
    from kora.conformance.lifetime import (
        ParentLifetime as ParentLifetime,
    )
    from kora.conformance.lifetime import (
        resume_close as resume_close,
    )
    from kora.conformance.transport import (
        LifetimeClient as LifetimeClient,
    )
    from kora.conformance.transport import (
        LifetimeEndpoint as LifetimeEndpoint,
    )
    from kora.conformance.transport import (
        LifetimeTransportError as LifetimeTransportError,
    )
    from kora.protocols.lifetime import (
        Acquisition as Acquisition,
    )
    from kora.protocols.lifetime import (
        ActivationAcknowledgement as ActivationAcknowledgement,
    )
    from kora.protocols.lifetime import (
        ActivationResult as ActivationResult,
    )
    from kora.protocols.lifetime import (
        BorrowedServiceRegistration as BorrowedServiceRegistration,
    )
    from kora.protocols.lifetime import (
        BorrowedServiceRequest as BorrowedServiceRequest,
    )
    from kora.protocols.lifetime import (
        CleanupOutcome as CleanupOutcome,
    )
    from kora.protocols.lifetime import (
        CleanupReference as CleanupReference,
    )
    from kora.protocols.lifetime import (
        ClosureReport as ClosureReport,
    )
    from kora.protocols.lifetime import (
        ConformanceLifetime as ConformanceLifetime,
    )
    from kora.protocols.lifetime import (
        EnvironmentVariable as EnvironmentVariable,
    )
    from kora.protocols.lifetime import (
        LifetimeLimits as LifetimeLimits,
    )
    from kora.protocols.lifetime import (
        LifetimeReason as LifetimeReason,
    )
    from kora.protocols.lifetime import (
        LocalLaunchAuthority as LocalLaunchAuthority,
    )
    from kora.protocols.lifetime import (
        LocalRuntimeBinding as LocalRuntimeBinding,
    )
    from kora.protocols.lifetime import (
        LocalRuntimeLaunch as LocalRuntimeLaunch,
    )
    from kora.protocols.lifetime import (
        LocalRuntimeRegistration as LocalRuntimeRegistration,
    )
    from kora.protocols.lifetime import (
        OriginalOutcome as OriginalOutcome,
    )
    from kora.protocols.lifetime import (
        ProcessDomain as ProcessDomain,
    )
    from kora.protocols.lifetime import (
        ProcessLaunch as ProcessLaunch,
    )
    from kora.protocols.lifetime import (
        ProcessOutput as ProcessOutput,
    )
    from kora.protocols.lifetime import (
        ProcessRegistration as ProcessRegistration,
    )
    from kora.protocols.lifetime import (
        Reservation as Reservation,
    )
    from kora.protocols.lifetime import (
        ReservationRequest as ReservationRequest,
    )
    from kora.protocols.lifetime import (
        ReservationToken as ReservationToken,
    )
    from kora.protocols.lifetime import (
        ResourceKind as ResourceKind,
    )
    from kora.protocols.lifetime import (
        ResourceObservation as ResourceObservation,
    )
    from kora.protocols.lifetime import (
        ResourceState as ResourceState,
    )
    from kora.protocols.lifetime import (
        TemporaryTreeRegistration as TemporaryTreeRegistration,
    )
    from kora.protocols.lifetime import (
        TemporaryTreeRequest as TemporaryTreeRequest,
    )
    from kora.protocols.lifetime import (
        new_operation_id as new_operation_id,
    )

LIFETIME_VALUES: frozenset[str] = frozenset(
    {
        "Acquisition",
        "ActivationAcknowledgement",
        "ActivationResult",
        "BorrowedServiceRegistration",
        "BorrowedServiceRequest",
        "CleanupOutcome",
        "CleanupReference",
        "ClosureReport",
        "ConformanceLifetime",
        "EnvironmentVariable",
        "LifetimeLimits",
        "LifetimeReason",
        "LocalLaunchAuthority",
        "LocalRuntimeBinding",
        "LocalRuntimeLaunch",
        "LocalRuntimeRegistration",
        "OriginalOutcome",
        "ProcessDomain",
        "ProcessLaunch",
        "ProcessOutput",
        "ProcessRegistration",
        "Reservation",
        "ReservationRequest",
        "ReservationToken",
        "ResourceKind",
        "ResourceObservation",
        "ResourceState",
        "TemporaryTreeRegistration",
        "TemporaryTreeRequest",
        "new_operation_id",
    }
)


class ConformanceExtraMissing(ImportError):
    def __init__(self) -> None:
        super().__init__("kora.conformance needs the kora[conformance] extra")


if find_spec("pytest") is None:
    raise ConformanceExtraMissing


def __getattr__(name: str) -> object:
    if name in LIFETIME_VALUES:
        from kora.protocols import lifetime

        return getattr(lifetime, name)
    if name in {"ParentLifetime", "LifetimeError", "resume_close"}:
        from kora.conformance import lifetime as controller

        return getattr(controller, name)
    if name in {"LifetimeClient", "LifetimeEndpoint", "LifetimeTransportError"}:
        from kora.conformance import transport

        return getattr(transport, name)
    raise AttributeError(name)
