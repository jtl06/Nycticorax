from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from nycti.discord.reply_delivery import send_message_reply_chunks
from nycti.market_report_images import (
    COLOR_LIMIT_PERCENT, GREEN, RED, NEUTRAL, ROW_HEIGHT, TOP, WIDTH,
    change_color, extract_market_report_images, render_market_report_images,
)
from nycti.market_reports import MarketReport, QuoteRow
from nycti.market_receipts import report_receipt
from nycti.chat.tools.stock_workflows import parse_market_report
from nycti.message_context import MessageContextCollector


def sample_report():
    return MarketReport((
        QuoteRow("AAA", 85, "USD", -15, "regular", "vs prev close", reference_price=100),
        QuoteRow("BBB", 98, "USD", -2, "regular", "vs prev close", reference_price=100),
        QuoteRow("CCC", 118, "USD", 18, "after-hours", "vs regular close", reference_price=100),
        QuoteRow("DDD", unavailable="no usable quote"),
    ), "latest")


def receipts(report, display="image"):
    return "[" + report_receipt(report, display=display) + "]"


class MarketReportImageTests(unittest.TestCase):
    def test_continuous_scale_caps_color_not_numeric_values(self):
        self.assertEqual(15, COLOR_LIMIT_PERCENT)
        self.assertEqual(RED, change_color(-15))
        self.assertEqual(RED, change_color(-90))
        self.assertEqual(GREEN, change_color(15))
        self.assertEqual(GREEN, change_color(90))
        self.assertEqual(NEUTRAL, change_color(0))
        self.assertEqual(NEUTRAL, change_color(None))
        self.assertEqual(NEUTRAL, change_color(float("nan")))
        self.assertLess(change_color(-1)[0], change_color(-5)[0])
        self.assertLess(change_color(-5)[0], change_color(-15)[0])

    def test_render_is_deterministic_and_colored_with_missing_rows(self):
        report = sample_report()
        a = render_market_report_images(report)[0]
        b = render_market_report_images(report)[0]
        self.assertEqual(a.data, b.data)
        image = Image.open(BytesIO(a.data))
        self.assertEqual((WIDTH, TOP + 4 * ROW_HEIGHT + 64), image.size)
        self.assertEqual(RED, image.getpixel((352, TOP + 2)))
        self.assertEqual(change_color(-2), image.getpixel((352, TOP + ROW_HEIGHT + 2)))
        self.assertEqual(GREEN, image.getpixel((352, TOP + 2 * ROW_HEIGHT + 2)))
        self.assertEqual(NEUTRAL, image.getpixel((352, TOP + 3 * ROW_HEIGHT + 2)))
        drawn = []
        from PIL import ImageDraw
        original = ImageDraw.ImageDraw.text

        def record(draw, position, text, **kwargs):
            drawn.append(text)
            return original(draw, position, text, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_market_report_images(report)
        self.assertIn("+18.00%", drawn)  # Never clamp the displayed return to 15%.
        self.assertIn("prev close", drawn)
        self.assertIn("close", drawn)
        self.assertIn("AH", drawn)

    def test_40_rows_paginate_without_losing_scope(self):
        report = MarketReport(tuple(replace(sample_report().rows[0], symbol=f"T{i}") for i in range(40)), "regular")
        images = render_market_report_images(report)
        self.assertEqual(2, len(images))
        self.assertTrue(all(Image.open(BytesIO(image.data)).height == TOP + 20 * ROW_HEIGHT + 64 for image in images))

    def test_only_exact_authoritative_reports_become_images(self):
        report = sample_report()
        text = report.render() + "\n\nExtra analysis stays here."
        result = extract_market_report_images(text, receipts(report))
        self.assertEqual("Extra analysis stays here.", result.text)
        self.assertEqual(1, len(result.images))
        changed = text.replace("$85.00", "$86.00")
        untouched = extract_market_report_images(changed, receipts(report))
        self.assertEqual(changed, untouched.text)
        self.assertFalse(untouched.images)
        for raw in ("invalid", "{}", "[]", receipts(report, "text"),
                    receipts(report).replace('"price": 85', '"price": "85"')):
            with self.subTest(raw=raw):
                self.assertFalse(extract_market_report_images(text, raw).images)

    def test_display_contract_defaults_to_image_and_allows_text(self):
        self.assertEqual("image", parse_market_report('{"symbols":["AAA"]}').display)
        self.assertEqual("text", parse_market_report('{"display":"text"}').display)
        for value in (False, [], "html"):
            self.assertIsNone(parse_market_report(json.dumps({"display": value})))

    def test_receipt_avoids_vision_for_own_chart_not_other_images(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=9, display_name="Nycti"))
        collector = object.__new__(MessageContextCollector)
        collector.bot = bot
        collector.max_context_image_count = 3
        attachment = SimpleNamespace(url="https://example.invalid/chart.png", filename="nycti-watchlist-1-1.png", content_type="image/png")
        own = SimpleNamespace(id=100, author=bot.user, attachments=[attachment], embeds=[])
        current = SimpleNamespace(id=101, author=SimpleNamespace(id=3, display_name="User"), attachments=[], embeds=[])
        options = dict(message=current, history_messages=[own], reply_chain_messages=[],
                       linked_messages=[], anchor_context_messages=[])
        self.assertFalse(collector._collect_image_refs(**options, covered_report_ids={100}))
        self.assertTrue(collector._collect_image_refs(**options))  # No receipt: keep the image as evidence.
        own.author = current.author
        self.assertTrue(collector._collect_image_refs(**options, covered_report_ids={100}))


class MarketReportImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_reply_has_no_duplicate_price_text_and_resolves_progress(self):
        report = sample_report()
        sent = SimpleNamespace(id=10)
        message = SimpleNamespace(reply=AsyncMock(return_value=sent), channel=SimpleNamespace(send=AsyncMock()))
        progress_message = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        progress = SimpleNamespace(mark_resolved=Mock())
        result = await send_message_reply_chunks(SimpleNamespace(), message, report.render(),
            market_receipts_json=receipts(report), progress_message=progress_message, progress=progress)
        self.assertTrue(result.complete)
        kwargs = message.reply.await_args.kwargs
        self.assertEqual(1, len(kwargs["files"]))
        self.assertEqual("", message.reply.await_args.args[0])
        self.assertTrue(kwargs["files"][0].filename.startswith("nycti-watchlist-"))
        progress_message.delete.assert_awaited_once()
        progress_message.edit.assert_not_awaited()
        progress.mark_resolved.assert_called_once()

    async def test_render_failure_and_missing_attachment_permission_keep_text(self):
        report = sample_report()
        for allowed in (True, False):
            message = SimpleNamespace(reply=AsyncMock(), guild=SimpleNamespace(me=object()),
                channel=SimpleNamespace(send=AsyncMock(), permissions_for=lambda _: SimpleNamespace(attach_files=allowed)))
            with patch("nycti.discord.reply_delivery.extract_market_report_images", side_effect=ValueError("renderer unavailable")):
                result = await send_message_reply_chunks(SimpleNamespace(), message, report.render(),
                    market_receipts_json=receipts(report))
            self.assertTrue(result.complete)
            self.assertIn("Quotes:", message.reply.await_args.args[0])
            self.assertEqual([], message.reply.await_args.kwargs["files"])
