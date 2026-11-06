"""Sessions: tabs with their own conversation and model tier, running in parallel. The local model serves one reply
at a time and file changes happen one at a time (harness/gates); a cloud tier exists only when configured."""
import asyncio
import os
import tempfile
import threading
import time
import unittest

from mini.harness.gates import FOLDER, LOCAL, Gate, Stopped
from mini.ui.app import MiniApp
from tests.helpers import call, fake_backend, step, temp_config


def quick():
    return fake_backend([step(call("understand", goal="g", inputs=[], outputs=["n"], plan="1")),
                         step(call("decide_coverage", reasoning="none needed", use=[], missing=[])),
                         step(call("finish", answer="done"))])


class GateTest(unittest.TestCase):
    def test_one_at_a_time_in_arrival_order(self):
        g, inside, order, lock = Gate(), [0], [], threading.Lock()

        def worker(i):
            with g.hold():
                with lock:
                    inside[0] += 1
                    assert inside[0] == 1, "two holders at once"
                    order.append(i)
                time.sleep(0.05)
                with lock:
                    inside[0] -= 1
        ts = []
        for i in range(4):
            t = threading.Thread(target=worker, args=(i,))
            ts.append(t)
            t.start()
            time.sleep(0.02)
        for t in ts:
            t.join()
        self.assertEqual(order, [0, 1, 2, 3])

    def test_waiting_is_announced_once_and_stop_leaves_the_line(self):
        g, stop, waited, res = Gate(), threading.Event(), [], []
        with g.hold():
            def other():
                try:
                    with g.hold(on_wait=lambda: waited.append(1), stop_event=stop):
                        res.append("got it")
                except Stopped:
                    res.append("stopped")
            t = threading.Thread(target=other)
            t.start()
            time.sleep(0.5)
            stop.set()
            t.join(2)
        self.assertEqual((waited, res), ([1], ["stopped"]))
        with g.hold():                   # nobody is left holding a place
            pass

    def test_holder_may_enter_again(self):
        with FOLDER.hold():
            with FOLDER.hold():
                pass
        self.assertFalse(FOLDER.busy or LOCAL.busy)


class SessionsUI(unittest.TestCase):
    def app(self, **over):
        cfg = temp_config(**{"approvals.confirm_first": False, **over})
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        return MiniApp(cfg, backend_factory=quick)

    def test_two_sessions_run_in_parallel_with_their_own_history(self):
        app = self.app()

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                first = app.active
                await pilot.press(*"/new second")
                await pilot.press("enter")
                for _ in range(50):
                    await pilot.pause(0.1)
                    if len(app.sessions) == 2 and app.active is not first:
                        break
                second = app.active
                self.assertEqual((len(app.sessions), second.name), (2, "second"))
                await pilot.press(*"task two")
                await pilot.press("enter")
                await pilot.press("alt+1")
                await pilot.pause(0.3)
                self.assertIs(app.active, first)
                await pilot.press(*"task one")
                await pilot.press("enter")
                for _ in range(150):
                    await pilot.pause(0.1)
                    if not first.busy and not second.busy:
                        break
                self.assertFalse(first.busy or second.busy)
                self.assertEqual([h["task"] for h in first.history], ["task one"])
                self.assertEqual([h["task"] for h in second.history], ["task two"])
                self.assertEqual({h["status"] for h in first.history + second.history}, {"success"})
        asyncio.run(go())

    def test_model_is_per_session_and_only_for_configured_tiers(self):
        app = self.app()

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"/model paid")
                await pilot.press("enter")
                self.assertEqual(app.active.tier, "local")          # no [paid] in the config: not enabled
                app.cfg["paid"]["enabled"] = True
                await pilot.press(*"/model paid")
                await pilot.press("enter")
                self.assertEqual(app.active.tier, "paid")
                first = app.active
                await pilot.press(*"/new")
                await pilot.press("enter")
                for _ in range(50):
                    await pilot.pause(0.1)
                    if app.active is not first:
                        break
                self.assertEqual((first.tier, app.active.tier), ("paid", "local"))
        asyncio.run(go())

    def test_closing_the_last_session_exits_and_forgets_it(self):
        path = os.path.join(tempfile.mkdtemp(), ".mini", "sessions.json")
        cfg = temp_config()
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        app = MiniApp(cfg, backend_factory=quick, sessions_file=path)

        async def go():
            async with app.run_test(size=(180, 50)) as pilot:
                await pilot.pause(0.2)
                self.assertTrue(os.path.exists(path))
                await pilot.press(*"/close")
                await pilot.press("enter")
                await pilot.pause(0.3)
            self.assertFalse(os.path.exists(path))
            self.assertFalse(app.is_running)
        asyncio.run(go())

    def test_sessions_survive_closing_the_app(self):
        path = os.path.join(tempfile.mkdtemp(), ".mini", "sessions.json")

        def make():
            cfg = temp_config(**{"approvals.confirm_first": False})
            cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
            return MiniApp(cfg, backend_factory=quick, sessions_file=path)

        async def first():
            a = make()
            async with a.run_test(size=(180, 50)) as pilot:
                await pilot.press(*"/new second")
                await pilot.press("enter")
                for _ in range(50):
                    await pilot.pause(0.1)
                    if len(a.sessions) == 2:
                        break
                await pilot.press(*"task two")
                await pilot.press("enter")
                for _ in range(150):
                    await pilot.pause(0.1)
                    if not a.active.busy and a.active.history:
                        break
        asyncio.run(first())

        async def again():
            a = make()
            async with a.run_test(size=(180, 50)) as pilot:
                await pilot.pause(0.3)
                self.assertEqual([x.name for x in a.sessions], ["1", "second"])
                self.assertEqual(a.active.name, "second")
                self.assertEqual([h["task"] for h in a.active.history], ["task two"])
                await pilot.press("ctrl+t")
                for _ in range(50):
                    await pilot.pause(0.1)
                    if len(a.sessions) == 3:
                        break
                self.assertEqual(a.sessions[-1].n, 3)               # numbers continue, none reused
        asyncio.run(again())

    def test_a_task_cut_off_by_closing_is_reported_interrupted(self):
        path = os.path.join(tempfile.mkdtemp(), ".mini", "sessions.json")
        from mini.ui.session import Session, save
        s = Session(1, "1", "local", 3, pending="half done")
        save(path, [s], s)
        cfg = temp_config()
        cfg.path = os.path.join(tempfile.mkdtemp(), "mini.toml")
        a = MiniApp(cfg, backend_factory=quick, sessions_file=path)

        async def go():
            async with a.run_test(size=(180, 50)) as pilot:
                await pilot.pause(0.3)
                self.assertEqual([(h["task"], h["status"]) for h in a.sessions[0].history], [("half done", "interrupted")])
                self.assertIsNone(a.sessions[0].pending)
        asyncio.run(go())


class TimeCapsTest(unittest.TestCase):
    def test_waiting_for_the_user_does_not_count_against_the_time_caps(self):
        from mini.agent.core import Agent
        agent = Agent(temp_config(), backend=quick(), approval_handler=lambda *a: time.sleep(0.5))
        before = (agent.wd.task_t0, agent.run_t0)
        agent.gate.handler("ask", "t", {}, [])
        self.assertGreaterEqual(agent.wd.task_t0 - before[0], 0.45)
        self.assertGreaterEqual(agent.run_t0 - before[1], 0.45)


if __name__ == "__main__":
    unittest.main()
