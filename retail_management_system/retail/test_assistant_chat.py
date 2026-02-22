from unittest.mock import patch
from datetime import date

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from retail.assistant_engine.entities import parse_date_window
from retail.assistant_engine.service import process_query
from retail.assistant_engine.handlers import handle_reorder_suggestions
from retail.models import Category, ChatMessage, ChatThread, Product, PurchaseOrder, Supplier


class AssistantChatRefactorTests(TestCase):
    def test_process_query_returns_answer_text(self):
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("hi", {})

        self.assertIn("answer_text", result)
        self.assertTrue(result["answer_text"].strip())

    def test_simple_intent_answer_text_hard_capped_to_two_lines(self):
        long_reply = (
            "Stock is healthy today. Stock is healthy today. "
            "You have enough inventory for current demand. "
            "Reorder only if weekend demand spikes. "
            "Track low stock daily."
        )
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=long_reply
        ):
            result = process_query("hi", {})

        lines = [line for line in result["answer_text"].splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2)

    def test_complex_intent_hybrid_allows_longer_but_concise_output(self):
        long_compare_reply = (
            "January units were higher on top items. "
            "Revenue also increased in the same period. "
            "A few SKUs declined and need review. "
            "Prioritize replenishment for winners. "
            "Check pricing on declining SKUs. "
            "Run another comparison by category."
        )
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=long_compare_reply
        ):
            result = process_query("compare sales between december 2025 and january 2025", {})

        lines = [line for line in result["answer_text"].splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 3)
        self.assertLessEqual(len(lines), 4)

    def test_answer_text_deduplicates_repeated_sentences(self):
        repeated = "Stock is stable. Stock is stable. Stock is stable. Reorder is not needed today."
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=repeated
        ):
            result = process_query("stock status", {})

        self.assertEqual(result["answer_text"].count("Stock is stable."), 1)

    def test_assistant_thread_message_persists_answer_text_as_content(self):
        user = get_user_model().objects.create_user(username="staff1", password="pass123")
        thread = ChatThread.objects.create(user=user, title="New Chat", context_json={})

        client = Client()
        client.force_login(user)

        payload = {
            "intent": "Unknown",
            "parser_used": "rules",
            "parser_confidence": 1.0,
            "summary": "Long summary that should not be saved as content.",
            "answer_text": "Short canonical answer.",
            "table": {"columns": [], "rows": []},
            "chart": None,
            "source": [],
        }

        with patch("retail.views.process_query", return_value=payload):
            res = client.post(
                reverse("assistant_thread_message", args=[thread.id]),
                data={"query": "hello"},
                content_type="application/json",
            )

        self.assertEqual(res.status_code, 200)
        assistant_msg = ChatMessage.objects.filter(thread=thread, role=ChatMessage.Role.ASSISTANT).latest("created_at")
        self.assertEqual(assistant_msg.content, "Short canonical answer.")

    def test_greeting_does_not_trigger_stock_lookup_table(self):
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("hi", {})

        self.assertEqual(result["intent"], "Unknown")
        self.assertEqual(result["table"]["rows"], [])

    def test_compare_jan_2024_vs_jan_2025_uses_distinct_ranges(self):
        captured = {}

        def fake_compare_handler(_query, entities):
            captured["start"] = entities["start"]
            captured["end"] = entities["end"]
            captured["compare_start"] = entities["compare_start"]
            captured["compare_end"] = entities["compare_end"]
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch("retail.assistant_engine.service.handle_compare_sales", side_effect=fake_compare_handler):
            process_query("compare sales between jan2024 and jan2025", {})

        self.assertEqual(str(captured["start"]), "2024-01-01")
        self.assertEqual(str(captured["end"]), "2024-01-31")
        self.assertEqual(str(captured["compare_start"]), "2025-01-01")
        self.assertEqual(str(captured["compare_end"]), "2025-01-31")

    def test_answer_text_prefers_summary_over_ai_insight(self):
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight",
            return_value="AI says 24 orders and 1080 revenue.",
        ), patch(
            "retail.assistant_engine.service.handle_sales_summary",
            return_value={
                "summary": "Sales from 2026-02-15 to 2026-02-21: revenue 24000 and quantity 16.",
                "table": {"columns": ["range", "revenue", "quantity", "orders"], "rows": []},
                "chart": None,
                "source": ["Sale", "SaleItem"],
            },
        ):
            result = process_query("how many sales happened this week", {})

        self.assertIn("revenue 24000", result["answer_text"])
        self.assertNotIn("1080", result["answer_text"])

    def test_reorder_history_query_checks_purchase_orders(self):
        supplier = Supplier.objects.create(supplier_name="supplier-x")
        PurchaseOrder.objects.create(
            supplier=supplier,
            status=PurchaseOrder.STATUS_PENDING,
            order_date="2026-02-16",
            total_amount="500.00",
        )
        entities = {"start": date(2026, 2, 15), "end": date(2026, 2, 21), "horizon_days": 7}
        result = handle_reorder_suggestions("did i reorder this week?", entities)
        self.assertIn("purchase orders were created", result["summary"].lower())

    def test_compare_december_with_second_year_only_maps_to_two_periods(self):
        captured = {}

        def fake_compare_handler(_query, entities):
            captured["start"] = entities["start"]
            captured["end"] = entities["end"]
            captured["compare_start"] = entities["compare_start"]
            captured["compare_end"] = entities["compare_end"]
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch("retail.assistant_engine.service.handle_compare_sales", side_effect=fake_compare_handler):
            process_query("compare december 2024 and 2026", {})

        self.assertEqual(str(captured["start"]), "2024-12-01")
        self.assertEqual(str(captured["end"]), "2024-12-31")
        self.assertEqual(str(captured["compare_start"]), "2026-12-01")
        self.assertEqual(str(captured["compare_end"]), "2026-12-31")

    def test_this_week_uses_last_seven_days(self):
        start, end, _label, _horizon = parse_date_window("sales this week")
        self.assertEqual(end, timezone.localdate())
        self.assertEqual((end - start).days, 6)

    def test_sets_in_stock_is_generic_count_not_product_clarification(self):
        cat = Category.objects.create(category_name="TestCat")
        Product.objects.create(
            sku="SET-001",
            product_name="Cord Set",
            category=cat,
            qty_in_stock=9,
            reorder_level=2,
            selling_price="10.00",
        )
        Product.objects.create(
            sku="SET-002",
            product_name="Bikini Set",
            category=cat,
            qty_in_stock=7,
            reorder_level=2,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many sets do we have in stock", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("set units", result.get("summary", "").lower())

    def test_low_or_out_stock_uses_reorder_level_logic(self):
        cat = Category.objects.create(category_name="StockCat")
        Product.objects.create(sku="L-1", product_name="Item A", category=cat, qty_in_stock=2, reorder_level=3, selling_price="10.00")
        Product.objects.create(sku="L-2", product_name="Item B", category=cat, qty_in_stock=0, reorder_level=3, selling_price="10.00")
        Product.objects.create(sku="L-3", product_name="Item C", category=cat, qty_in_stock=1, reorder_level=4, selling_price="10.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many items are low in stock or are out of stock?", {})

        self.assertIn("2 low-stock and 1 out-of-stock", result.get("summary", "").lower())
