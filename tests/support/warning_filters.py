import warnings
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.exc import SAWarning

EXPRESSION_INDEX_WARNING: str = ".*expression-based index.*"


@contextmanager
def expression_index_warnings_ignored() -> Generator[None, None, None]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", EXPRESSION_INDEX_WARNING, SAWarning)
        warnings.filterwarnings("ignore", EXPRESSION_INDEX_WARNING, UserWarning)
        yield


@contextmanager
def every_warning_raised() -> Generator[None, None, None]:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        yield
