import asyncio
import os
import socket
import sys
from pathlib import Path

from kora.cli import CLI_COMPONENT
from kora.observability import LoggingSettings, configure_process
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.measurements.lifetime_observation import MeasuredLoop
from tests.measurements.listing_observation import ListingObserver
from tests.measurements.listing_workload import Workload


def main() -> None:
    folder = Path(sys.argv[1])
    control = Path(sys.argv[2]) / f"{os.getpid()}.sock"
    count = int(sys.argv[3])
    case = sys.argv[4]
    observer = ListingObserver(folder)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(control))
    listener.listen(1)
    listener.setblocking(False)
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "listing-population", ProbeClock().now
    )
    workload = Workload(observer, listener, diagnostics, count)
    runner = asyncio.Runner(loop_factory=MeasuredLoop)
    loop = runner.get_loop()
    assert isinstance(loop, MeasuredLoop)
    observer.loop = loop
    try:
        runner.run(observer.imported())
        runner.run(workload.acquire(0, case))
        runner.run(workload.exercise(case))
        runner.run(workload.finish(case))
        runner.run(workload.settled())
    finally:
        try:
            runner.run(workload.abort())
        finally:
            runner.close()
            diagnostics.shutdown()
            listener.close()
            control.unlink()
            observer.close()


if __name__ == "__main__":
    main()
