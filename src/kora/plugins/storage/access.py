from kora.plugins.storage.content_files import ContentFiles, address_lock, content_path
from kora.plugins.storage.errors import refuse
from kora.plugins.storage.payload import FileRunner, FileSegment, same_file
from kora.plugins.storage.readers import LocalReadStream, ReaderManager
from kora.plugins.storage.state import LocalState
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import ContentAddress, ContentRef


class ContentAccess:
    def __init__(
        self, state: LocalState, runner: FileRunner, readers: ReaderManager
    ) -> None:
        self.state = state
        self.root = state.root
        self.runner = runner
        self.readers = readers
        self.files = ContentFiles(self.root)

    def locate(self, content: ContentRef, ctx: ProtocolContext) -> FileSegment:
        with self.state.transaction(synchronize=False) as connection:
            self.state.admit(connection, ctx)
            indexed = self.state.content(connection, content.address)
        if indexed is None:
            refuse("storage_unavailable")
        if indexed[0] != content:
            refuse("storage_conflict")
        return FileSegment(
            content_path(content.address), indexed[1], content.total_size
        )

    async def open(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> LocalReadStream:
        segment = await self.runner(lambda: self.locate(content, ctx))

        def validate() -> None:
            if self.locate(content, ctx) != segment:
                refuse("storage_conflict")
            actual = self.files.locate(content.address)
            if actual is None:
                refuse("storage_unavailable")
            if (
                not same_file(actual.identity, segment.identity)
                or actual.size != segment.size
            ):
                refuse("storage_integrity")

        return await self.readers.open(
            (segment,), address_lock(content.address), validate, offset, length, ctx
        )

    async def exists(
        self, address: ContentAddress, ctx: ProtocolContext
    ) -> ContentRef | None:
        if address.storage_identity != self.root.identity:
            refuse("storage_conflict")

        def inspect() -> ContentRef | None:
            stripe = self.root.try_lock(address_lock(address), shared=True)
            if stripe is None:
                refuse("storage_busy")
            with stripe:
                with self.state.transaction(synchronize=False) as connection:
                    self.state.admit(connection, ctx)
                    indexed = self.state.content(connection, address)
                actual = self.files.locate(address)
                if indexed is None:
                    if actual is not None:
                        refuse("storage_unavailable")
                    return None
                if (
                    actual is None
                    or not same_file(actual.identity, indexed[1])
                    or actual.size != indexed[0].total_size
                ):
                    refuse("storage_integrity")
                return indexed[0]

        return await self.runner(inspect)
