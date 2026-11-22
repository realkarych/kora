import pytest

from kora.protocols.harness_versions import tested_version_refusal as version_refusal


@pytest.mark.parametrize(
    ("installed", "tested", "expected"),
    [
        ("2.1.301", "==2.1.301", None),
        ("2.1.302", "==2.1.301", "outside_tested_versions"),
        ("2.0", ">=2,<3", None),
        ("3.0", ">=2,<3", "outside_tested_versions"),
        ("1.9", ">=2,<3", "outside_tested_versions"),
        ("2.1.0", "~=2.1.0", None),
        ("2.1.99", "~=2.1.0", None),
        ("2.2.0", "~=2.1.0", "outside_tested_versions"),
        ("2.0.99", "~=2.1.0", "outside_tested_versions"),
        ("2.2rc1", ">=2,<3", None),
        ("3.0rc1", ">=2,<3", "outside_tested_versions"),
        ("2.2rc1", ">=2.2rc1,<3", None),
        ("not-a-version", ">=2", "version_invalid"),
        ("2", "not-a-range", "tested_versions_invalid"),
    ],
)
def test_harness_version_inside_tested_range(
    installed: str, tested: str, expected: str | None
) -> None:
    actual = version_refusal(installed, tested)

    assert actual == expected
