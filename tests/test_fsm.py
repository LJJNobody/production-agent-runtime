import unittest

from agent_runtime.errors import InvalidStateTransition
from agent_runtime.fsm import LEGAL_TRANSITIONS, StateMachine
from agent_runtime.models import AgentKind, RunRecord, RunState


def make_run():
    return RunRecord(
        id="run-1",
        input="test",
        kind=AgentKind.SIMPLE,
        session_id="session-1",
        trace_id="trace-1",
    )


class FSMTests(unittest.IsolatedAsyncioTestCase):
    def test_exactly_seven_states_and_twelve_transitions(self):
        self.assertEqual(len(RunState), 7)
        self.assertEqual(len(LEGAL_TRANSITIONS), 12)

    async def test_valid_lifecycle(self):
        run = make_run()
        fsm = StateMachine()
        await fsm.transition(run, RunState.READY)
        await fsm.transition(run, RunState.RUNNING)
        await fsm.transition(run, RunState.WAITING)
        await fsm.transition(run, RunState.RUNNING)
        await fsm.transition(run, RunState.SUCCEEDED)
        self.assertEqual(run.state, RunState.SUCCEEDED)
        self.assertEqual(len(run.transitions), 5)

    async def test_illegal_transition_is_rejected_without_mutation(self):
        run = make_run()
        with self.assertRaises(InvalidStateTransition):
            await StateMachine().transition(run, RunState.SUCCEEDED)
        self.assertEqual(run.state, RunState.CREATED)


if __name__ == "__main__":
    unittest.main()
