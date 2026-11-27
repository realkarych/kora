import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from typing import TypeVar

from kora.db import CUSTODY_REFUSED, CUSTODY_TRANSITION, CUSTODY_UNFINISHED, custody_log
from kora.db.custody_journal import (
    CustodyJournalPhase,
    CustodyJournalTransaction,
    complete_native_journal_job,
)
from kora.db.custody_models import CustodyRecoveryRegistration, CustodyUnavailable
from kora.db.custody_native import (
    CustodyDescriptor,
    CustodyLatch,
    CustodyLifetimeLease,
    CustodyNativeDirectory,
    CustodyNativeWait,
    CustodyRecoveryLease,
)

Result = TypeVar("Result")


class CustodyRecoveryPriority:
    _loop: asyncio.AbstractEventLoop
    _root: CustodyNativeDirectory
    _coordinator: CustodyRecoveryLease
    _lifetime: CustodyLifetimeLease
    _journal: CustodyJournalPhase
    _worker: ThreadPoolExecutor
    _registration: CustodyRecoveryRegistration | None
    _registrar: CustodyDescriptor | None
    _turn: CustodyNativeWait | None
    _busy: bool
    _open: bool
    _ready: bool
    _commit_uncertain: bool
    _must_finish: bool
    _closed: bool

    def __init__(self, root: CustodyNativeDirectory) -> None:
        self._loop = asyncio.get_running_loop()
        self._root = root
        with ExitStack() as cleanup:
            self._coordinator = CustodyRecoveryLease(root)
            cleanup.callback(self._coordinator.close)
            self._lifetime = CustodyLifetimeLease(root)
            cleanup.callback(self._lifetime.close)
            self._worker = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="kora-custody-recovery"
            )
            cleanup.pop_all()
        self._journal = CustodyJournalPhase(root)
        self._registration = None
        self._registrar = None
        self._turn = None
        self._busy = False
        self._open = False
        self._ready = False
        self._commit_uncertain = False
        self._must_finish = False
        self._closed = False

    async def register(
        self, *, journal_id: bytes, generation: int
    ) -> CustodyRecoveryRegistration:
        return await self._run(lambda: self._register(journal_id, generation))

    def _register(
        self, journal_id: bytes, generation: int
    ) -> CustodyRecoveryRegistration:
        if self._registration is not None:
            raise CustodyUnavailable
        self._root.require_fresh_provision()
        registrar = self._root.lock("commit.lock")
        self._registrar = registrar
        try:
            self._registration = self._transaction(
                lambda journal: self._register_fresh(journal, journal_id, generation)
            )
        finally:
            if self._journal.released and not self._commit_uncertain:
                registrar.close()
                self._registrar = None
        self._reopen()
        return self._registration

    def _register_fresh(
        self, journal: CustodyJournalTransaction, journal_id: bytes, generation: int
    ) -> CustodyRecoveryRegistration:
        journal.require_fresh_recovery(self._coordinator, journal_id, generation)
        return journal.reserve_recovery_coordinator(
            self._coordinator,
            self._lifetime,
            journal_id=journal_id,
            generation=generation,
        )

    async def request(self) -> None:
        await self._run(self._request)

    def _request(self) -> None:
        registration = self._registration
        if registration is None or not self._open:
            raise CustodyUnavailable
        self._open = False
        self._must_finish = True
        self._coordinator.arm()
        self._publish_priority(registration)
        self._take_turn()
        self._mark_ready(registration)

    async def succeed_native_only(self, *, journal_id: bytes, generation: int) -> None:
        await self._run(lambda: self._succeed_native_only(journal_id, generation))

    def _succeed_native_only(self, journal_id: bytes, generation: int) -> None:
        if self._registration is not None or self._turn is not None:
            raise CustodyUnavailable
        latch = self._coordinator.deny_for_successor(self._lifetime)
        if latch is CustodyLatch.BOOTSTRAP_FROM_OPEN:
            custody_log(CUSTODY_TRANSITION, "successor_denial_persisted")
        turn = self._take_turn()
        try:
            registration = self._transaction(
                lambda journal: journal.reconcile_native_only_successor(
                    self._coordinator,
                    self._lifetime,
                    turn,
                    journal_id=journal_id,
                    generation=generation,
                )
            )
            self._registration = registration
        finally:
            if (
                self._registration is None
                and self._journal.released
                and not self._commit_uncertain
            ):
                self._close_turn(turn)
        self._must_finish = True
        custody_log(CUSTODY_TRANSITION, "successor_registered")
        self._coordinator.arm_successor(self._lifetime)
        self._publish_priority(registration)
        self._mark_ready(registration)

    def _take_turn(self) -> CustodyNativeWait:
        turn = self._root.open_wait("commit.lock")
        self._turn = turn
        turn.acquire()
        return turn

    def _close_turn(self, turn: CustodyNativeWait) -> None:
        if self._turn is not turn:
            raise CustodyUnavailable
        turn.close()
        self._turn = None

    def _publish_priority(self, registration: CustodyRecoveryRegistration) -> None:
        self._transaction(
            lambda journal: journal.current_recovery_priority(
                self._coordinator, registration, request=True
            )
        )
        custody_log(CUSTODY_TRANSITION, "hint_published")

    def _mark_ready(self, registration: CustodyRecoveryRegistration) -> None:
        self._require_live_priority(registration)
        self._ready = True
        custody_log(CUSTODY_TRANSITION, "native_turn")

    async def withdraw(self) -> None:
        await self._run(self._withdraw)

    def _withdraw(self) -> None:
        registration = self._registration
        turn = self._turn
        if registration is None or turn is None or not self._ready:
            raise CustodyUnavailable
        turn.require_acquired()
        self._require_live_priority(registration, withdraw=True)
        self._ready = False
        self._close_turn(turn)
        self._reopen()

    def _require_live_priority(
        self, registration: CustodyRecoveryRegistration, *, withdraw: bool = False
    ) -> None:
        if self._root.read_latch() is not CustodyLatch.ARMED:
            raise CustodyUnavailable

        def verify(journal: CustodyJournalTransaction) -> None:
            journal.require_live_priority(self._coordinator, registration)
            if withdraw:
                journal.current_recovery_priority(
                    self._coordinator, registration, request=False
                )

        self._transaction(verify)

    def _reopen(self) -> None:
        self._must_finish = True
        self._lifetime.require_current()
        self._coordinator.reopen()
        self._open = True
        self._must_finish = False
        custody_log(CUSTODY_TRANSITION, "ordinary_open")

    async def _run(self, operation: Callable[[], Result]) -> Result:
        if (
            asyncio.get_running_loop() is not self._loop
            or self._closed
            or self._busy
            or not self._journal.released
            or self._commit_uncertain
            or self._registrar is not None
        ):
            custody_log(CUSTODY_REFUSED, "coordinator_admission")
            raise CustodyUnavailable
        self._coordinator.require_current()
        self._lifetime.require_current()
        self._busy = True
        try:
            task = self._loop.run_in_executor(self._worker, operation)
            self._journal.start_job()
            return await complete_native_journal_job(task)
        except (CustodyUnavailable, OSError, sqlite3.Error):
            if (
                self._must_finish
                or self._turn is not None
                or not self._journal.released
                or self._commit_uncertain
            ):
                custody_log(CUSTODY_UNFINISHED, "coordinator_operation")
            else:
                custody_log(CUSTODY_REFUSED, "coordinator_operation")
            raise
        finally:
            self._busy = False
            self._journal.finish_job()

    def _transaction(
        self, operation: Callable[[CustodyJournalTransaction], Result]
    ) -> Result:
        with self._journal.connection() as connection:
            with closing(connection.execute("BEGIN IMMEDIATE")):
                pass
            journal = CustodyJournalTransaction(connection, completion_owner=None)
            try:
                result = operation(journal)
                self._coordinator.require_current()
                self._lifetime.require_current()
                self._commit_uncertain = True
                connection.commit()
                self._commit_uncertain = False
            finally:
                journal.close()
                if connection.in_transaction:
                    connection.rollback()
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._coordinator.require_current()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise CustodyUnavailable from None
        if (
            loop is not self._loop
            or self._busy
            or self._registrar is not None
            or self._turn is not None
            or self._commit_uncertain
            or self._must_finish
        ):
            custody_log(CUSTODY_UNFINISHED, "coordinator_closure")
            raise CustodyUnavailable
        if not self._journal.released:
            self._journal.after_release(self._close_after_retirement)
            custody_log(CUSTODY_UNFINISHED, "coordinator_closure")
            raise CustodyUnavailable
        self._worker.shutdown(wait=True)
        self._lifetime.close()
        self._coordinator.close()
        self._closed = True

    def _close_after_retirement(self) -> None:
        try:
            self.close()
        except (CustodyUnavailable, OSError):
            custody_log(CUSTODY_UNFINISHED, "coordinator_closure")
