import pytest

from scripts.ci.ubuntu_nfs_apt import Refused, rewrite_source

ORIGINAL_SIGNER = b"/usr/share/keyrings/ubuntu-archive-keyring.gpg"
STAGED_SIGNER = b"/run/job/attempt/ubuntu-archive-keyring.gpg"
NOBLE = (
    b"# Preserve the runner contents.\n"
    b"Types: deb\n"
    b"URIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
    b"Suites: noble noble-updates noble-backports\n"
    b"Components: main universe restricted multiverse\n"
    b"Signed-By:\t" + ORIGINAL_SIGNER + b"  \n"
)
SECURITY = NOBLE.replace(
    b"noble noble-updates noble-backports", b"noble-security"
).rstrip(b"\n")


def test_scoped_source_changes_only_each_explicit_signer_value() -> None:
    original = NOBLE + b"\n" + SECURITY

    scoped = rewrite_source(original, STAGED_SIGNER, ORIGINAL_SIGNER)

    assert scoped == original.replace(ORIGINAL_SIGNER, STAGED_SIGNER)
    assert rewrite_source(scoped, ORIGINAL_SIGNER, STAGED_SIGNER) == original


@pytest.mark.parametrize(
    "changed",
    [
        NOBLE.replace(ORIGINAL_SIGNER, b"/etc/apt/trusted.gpg"),
        NOBLE.partition(b"Signed-By:")[0],
        NOBLE + b"Trusted: yes\n",
        NOBLE.replace(b"Signed-By:", b"# comment\rSigned-By:"),
        NOBLE + b"Signed-By: " + ORIGINAL_SIGNER + b"\n",
        NOBLE.replace(b"Types: deb", b"Types: deb deb-src"),
        NOBLE.replace(b"mirror+file:/etc/apt/apt-mirrors.txt", b"https://archive.test"),
    ],
    ids=[
        "replacement",
        "missing",
        "extra",
        "hidden-signer",
        "duplicate",
        "source-packages",
        "mirror",
    ],
)
def test_scoped_source_refuses_settings_outside_the_approved_scope(
    changed: bytes,
) -> None:
    signer = STAGED_SIGNER

    with pytest.raises(Refused):
        rewrite_source(changed, signer, ORIGINAL_SIGNER)
