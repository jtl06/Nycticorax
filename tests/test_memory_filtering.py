import unittest
from types import SimpleNamespace

from nycti.memory.filtering import (
    contains_sensitive_pattern,
    has_explicit_working_memory_directive,
    has_guild_lore_signal,
    has_memory_retraction_signal,
    lexical_similarity,
    should_skip_memory_extraction,
)
from nycti.memory.extractor import MemoryExtractor
from nycti.memory.lifecycle import MemoryKind, MemoryOperation
from nycti.memory.profile import (
    clean_profile_markdown,
    strip_noncaller_profile_lines,
)
from nycti.memory.visibility import MemoryVisibility
from nycti.llm.types import LLMResult, LLMUsage


class MemoryFilteringTests(unittest.TestCase):
    def test_sensitive_content_is_rejected(self) -> None:
        self.assertTrue(contains_sensitive_pattern("my password is swordfish123"))
        self.assertEqual(should_skip_memory_extraction("my API key is sk-1234567890abc")[1], "sensitive")

    def test_low_value_chatter_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("lol")
        self.assertTrue(skip)
        self.assertEqual(reason, "low_value")

    def test_preference_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I prefer crunchy tacos over soft tacos.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_project_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I'm working on a retro racing game after work.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_plain_first_person_facts_reach_memory_classification(self) -> None:
        for message in (
            "I live in Chicago",
            "I am vegetarian",
            "I have two children",
            "I code in Rust",
        ):
            with self.subTest(message=message):
                skip, reason = should_skip_memory_extraction(message)
                self.assertFalse(skip)
                self.assertEqual(reason, "candidate")

    def test_ordinary_addressed_question_does_not_call_memory_model(self) -> None:
        skip, reason = should_skip_memory_extraction("What is OpenAI's newest model?")
        self.assertTrue(skip)
        self.assertEqual(reason, "no_durable_signal")

    def test_generic_project_question_is_not_a_personal_memory_signal(self) -> None:
        skip, reason = should_skip_memory_extraction("Can you explain the project deadline?")
        self.assertTrue(skip)
        self.assertEqual(reason, "no_durable_signal")

    def test_explicit_memory_update_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("Remember that I no longer work at Acme.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_working_memory_requires_explicit_temporary_scope(self) -> None:
        self.assertTrue(
            has_explicit_working_memory_directive(
                "Remember that I am using Zed for the next 7 days."
            )
        )
        self.assertFalse(has_explicit_working_memory_directive("Remember that I use Zed."))

    def test_retraction_signal_is_local_and_explicit(self) -> None:
        self.assertTrue(has_memory_retraction_signal("I no longer work at Acme."))
        self.assertFalse(has_memory_retraction_signal("Lucis no longer works at Acme."))

    def test_guild_lore_requires_explicit_group_language(self) -> None:
        self.assertTrue(has_guild_lore_signal("We always call broken deploys a moon launch."))
        self.assertTrue(has_guild_lore_signal("That is a running joke in this server."))
        self.assertFalse(has_guild_lore_signal("Mat likes mechanical keyboards."))

    def test_goal_signal_is_not_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want to get a job at Optiver.")
        self.assertFalse(skip)
        self.assertEqual(reason, "candidate")

    def test_transient_phone_plan_shopping_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want a phone plan that gives me a good iPhone deal.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_transient_promo_hunting_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("I want a free Apple Watch promotion.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_link_request_memory_is_skipped(self) -> None:
        skip, reason = should_skip_memory_extraction("Please give me official Cartier product page links for the Tank and Santos.")
        self.assertTrue(skip)
        self.assertEqual(reason, "transient")

    def test_lexical_similarity_prefers_overlap(self) -> None:
        high = lexical_similarity(
            "What games do I like to play on Friday nights?",
            "Prefers co-op horror games on Friday nights.",
            ["games", "friday"],
        )
        low = lexical_similarity(
            "What games do I like to play on Friday nights?",
            "Owns a blue road bike for weekend rides.",
            ["bike"],
        )
        self.assertGreater(high, low)

    def test_clean_profile_markdown_normalizes_and_caps(self) -> None:
        cleaned = clean_profile_markdown("```markdown\n-  likes   direct answers\n- works on Nycti\n```")
        self.assertEqual(cleaned, "- likes direct answers\n- works on Nycti")
        self.assertLessEqual(len(clean_profile_markdown("x" * 1000)), 800)

    def test_strip_noncaller_profile_lines_removes_mention_markers(self) -> None:
        cleaned = strip_noncaller_profile_lines(
            "- Likes concise replies\n- GTS is @gts81 (user_id=456)\n- Works on Nycti"
        )
        self.assertEqual(cleaned, "- Likes concise replies\n- Works on Nycti")


class MemoryExtractorScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_group_convention_can_become_lore(self) -> None:
        client = _MemoryLLMClient(
            '{"should_store":true,"confidence":0.95,"category":"lore",'
            '"memory":"Calls broken deploys moon launches","tags":["deploy"],'
            '"visibility":"lore","contains_sensitive":false}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            client,
        )

        candidate, _ = await extractor.extract(
            current_message="We always call broken deploys a moon launch.",
            recent_context="GTS: the deploy failed again",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(MemoryVisibility.LORE, candidate.suggested_visibility)
        self.assertIn("current message is authored by the memory owner", client.system_prompt)

    async def test_personal_fact_cannot_be_auto_promoted_to_lore(self) -> None:
        client = _MemoryLLMClient(
            '{"should_store":true,"confidence":0.95,"category":"preference",'
            '"memory":"Prefers dark mode","tags":["theme"],'
            '"visibility":"lore","contains_sensitive":false}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            client,
        )

        candidate, _ = await extractor.extract(
            current_message="I prefer dark mode.",
            recent_context="Lucis: everyone should use it",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(MemoryVisibility.PRIVATE, candidate.suggested_visibility)

    async def test_string_false_is_not_treated_as_true(self) -> None:
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            _MemoryLLMClient(
                '{"should_store":"false","confidence":0.99,"category":"preference",'
                '"memory":"Should not store","tags":[],"visibility":"private",'
                '"contains_sensitive":"false"}'
            ),
        )

        candidate, _ = await extractor.extract(
            current_message="I prefer this long enough candidate message.",
            recent_context="",
        )

        self.assertIsNone(candidate)

    async def test_typed_fact_fields_are_normalized(self) -> None:
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            _MemoryLLMClient(
                '{"should_store":true,"confidence":0.95,"category":"preference",'
                '"memory":"Prefers Zed","tags":["editor"],"visibility":"private",'
                '"contains_sensitive":false,"memory_kind":"fact","operation":"upsert",'
                '"predicate":"Preferred Editor","value":"Zed",'
                '"related_entities":["Zed Editor","Zed Editor"]}'
            ),
        )

        candidate, _ = await extractor.extract(
            current_message="I prefer Zed for coding.",
            recent_context="Lucis: Helix is better",
        )

        assert candidate is not None
        self.assertEqual(MemoryKind.FACT, candidate.memory_kind)
        self.assertEqual(MemoryOperation.UPSERT, candidate.operation)
        self.assertEqual("preferred_editor", candidate.predicate)
        self.assertEqual("Zed", candidate.object_text)
        self.assertEqual(("zed_editor",), candidate.related_entities)

    async def test_retraction_requires_current_message_signal_and_empty_replacement(self) -> None:
        payload = (
            '{"should_store":true,"confidence":0.95,"category":"project",'
            '"memory":"","tags":["employer"],"visibility":"private",'
            '"contains_sensitive":false,"memory_kind":"fact","operation":"retract",'
            '"predicate":"employer","value":""}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            _MemoryLLMClient(payload),
        )

        candidate, _ = await extractor.extract(
            current_message="I no longer work at Acme.",
            recent_context="Lucis: still works at Acme",
        )
        assert candidate is not None
        self.assertEqual(MemoryOperation.RETRACT, candidate.operation)

        rejected, _ = await extractor.extract(
            current_message="I am discussing my ongoing Acme project.",
            recent_context="Lucis: I no longer work at Acme",
        )
        self.assertIsNone(rejected)

    async def test_working_kind_requires_explicit_temporary_request(self) -> None:
        payload = (
            '{"should_store":true,"confidence":0.95,"category":"plan",'
            '"memory":"Using Zed for a migration","tags":["editor"],'
            '"visibility":"private","contains_sensitive":false,"memory_kind":"working",'
            '"operation":"upsert","predicate":"migration_editor","value":"Zed",'
            '"ttl_days":7}'
        )
        extractor = MemoryExtractor(
            SimpleNamespace(openai_memory_model="memory-model", memory_confidence_threshold=0.78),
            _MemoryLLMClient(payload),
        )

        durable, _ = await extractor.extract(
            current_message="Remember that I use Zed for migrations.",
            recent_context="",
        )
        temporary, _ = await extractor.extract(
            current_message="Remember that I use Zed for the next 7 days.",
            recent_context="",
        )

        assert durable is not None and temporary is not None
        self.assertEqual(MemoryKind.FACT, durable.memory_kind)
        self.assertIsNone(durable.ttl_days)
        self.assertEqual(MemoryKind.WORKING, temporary.memory_kind)
        self.assertEqual(7, temporary.ttl_days)


class _MemoryLLMClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.system_prompt = ""

    def is_model_available(self, _model: str) -> bool:
        return True

    async def complete_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.system_prompt = kwargs["messages"][0]["content"]
        return LLMResult(
            text=self.text,
            usage=LLMUsage(
                feature="memory_extract",
                model="memory-model",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
