"""Every Inventor call has to happen on one thread, and none has yet.

Inventor's API is apartment-threaded: an interface obtained on one thread may
not be used from another, and `CoInitialize` applies per thread rather than per
process. The MCP SDK runs synchronous tool functions on a pool of worker
threads, and `connect` calls `CoInitialize` exactly once -- on whichever worker
happened to serve it. So the first real client call can land on a thread that
never initialised COM.

Nothing has caught this because nothing has ever driven the tool layer against
Inventor: `live_smoke.py` imports the builder directly and skips the server. The
machinery is testable without Inventor, though, and that is what this does.
"""

from __future__ import annotations

import threading
import time

import pytest

from inventor_mcp.backend.com.marshal import SingleThread, ThreadStopped, on_thread


@pytest.fixture
def worker():
    thread = SingleThread("test-worker")
    yield thread
    thread.stop()


class TestOneThreadForEverything:
    def test_work_runs_somewhere_other_than_the_caller(self, worker):
        where = worker.call(threading.get_ident)
        assert where != threading.get_ident()

    def test_and_always_on_the_same_one(self, worker):
        seen = {worker.call(threading.get_ident) for _ in range(20)}
        assert len(seen) == 1

    def test_even_when_called_from_many_threads_at_once(self, worker):
        """The case that actually happens: a pool of MCP tool workers."""
        seen: list[int] = []
        lock = threading.Lock()

        def ask():
            answer = worker.call(threading.get_ident)
            with lock:
                seen.append(answer)

        callers = [threading.Thread(target=ask) for _ in range(12)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()
        assert len(seen) == 12
        assert len(set(seen)) == 1, "all work must land on one apartment"

    def test_results_come_back_intact(self, worker):
        assert worker.call(lambda a, b=1: (a, b), 5, b=7) == (5, 7)

    def test_calls_are_serialised(self, worker):
        """Inventor drives one document; overlapping calls are not wanted."""
        overlaps = []
        active = []
        lock = threading.Lock()

        def slow():
            with lock:
                active.append(1)
                overlaps.append(len(active))
            time.sleep(0.01)
            with lock:
                active.pop()

        callers = [threading.Thread(target=lambda: worker.call(slow)) for _ in range(6)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()
        assert max(overlaps) == 1


class TestFailuresStillReadAsFailures:
    def test_an_exception_keeps_its_type_and_message(self, worker):
        from inventor_mcp.errors import FeatureError

        def fail():
            raise FeatureError("the hole removed no material", hint="check the centres")

        with pytest.raises(FeatureError, match="removed no material") as raised:
            worker.call(fail)
        assert raised.value.hint == "check the centres"

    def test_calling_after_a_shutdown_says_so(self):
        """It must not quietly start a new thread.

        A new thread is a new COM apartment, and every interface the backend is
        still holding came from the old one -- so a silent restart would turn a
        clean "reconnect first" into the cross-apartment failure this whole
        class exists to prevent.
        """
        worker = SingleThread("short-lived")
        worker.start()
        worker.stop()
        with pytest.raises(ThreadStopped, match="apartment"):
            worker.call(lambda: None)

    def test_an_explicit_restart_is_allowed(self):
        """And initialises COM again, since it is a new apartment.

        Not asserted on the thread identifier: CPython reuses those once a
        thread has exited, so a genuinely new thread can report the old number.
        Whether setup ran again is the fact that matters.
        """
        started = []
        worker = SingleThread("restartable", setup=lambda: started.append(1))
        worker.start()
        worker.call(lambda: None)
        worker.stop()
        worker.start()
        try:
            assert worker.call(lambda: "alive") == "alive"
            assert started == [1, 1], "a restarted thread must initialise COM again"
        finally:
            worker.stop()

    def test_a_call_from_the_worker_itself_does_not_deadlock(self, worker):
        """A backend method calling another one must not queue behind itself."""
        def outer():
            return worker.call(lambda: "inner ran")

        assert worker.call(outer) == "inner ran"


class TestSetupRunsWhereItMatters:
    def test_setup_runs_on_the_worker_not_the_caller(self):
        seen: dict[str, int] = {}
        worker = SingleThread("with-setup",
                              setup=lambda: seen.__setitem__("setup", threading.get_ident()))
        try:
            worked = worker.call(threading.get_ident)
            assert seen["setup"] == worked, "CoInitialize must run on the work thread"
            assert seen["setup"] != threading.get_ident()
        finally:
            worker.stop()

    def test_setup_runs_once_however_many_calls(self):
        count = []
        worker = SingleThread("once", setup=lambda: count.append(1))
        try:
            for _ in range(5):
                worker.call(lambda: None)
            assert count == [1]
        finally:
            worker.stop()

    def test_teardown_runs_on_the_way_out(self):
        done = []
        worker = SingleThread("bye", teardown=lambda: done.append(1))
        worker.call(lambda: None)
        worker.stop()
        assert done == [1]

    def test_a_broken_setup_does_not_wedge_the_thread(self):
        """CoInitialize failing is reported per call, not by hanging."""
        def bad():
            raise RuntimeError("CoInitialize failed")

        worker = SingleThread("broken", setup=bad)
        try:
            assert worker.call(lambda: "still works") == "still works"
        finally:
            worker.stop()


class TestTheProxy:
    class Backend:
        name = "pretend"

        def __init__(self):
            self.threads = []

        def mass_properties(self, doc_id):
            self.threads.append(threading.get_ident())
            return f"weighed {doc_id}"

        def explode(self):
            raise ValueError("nope")

    def test_methods_are_routed_and_attributes_are_not(self, worker):
        backend = self.Backend()
        proxy = on_thread(backend, worker)
        assert proxy.name == "pretend"          # a plain read needs no thread
        assert proxy.mass_properties("doc1") == "weighed doc1"
        assert backend.threads == [worker.thread_id]

    def test_every_call_lands_on_the_same_thread(self, worker):
        backend = self.Backend()
        proxy = on_thread(backend, worker)
        for index in range(8):
            proxy.mass_properties(f"doc{index}")
        assert len(set(backend.threads)) == 1

    def test_errors_pass_straight_through(self, worker):
        proxy = on_thread(self.Backend(), worker)
        with pytest.raises(ValueError, match="nope"):
            proxy.explode()

    def test_the_real_object_stays_reachable(self, worker):
        backend = self.Backend()
        proxy = on_thread(backend, worker)
        assert proxy.unmarshalled is backend

    def test_the_method_keeps_its_name_for_error_messages(self, worker):
        proxy = on_thread(self.Backend(), worker)
        assert proxy.mass_properties.__name__ == "mass_properties"


class TestWiring:
    def test_the_simulator_is_never_pinned(self):
        """It is pure Python; a thread hop would only add latency."""
        from inventor_mcp.backend import create_backend

        backend = create_backend("mock")
        assert not hasattr(backend, "marshalling_thread")

    def test_the_com_backend_is_pinned_by_default(self):
        from inventor_mcp.backend import _pinned

        pinned = _pinned(TestTheProxy.Backend())
        try:
            assert pinned.marshalling_thread is not None
            assert pinned.mass_properties("d") == "weighed d"
        finally:
            pinned.marshalling_thread.stop()

    def test_it_can_be_turned_off_to_isolate_a_problem(self, monkeypatch):
        from inventor_mcp.backend import _pinned

        monkeypatch.setenv("INVENTOR_MCP_THREADING", "off")
        backend = TestTheProxy.Backend()
        assert _pinned(backend) is backend
