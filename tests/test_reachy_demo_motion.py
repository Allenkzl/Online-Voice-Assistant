"""Phase ownership of the Reachy head in the showroom motion loop."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


DEMO_PATH = Path(__file__).resolve().parents[1] / "deploy/reachy-demo/demo.py"
spec = importlib.util.spec_from_file_location("reachy_demo_motion", DEMO_PATH)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class EndLoop(Exception):
    pass


class ReachyDemoMotionTests(unittest.TestCase):
    def run_phases(self, phases, tracking_result=True):
        events = []
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps > len(phases):  # startup wake sleep + one per loop
                raise EndLoop

        def tracking(on):
            events.append(("tracking", on))
            return tracking_result

        with (
            patch.object(demo, "TRACKING_ENABLED", True),
            patch.object(demo, "_ensure_backend", return_value=True),
            patch.object(demo, "_ensure_motors"),
            patch.object(demo, "_req", return_value={}),
            patch.object(demo, "_neutral", side_effect=lambda: events.append(("neutral",))),
            patch.object(demo, "_run_one_move", side_effect=lambda name: events.append(("move", name))),
            patch.object(demo, "_tracking", side_effect=tracking),
            patch.object(demo, "_read_phase", side_effect=phases),
            patch.object(demo.time, "sleep", side_effect=sleep),
        ):
            with self.assertRaises(EndLoop):
                demo.main()
        return events

    def test_speaking_disables_externally_enabled_tracking_before_motion(self):
        # main() starts with tracking_on=False, though the daemon may already
        # have tracking active from a separate API call.
        events = self.run_phases(["speaking"])
        self.assertEqual(events[0:2], [("neutral",), ("tracking", False)])
        self.assertEqual(events[2][0], "neutral")
        self.assertEqual(events[3][0], "move")

    def test_idle_restores_face_tracking_after_speaking(self):
        events = self.run_phases(["speaking", "idle"])
        self.assertIn(("tracking", False), events)
        self.assertIn(("tracking", True), events)
        self.assertLess(events.index(("tracking", False)), events.index(("tracking", True)))

    def test_failed_pause_does_not_start_speaking_move(self):
        events = self.run_phases(["speaking"], tracking_result=False)
        self.assertIn(("tracking", False), events)
        self.assertFalse(any(event[0] == "move" for event in events))


if __name__ == "__main__":
    unittest.main()
