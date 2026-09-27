import json
import unittest

from session_inspector import render, summarize


def record(seconds, kind, **payload):
    return json.dumps({"timestamp": f"2026-09-16T00:00:{seconds:02d}Z", "type": kind, "payload": payload})


class InspectorTests(unittest.TestCase):
    def test_pairs_results_by_call_id_and_does_not_mark_workers_cancelled(self):
        data = summarize([
            record(0, "event_msg", type="task_started", turn_id="t"),
            record(1, "response_item", type="function_call", name="shell", call_id="a"),
            record(2, "response_item", type="function_call", name="shell", call_id="b"),
            record(3, "event_msg", type="turn_aborted", turn_id="t"),
            record(5, "response_item", type="function_call_output", call_id="b"),
            record(8, "response_item", type="function_call_output", call_id="a"),
        ])
        turn = data["turns"][0]
        self.assertEqual(turn["state"], "turn interrupted")
        self.assertEqual([c["end"] - c["start"] for c in turn["tools"]], [7000, 3000])
        self.assertEqual(turn["end"] - turn["start"], 3000)

    def test_incomplete_records_and_missing_results_remain_unknown(self):
        data = summarize(['{"timestamp":', '[]', record(0, "event_msg", type="task_started", turn_id="t"),
                          record(1, "response_item", type="custom_tool_call", name="exec", call_id="c")])
        self.assertEqual(data["skipped_lines"], 2)
        self.assertIsNone(data["turns"][0]["end"])
        self.assertIsNone(data["turns"][0]["tools"][0]["end"])

    def test_transcripts_commands_and_results_are_not_copied(self):
        secret = "sensitive-body-never-render"
        data = summarize([
            record(0, "realtime_item", type="transcript_segment", role="assistant", text=secret),
            record(1, "realtime_item", type="transcript_segment", role="assistant", text=secret),
            record(2, "response_item", type="custom_tool_call", name="exec", input=secret, call_id="c"),
            record(3, "response_item", type="custom_tool_call_output", output=secret, call_id="c"),
        ])
        self.assertNotIn(secret, json.dumps(data))
        self.assertIn("repeated", data["events"][1]["label"])
        self.assertIn("not playback time", data["events"][0]["detail"])

    def test_script_context_is_escaped(self):
        result = render({"tool": "</script><img src=x onerror=alert(1)>"})
        self.assertNotIn("</script><img", result)
        self.assertIn("\\u003c/script\\u003e", result)
        self.assertNotIn('http-equiv="refresh"', result)
        self.assertIn('http-equiv="refresh"', render({}, refresh=True))


if __name__ == "__main__":
    unittest.main()
