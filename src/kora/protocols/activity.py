from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Protocol


class ActivityReporter(Protocol):
    def span(self, name: str) -> AbstractAsyncContextManager[None]: ...


class NoActivity(ActivityReporter):
    def span(self, name: str) -> AbstractAsyncContextManager[None]:
        return nullcontext()
