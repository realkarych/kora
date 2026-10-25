from typing import Final

from kora.protocols.evidence import DispatcherClosure
from kora.protocols.history import (
    CLOSURE_LABEL,
    GRANT_HISTORY_LABEL,
    chain_seed,
    chain_step,
    extend,
    item_digest,
    record_bytes,
)
from kora.protocols.identity import GrantBinding

DEMO_SCOPE: Final[tuple[str, str, str]] = ("ns_demo", "att_demo", "domain_demo")
GRANT_H0: Final[str] = (
    "24eb797a85d0bf59ca7c580e29332241b045115a589d516431a1a19d62ad259c"
)
GRANT_H1: Final[str] = (
    "68fdb785e1b1bac6b5fa62c10c706f84594924b71efed13e3b11d3c45ea821a8"
)
GRANT_BYTES: Final[bytes] = (
    b'{"dispatcher_id":"disp_1","execution_domain_id":"domain_demo",'
    b'"incarnation_id":"inc_1","lease_epoch":1,"process":null,'
    b'"worker_id":"worker_1"}'
)
CLOSURE_H0: Final[str] = (
    "490541730c04db070c2ce661387ef85d4454119fbf9a3d7c01b419f0c45ac850"
)
CLOSURE_H1: Final[str] = (
    "a5900150d51ec7df06bdbf00a526eeb8cf640409fc8ea1e494bdef49d7bbc59a"
)
CLOSURE_ITEM: Final[str] = (
    "fd2a1d33bcc51acb1a0761c1e012fc49f35e4f17b8f3afd14b5cf9177f5e3991"
)
CLOSURE_BYTES: Final[bytes] = (
    b'{"process":null,"provenance":"registry_acknowledgement",'
    b'"quiescence":{"attempt_id":"att_demo",'
    b'"closed_at":"2026-10-23T00:00:00.000000Z","dispatcher_id":"disp_1",'
    b'"evidence_ref":"ack_1","execution_domain_id":"domain_demo",'
    b'"lease_generation":1,"worker_incarnation_id":"inc_1"}}'
)
PROCESS: Final[dict[str, object]] = {
    "host_id": "host-1",
    "boot_id": None,
    "pid_namespace": None,
    "pid": 42,
    "start_time": 7,
    "leads_group": True,
}


def demo_grant(**changes: object) -> GrantBinding:
    return GrantBinding.model_validate(
        {
            "dispatcher_id": "disp_1",
            "worker_id": "worker_1",
            "incarnation_id": "inc_1",
            "lease_epoch": 1,
            "execution_domain_id": "domain_demo",
            "process": None,
            **changes,
        }
    )


def demo_closure() -> DispatcherClosure:
    return DispatcherClosure.model_validate(
        {
            "quiescence": {
                "attempt_id": "att_demo",
                "execution_domain_id": "domain_demo",
                "dispatcher_id": "disp_1",
                "worker_incarnation_id": "inc_1",
                "lease_generation": 1,
                "closed_at": "2026-10-23T00:00:00Z",
                "evidence_ref": "ack_1",
            },
            "provenance": "registry_acknowledgement",
            "process": None,
        }
    )


def test_grant_chain_matches_the_contract_vectors() -> None:
    grant = demo_grant()

    seed = chain_seed(GRANT_HISTORY_LABEL, *DEMO_SCOPE)
    encoded = record_bytes(grant)

    assert seed == GRANT_H0
    assert encoded == GRANT_BYTES
    assert len(encoded) == 141
    assert chain_step(seed, encoded) == GRANT_H1
    assert extend(seed, [encoded]) == GRANT_H1


def test_closure_chain_matches_the_contract_vector() -> None:
    closure = demo_closure()

    seed = chain_seed(CLOSURE_LABEL, *DEMO_SCOPE)
    encoded = record_bytes(closure)

    assert seed == CLOSURE_H0
    assert encoded == CLOSURE_BYTES
    assert chain_step(seed, encoded) == CLOSURE_H1
    assert item_digest(encoded) == CLOSURE_ITEM


def test_chain_includes_group_leadership_that_equality_ignores() -> None:
    leader = demo_grant(process=PROCESS)
    member = demo_grant(process={**PROCESS, "leads_group": False})

    leader_digest = chain_step(GRANT_H0, record_bytes(leader))
    member_digest = chain_step(GRANT_H0, record_bytes(member))

    assert leader == member
    assert leader_digest != member_digest


def test_chain_is_independent_of_page_boundaries() -> None:
    records = [record_bytes(demo_grant(lease_epoch=epoch)) for epoch in range(1, 6)]

    whole = extend(GRANT_H0, records)
    split = extend(extend(GRANT_H0, records[:2]), records[2:])

    assert whole == split


def test_year_one_timestamps_have_one_encoding() -> None:
    closure = demo_closure()
    document = closure.model_dump(mode="json")
    document["quiescence"]["closed_at"] = "0001-01-01T00:00:00+00:00"

    encoded = record_bytes(DispatcherClosure.model_validate(document))

    assert b'"closed_at":"0001-01-01T00:00:00.000000Z"' in encoded
