import os
import sys

from kora.protocols.metadata import ProtocolName, ProviderMetadata

sys.stdout.write("doctor-provider-import\n")
if sys.__stdout__ is not None:
    sys.__stdout__.write("doctor-provider-original\n")
os.write(1, b"doctor-provider-descriptor\n")


class DoctorHarness:
    name: str = "doctor_noisy"


def provider_metadata() -> ProviderMetadata:
    sys.stdout.write("doctor-provider-metadata\n")
    return ProviderMetadata(
        protocol=ProtocolName.HARNESS,
        name=DoctorHarness.name,
        implementation=DoctorHarness,
        config_schema={"type": "object"},
        version="1.0",
    )
