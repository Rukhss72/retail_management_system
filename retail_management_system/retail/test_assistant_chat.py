from unittest.mock import patch
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from retail.assistant_engine.entities import parse_date_window, parse_top_n, resolve_festival_name
from retail.assistant_engine.service import process_query
from retail.assistant_engine.handlers import handle_reorder_suggestions
from retail.models import Category, ChatMessage, ChatThread, FestiveCalendar, Product, PurchaseOrder, Supplier
from retail.models import DailyProductSales, ForecastRun, ForecastDaily, Sale, SaleItem


# Regression tests for assistant intent mapping, replies, date parsing, and chat persistence.
class AssistantChatRefactorTests(TestCase):
    # Intent-routing and phrasing tests.
    def test_best_sellers_phrasings_map_to_top_sellers(self):
        captured_queries = []

        def fake_top_sellers_handler(query, _entities):
            captured_queries.append(query)
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        with patch(
            "retail.assistant_engine.service.parse_with_ollama",
            return_value={"intent": "SalesSummary", "confidence": 0.9, "entities": {}},
        ), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_top_sellers", side_effect=fake_top_sellers_handler
        ):
            first = process_query("what are the best seller last week", {})
            second = process_query("what's last week's best sellers", {})

        self.assertEqual(first["intent"], "TopSellers")
        self.assertEqual(second["intent"], "TopSellers")
        self.assertEqual(captured_queries, ["what are the best seller last week", "what's last week's best sellers"])

    def test_revenue_and_quantity_sold_query_maps_to_sales_summary(self):
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_sales_summary",
            return_value={"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []},
        ):
            result = process_query("Revenue and quantity sold this week", {})

        self.assertEqual(result["intent"], "SalesSummary")

    # Answer formatting and reply-shaping tests.
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

    # Chat thread and saved-message behavior tests.
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

    # Sales, category, and section query behavior tests.
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

    def test_sales_summary_includes_total_and_per_product_breakdown(self):
        user = get_user_model().objects.create_user(username="cashier1", password="pass123")
        cat = Category.objects.create(category_name="Traditional Wear")
        abaya = Product.objects.create(
            sku="TRD-101",
            product_name="Abaya",
            category=cat,
            qty_in_stock=20,
            reorder_level=5,
            selling_price="100.00",
        )
        kurta = Product.objects.create(
            sku="TRD-102",
            product_name="Kurta",
            category=cat,
            qty_in_stock=20,
            reorder_level=5,
            selling_price="80.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-1001",
            cashier=user,
            subtotal="440.00",
            total="440.00",
            sale_date=timezone.localdate(),
        )
        SaleItem.objects.create(sale=sale, product=abaya, qty=2, unit_price="100.00", line_total="200.00")
        SaleItem.objects.create(sale=sale, product=kurta, qty=3, unit_price="80.00", line_total="240.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("Revenue and quantity sold this week", {})

        self.assertEqual(result["intent"], "SalesSummary")
        self.assertEqual(result["table"]["columns"], ["sku", "product", "quantity", "revenue"])
        self.assertEqual(result["table"]["rows"][0]["sku"], "TOTAL")
        self.assertEqual(result["table"]["rows"][0]["quantity"], 5)
        self.assertEqual(result["table"]["rows"][0]["revenue"], 440.0)
        row_skus = {row["sku"] for row in result["table"]["rows"][1:]}
        self.assertEqual(row_skus, {"TRD-101", "TRD-102"})

    def test_top_selling_categories_last_month_returns_category_rows(self):
        user = get_user_model().objects.create_user(username="cashier2", password="pass123")
        cat_a = Category.objects.create(category_name="Tops")
        cat_b = Category.objects.create(category_name="Accessories")
        blouse = Product.objects.create(
            sku="TOP-201",
            product_name="Blouse",
            category=cat_a,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="100.00",
        )
        hijab = Product.objects.create(
            sku="ACC-201",
            product_name="Hijab",
            category=cat_b,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="80.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-2001",
            cashier=user,
            subtotal="360.00",
            total="360.00",
            sale_date=date(2026, 2, 10),
        )
        SaleItem.objects.create(sale=sale, product=blouse, qty=2, unit_price="100.00", line_total="200.00")
        SaleItem.objects.create(sale=sale, product=hijab, qty=2, unit_price="80.00", line_total="160.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("top selling categories last month", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertEqual(result["table"]["columns"], ["category", "units", "revenue"])
        row_categories = {row["category"] for row in result["table"]["rows"]}
        self.assertEqual(row_categories, {"Tops", "Accessories"})

    def test_top_selling_sections_last_month_returns_section_rows(self):
        user = get_user_model().objects.create_user(username="cashier3", password="pass123")
        cat = Category.objects.create(category_name="Traditional Wear")
        men_product = Product.objects.create(
            sku="MEN-301",
            product_name="Kurta",
            category=cat,
            section="men",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="90.00",
        )
        women_product = Product.objects.create(
            sku="WOM-301",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="110.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-3001",
            cashier=user,
            subtotal="310.00",
            total="310.00",
            sale_date=date(2026, 2, 12),
        )
        SaleItem.objects.create(sale=sale, product=men_product, qty=1, unit_price="90.00", line_total="90.00")
        SaleItem.objects.create(sale=sale, product=women_product, qty=2, unit_price="110.00", line_total="220.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("top selling sections last month", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertEqual(result["table"]["columns"], ["section", "units", "revenue"])
        row_sections = {row["section"] for row in result["table"]["rows"]}
        self.assertEqual(row_sections, {"Men", "Women"})

    def test_top_10_best_selling_products_this_month_uses_top_sellers_intent(self):
        user = get_user_model().objects.create_user(username="cashier_top_sellers", password="pass123")
        cat = Category.objects.create(category_name="Traditional Wear")
        abaya = Product.objects.create(
            sku="TRD-501",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="120.00",
        )
        saree = Product.objects.create(
            sku="TRD-502",
            product_name="Saree",
            category=cat,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="140.00",
        )
        today = timezone.localdate()
        sale = Sale.objects.create(
            receipt_no="R-5001",
            cashier=user,
            subtotal="400.00",
            total="400.00",
            sale_date=today,
        )
        SaleItem.objects.create(sale=sale, product=abaya, qty=1, unit_price="120.00", line_total="120.00")
        SaleItem.objects.create(sale=sale, product=saree, qty=2, unit_price="140.00", line_total="280.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("Top 10 best-selling products this month", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertEqual(result["table"]["columns"], ["sku", "product", "units", "revenue"])
        self.assertEqual(result["table"]["rows"][0]["sku"], "TRD-502")

    def test_top_10_best_selling_products_last_month_uses_top_sellers_intent(self):
        user = get_user_model().objects.create_user(username="cashier_last_month", password="pass123")
        cat = Category.objects.create(category_name="Outerwear")
        blazer = Product.objects.create(
            sku="OUT-601",
            product_name="Blazer",
            category=cat,
            section="unisex",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="200.00",
        )
        jacket = Product.objects.create(
            sku="OUT-602",
            product_name="Jacket",
            category=cat,
            section="unisex",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="180.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-6001",
            cashier=user,
            subtotal="740.00",
            total="740.00",
            sale_date=date(2026, 3, 15),
        )
        SaleItem.objects.create(sale=sale, product=blazer, qty=2, unit_price="200.00", line_total="400.00")
        SaleItem.objects.create(sale=sale, product=jacket, qty=2, unit_price="170.00", line_total="340.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("Top 10 best-selling products last month", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertEqual(result["table"]["columns"], ["sku", "product", "units", "revenue"])
        self.assertLessEqual(len(result["table"]["rows"]), 10)

    def test_show_trend_for_category_returns_category_trend_without_confirmation(self):
        user = get_user_model().objects.create_user(username="cashier4", password="pass123")
        cat = Category.objects.create(category_name="Accessories")
        p1 = Product.objects.create(
            sku="ACC-401",
            product_name="Hijab",
            category=cat,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="50.00",
        )
        p2 = Product.objects.create(
            sku="ACC-402",
            product_name="Jewelry",
            category=cat,
            section="women",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="40.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-4001",
            cashier=user,
            subtotal="180.00",
            total="180.00",
            sale_date=timezone.localdate(),
        )
        SaleItem.objects.create(sale=sale, product=p1, qty=2, unit_price="50.00", line_total="100.00")
        SaleItem.objects.create(sale=sale, product=p2, qty=2, unit_price="40.00", line_total="80.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("show trend for accessories", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("accessories trend", result["summary"].lower())
        self.assertEqual(result["table"]["columns"], ["date", "units", "revenue"])

    def test_show_trend_for_section_returns_section_trend_without_confirmation(self):
        user = get_user_model().objects.create_user(username="cashier5", password="pass123")
        cat = Category.objects.create(category_name="Traditional Wear")
        p1 = Product.objects.create(
            sku="SEC-501",
            product_name="Kurta",
            category=cat,
            section="men",
            qty_in_stock=20,
            reorder_level=5,
            selling_price="60.00",
        )
        sale = Sale.objects.create(
            receipt_no="R-5001",
            cashier=user,
            subtotal="120.00",
            total="120.00",
            sale_date=timezone.localdate(),
        )
        SaleItem.objects.create(sale=sale, product=p1, qty=2, unit_price="60.00", line_total="120.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("show trend for men", {})

        self.assertEqual(result["intent"], "TopSellers")
        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("men section trend", result["summary"].lower())
        self.assertEqual(result["table"]["columns"], ["date", "units", "revenue"])

    # Forecast query behavior tests.
    def test_forecast_best_sellers_uses_run_period_when_today_outside_window(self):
        cat = Category.objects.create(category_name="ForecastCat")
        p1 = Product.objects.create(
            sku="FC-001",
            product_name="Forecast One",
            category=cat,
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FC-002",
            product_name="Forecast Two",
            category=cat,
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 7),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=date(2026, 3, 1), predicted_qty="12.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=date(2026, 3, 1), predicted_qty="8.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("best sellers next week", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(len(result["table"]["rows"]), 2)
        self.assertIn("2026-03-01", result["summary"])
        self.assertIn("2026-03-07", result["summary"])

    def test_forecast_best_sellers_rounds_predicted_qty_for_display(self):
        cat = Category.objects.create(category_name="ForecastRounded")
        p1 = Product.objects.create(
            sku="FR-001",
            product_name="Rounded One",
            category=cat,
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=6),
        )
        ForecastDaily.objects.create(
            run=run,
            product=p1,
            forecast_date=timezone.localdate(),
            predicted_qty="9.62",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("best sellers next week", {})

        self.assertEqual(result["table"]["rows"][0]["predicted_qty"], 10)

    def test_forecast_best_sellers_respects_top_n_limit(self):
        cat = Category.objects.create(category_name="ForecastLimit")
        today = timezone.localdate()
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=today,
            period_end=today + timedelta(days=6),
        )
        for idx, qty in enumerate([10, 9, 8, 7, 6, 5], start=1):
            product = Product.objects.create(
                sku=f"FLT-{idx:03d}",
                product_name=f"Forecast Item {idx}",
                category=cat,
                qty_in_stock=10,
                reorder_level=2,
                selling_price="10.00",
            )
            ForecastDaily.objects.create(run=run, product=product, forecast_date=today, predicted_qty=str(qty))

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("predict the top 5 best sellers for next week", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(len(result["table"]["rows"]), 5)
        self.assertEqual([row["predicted_qty"] for row in result["table"]["rows"]], [10, 9, 8, 7, 6])

    def test_product_forecast_demand_returns_total_row_and_daily_chart(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        p1 = Product.objects.create(
            sku="FRD-001",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=13),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="2.00")
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate() + timedelta(days=1), predicted_qty="3.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("what's the forecast demand for abaya for the next 14 days", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(result["table"]["columns"], ["product", "predicted_qty", "days"])
        self.assertEqual(result["table"]["rows"][0]["product"], "Abaya")
        self.assertEqual(result["table"]["rows"][0]["predicted_qty"], 5)
        self.assertEqual(result["table"]["rows"][0]["days"], 2)
        self.assertIn("5 units total", result["summary"])
        self.assertEqual(result["chart"]["type"], "line")
        self.assertEqual(result["chart"]["datasets"][0]["data"], [2, 3])

    def test_category_forecast_demand_returns_specific_category_total(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        other_cat = Category.objects.create(category_name="Accessories")
        p1 = Product.objects.create(
            sku="FCD-001",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FCD-002",
            product_name="Hijab",
            category=other_cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=13),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="4.00")
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate() + timedelta(days=1), predicted_qty="5.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=timezone.localdate(), predicted_qty="9.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("what's the forecast demand for traditional wear category for the next 14 days", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(result["table"]["columns"], ["category", "predicted_qty", "days"])
        self.assertEqual(result["table"]["rows"][0]["category"], "Traditional Wear")
        self.assertEqual(result["table"]["rows"][0]["predicted_qty"], 9)
        self.assertEqual(result["chart"]["datasets"][0]["data"], [4, 5])

    def test_section_forecast_demand_returns_specific_section_total(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        p1 = Product.objects.create(
            sku="FSD-001",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FSD-002",
            product_name="Kurta",
            category=cat,
            section="men",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=13),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="6.00")
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate() + timedelta(days=1), predicted_qty="2.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=timezone.localdate(), predicted_qty="7.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("what's the forecast demand for women section for the next 14 days", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(result["table"]["columns"], ["section", "predicted_qty", "days"])
        self.assertEqual(result["table"]["rows"][0]["section"], "Women")
        self.assertEqual(result["table"]["rows"][0]["predicted_qty"], 8)
        self.assertEqual(result["chart"]["datasets"][0]["data"], [6, 2])

    def test_resolve_festival_name_ignores_question_phrase_after_during(self):
        self.assertIsNone(resolve_festival_name("during which festival does lehenga sell best?"))

    def test_during_which_festival_wording_matches_best_festival_query(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        lehenga = Product.objects.create(
            sku="FES-001",
            product_name="Lehenga",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 10, 20),
            product=lehenga,
            category=cat,
            qty_sold=25,
            sales_amt="50000.00",
            is_festive=True,
            festival_name="Diwali",
        )
        DailyProductSales.objects.create(
            date=date(2026, 12, 20),
            product=lehenga,
            category=cat,
            qty_sold=10,
            sales_amt="18000.00",
            is_festive=True,
            festival_name="Christmas",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("during which festival does lehenga sell best?", {})

        self.assertEqual(result["intent"], "FestivalInsights")
        self.assertEqual(result["table"]["columns"], ["festival", "qty", "sales"])
        self.assertEqual(result["table"]["rows"][0]["festival"], "Diwali")
        self.assertIn("lehenga sells best during diwali", result["summary"].lower())

    def test_product_sales_during_named_festival_returns_product_total_not_stock(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        lehenga = Product.objects.create(
            sku="FES-010",
            product_name="Lehenga",
            category=cat,
            section="women",
            qty_in_stock=0,
            reorder_level=2,
            selling_price="10.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 4, 1),
            product=lehenga,
            category=cat,
            qty_sold=12,
            sales_amt="24000.00",
            is_festive=True,
            festival_name="Ugadi",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many lehenga did we sell during ugadi", {})

        self.assertEqual(result["intent"], "FestivalInsights")
        self.assertEqual(result["table"]["columns"], ["product", "festival", "qty", "sales"])
        self.assertEqual(result["table"]["rows"][0]["product"], "Lehenga")
        self.assertEqual(result["table"]["rows"][0]["festival"], "ugadi")
        self.assertEqual(result["table"]["rows"][0]["qty"], 12)
        self.assertIn("sold 12 units during ugadi", result["summary"].lower())

    def test_category_seasonality_returns_best_season_for_category(self):
        dresses = Category.objects.create(category_name="Dresses")
        dress = Product.objects.create(
            sku="SEA-001",
            product_name="Maxi Dress",
            category=dresses,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 4, 10),
            product=dress,
            category=dresses,
            qty_sold=20,
            sales_amt="20000.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 7, 10),
            product=dress,
            category=dresses,
            qty_sold=5,
            sales_amt="5000.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("which season do dresses sell best in?", {})

        self.assertEqual(result["intent"], "Seasonality")
        self.assertIn("dresses sells best in autumn", result["summary"].lower())
        self.assertEqual(result["table"]["rows"][0]["season"], "Autumn")

    def test_seasonality_follow_up_replaces_product_with_category(self):
        abaya_cat = Category.objects.create(category_name="Traditional Wear")
        dresses_cat = Category.objects.create(category_name="Dresses")
        abaya = Product.objects.create(
            sku="SEA-002",
            product_name="Abaya",
            category=abaya_cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        dress = Product.objects.create(
            sku="SEA-003",
            product_name="Maxi Dress",
            category=dresses_cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 4, 10),
            product=abaya,
            category=abaya_cat,
            qty_sold=8,
            sales_amt="8000.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 1, 10),
            product=abaya,
            category=abaya_cat,
            qty_sold=3,
            sales_amt="3000.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 4, 12),
            product=dress,
            category=dresses_cat,
            qty_sold=20,
            sales_amt="20000.00",
        )
        DailyProductSales.objects.create(
            date=date(2026, 7, 12),
            product=dress,
            category=dresses_cat,
            qty_sold=4,
            sales_amt="4000.00",
        )

        session = {}
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            first = process_query("Which season does abaya sell best in?", session)
            second = process_query("and for dresses?", session)

        self.assertEqual(first["intent"], "Seasonality")
        self.assertEqual(second["intent"], "Seasonality")
        self.assertIn("abaya sells best in autumn", first["summary"].lower())
        self.assertIn("dresses sells best in autumn", second["summary"].lower())

    def test_top_selling_categories_next_month_returns_category_forecast_rows(self):
        cat_a = Category.objects.create(category_name="Tops")
        cat_b = Category.objects.create(category_name="Accessories")
        p1 = Product.objects.create(
            sku="FCAT-001",
            product_name="Blouse",
            category=cat_a,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FCAT-002",
            product_name="Jewelry",
            category=cat_b,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="9.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=timezone.localdate(), predicted_qty="7.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("top selling categories next month", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(result["table"]["columns"], ["category", "predicted_qty"])
        row_categories = {row["category"] for row in result["table"]["rows"]}
        self.assertEqual(row_categories, {"Tops", "Accessories"})

    def test_top_selling_sections_next_month_returns_section_forecast_rows(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        p1 = Product.objects.create(
            sku="FSEC-001",
            product_name="Kurta",
            category=cat,
            section="men",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FSEC-002",
            product_name="Abaya",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="6.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=timezone.localdate(), predicted_qty="8.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("top selling sections next month", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertEqual(result["table"]["columns"], ["section", "predicted_qty"])
        row_sections = {row["section"] for row in result["table"]["rows"]}
        self.assertEqual(row_sections, {"Men", "Women"})

    def test_forecast_best_sellers_uses_next_festive_window_instead_of_default_week(self):
        cat = Category.objects.create(category_name="Accessories")
        p1 = Product.objects.create(
            sku="FEST-001",
            product_name="Handbag",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        p2 = Product.objects.create(
            sku="FEST-002",
            product_name="Jewelry",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        today = timezone.localdate()
        FestiveCalendar.objects.create(
            festival_name="Eid",
            start_date=today + timedelta(days=10),
            end_date=today + timedelta(days=12),
            festival_date=today + timedelta(days=12),
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=today,
            period_end=today + timedelta(days=29),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=today + timedelta(days=1), predicted_qty="50.00")
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=today + timedelta(days=10), predicted_qty="3.00")
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=today + timedelta(days=11), predicted_qty="4.00")
        ForecastDaily.objects.create(run=run, product=p2, forecast_date=today + timedelta(days=12), predicted_qty="10.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("predict the best sellers for next festive period", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertIn("during eid", result["summary"].lower())
        self.assertIn(str(today + timedelta(days=10)), result["summary"])
        self.assertIn(str(today + timedelta(days=12)), result["summary"])
        self.assertEqual(len(result["table"]["rows"]), 2)
        self.assertEqual(result["table"]["rows"][0]["product"], "Jewelry")
        self.assertEqual(result["table"]["rows"][0]["predicted_qty"], 10)
        self.assertEqual(result["table"]["rows"][1]["product"], "Handbag")
        self.assertEqual(result["table"]["rows"][1]["predicted_qty"], 7)

    def test_forecast_best_sellers_reports_missing_festival_calendar_when_requested(self):
        cat = Category.objects.create(category_name="Accessories")
        p1 = Product.objects.create(
            sku="FEST-003",
            product_name="Sandals",
            category=cat,
            section="women",
            qty_in_stock=10,
            reorder_level=2,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="model_v1",
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=29),
        )
        ForecastDaily.objects.create(run=run, product=p1, forecast_date=timezone.localdate(), predicted_qty="5.00")

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("predict the best sellers for next festive period", {})

        self.assertEqual(result["intent"], "ForecastBestSellers")
        self.assertIn("no festival calendar window was found", result["summary"].lower())

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

    def test_reorder_suggestions_exclude_items_with_enough_stock_for_horizon(self):
        cat = Category.objects.create(category_name="Accessories")
        product = Product.objects.create(
            sku="WOM-ACC-00012",
            product_name="Jewelry",
            category=cat,
            qty_in_stock=35,
            reorder_level=35,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="test-model",
            version="v1",
            horizon_days=7,
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=6),
        )
        ForecastDaily.objects.create(
            run=run,
            product=product,
            forecast_date=timezone.localdate(),
            predicted_qty="7.54",
        )

        entities = {"start": timezone.localdate(), "end": timezone.localdate() + timedelta(days=6), "horizon_days": 7}
        result = handle_reorder_suggestions("what should i restock for next week?", entities)

        self.assertEqual(result["summary"], "0 products need reorder for next 7 days.")
        self.assertEqual(result["table"]["rows"], [])

    def test_reorder_suggestions_include_items_that_will_run_out_within_horizon(self):
        cat = Category.objects.create(category_name="Accessories")
        product = Product.objects.create(
            sku="WOM-ACC-00013",
            product_name="Handbag",
            category=cat,
            qty_in_stock=4,
            reorder_level=20,
            selling_price="10.00",
        )
        run = ForecastRun.objects.create(
            model_name="test-model",
            version="v1",
            horizon_days=7,
            period_start=timezone.localdate(),
            period_end=timezone.localdate() + timedelta(days=6),
        )
        ForecastDaily.objects.create(
            run=run,
            product=product,
            forecast_date=timezone.localdate(),
            predicted_qty="9.10",
        )

        entities = {"start": timezone.localdate(), "end": timezone.localdate() + timedelta(days=6), "horizon_days": 7}
        result = handle_reorder_suggestions("what should i restock for next week?", entities)

        self.assertEqual(len(result["table"]["rows"]), 1)
        self.assertEqual(result["table"]["rows"][0]["product"], "Handbag")
        self.assertEqual(result["table"]["rows"][0]["suggested_qty"], 26)

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

    # Date parsing and follow-up query memory tests.
    def test_this_week_uses_last_seven_days(self):
        start, end, _label, _horizon = parse_date_window("sales this week")
        self.assertEqual(end, timezone.localdate())
        self.assertEqual((end - start).days, 6)

    def test_last_week_uses_previous_seven_day_window(self):
        start, end, label, _horizon = parse_date_window("what's last week's best sellers")
        self.assertEqual(label, "last week")
        self.assertEqual(end, timezone.localdate() - timedelta(days=7))
        self.assertEqual((end - start).days, 6)

    def test_parse_top_n_extracts_numeric_limit(self):
        self.assertEqual(parse_top_n("predict the top 5 best sellers for next week"), 5)

    def test_last_x_days_uses_requested_window(self):
        start, end, label, _horizon = parse_date_window("stock movements last 10 days")
        self.assertEqual(label, "last 10 days")
        self.assertEqual(end, timezone.localdate())
        self.assertEqual((end - start).days, 9)

    def test_last_x_days_supports_values_other_than_thirty(self):
        start, end, label, _horizon = parse_date_window("stock movements last 25 days")
        self.assertEqual(label, "last 25 days")
        self.assertEqual(end, timezone.localdate())
        self.assertEqual((end - start).days, 24)

    def test_next_week_sets_forecast_horizon_to_seven_days(self):
        _start, _end, _label, horizon = parse_date_window("best sellers next week")
        self.assertEqual(horizon, 7)

    def test_next_month_sets_forecast_horizon_to_thirty_days(self):
        _start, _end, _label, horizon = parse_date_window("best sellers next month")
        self.assertEqual(horizon, 30)

    def test_single_explicit_date_supports_slashes(self):
        start, end, label, horizon = parse_date_window("what about on 2026/02/16")
        self.assertEqual(str(start), "2026-02-16")
        self.assertEqual(str(end), "2026-02-16")
        self.assertEqual(label, "2026-02-16")
        self.assertEqual(horizon, 1)

    def test_explicit_date_range_supports_slashes(self):
        start, end, label, horizon = parse_date_window("show me stock movements from 2026/03/04 to 2026/03/13")
        self.assertEqual(str(start), "2026-03-04")
        self.assertEqual(str(end), "2026-03-13")
        self.assertEqual(label, "2026-03-04 to 2026-03-13")
        self.assertEqual(horizon, 10)

    def test_explicit_date_range_supports_between_phrasing(self):
        start, end, label, horizon = parse_date_window("show me stock movements between 2026-03-04 and 2026-03-13")
        self.assertEqual(str(start), "2026-03-04")
        self.assertEqual(str(end), "2026-03-13")
        self.assertEqual(label, "2026-03-04 to 2026-03-13")
        self.assertEqual(horizon, 10)

    def test_follow_up_with_explicit_date_overrides_previous_range(self):
        captured = {}

        def fake_stock_movement_handler(_query, entities):
            captured["start"] = entities["start"]
            captured["end"] = entities["end"]
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        session = {"last_intent": "StockMovements"}
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_stock_movements", side_effect=fake_stock_movement_handler
        ):
            result = process_query("what about on 2026/02/16", session)

        self.assertEqual(result["intent"], "StockMovements")
        self.assertEqual(str(captured["start"]), "2026-02-16")
        self.assertEqual(str(captured["end"]), "2026-02-16")

    def test_follow_up_replaces_previous_single_date_in_stock_movements(self):
        captured_ranges = []

        def fake_stock_movement_handler(_query, entities):
            captured_ranges.append((str(entities["start"]), str(entities["end"])))
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        session = {}
        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_stock_movements", side_effect=fake_stock_movement_handler
        ):
            first = process_query("show me stock movements on 2026/03/07", session)
            second = process_query("what about on 2026-03-13", session)
            third = process_query("what about on 2026-03-04", session)

        self.assertEqual(first["intent"], "StockMovements")
        self.assertEqual(second["intent"], "StockMovements")
        self.assertEqual(third["intent"], "StockMovements")
        self.assertEqual(
            captured_ranges,
            [
                ("2026-03-07", "2026-03-07"),
                ("2026-03-13", "2026-03-13"),
                ("2026-03-04", "2026-03-04"),
            ],
        )

    def test_follow_up_keeps_raw_user_query_separate_from_resolved_query(self):
        session = {}

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_stock_movements",
            return_value={"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []},
        ):
            process_query("show me stock movements on 2026/03/07", session)
            process_query("what about on 2026-03-13", session)

        self.assertEqual(session["last_user_query"], "what about on 2026-03-13")
        self.assertIn("stock movements", session["last_resolved_query"].lower())

    # Stock counting and stock-status interpretation tests.
    def test_generic_stock_movements_query_does_not_keep_accidental_product_filter(self):
        fake_entities = {
            "start": timezone.localdate() - timedelta(days=29),
            "end": timezone.localdate(),
            "compare_start": None,
            "compare_end": None,
            "date_label": "last 30 days",
            "horizon_days": 7,
            "threshold": None,
            "category": None,
            "product": object(),
            "product_candidates": [{"id": 1, "name": "Abaya", "sku": "SKU-1"}],
            "product_confidence": 0.8,
            "festival_name": None,
        }
        captured = {}

        def fake_stock_movement_handler(_query, entities):
            captured["product"] = entities.get("product")
            return {"summary": "ok", "table": {"columns": [], "rows": []}, "chart": None, "source": []}

        with patch("retail.assistant_engine.service.build_entities", return_value=fake_entities), patch(
            "retail.assistant_engine.service.parse_with_ollama", return_value=None
        ), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ), patch(
            "retail.assistant_engine.service.handle_stock_movements", side_effect=fake_stock_movement_handler
        ):
            result = process_query("Stock movements last 30 days", {})

        self.assertEqual(result["intent"], "StockMovements")
        self.assertIsNone(captured["product"])

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

    def test_category_stock_count_uses_category_not_accidental_product_match(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        Product.objects.create(
            sku="TRD-001",
            product_name="Abaya",
            category=cat,
            qty_in_stock=24,
            reorder_level=5,
            selling_price="10.00",
        )
        Product.objects.create(
            sku="TRD-002",
            product_name="Kurta",
            category=cat,
            qty_in_stock=16,
            reorder_level=5,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many traditional wear do we have in stock?", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("traditional wear", result.get("summary", "").lower())
        self.assertIn("40 units", result.get("summary", "").lower())

    def test_tops_in_stock_uses_category_count(self):
        cat = Category.objects.create(category_name="Tops")
        Product.objects.create(
            sku="TOP-001",
            product_name="Blouse",
            category=cat,
            qty_in_stock=11,
            reorder_level=3,
            selling_price="10.00",
        )
        Product.objects.create(
            sku="TOP-002",
            product_name="Shirt",
            category=cat,
            qty_in_stock=9,
            reorder_level=3,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many tops do we have in stock?", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("tops", result.get("summary", "").lower())
        self.assertIn("20 units", result.get("summary", "").lower())

    def test_section_stock_count_uses_section_total(self):
        cat = Category.objects.create(category_name="Outerwear")
        Product.objects.create(
            sku="WM-001",
            product_name="Women Jacket",
            category=cat,
            section="women",
            qty_in_stock=13,
            reorder_level=3,
            selling_price="10.00",
        )
        Product.objects.create(
            sku="WM-002",
            product_name="Women Blazer",
            category=cat,
            section="women",
            qty_in_stock=7,
            reorder_level=3,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many women items do we have in stock?", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("women section", result.get("summary", "").lower())
        self.assertIn("20 units", result.get("summary", "").lower())

    def test_product_name_stock_count_returns_specific_product_units(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        Product.objects.create(
            sku="TRD-010",
            product_name="Abaya",
            category=cat,
            qty_in_stock=24,
            reorder_level=5,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many abaya do we have in stock?", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("24 units", result.get("summary", "").lower())
        self.assertIn("abaya", result.get("summary", "").lower())

    def test_sku_stock_count_returns_specific_product_units(self):
        cat = Category.objects.create(category_name="Traditional Wear")
        Product.objects.create(
            sku="UNI-TRD-00003",
            product_name="Abaya",
            category=cat,
            qty_in_stock=24,
            reorder_level=5,
            selling_price="10.00",
        )

        with patch("retail.assistant_engine.service.parse_with_ollama", return_value=None), patch(
            "retail.assistant_engine.service.generate_insight", return_value=None
        ):
            result = process_query("how many UNI-TRD-00003 do we have in stock?", {})

        self.assertFalse(result.get("needs_confirmation", False))
        self.assertIn("24 units", result.get("summary", "").lower())
        self.assertIn("uni-trd-00003", result.get("summary", "").lower())

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
