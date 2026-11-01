from typing import Final

HOSTILE_ITEMS: Final[int] = (12 * 1048576 - 4) // 3

CODEC_PROBE: Final[str] = """
import asyncio
import gc
import json
import os
import resource
import socket
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    decode_section,
    receive_message,
    send_message,
    workload_digest,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import Workload
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel

payload = Path(sys.argv[1]).read_bytes()
traced = sys.argv[2] == "traced"
scale = 1 if sys.platform == "darwin" else 1024


def peak_rss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale


def current_rss() -> int:
    result = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], text=True
    )
    return int(result.strip()) * 1024


async def send(sender: BoundedChannel, clock: SystemClock) -> float:
    started = time.perf_counter()
    await send_message(sender, ACCEPT_WORKLOAD, "request-1", [payload], clock)
    return time.perf_counter() - started


async def receive() -> tuple[bytearray, float, float]:
    clock = SystemClock()
    first, second = socket.socketpair()
    sender = BoundedChannel(first, clock)
    receiver = BoundedChannel(second, clock)
    try:
        sending = asyncio.create_task(
            send(sender, clock)
        )
        started = time.perf_counter()
        message = await receive_message(receiver, ACCEPT_WORKLOAD, clock)
        received_wall = time.perf_counter() - started
        sent_wall = await sending
        return message.sections[0], sent_wall, received_wall
    finally:
        sender.close()
        receiver.close()


gc.collect()
if traced:
    tracemalloc.start()
baseline_rss = peak_rss()
baseline_traced = tracemalloc.get_traced_memory()[0] if traced else 0
started = time.process_time()
section, sent_wall, received_wall = asyncio.run(receive())
received_cpu = time.process_time() - started
received_rss = peak_rss()
received_traced = tracemalloc.get_traced_memory()[1] if traced else 0
if traced:
    tracemalloc.reset_peak()
started = time.process_time()
decode_started = time.perf_counter()
try:
    workload = decode_section(section, ACCEPT_WORKLOAD.sections[0], Workload)
except KoraProtocolError:
    workload = None
decoded_cpu = time.process_time() - started
decoded_wall = time.perf_counter() - decode_started
decoded_rss = peak_rss()
decoded_traced = tracemalloc.get_traced_memory()[1] if traced else 0
refused = workload is None
digest_started = time.perf_counter()
started = time.process_time()
if workload is not None:
    workload_digest(workload)
digest_cpu = time.process_time() - started
digest_wall = time.perf_counter() - digest_started
digest_rss = peak_rss()
del section
gc.collect()
held_rss = current_rss()
del workload
gc.collect()
retained_traced = tracemalloc.get_traced_memory()[0] - baseline_traced if traced else 0
print(json.dumps({
    "payload_bytes": len(payload),
    "baseline_peak_rss_bytes": baseline_rss,
    "send_wall_seconds": sent_wall,
    "receive_wall_seconds": received_wall,
    "receive_cpu_seconds": received_cpu,
    "receive_peak_rss_bytes": received_rss,
    "receive_traced_peak_bytes": received_traced,
    "decode_wall_seconds": decoded_wall,
    "digest_cpu_seconds": digest_cpu,
    "digest_wall_seconds": digest_wall,
    "digest_peak_rss_bytes": digest_rss,
    "held_current_rss_bytes": held_rss,
    "decode_cpu_seconds": decoded_cpu,
    "decode_peak_rss_bytes": decoded_rss,
    "decode_traced_peak_bytes": decoded_traced,
    "retained_traced_bytes": retained_traced,
    "refused": refused,
}))
"""
