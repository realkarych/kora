from enum import StrEnum

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


class VersionRefusal(StrEnum):
    VERSION_INVALID = "version_invalid"
    TESTED_VERSIONS_INVALID = "tested_versions_invalid"
    OUTSIDE_TESTED_VERSIONS = "outside_tested_versions"


def tested_version_refusal(installed: str, tested: str) -> VersionRefusal | None:
    try:
        version = Version(installed)
    except InvalidVersion:
        return VersionRefusal.VERSION_INVALID
    try:
        versions = SpecifierSet(tested)
    except InvalidSpecifier:
        return VersionRefusal.TESTED_VERSIONS_INVALID
    return None if version in versions else VersionRefusal.OUTSIDE_TESTED_VERSIONS
