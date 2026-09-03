from __future__ import annotations

import unittest

from nycti.chat.answer_delivery import normalize_discord_answer


class AnswerDeliveryTests(unittest.TestCase):
    def test_unwraps_answer_and_caveat_envelope(self) -> None:
        answer = normalize_discord_answer(
            '```json\n{"answer":"NVDA is green while the broader basket is red.",'
            '"caveat":"The exact catalyst is unverified."}\n```',
            request_text="Why is NVDA green then?",
        )

        self.assertEqual(
            "NVDA is green while the broader basket is red.\n\n"
            "The exact catalyst is unverified.",
            answer,
        )

    def test_unwraps_market_read_with_compact_supporting_data(self) -> None:
        answer = normalize_discord_answer(
            '{"as_of":"10:47 AM ET","semi_indexes":{"SOXX":"$493.63 (-1.56%)",'
            '"SMH":"$544.09 (-1.16%)"},"read":"Semis are broadly red today."}',
            request_text="How are semis doing today?",
        )

        self.assertTrue(answer.startswith("Semis are broadly red today."))
        self.assertIn("As of 10:47 AM ET.", answer)
        self.assertIn("Semi indexes: SOXX $493.63 (-1.56%); SMH $544.09 (-1.16%)", answer)
        self.assertNotIn('{"', answer)

    def test_preserves_explicitly_requested_json(self) -> None:
        raw = '{"answer":"green","caveat":"volatile"}'

        self.assertEqual(
            raw,
            normalize_discord_answer(raw, request_text="Return this as JSON."),
        )

    def test_leaves_non_envelope_json_unchanged(self) -> None:
        raw = '{"SOXX":-1.56,"NVDA":0.4}'

        self.assertEqual(raw, normalize_discord_answer(raw, request_text="How are semis?"))


if __name__ == "__main__":
    unittest.main()
