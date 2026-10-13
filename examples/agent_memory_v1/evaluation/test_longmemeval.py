from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from .longmemeval import load_items, parse_item


class LongMemEvalAdapterTest(unittest.TestCase):
    def test_parse_session_messages_and_normalize_time(self) -> None:
        item = parse_item(
            {
                "question_id": "q1",
                "question_type": "single-session-user",
                "question": "What?",
                "answer": "A",
                "question_date": "2025-01-03",
                "haystack_session_ids": ["s1"],
                "haystack_dates": ["2025-01-02T12:00:00+08:00"],
                "haystack_sessions": [[
                    {"role": "user", "content": "A fact"},
                    {"role": "assistant", "content": "Okay"},
                ]],
                "answer_session_ids": ["s1"],
            }
        )
        self.assertEqual(item.question_id, "q1")
        self.assertEqual(item.sessions[0].messages[0].role, "user")
        self.assertEqual(item.sessions[0].messages[1].role, "assistant")
        self.assertEqual(item.sessions[0].messages[0].observed_at.tzinfo, timezone.utc)
        self.assertEqual(item.question_date.tzinfo, timezone.utc)
        self.assertEqual(
            item.sessions[0].messages[0].id,
            "q1/s1/occurrence-0/message-0",
        )

    def test_repeated_session_ids_keep_unique_source_ids(self) -> None:
        item = parse_item(
            {
                "question_id": "q2",
                "question_type": "multi-session",
                "question": "What?",
                "answer": "A",
                "question_date": "2025-01-03",
                "haystack_session_ids": ["same", "same"],
                "haystack_dates": ["2025-01-01", "2025-01-02"],
                "haystack_sessions": [
                    [["user", "First occurrence"]],
                    [["user", "Second occurrence"]],
                ],
                "answer_session_ids": ["same"],
            }
        )
        first_id = item.sessions[0].messages[0].id
        second_id = item.sessions[1].messages[0].id
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("q2/same/occurrence-0/"))
        self.assertTrue(second_id.startswith("q2/same/occurrence-1/"))

    def test_load_list_and_reject_mismatched_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps([
                {
                    "question_id": "q1",
                    "question_type": "multi-session",
                    "question": "Q",
                    "answer": "A",
                    "question_date": "2025-01-03T00:00:00Z",
                    "haystack_session_ids": ["s1"],
                    "haystack_dates": ["2025-01-02T00:00:00Z"],
                    "haystack_sessions": [[["user", "fact"]]],
                    "answer_session_ids": [],
                }
            ]), encoding="utf-8")
            self.assertEqual(len(load_items(path)), 1)

        with self.assertRaises(ValueError):
            parse_item({
                "question_id": "bad",
                "question_type": "multi-session",
                "question": "Q",
                "question_date": "2025-01-03T00:00:00Z",
                "haystack_session_ids": ["s1"],
                "haystack_dates": [],
                "haystack_sessions": [],
            })


if __name__ == "__main__":
    unittest.main()
