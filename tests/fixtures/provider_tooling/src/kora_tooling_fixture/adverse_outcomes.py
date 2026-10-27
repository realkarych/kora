from typing import NoReturn

import pytest


def skip_fixture() -> NoReturn:
    pytest.skip("intentional installed fixture skip")


def xfail_fixture() -> NoReturn:
    pytest.xfail("intentional installed fixture xfail")
