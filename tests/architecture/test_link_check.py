import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from scripts.ci.link_check import (
    ASSET_URL,
    BINARY_MEMBER,
    INVENTORY_OPTIONS,
    LYCHEE,
    Asset,
    LinkCheckFailed,
    executable,
    markdown_arguments,
    next_request,
    private_environment,
    report_has_links,
    require_curl_version,
)

REDIRECT = b"https://release-assets.githubusercontent.com/path?sig=test%2Bvalue"
REPORT_START = "# Summary\n\n| Status         | Count |\n|----------------|-------|\n"
TOTAL = "| 🔍 Total       | 1     |\n"


def test_lychee_release_identity_is_exact() -> None:
    assert ASSET_URL == (
        "https://api.github.com/repos/lycheeverse/lychee/releases/assets/409959156"
    )
    assert BINARY_MEMBER == "lychee-x86_64-unknown-linux-gnu/lychee"
    assert LYCHEE == Asset(
        7_626_970,
        "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a",
        19_827_680,
        "87e6e75195df5753f08c53b5c0a13694b8328edd8df009914ae9e29b5000162d",
    )


def test_curl_direct_or_single_redirect_and_no_inherited_environment(
    tmp_path: Path,
) -> None:
    direct = b"200\n\n"
    redirected = b"302\n" + REDIRECT + b"\n"

    direct_config = next_request(direct)
    redirected_config = next_request(redirected)
    environment = private_environment(tmp_path)

    assert direct_config is None
    assert redirected_config == b'url = "' + REDIRECT + b'"\n'
    assert environment == {
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
    }


@pytest.mark.parametrize(
    "response",
    [
        b"301\n" + REDIRECT + b"\n",
        b"403\n\n",
        b"200\n" + REDIRECT + b"\n",
        b"302\n\n",
        b"302\n" + REDIRECT,
        b"302\n" + REDIRECT + b"\n\n",
        b"302\n" + REDIRECT + b"\r\n",
        b"302\n" + REDIRECT + b"#\n",
        b"302\n" + REDIRECT + b"\x7f\n",
        b"302\n" + REDIRECT + b"\\n",
        b"302\n" + REDIRECT + b' "header = token"\n',
        b"302\n" + REDIRECT + b"{a,b}\n",
        b"302\n" + REDIRECT + b"[1-2]\n",
        b"302\n" + REDIRECT + b"x" * 8192 + b"\n",
        b"302\nhttp://release-assets.githubusercontent.com/file\n",
        b"302\nhttps://release-assets.githubusercontent.com.evil.example/file\n",
        b"302\nhttps://bad@release-assets.githubusercontent.com/file\n",
        b"302\nhttps://release-assets.githubusercontent.com:443/file\n",
        b"302\nhttps://release-assets.githubusercontent.com\n",
    ],
)
def test_refused_release_response_omits_private_data(response: bytes) -> None:
    with pytest.raises(LinkCheckFailed) as rejected:
        next_request(response)

    assert str(rejected.value) in {
        "The Lychee release response was refused.",
        "The Lychee release redirect was refused.",
    }


@pytest.mark.parametrize(
    "version", [b"curl 8.3.0 platform\n", b"invalid", b"curl 7.88.1 "]
)
def test_curl_must_support_streaming_transfer_ceiling(version: bytes) -> None:
    with pytest.raises(LinkCheckFailed) as rejected:
        require_curl_version(version)

    assert str(rejected.value) == (
        "A curl version with bounded transfer support is required."
    )


def test_curl_8_4_and_newer_are_supported() -> None:
    assert require_curl_version(b"curl 8.4.0 (platform)\n") is None
    assert require_curl_version(b"curl 9.0.0 (platform)\n") is None


def write_bundle(
    archive: Path, member_types: tuple[bytes, ...], binary: bytes
) -> Asset:
    with tarfile.open(archive, mode="w:gz") as bundle:
        for member_type in member_types:
            member = tarfile.TarInfo(BINARY_MEMBER)
            member.type = member_type
            if member_type == tarfile.REGTYPE:
                member.size = len(binary)
                bundle.addfile(member, io.BytesIO(binary))
            else:
                member.linkname = "../unexpected"
                bundle.addfile(member)
    content = archive.read_bytes()
    return Asset(
        len(content),
        hashlib.sha256(content).hexdigest(),
        len(binary),
        hashlib.sha256(binary).hexdigest(),
    )


def test_only_the_verified_regular_member_becomes_executable(tmp_path: Path) -> None:
    archive, destination = tmp_path / "archive.tar.gz", tmp_path / "checker"
    binary = b"known fixture bytes"
    asset = write_bundle(archive, (tarfile.REGTYPE,), binary)

    result = executable(archive, destination, asset)

    assert result == destination
    assert result.read_bytes() == binary
    assert result.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "member_types",
    [(), (tarfile.SYMTYPE,), (tarfile.LNKTYPE,), (tarfile.REGTYPE, tarfile.REGTYPE)],
)
def test_archive_requires_a_unique_regular_member(
    tmp_path: Path, member_types: tuple[bytes, ...]
) -> None:
    archive, destination = tmp_path / "archive.tar.gz", tmp_path / "checker"
    asset = write_bundle(archive, member_types, b"fixture bytes")

    with pytest.raises(LinkCheckFailed, match="unique regular binary"):
        executable(archive, destination, asset)

    assert not destination.exists()


@pytest.mark.parametrize(
    "field", ["archive_size", "archive_sha256", "binary_size", "binary_sha256"]
)
def test_archive_and_binary_size_and_digest_are_independently_required(
    tmp_path: Path, field: str
) -> None:
    archive, destination = tmp_path / "archive.tar.gz", tmp_path / "checker"
    asset = write_bundle(archive, (tarfile.REGTYPE,), b"fixture bytes")
    wrong = Asset(
        asset.archive_size + (field == "archive_size"),
        "0" * 64 if field == "archive_sha256" else asset.archive_sha256,
        asset.binary_size + (field == "binary_size"),
        "0" * 64 if field == "binary_sha256" else asset.binary_sha256,
    )

    with pytest.raises(LinkCheckFailed, match="failed verification|unexpected size"):
        executable(archive, destination, wrong)

    assert not destination.exists()


def test_inventory_produces_exact_local_arguments(tmp_path: Path) -> None:
    inventory = (
        b"space name.md\0line\nbreak.md\0http:/localhost/path.md\0[?]*.md\0--.md\0"
    )

    arguments = markdown_arguments(tmp_path, inventory)

    assert INVENTORY_OPTIONS == ("--hidden",)
    assert arguments == (
        "./space name.md",
        "./line\nbreak.md",
        "./http:/localhost/path.md",
        "./[[][?][]][*].md",
        "./--.md",
    )


@pytest.mark.parametrize("inventory", [b"", b"one.md", b"\0", b"one.md\0\0"])
def test_missing_or_partial_inventory_refuses(tmp_path: Path, inventory: bytes) -> None:
    with pytest.raises(LinkCheckFailed) as rejected:
        markdown_arguments(tmp_path, inventory)

    assert str(rejected.value) == "The Markdown inventory is missing or invalid."


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (REPORT_START + TOTAL, True),
        (REPORT_START + TOTAL + "\n## Details\n" + TOTAL, True),
        (REPORT_START + TOTAL.replace("1", "0"), False),
        (REPORT_START + TOTAL + TOTAL, False),
        (REPORT_START + "\n## Details\n" + TOTAL, False),
        (TOTAL, False),
        ("", False),
    ],
)
def test_only_one_positive_total_in_the_initial_report_qualifies(
    report: str, expected: bool
) -> None:
    qualified = report_has_links(report)

    assert qualified is expected


def test_inventory_refuses_markdown_alias_to_directory(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / ".extra.md").write_text("[unselected](./absent.md)\n")
    (repository / "alias.md").symlink_to(directory, target_is_directory=True)

    with pytest.raises(LinkCheckFailed) as refusal:
        markdown_arguments(repository, b"valid.md\0alias.md\0")

    assert str(refusal.value) == "A selected Markdown input resolves to a directory."
