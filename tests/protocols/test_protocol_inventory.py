import kora.protocols.common
import kora.protocols.identity
import kora.protocols.secret_value
import kora.protocols.secrets

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
    ),
    kora.protocols.identity: (
        "ProcessIdentity.identity_key",
        "ProcessIdentitySource.current",
        "GrantBinding",
    ),
    kora.protocols.secrets: (
        "SecretsProvider.resolve",
        "SecretsProvider.list_names",
        "SecretsProvider.capabilities",
        "SecretsCapabilities",
    ),
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
