from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, INSTANCE_ID, utc_now
from kora.conformance.vcs import (
    Checkout,
    Kept,
    pinned,
    publish_request,
    run,
    sealed_input,
)
from kora.executor.failures import ExecutorError
from kora.ir.node import ErrorClass
from kora.observability import LoggingSettings
from kora.protocols.vcs import PlanStep, PublicationInput, PublishPlan
from tests.plugins.vcs.dirvcs_support import DirWorld, make_dir_world

FAULTS = Path(__file__).with_name("dirvcs_decoder_faults.py")
UNCOMMITTED = "uncommitted"
CHAIN = "chain"
SHARED_FAULTS: tuple[tuple[str, str], ...] = (
    ("truncate", "content_index_incomplete"),
    ("incomplete", "content_index_incomplete"),
    ("digest", "content_index_digest_mismatch"),
    ("corrupt", "content_digest_mismatch"),
)


def faulty(plan: PublishPlan, mode: str) -> PublishPlan:
    decode = plan.inspect[0]
    helper = decode.argv.index("-I") + 1
    argv = ("python", "-I", str(FAULTS), mode, *decode.argv[helper:])
    replaced = PlanStep.model_validate({**decode.model_dump(), "argv": argv})
    return plan.model_copy(update={"inspect": (replaced, *plan.inspect[1:])})


@dataclass(frozen=True)
class FaultBench:
    world: DirWorld
    checkout: Checkout
    sealed: PublicationInput


def sealed_bench(world: DirWorld, checkout: Checkout) -> FaultBench:
    _, sealed = run(
        sealed_input(
            world.provider,
            world.executor,
            world.remote_fixture(),
            checkout,
            world.context(),
        )
    )
    return FaultBench(world, checkout, sealed)


@pytest.fixture(scope="module")
def fault_benches(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, FaultBench]]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), INSTANCE_ID, utc_now
    ) as capture:
        world = make_dir_world(tmp_path_factory.mktemp("faults"), capture)
        uncommitted = world.checkout()
        (world.candidate(uncommitted) / "notes.txt").write_text("draft\n")
        chain = world.checkout()
        world.commit(chain, {"notes.txt": "draft\n"}, "intermediate")
        world.commit(chain, {"notes.txt": "final\n"}, "replacement")
        (world.candidate(chain) / "fresh.txt").write_text("dirty\n")
        yield {
            UNCOMMITTED: sealed_bench(world, uncommitted),
            CHAIN: sealed_bench(world, chain),
        }


@pytest.mark.parametrize(
    ("bench", "mode", "code"),
    [
        *((UNCOMMITTED, *fault) for fault in SHARED_FAULTS),
        *((CHAIN, *fault) for fault in SHARED_FAULTS),
        (CHAIN, "omit", "content_index_incomplete"),
    ],
)
def test_directory_decoder_faults_fail_before_authorization(
    fault_benches: dict[str, FaultBench], bench: str, mode: str, code: str
) -> None:
    fault_bench = fault_benches[bench]
    world, checkout = fault_bench.world, fault_bench.checkout
    context = world.context()
    request = publish_request(world.remote_fixture(), checkout)
    plan = faulty(run(world.provider.publish_plan(request, context)), mode)

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                fault_bench.sealed,
                checkout.workspace(),
                pinned(world.binding()),
                checkout.areas,
                (),
                (),
                Kept(),
                context,
            )
        )

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert raised.value.code == code
    assert world.remote_head() is None
