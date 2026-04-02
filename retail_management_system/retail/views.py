#from django.contrib.auth.decorators import login_required
#from django.db.models import F, Value, IntegerField, Case, When, Sum, Q
from django.db.models import Q, Sum, F, IntegerField, DecimalField, ExpressionWrapper, Count
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, get_user_model
from django.core.mail import send_mail
from django.conf import settings

import secrets
import hashlib
from django.db import models

from django.db import transaction
#from urllib3 import request
from .models import Product, StockMovement, Category, SKUCounter, Supplier, ReorderRequest, ReorderRequestItem, PurchaseOrder, PurchaseOrderItem, ProductSupplier, DailyProductSales, TrustedDevice, ForecastRun, ForecastDaily, FestiveCalendar
from .forms import RoleAuthenticationForm, TwoFactorForm, StockInForm, StockOutForm, AdjustmentForm, HistoryFilterForm, ProductCreateForm, SupplierForm, PurchaseOrderCreateForm

from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.urls import reverse_lazy

from django.contrib.auth.decorators import user_passes_test
from django.views.decorators.http import require_POST

from django.core.exceptions import ValidationError
from .services import apply_stock_movement

from django.core.paginator import Paginator
from .models import Product, StockMovement

from datetime import datetime, time, timedelta, date
from calendar import monthrange

from .permissions import is_admin, is_staff_or_admin, is_staff_user
from .services import get_reorder_queryset, compute_reorder_row


#sales import
import json
from django.contrib import messages
from .models import Sale, SaleItem, ChatThread, ChatMessage
from .services import create_sale

from decimal import Decimal, InvalidOperation
from .assistant_engine.service import process_query

import re

# Authentication and session helpers.

# views.py
TRUSTED_DEVICE_COOKIE = "trusted_device"
TRUSTED_DEVICE_DAYS = 30


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_client_ip(request):
    # Reads the client IP in a simple single-proxy setup.
    return request.META.get("REMOTE_ADDR")


def _get_valid_trusted_device(request, user):
    raw = request.COOKIES.get(TRUSTED_DEVICE_COOKIE)
    if not raw or ":" not in raw:
        return None
    device_id, token = raw.split(":", 1)
    token_hash = _token_hash(token)
    now = timezone.now()
    try:
        td = TrustedDevice.objects.get(
            user=user,
            device_id=device_id,
            token_hash=token_hash,
        )
    except TrustedDevice.DoesNotExist:
        return None
    if td.expires_at <= now:
        return None
    return td


def _set_trusted_device_cookie(response, device_id, token, days=TRUSTED_DEVICE_DAYS):
    max_age = 60 * 60 * 24 * days
    response.set_cookie(
        TRUSTED_DEVICE_COOKIE,
        f"{device_id}:{token}",
        max_age=max_age,
        secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
        httponly=True,
        samesite=getattr(settings, "SESSION_COOKIE_SAMESITE", "Lax"),
    )


def _create_trusted_device(response, request, user):
    device_id = secrets.token_urlsafe(16)
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    TrustedDevice.objects.create(
        user=user,
        device_id=device_id,
        token_hash=_token_hash(token),
        last_used_at=now,
        expires_at=now + timedelta(days=TRUSTED_DEVICE_DAYS),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        ip_at_creation=_get_client_ip(request),
    )
    _set_trusted_device_cookie(response, device_id, token)


def _rotate_trusted_device(response, request, td):
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    td.token_hash = _token_hash(token)
    td.last_used_at = now
    td.expires_at = now + timedelta(days=TRUSTED_DEVICE_DAYS)
    td.save(update_fields=["token_hash", "last_used_at", "expires_at"])
    _set_trusted_device_cookie(response, td.device_id, token)


def _next_po_number():
    year = timezone.localdate().year
    prefix = f"PO-{year}-"
    last = (
        PurchaseOrder.objects
        .filter(po_number__startswith=prefix)
        .values_list("po_number", flat=True)
        .order_by("-po_number")
        .first()
    )
    if last:
        m = re.search(rf"{re.escape(prefix)}(\d+)$", last)
        if m:
            seq = int(m.group(1)) + 1
        else:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


class AdminLoginView(LoginView):
    template_name = "registration/admin_login.html"
    authentication_form = RoleAuthenticationForm
    redirect_authenticated_user = True  # Avoids redirect loops for logged-in users.

    def form_valid(self, form):
        user = form.get_user()
        if not (is_admin(user) or is_staff_user(user)):
            return redirect("admin_login")

        if not user.email:
            form.add_error(None, "No email on this account. Contact admin.")
            return self.form_invalid(form)

        trusted = _get_valid_trusted_device(self.request, user)
        if trusted:
            login(self.request, user)
            self.request.session.set_expiry(60 * 60 * 24 * TRUSTED_DEVICE_DAYS)
            response = redirect(self.get_success_url())
            _rotate_trusted_device(response, self.request, trusted)
            return response

        code = f"{secrets.randbelow(1000000):06d}"
        self.request.session["pending_2fa_user_id"] = user.id
        self.request.session["pending_2fa_code"] = code
        self.request.session["pending_2fa_exp"] = int(timezone.now().timestamp()) + 600
        self.request.session["pending_2fa_remember"] = bool(form.cleaned_data.get("remember_me"))

        try:
            send_mail(
                subject="Your login verification code",
                message=f"Your verification code is {code}. It expires in 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception:
            form.add_error(None, "Email delivery failed. Check Gmail SMTP settings and restart the server.")
            return self.form_invalid(form)

        return redirect("two_factor_verify")

    def get_success_url(self):
        return reverse_lazy("home")  # Send successful logins to the home page.
    

# Permission wrappers used across the page views below.
admin_required = user_passes_test(is_admin, login_url="/admin-login/")
staff_required = user_passes_test(is_staff_or_admin, login_url="/admin-login/")

# SKU generation helpers.
SECTION_CODE = {
    "men": "MEN",
    "women": "WOM",
    "kids": "KID",
    "unisex": "UNI",
}

CATEGORY_CODE = {
    "Tops": "TOP",
    "Bottoms": "BOT",
    "Dresses": "DRS",
    "Outerwear": "OUT",
    "Traditional Wear": "TRD",
    "Footwear": "FWT",
    "Accessories": "ACC",
    "Sets": "SET",
    "Sportswear": "SPT",
    "Swimwear": "SWM",
}

def generate_sku_for(section_value: str, category_obj: Category) -> str:
    sec = (section_value or "").strip().lower()
    sec_code = SECTION_CODE.get(sec)
    if not sec_code:
        raise ValueError(f"Invalid section: {section_value}")

    cat_code = CATEGORY_CODE.get(category_obj.category_name)
    if not cat_code:
        raise ValueError(f"No category code mapping for: {category_obj.category_name}")

    with transaction.atomic():
        counter, _ = SKUCounter.objects.select_for_update().get_or_create(
            section=sec,
            category=category_obj,
            defaults={"last_seq": 0},
        )
        counter.last_seq += 1
        counter.save(update_fields=["last_seq"])

        seq = str(counter.last_seq).zfill(5)
        sku = f"{sec_code}-{cat_code}-{seq}"

        # Extra safety check in case the generated SKU already exists.
        if Product.objects.filter(sku=sku).exists():
            raise ValueError("SKU collision detected (unexpected).")

        return sku

## Home page views.
@staff_required
def home(request):
    # Builds the home page stock summary cards.
    low_stock_count = Product.objects.filter(
        qty_in_stock__gt=0,
        qty_in_stock__lte=models.F("reorder_level"),
        is_active=True
    ).count()

    out_of_stock_count = Product.objects.filter(
        qty_in_stock=0,
        is_active=True
    ).count()

    # Counts reorder requests that are waiting for admin action.
    if hasattr(ReorderRequest, "STATUS_SUBMITTED"):
        reorders_pending_count = ReorderRequest.objects.filter(status=ReorderRequest.STATUS_SUBMITTED).count()
    else:
        # Fallback for projects using older status names.
        reorders_pending_count = ReorderRequest.objects.filter(status__in=["SUBMITTED", "PENDING", "Pending"]).count()

    # Builds the recent activity list from the latest stock movements.
    recent_activity = []

    movements = (
        StockMovement.objects
        .select_related("product")
        .order_by("-movement_date")[:5]
    )

    for m in movements:
        # Maps movement types to the UI style used on the home page.
        kind = "adjustment"
        if m.movement_type == "IN":
            kind = "stockin"
        elif m.movement_type == "OUT":
            kind = "stockout"
        elif m.movement_type == "SALE":
            kind = "sale"
        elif m.movement_type == "ADJUST":
            kind = "adjustment"
        elif m.movement_type == "PO":
            kind = "po"

        # Human-readable label shown in the activity timeline.
        type_label = {
            "IN": "Stock In",
            "OUT": "Stock Out",
            "ADJUST": "Adjustment",
            "SALE": "Sale",
            "PO": "PO Created",
        }.get(m.movement_type, m.movement_type)

        t = m.movement_date
        local_t = timezone.localtime(t) if t else None
        today_local = timezone.localdate()
        t_str = local_t.strftime("%I:%M %p") if local_t else "-"

        item_text = f"{m.product.product_name}"
        if m.product.sku:
            item_text += f" (SKU: {m.product.sku})"
        item_text += f" - Qty: {m.qty_moved}"

        recent_activity.append({
            "kind": kind,
            "type": type_label,
            "item": item_text,
            "time": t_str,
            "display_date": "Today" if local_t and local_t.date() == today_local else (local_t.strftime("%d %b %Y") if local_t else "-"),
        })

    now = timezone.localtime()

    context = {
        "store_name": "",
        "today_date": now.strftime("%A, %d %B %Y"),
        "now_time": now.strftime("%I:%M %p"),

        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
        "reorders_pending_count": reorders_pending_count,

        "recent_activity": recent_activity,
    }
    return render(request, "retail/home.html", context)


## Inventory page views.
@staff_required
def inventory(request):
    # Reads inventory filters and decides which tab to show.
    categories = Category.objects.order_by("category_name")
    suppliers_for_filter = Supplier.objects.order_by("supplier_name")

    category_id = request.GET.get("category", "")
    section = request.GET.get("section", "")
    supplier_id = request.GET.get("supplier", "")
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "")
    tab = request.GET.get("tab", "products")

    # Separate pagination values for the products tab and history tab.
    ppage = request.GET.get("ppage", 1)  # Products page number.
    hpage = request.GET.get("hpage", 1)  # History page number.

    # Keeps the current filters while rebuilding pagination links.
    qs = request.GET.copy()
    for k in ("tab", "ppage", "hpage"):
        qs.pop(k, None)
    base_qs = qs.urlencode()

    # Builds the filtered products queryset.
    products_qs = Product.objects.filter(is_active=True)

    if category_id:
        products_qs = products_qs.filter(category_id=category_id)

    if section:
        products_qs = products_qs.filter(section=section)

    if supplier_id:
        products_qs = products_qs.filter(
            Q(supplier_id=supplier_id) |
            Q(supplier_links__supplier_id=supplier_id) |
            Q(purchaseorderitem__po__supplier_id=supplier_id)
        )

    if q:
        products_qs = products_qs.filter(
            Q(product_name__icontains=q) | Q(sku__icontains=q)
        )

    products_qs = products_qs.select_related("category").distinct().order_by("product_name")
    all_products = Product.objects.filter(is_active=True).select_related("category").order_by("product_name")
    cp_suppliers = Supplier.objects.filter(is_active=True).prefetch_related("categories").order_by("supplier_name")

    # Builds the category-to-supplier map used by the create-product modal.
    cp_supplier_map = {}
    for s in cp_suppliers:
        linked_category_ids = list(s.categories.values_list("id", flat=True))
        if not linked_category_ids and s.category_id:
            linked_category_ids = [s.category_id]

        for cid in linked_category_ids:
            key = str(cid)
            cp_supplier_map.setdefault(key, []).append({
                "id": s.id,
                "name": s.supplier_name,
            })

    # Computes stock status labels and summary counts in Python.
    products = []
    low_count = 0
    out_count = 0

    for p in products_qs:
        on_hand = int(p.qty_in_stock or 0)

        if on_hand == 0:
            p.stock_status = "Out"
            out_count += 1
        elif on_hand <= p.reorder_level:
            p.stock_status = "Low"
            low_count += 1
        else:
            p.stock_status = "OK"

        # Applies the status filter used by the product cards tab.
        if status:
            if status == "LowOut" and p.stock_status not in ("Low", "Out"):
                continue
            if status in ("OK", "Low", "Out") and p.stock_status != status:
                continue

        products.append(p)

    # Paginates the final filtered product list.
    product_paginator = Paginator(products, 8)
    products_page = product_paginator.get_page(ppage)

    # Handles stock-in, stock-out, adjustments, and product creation.
    stock_in_form = StockInForm(prefix="in")
    stock_out_form = StockOutForm(prefix="out")
    adjust_form = AdjustmentForm(prefix="adj")
    product_create_form = ProductCreateForm(prefix="cp")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "IN":
            stock_in_form = StockInForm(request.POST, prefix="in")
            if stock_in_form.is_valid():
                cd = stock_in_form.cleaned_data
                with transaction.atomic():
                    product = Product.objects.select_for_update().get(pk=cd["product"].pk)
                    StockMovement.objects.create(
                        product=product,
                        movement_type="IN",
                        qty_moved=cd["qty"],
                        reference=cd["reference"] or None,
                        comments=cd["comments"] or None,
                        created_by=request.user,
                    )
                    product.qty_in_stock += cd["qty"]
                    product.save(update_fields=["qty_in_stock", "updated_at"])
                return redirect("inventory")

        elif action == "OUT":
            stock_out_form = StockOutForm(request.POST, prefix="out")
            if stock_out_form.is_valid():
                cd = stock_out_form.cleaned_data
                with transaction.atomic():
                    product = Product.objects.select_for_update().get(pk=cd["product"].pk)
                    if product.qty_in_stock < cd["qty"]:
                        stock_out_form.add_error("qty", "Insufficient stock.")
                    else:
                        StockMovement.objects.create(
                            product=product,
                            movement_type="OUT",
                            qty_moved=cd["qty"],
                            reason=cd["reason"],
                            reference=cd["reference"] or None,
                            comments=cd["comments"] or None,
                            created_by=request.user,
                        )
                        product.qty_in_stock -= cd["qty"]
                        product.save(update_fields=["qty_in_stock", "updated_at"])
                        return redirect("inventory")

        elif action == "ADJUST":
            if not is_admin(request.user):
                return redirect("inventory")
            adjust_form = AdjustmentForm(request.POST, prefix="adj")
            if adjust_form.is_valid():
                cd = adjust_form.cleaned_data
                with transaction.atomic():
                    product = Product.objects.select_for_update().get(pk=cd["product"].pk)
                    fields_to_update = []

                    if product.section != cd["section"]:
                        product.section = cd["section"]
                        fields_to_update.append("section")

                    if product.selling_price != cd["selling_price"]:
                        product.selling_price = cd["selling_price"]
                        fields_to_update.append("selling_price")

                    target = cd.get("set_stock_to")
                    if target is not None:
                        target = int(target)
                        current_on_hand = int(product.qty_in_stock or 0)
                        diff = target - current_on_hand

                        if diff != 0:
                            adj_dir = "INC" if diff > 0 else "DEC"
                            qty = abs(diff)

                            StockMovement.objects.create(
                                product=product,
                                movement_type="ADJUST",
                                qty_moved=qty,
                                adjust_direction=adj_dir,
                                reason=cd["reason"],
                                reference=cd["reference"] or None,
                                comments=cd["comments"] or None,
                                created_by=request.user,
                            )

                            product.qty_in_stock = target
                            fields_to_update.append("qty_in_stock")

                    if fields_to_update:
                        product.save(update_fields=fields_to_update + ["updated_at"])

                return redirect("inventory")

        elif action == "CREATE_PRODUCT":
            if not is_admin(request.user):
                return redirect("inventory")
            product_create_form = ProductCreateForm(request.POST, prefix="cp")
            if product_create_form.is_valid():
                with transaction.atomic():
                    new_obj = product_create_form.save(commit=False)

                    existing = Product.objects.select_for_update().filter(
                        product_name__iexact=new_obj.product_name.strip(),
                        category=new_obj.category,
                        section=new_obj.section,
                        selling_price=new_obj.selling_price,
                        is_active=True,
                    ).first()

                    # Case 1: update an existing matching product instead of creating a duplicate.
                    if existing:
                        opening_qty = product_create_form.cleaned_data.get("opening_qty", 0)

                        # Adds opening stock to the existing product when needed.
                        if opening_qty and opening_qty > 0:
                            StockMovement.objects.create(
                                product=existing,
                                movement_type="IN",
                                qty_moved=opening_qty,
                                reference=product_create_form.cleaned_data.get("opening_reference") or "OPENING",
                                comments=product_create_form.cleaned_data.get("opening_comments")
                                        or "Opening stock via Create Product (duplicate match)",
                                created_by=request.user,
                            )
                            existing.qty_in_stock += opening_qty
                            existing.save(update_fields=["qty_in_stock", "updated_at"])

                        # Updates the primary supplier link for the existing product.
                        supplier = product_create_form.cleaned_data.get("primary_supplier")
                        if supplier:
                            if existing.supplier_id != supplier.id:
                                existing.supplier = supplier
                                existing.save(update_fields=["supplier", "updated_at"])
                            ProductSupplier.objects.filter(product=existing).update(is_primary=False)
                            ProductSupplier.objects.update_or_create(
                                product=existing,
                                supplier=supplier,
                                defaults={
                                    "is_primary": True,
                                    "lead_time_days": product_create_form.cleaned_data.get("supplier_lead_time_days") or None,
                                    "last_unit_cost": product_create_form.cleaned_data.get("last_unit_cost") or None,
                                }
                            )

                    # Case 2: create a brand-new product record.
                    else:
                        # Generates the SKU and saves the new product.
                        supplier = product_create_form.cleaned_data.get("primary_supplier")
                        if supplier:
                            new_obj.supplier = supplier
                        new_obj.opening_stock = int(product_create_form.cleaned_data.get("opening_qty") or 0)
                        new_obj.sku = generate_sku_for(new_obj.section, new_obj.category)
                        new_obj.save()

                        # Rebinds the saved instance so the helper can create opening stock correctly.
                        product_create_form.instance = new_obj

                        # Creates the primary supplier link for the new product.
                        if supplier:
                            ProductSupplier.objects.filter(product=new_obj).update(is_primary=False)
                            ProductSupplier.objects.update_or_create(
                                product=new_obj,
                                supplier=supplier,
                                defaults={
                                    "is_primary": True,
                                    "lead_time_days": product_create_form.cleaned_data.get("supplier_lead_time_days") or None,
                                    "last_unit_cost": product_create_form.cleaned_data.get("last_unit_cost") or None,
                                }
                            )

                        # Creates the opening stock movement for the new product.
                        product_create_form.save_with_opening_stock(created_by=request.user)

                return redirect("inventory")


    # Builds the stock history tab filters and paginated movement list.
    history_form = HistoryFilterForm(request.GET or None, prefix="h")

    history_qs = StockMovement.objects.select_related("product", "created_by")

    if history_form.is_valid():
        cd = history_form.cleaned_data
        if cd.get("product"):
            history_qs = history_qs.filter(product=cd["product"])
        if cd.get("movement_type"):
            history_qs = history_qs.filter(movement_type=cd["movement_type"])
        if cd.get("from_date"):
            start_dt = timezone.make_aware(datetime.combine(cd["from_date"], time.min))
            history_qs = history_qs.filter(movement_date__gte=start_dt)

        if cd.get("to_date"):
            end_dt = timezone.make_aware(datetime.combine(cd["to_date"] + timedelta(days=1), time.min))
            history_qs = history_qs.filter(movement_date__lt=end_dt)

    # Keeps the queryset unsliced so pagination works correctly.
    history_qs = history_qs.order_by("-movement_date")

    movement_paginator = Paginator(history_qs, 10)
    history_page = movement_paginator.get_page(hpage)

    context = {
        "categories": categories,
        "selected_category": category_id,
        "section": section,
        "selected_supplier": supplier_id,
        "suppliers_for_filter": suppliers_for_filter,
        "q": q,
        "status": status,

        # paginated objects
        "products_page": products_page,
        "history_page": history_page,

        # counts (based on filtered queryset before status filter is applied)
        "low_count": low_count,
        "out_count": out_count,

        "stock_in_form": stock_in_form,
        "stock_out_form": stock_out_form,
        "adjust_form": adjust_form,
        "product_create_form": product_create_form,

        "history_form": history_form,

        "all_products": all_products,

        "tab": tab,
        "base_qs": base_qs,
        "cp_supplier_map": cp_supplier_map,
        "is_admin": is_admin(request.user),
    }
    return render(request, "retail/inventory.html", context)

# Legacy SKU helpers kept for compatibility with older code paths.
def generate_sku():
    return f"SKU-{secrets.token_hex(4).upper()}"

def generate_unique_sku():
    while True:
        sku = generate_sku()
        if not Product.objects.filter(sku=sku).exists():
            return sku
'''
Legacy stock-in example kept as commented reference.
@transaction.atomic
def stock_in(request):
    if request.method == "POST":
        product_id = request.POST["product"]
        qty = int(request.POST["qty_moved"])  # your input name
        product = Product.objects.select_for_update().get(pk=product_id)

        StockMovement.objects.create(
            product=product,
            movement_type="IN",
            qty_moved=qty,
            created_by=request.user,
        )

        apply_stock_movement(product=product, movement_type="IN", qty=qty)
        return redirect("inventory")

'''

## Sales page views.
@staff_required
def sales(request):
    # Handles the new-sale flow, sales history, and sale details tabs.
    tab = (request.GET.get("tab") or "new").strip()  # new | history | details

    # Product filters used on the new-sale tab.
    q = (request.GET.get("q") or "").strip()
    category_id = (request.GET.get("category") or "").strip()
    history_from = (request.GET.get("history_from") or "").strip()
    history_to = (request.GET.get("history_to") or "").strip()

    products_qs = Product.objects.filter(is_active=True).select_related("category").order_by("product_name")
    categories = Category.objects.order_by("category_name")
    section = (request.GET.get("section") or "").strip()

    if q:
        products_qs = products_qs.filter(Q(product_name__icontains=q) | Q(sku__icontains=q))
    if category_id:
        products_qs = products_qs.filter(category_id=category_id)
    if section:
        products_qs = products_qs.filter(section=section)

    # Saves a completed sale from the cart payload.
    if request.method == "POST":
        customer_name = request.POST.get("customer_name", "")
        customer_phone = request.POST.get("customer_phone", "")
        discount = request.POST.get("discount", "0.00")
        payment_method = request.POST.get("payment_method", "cash")

        cart_json = request.POST.get("cart_json", "[]")
        try:
            cart_items = json.loads(cart_json)
        except Exception:
            cart_items = []

        try:
            sale = create_sale(
                cashier=request.user,
                customer_name=customer_name,
                customer_phone=customer_phone,
                discount=discount,
                payment_method=payment_method,
                cart_items=cart_items,
            )
            messages.success(request, "Sale saved and stock updated.")
            return redirect(f"/sales/?tab=details&sale_id={sale.id}")
        except Exception as e:
            messages.error(request, str(e))
            tab = "new"

    # Builds the sales history queryset and filters.
    sales_qs = Sale.objects.select_related("cashier").order_by("-sale_date", "-created_at", "-id")

    if history_from:
        try:
            sales_qs = sales_qs.filter(sale_date__gte=date.fromisoformat(history_from))
        except ValueError:
            history_from = ""

    if history_to:
        try:
            sales_qs = sales_qs.filter(sale_date__lte=date.fromisoformat(history_to))
        except ValueError:
            history_to = ""

    page_obj = Paginator(sales_qs, 10).get_page(request.GET.get("page"))

    history_params = request.GET.copy()
    history_params.pop("page", None)
    history_query = history_params.urlencode()

    # Loads one sale when the details tab is requested.
    sale = None
    sale_id = request.GET.get("sale_id")
    if sale_id:
        sale = get_object_or_404(
            Sale.objects.select_related("cashier").prefetch_related("items__product"),
            id=sale_id
        )

    return render(request, "retail/sales.html", {
        "tab": tab,
        "sale_datetime": timezone.now(),

        "products": products_qs,
        "categories": categories,
        "q": q,
        "selected_category": category_id,

        "page_obj": page_obj,  # history
        "sale": sale,          # details (can be None)
        "history_from": history_from,
        "history_to": history_to,
        "history_query": history_query,

        "section_choices": Product._meta.get_field("section").choices,
        "selected_section": section,

    })

## Supplier, reorder, and purchase-order views.
@staff_required
def suppliers(request):
    tab = request.GET.get("tab", "suppliers")
    if tab not in ("suppliers", "create-reorder-draft", "purchase-orders", "reorder-requests"):
        tab = "suppliers"

    # Restricts supplier master data to admins.
    if not is_admin(request.user) and tab == "suppliers":
        return redirect(f"{request.path}?tab=create-reorder-draft")
    # Restricts purchase-order management to admins.
    if not is_admin(request.user) and tab == "purchase-orders":
        return redirect(f"{request.path}?tab=create-reorder-draft")

    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")
    rr_status = request.GET.get("rr_status", "all").strip().upper() or "ALL"
    page_number = request.GET.get("page", 1)

    supplier_form = SupplierForm()
    po_form = PurchaseOrderCreateForm()

    # Handles supplier, purchase-order, and conversion actions from the tabbed page.
    if request.method == "POST":
        form_type = request.POST.get("form_type", "")

        if (not is_admin(request.user)) and form_type in {"create_supplier", "update_supplier", "delete_supplier"}:
            messages.error(request, "Only admin can manage suppliers.")
            return redirect(f"{request.path}?tab=create-reorder-draft")
        if (not is_admin(request.user)) and form_type in {"create_po", "update_po", "delete_po", "create_po_from_rr"}:
            messages.error(request, "Only admin can manage purchase orders.")
            return redirect(f"{request.path}?tab=create-reorder-draft")

        # Creates a new supplier.
        if form_type == "create_supplier":
            supplier_form = SupplierForm(request.POST)
            if supplier_form.is_valid():
                supplier_form.save()
                messages.success(request, "Supplier created successfully.")
                return redirect(f"{request.path}?tab=suppliers")
            messages.error(request, "Please fix the errors in the supplier form.")
            tab = "suppliers"

        # Updates an existing supplier.
        elif form_type == "update_supplier":
            supplier_id = request.POST.get("supplier_id")
            s = get_object_or_404(Supplier, pk=supplier_id)
            supplier_form = SupplierForm(request.POST, instance=s)
            if supplier_form.is_valid():
                supplier_form.save()
                messages.success(request, "Supplier updated.")
                return redirect(f"{request.path}?tab=suppliers")
            messages.error(request, "Please fix the errors in the supplier form.")
            tab = "suppliers"

        # Deletes a supplier.
        elif form_type == "delete_supplier":
            supplier_id = request.POST.get("supplier_id")
            s = get_object_or_404(Supplier, pk=supplier_id)
            s.delete()
            messages.success(request, "Supplier deleted.")
            return redirect(f"{request.path}?tab=suppliers")

        # Creates a purchase order from the modal form.
        elif form_type == "create_po":
            create_po_data = request.POST.copy()
            product_id_raw = (create_po_data.get("product_id") or "").strip()
            supplier_raw = (create_po_data.get("supplier") or "").strip()
            qty_raw = (create_po_data.get("qty_ordered") or "").strip()
            if product_id_raw and not supplier_raw:
                selected_product = Product.objects.filter(id=product_id_raw, is_active=True).only("supplier_id").first()
                if selected_product and selected_product.supplier_id:
                    create_po_data["supplier"] = str(selected_product.supplier_id)

            po_form = PurchaseOrderCreateForm(create_po_data)
            if po_form.is_valid():
                try:
                    qty_ordered = int(qty_raw or "0")
                except ValueError:
                    qty_ordered = 0

                if not product_id_raw:
                    messages.error(request, "Select a product for this purchase order.")
                    tab = "purchase-orders"
                    po_form = PurchaseOrderCreateForm(create_po_data)
                elif qty_ordered <= 0:
                    messages.error(request, "Enter a quantity greater than 0.")
                    tab = "purchase-orders"
                    po_form = PurchaseOrderCreateForm(create_po_data)
                else:
                    product = Product.objects.filter(id=product_id_raw, is_active=True).first()
                    supplier_obj = po_form.cleaned_data.get("supplier")
                    if not product or not supplier_obj:
                        messages.error(request, "Invalid supplier or product selection.")
                        tab = "purchase-orders"
                    else:
                        supplier_link = product.supplier_links.filter(supplier_id=supplier_obj.id).first()
                        if not supplier_link and product.supplier_id != supplier_obj.id:
                            messages.error(request, "The selected product is not connected to the chosen supplier.")
                            tab = "purchase-orders"
                        else:
                            po = po_form.save(commit=False)
                            po.created_by = request.user
                            if not po.status:
                                po.status = PurchaseOrder.STATUS_PENDING
                            if not po.order_date:
                                po.order_date = timezone.localdate()
                            if not po.delivery_date and po.supplier_id and po.order_date:
                                lead_days = int(getattr(po.supplier, "lead_time_days", 0) or 0)
                                po.delivery_date = po.order_date + timedelta(days=max(lead_days, 0))
                            po.po_number = _next_po_number()

                            unit_cost = getattr(supplier_link, "last_unit_cost", None)
                            if unit_cost is None:
                                unit_cost = Decimal("0.00")
                            po.total_amount = unit_cost * Decimal(qty_ordered)
                            po.save()

                            PurchaseOrderItem.objects.create(
                                po=po,
                                product=product,
                                qty_ordered=qty_ordered,
                                unit_cost=unit_cost,
                            )

                            messages.success(request, f"PO created: {po}")
                            return redirect(f"{request.path}?tab=purchase-orders")

            messages.error(request, "Please fix the errors in the purchase order form.")
            tab = "purchase-orders"


        # Updates an existing purchase order from the edit modal.
        elif form_type == "update_po":
            po_id = request.POST.get("po_id")
            po = get_object_or_404(PurchaseOrder, pk=po_id)
            original_status = po.status

            # Updates the basic purchase-order fields.
            po_number = (po.po_number or "").strip()
            if "\\u002D" in po_number:
                po_number = po_number.replace("\\u002D", "-")
            po.po_number = po_number
            po.notes = request.POST.get("notes", "").strip()

            # Updates the linked supplier if one was chosen.
            supplier_id = request.POST.get("supplier_id")
            if supplier_id:
                po.supplier = get_object_or_404(Supplier, pk=int(supplier_id))

            # Reads optional order and delivery dates from the form.
            po.order_date = request.POST.get("order_date") or None
            po.delivery_date = request.POST.get("delivery_date") or None

            # Recalculates the PO total from items when possible.
            item_total = Decimal("0.00")
            items = po.items.all()
            for it in items:
                if it.unit_cost is not None:
                    item_total += (it.unit_cost * Decimal(it.qty_ordered))
            if item_total > 0:
                po.total_amount = item_total
            else:
                amt = request.POST.get("total_amount", "").strip()
                try:
                    po.total_amount = Decimal(amt) if amt else Decimal("0.00")
                except InvalidOperation:
                    po.total_amount = Decimal("0.00")

            # Applies the selected purchase-order status.
            status_val = request.POST.get("status")
            if status_val in dict(PurchaseOrder.STATUS_CHOICES):
                po.status = status_val

            if not po.po_number:
                messages.error(request, "PO Number is required.")
                return redirect(f"{request.path}?tab=purchase-orders")

            # Blocks edits after the PO has been finalized.
            if original_status in (PurchaseOrder.STATUS_RECEIVED, PurchaseOrder.STATUS_CANCELLED):
                messages.error(request, "Received/Cancelled purchase orders cannot be edited.")
                return redirect(f"{request.path}?tab=purchase-orders")

            po.save()

            # Receives the PO into stock once when the status becomes RECEIVED.
            if po.status == PurchaseOrder.STATUS_RECEIVED and not po.received_at:
                now = timezone.now()
                with transaction.atomic():
                    items = po.items.select_related("product")
                    for it in items:
                        qty_to_receive = max(int(it.qty_ordered) - int(it.qty_received or 0), 0)
                        if qty_to_receive <= 0:
                            continue
                        StockMovement.objects.create(
                            product=it.product,
                            movement_type=StockMovement.MovementType.PO,
                            qty_moved=qty_to_receive,
                            created_by=request.user,
                            reference=po.po_number or None,
                            comments="Stock in from PO receipt",
                        )
                        it.product.qty_in_stock += qty_to_receive
                        it.product.save(update_fields=["qty_in_stock", "updated_at"])
                        it.qty_received += qty_to_receive
                        it.save(update_fields=["qty_received"])

                    po.received_at = now
                    po.save(update_fields=["received_at"])

            messages.success(request, "Purchase order updated.")
            return redirect(f"{request.path}?tab=purchase-orders")


        # Deletes a draft purchase order.
        elif form_type == "delete_po":
            po_id = request.POST.get("po_id")
            po = get_object_or_404(PurchaseOrder, pk=po_id)

            # Only draft purchase orders can be deleted.
            if po.status != PurchaseOrder.STATUS_DRAFT:
                messages.error(request, "Only DRAFT purchase orders can be deleted.")
                return redirect(f"{request.path}?tab=purchase-orders")

            po.delete()
            messages.success(request, "Purchase order deleted.")
            return redirect(f"{request.path}?tab=purchase-orders")
        
        
        # Converts a draft reorder request into a purchase order.
        elif form_type == "create_po_from_rr":
            rr_id = request.POST.get("rr_id")
            rr = get_object_or_404(ReorderRequest, pk=rr_id)

            if rr.status != ReorderRequest.STATUS_DRAFT:
                messages.error(request, "Only DRAFT reorder requests can be converted to a PO.")
                return redirect(f"{request.path}?tab=purchase-orders")

            if not rr.supplier_id:
                messages.error(request, "This reorder request has no supplier.")
                return redirect(f"{request.path}?tab=purchase-orders")

            # Prevents duplicate conversion of the same request.
            if rr.purchase_orders.exists():
                messages.error(request, "A PO was already created from this request.")
                return redirect(f"{request.path}?tab=purchase-orders")

            with transaction.atomic():
                rr_supplier = Supplier.objects.filter(id=rr.supplier_id).only("lead_time_days").first()
                lead_days = int(getattr(rr_supplier, "lead_time_days", 0) or 0)
                order_date = timezone.localdate()
                po = PurchaseOrder.objects.create(
                    supplier_id=rr.supplier_id,
                    status=PurchaseOrder.STATUS_PENDING,
                    created_by=request.user,
                    order_date=order_date,
                    delivery_date=order_date + timedelta(days=max(lead_days, 0)),
                    po_number=_next_po_number(),
                )

                items = rr.items.select_related("product")
                total = Decimal("0.00")
                for it in items:
                    # Uses the supplier-specific unit cost when available.
                    unit_cost = None
                    link = it.product.supplier_links.filter(
                        supplier_id=rr.supplier_id
                    ).first()
                    if link and link.last_unit_cost is not None:
                        unit_cost = link.last_unit_cost
                    PurchaseOrderItem.objects.create(
                        po=po,
                        product=it.product,
                        qty_ordered=it.qty_requested,
                        reorder_request=rr,
                        unit_cost=unit_cost,
                    )
                    if unit_cost is not None:
                        total += (unit_cost * Decimal(it.qty_requested))

                po.total_amount = total
                po.save(update_fields=["total_amount"])
                rr.status = ReorderRequest.STATUS_APPROVED
                rr.decided_at = timezone.now()
                rr.decided_by = request.user
                rr.save(update_fields=["status", "decided_at", "decided_by"])

            messages.success(request, f"Draft PO created from RR-{rr.id}: {po}")
            return redirect(f"{request.path}?tab=purchase-orders")

    # Shared context used by all suppliers-page tabs.
    context = {
        "tab": tab,
        "q": q,
        "status": status,
        "rr_status": rr_status,
        "is_admin": is_admin(request.user),
        "form": supplier_form,
        "po_form": po_form,
    }
    context["open_po_count"] = 0
    if is_admin(request.user):
        context["open_po_count"] = PurchaseOrder.objects.filter(
            status__in=[PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_PENDING]
        ).count()

    context["suppliers_all"] = Supplier.objects.filter(is_active=True).order_by("supplier_name")
    context["categories"] = Category.objects.order_by("category_name")

    # Suppliers master-data tab.
    if tab == "suppliers":
        suppliers_qs = Supplier.objects.prefetch_related("categories").all().order_by("-created_at")

        if q:
            suppliers_qs = suppliers_qs.filter(
                Q(supplier_name__icontains=q) |
                Q(phone__icontains=q) |
                Q(email__icontains=q)
            )

        if status == "active":
            suppliers_qs = suppliers_qs.filter(is_active=True)
        elif status == "inactive":
            suppliers_qs = suppliers_qs.filter(is_active=False)

        paginator = Paginator(suppliers_qs, 10)
        context["suppliers"] = paginator.get_page(page_number)
        return render(request, "retail/suppliers.html", context)

    # Purchase-orders tab.
    if tab == "purchase-orders":
        context["products_for_po"] = Product.objects.filter(is_active=True).select_related("category", "supplier").prefetch_related("supplier_links").order_by("product_name")
        context["rr_drafts"] = ReorderRequest.objects.select_related("supplier").filter(
            status=ReorderRequest.STATUS_DRAFT
        ).order_by("-created_at")[:50]

        po_qs = PurchaseOrder.objects.select_related("supplier").prefetch_related("items__product__category").all().order_by("-created_at")

        if q:
            po_qs = po_qs.filter(
                Q(po_number__icontains=q) |
                Q(supplier__supplier_name__icontains=q)
            )

        if status != "all":
            po_qs = po_qs.filter(status=status)

        # Backfills older records that are missing a PO number or order date.
        missing_qs = po_qs.filter(Q(po_number__isnull=True) | Q(po_number="") | Q(order_date__isnull=True)).order_by("created_at")
        if missing_qs.exists():
            with transaction.atomic():
                for po in missing_qs:
                    changed = False
                    if not po.po_number:
                        po.po_number = _next_po_number()
                        changed = True
                    if not po.order_date:
                        po.order_date = timezone.localdate()
                        changed = True
                    if changed:
                        po.save(update_fields=["po_number", "order_date"])

        # Normalizes escaped PO numbers saved from older HTML or JS flows.
        escaped_qs = po_qs.filter(po_number__contains="\\u002D")
        if escaped_qs.exists():
            with transaction.atomic():
                for po in escaped_qs:
                    po.po_number = (po.po_number or "").replace("\\u002D", "-")
                    po.save(update_fields=["po_number"])

        paginator = Paginator(po_qs, 10)
        context["purchase_orders"] = paginator.get_page(page_number)
        return render(request, "retail/suppliers.html", context)
    # Reorder draft creation tab.
    if tab == "create-reorder-draft":
        qs, since = get_reorder_queryset()
        qs = qs.order_by("created_at", "id")[:15]

        grouped = {}
        for p in qs:
            extra = compute_reorder_row(p)
            sid = extra["supplier_id"]

            if sid not in grouped:
                grouped[sid] = {
                    "supplier": extra["supplier"],
                    "supplier_name": extra["supplier_name"],
                    "items": []
                }

            grouped[sid]["items"].append({
                "product_id": p.id,
                "sku": p.sku,
                "product_name": p.product_name,
                "category": p.category,
                "opening_stock": p.opening_stock,
                "qty_in_stock": p.qty_in_stock,
                "reorder_level": p.reorder_level,
                **extra,
            })

        context.update({
            "grouped": grouped,
            "since": since,
            "safety_stock": 5,
        })
        return render(request, "retail/suppliers.html", context)

    # Reorder requests review tab.
    requests_base_qs = ReorderRequest.objects.select_related("supplier").prefetch_related("items__product__category").annotate(
        total_qty_to_order=Coalesce(Sum("items__qty_requested"), 0, output_field=IntegerField())
    )
    if rr_status != "ALL":
        requests_base_qs = requests_base_qs.filter(status=rr_status)
    if is_admin(request.user):
        requests_qs = requests_base_qs.order_by("created_at", "id")
    else:
        requests_qs = requests_base_qs.filter(created_by=request.user).order_by("created_at", "id")

    rr_paginator = Paginator(requests_qs, 10)
    rr_page = rr_paginator.get_page(page_number)

    rr_ids = [r.id for r in rr_page.object_list]
    linked_po_rows = PurchaseOrderItem.objects.filter(
        reorder_request_id__in=rr_ids
    ).select_related(
        "po", "po__supplier", "product", "product__category"
    ).order_by("po__created_at", "po_id", "id")

    rr_po_map = {}
    for row in linked_po_rows:
        rr_entry = rr_po_map.setdefault(row.reorder_request_id, {})
        po_entry = rr_entry.setdefault(
            row.po_id,
            {"po": row.po, "items": []},
        )
        po_entry["items"].append(row)

    for rr in rr_page.object_list:
        linked = rr_po_map.get(rr.id, {})
        rr.linked_pos = list(linked.values())
        rr.linked_po = rr.linked_pos[0]["po"] if rr.linked_pos else None

    context.update({
        "requests": rr_page,
        "products_for_rr_edit": Product.objects.filter(is_active=True).select_related("category").order_by("product_name"),
    })
    return render(request, "retail/suppliers.html", context)

# Reorder-request actions.
# Creates one or more draft reorder requests grouped by supplier.
def reorder_request_create_draft(request):
    if request.method != "POST":
        return redirect("suppliers")

    product_ids = request.POST.getlist("product_ids")
    if not product_ids:
        messages.error(request, "Select at least one item to create a request.")
        return redirect("/suppliers/?tab=create-reorder-draft")

    # Groups selected products by supplier so each request has one supplier.
    by_supplier = {}

    for pid in product_ids:
        supplier_id = request.POST.get(f"supplier_{pid}")
        qty = int(request.POST.get(f"qty_{pid}", "0") or 0)
        suggested = int(request.POST.get(f"suggested_{pid}", "0") or 0)

        if qty <= 0:
            continue

        if not supplier_id:
            # A request cannot be created if a selected item has no supplier.
            messages.error(request, "Some selected items have no supplier assigned. Please set a primary supplier.")
            return redirect("/suppliers/?tab=create-reorder-draft")

        by_supplier.setdefault(int(supplier_id), []).append({
            "pid": int(pid),
            "qty": qty,
            "suggested": suggested,
        })

    if not by_supplier:
        messages.error(request, "All selected items had quantity 0. Please enter a quantity.")
        return redirect("/suppliers/?tab=create-reorder-draft")

    created_ids = []

    with transaction.atomic():
        for supplier_id, items in by_supplier.items():
            rr = ReorderRequest.objects.create(
                created_by=request.user,
                status=ReorderRequest.STATUS_DRAFT,
                supplier_id=supplier_id
            )
            for it in items:
                ReorderRequestItem.objects.create(
                    request=rr,
                    product_id=it["pid"],
                    supplier_id=supplier_id,
                    qty_requested=it["qty"],
                    suggested_qty_snapshot=it["suggested"],
                )
            created_ids.append(rr.id)

    messages.success(request, f"Created {len(created_ids)} draft request(s): " + ", ".join([f"RR-{i}" for i in created_ids]))
    return redirect("/suppliers/?tab=create-reorder-draft")

# Submits a draft reorder request for approval.
def reorder_request_submit(request, rr_id):
    next_url = request.POST.get("next") or "/suppliers/?tab=reorder-requests"
    if request.method != "POST":
        return redirect(next_url)

    rr = get_object_or_404(ReorderRequest, id=rr_id)
    if not is_admin(request.user) and rr.created_by_id != request.user.id:
        messages.error(request, "Not authorized to submit this request.")
        return redirect(next_url)

    if rr.status != ReorderRequest.STATUS_DRAFT:
        messages.error(request, "Only draft requests can be submitted.")
        return redirect(next_url)

    rr.status = ReorderRequest.STATUS_SUBMITTED
    rr.submitted_at = timezone.now()
    rr.save(update_fields=["status", "submitted_at"])

    messages.success(request, f"RR-{rr.id} submitted to admin.")
    return redirect(next_url)

# Updates a draft or submitted reorder request.
def reorder_request_update(request, rr_id):
    next_url = request.POST.get("next") or "/suppliers/?tab=reorder-requests"
    if request.method != "POST":
        return redirect(next_url)

    rr = get_object_or_404(ReorderRequest, id=rr_id)
    if not is_admin(request.user) and rr.created_by_id != request.user.id:
        messages.error(request, "Not authorized to edit this request.")
        return redirect(next_url)
    if rr.status not in (ReorderRequest.STATUS_DRAFT, ReorderRequest.STATUS_SUBMITTED):
        messages.error(request, "Only draft or submitted requests can be edited.")
        return redirect(next_url)

    save_action = (request.POST.get("save_action") or "draft").strip().lower()
    supplier_id = (request.POST.get("supplier_id") or "").strip()

    items = list(rr.items.select_related("product", "product__category"))
    allowed_products = {p.id: p for p in Product.objects.filter(is_active=True).select_related("category", "supplier")}
    qty_updates = []
    product_updates = []
    chosen_product_ids = set()
    for it in items:
        pid_raw = (request.POST.get(f"product_{it.id}") or "").strip()
        try:
            product_id = int(pid_raw) if pid_raw else int(it.product_id)
        except (TypeError, ValueError):
            messages.error(request, f"Invalid product selection for item #{it.id}.")
            return redirect(next_url)
        if product_id not in allowed_products:
            messages.error(request, "Selected product is invalid or inactive.")
            return redirect(next_url)
        if product_id in chosen_product_ids:
            messages.error(request, "Duplicate products are not allowed in one request.")
            return redirect(next_url)
        chosen_product_ids.add(product_id)

        raw = (request.POST.get(f"qty_{it.id}") or "").strip()
        try:
            qty = int(raw)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            messages.error(request, f"Quantity for {it.product.product_name} must be at least 1.")
            return redirect(next_url)
        qty_updates.append((it, qty))
        product_updates.append((it, product_id))

    new_product_raw = (request.POST.get("new_product_id") or "").strip()
    new_qty_raw = (request.POST.get("new_qty") or "").strip()
    create_new_item = False
    new_product = None
    new_qty = 0
    if new_product_raw:
        try:
            new_product_id = int(new_product_raw)
            new_product = allowed_products.get(new_product_id)
        except (TypeError, ValueError):
            new_product = None
        if not new_product:
            messages.error(request, "Invalid new product selection.")
            return redirect(next_url)
        if new_product.id in chosen_product_ids:
            messages.error(request, "That product is already in this request.")
            return redirect(next_url)
        try:
            new_qty = int(new_qty_raw or "0")
        except (TypeError, ValueError):
            new_qty = 0
        if new_qty <= 0:
            messages.error(request, "Items must be at least 1 for a new product.")
            return redirect(next_url)
        create_new_item = True

    with transaction.atomic():
        rr = ReorderRequest.objects.select_for_update().get(id=rr.id)
        fields_to_update = []

        if supplier_id:
            supplier = Supplier.objects.filter(id=int(supplier_id), is_active=True).first()
            if supplier and rr.supplier_id != supplier.id:
                rr.supplier = supplier
                fields_to_update.append("supplier")

        if save_action == "submit":
            rr.status = ReorderRequest.STATUS_SUBMITTED
            rr.submitted_at = timezone.now()
            fields_to_update.extend(["status", "submitted_at", "updated_at"])
            success_msg = f"RR-{rr.id} updated and submitted to admin."
        else:
            rr.status = ReorderRequest.STATUS_DRAFT
            rr.submitted_at = None
            fields_to_update.extend(["status", "submitted_at", "updated_at"])
            success_msg = f"RR-{rr.id} updated and saved as draft."

        if fields_to_update:
            rr.save(update_fields=list(dict.fromkeys(fields_to_update)))

        for it, qty in qty_updates:
            if it.qty_requested != qty:
                it.qty_requested = qty
                it.save(update_fields=["qty_requested"])
        for it, product_id in product_updates:
            if it.product_id != product_id:
                it.product_id = product_id
                if rr.supplier_id:
                    it.supplier_id = rr.supplier_id
                it.save(update_fields=["product", "supplier"])

        if create_new_item and new_product:
            ReorderRequestItem.objects.create(
                request=rr,
                product_id=new_product.id,
                supplier_id=rr.supplier_id or new_product.supplier_id,
                qty_requested=new_qty,
                suggested_qty_snapshot=int(new_product.opening_stock or 0),
            )

    messages.success(request, success_msg)
    return redirect(next_url)

# Approves a submitted reorder request and creates draft purchase orders.
def reorder_request_approve(request, rr_id):
    next_url = request.POST.get("next") or "/suppliers/?tab=reorder-requests"
    if request.method != "POST":
        return redirect(next_url)

    if not is_admin(request.user):
        messages.error(request, "Not authorized.")
        return redirect(next_url)

    rr = get_object_or_404(ReorderRequest, id=rr_id)

    if rr.status != ReorderRequest.STATUS_SUBMITTED:
        messages.error(request, "Only submitted requests can be approved.")
        return redirect(next_url)

    items = rr.items.select_related("product", "supplier")

    with transaction.atomic():
        by_supplier = {}
        for it in items:
            # Falls back to the product's primary supplier when needed.
            if not it.supplier_id:
                link = it.product.supplier_links.filter(is_primary=True).select_related("supplier").first()
                if link:
                    it.supplier = link.supplier
                    it.save(update_fields=["supplier"])

            if it.supplier_id:
                by_supplier.setdefault(it.supplier_id, []).append(it)

        for supplier_id, its in by_supplier.items():
            supplier_obj = Supplier.objects.filter(id=supplier_id).only("lead_time_days").first()
            lead_days = int(getattr(supplier_obj, "lead_time_days", 0) or 0)
            order_date = timezone.localdate()
            po = PurchaseOrder.objects.create(
                supplier_id=supplier_id,
                status=PurchaseOrder.STATUS_PENDING,
                created_by=request.user,
                order_date=order_date,
                delivery_date=order_date + timedelta(days=max(lead_days, 0)),
                po_number=_next_po_number(),
            )
            total = Decimal("0.00")
            for it in its:
                unit_cost = None
                link = it.product.supplier_links.filter(
                    supplier_id=supplier_id
                ).first()
                if link and link.last_unit_cost is not None:
                    unit_cost = link.last_unit_cost
                PurchaseOrderItem.objects.create(
                    po=po,
                    product=it.product,
                    qty_ordered=it.qty_requested,
                    reorder_request=rr,
                    unit_cost=unit_cost,
                )
                if unit_cost is not None:
                    total += (unit_cost * Decimal(it.qty_requested))

            po.total_amount = total
            po.save(update_fields=["total_amount"])
            rr.status = ReorderRequest.STATUS_APPROVED
            rr.decided_at = timezone.now()
            rr.decided_by = request.user
            rr.save(update_fields=["status", "decided_at", "decided_by"])

    messages.success(request, f"RR-{rr.id} approved. Draft PO(s) created.")
    return redirect(next_url)

# Rejects a submitted reorder request.
def reorder_request_reject(request, rr_id):
    next_url = request.POST.get("next") or "/suppliers/?tab=reorder-requests"
    if request.method != "POST":
        return redirect(next_url)

    if not is_admin(request.user):
        messages.error(request, "Not authorized.")
        return redirect(next_url)

    rr = get_object_or_404(ReorderRequest, id=rr_id)

    if rr.status != ReorderRequest.STATUS_SUBMITTED:
        messages.error(request, "Only submitted requests can be rejected.")
        return redirect(next_url)

    rr.status = ReorderRequest.STATUS_REJECTED
    rr.decided_at = timezone.now()
    rr.decided_by = request.user
    rr.decision_note = request.POST.get("decision_note", "").strip()
    rr.save(update_fields=["status", "decided_at", "decided_by", "decision_note"])

    messages.success(request, f"RR-{rr.id} rejected.")
    return redirect(next_url)

## Dashboard and analytics views.
@staff_required
def dashboard(request):
    # Builds the analytics dashboard for sales, stock, and forecast data.
    today = timezone.localdate()
    range_days = 30
    start_str = (request.GET.get("start") or "").strip()
    end_str = (request.GET.get("end") or "").strip()

    start_date = None
    end_date = None
    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            start_date = None
            end_date = None

    if start_date and end_date:
        since = start_date
        today = end_date
        if since > today:
            since, today = today, since
        range_days = (today - since).days + 1
    else:
        since = today - timedelta(days=range_days - 1)

    def month_end(day_value: date) -> date:
        return day_value.replace(day=monthrange(day_value.year, day_value.month)[1])

    def next_month_start(day_value: date) -> date:
        if day_value.month == 12:
            return date(day_value.year + 1, 1, 1)
        return date(day_value.year, day_value.month + 1, 1)

    sales_qs = Sale.objects.filter(sale_date__gte=since, sale_date__lte=today)
    orders_count = sales_qs.count()
    sales_agg = sales_qs.aggregate(
        total_revenue=Coalesce(Sum("total"), Decimal("0.00")),
    )
    units_agg = SaleItem.objects.filter(sale__sale_date__gte=since, sale__sale_date__lte=today).aggregate(
        total_units=Coalesce(Sum("qty"), 0)
    )
    total_revenue = sales_agg["total_revenue"]
    total_units = units_agg["total_units"]
    avg_order_value = (total_revenue / orders_count) if orders_count else Decimal("0.00")
    avg_basket_size = (Decimal(total_units) / orders_count) if orders_count else Decimal("0.00")

    products_qs = Product.objects.filter(is_active=True)
    total_skus = products_qs.count()
    total_units_on_hand = products_qs.aggregate(total=Coalesce(Sum("qty_in_stock"), 0))["total"]
    stock_value_expr = ExpressionWrapper(
        F("qty_in_stock") * F("selling_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    stock_value = products_qs.aggregate(total=Coalesce(Sum(stock_value_expr), Decimal("0.00")))["total"]
    out_of_stock_count = products_qs.filter(qty_in_stock=0).count()
    low_stock_count = products_qs.filter(qty_in_stock__gt=0, qty_in_stock__lte=4).count()
    ok_stock_count = products_qs.filter(qty_in_stock__gte=5).count()
    stock_attention_count = low_stock_count + out_of_stock_count
    low_stock_items = list(
        products_qs.filter(qty_in_stock__lte=4).select_related("category").order_by("qty_in_stock", "product_name")
    )
    total_stock_count = total_skus or 1
    ok_pct = int(round(ok_stock_count / total_stock_count * 100))
    low_pct = int(round(low_stock_count / total_stock_count * 100))
    out_pct = int(round(out_of_stock_count / total_stock_count * 100))

    dps_range = DailyProductSales.objects.filter(date__gte=since, date__lte=today)
    sales_daily_rows = list(
        Sale.objects.filter(sale_date__gte=since, sale_date__lte=today)
        .values("sale_date")
        .annotate(
            revenue=Coalesce(Sum("total"), Decimal("0.00")),
            units=Coalesce(Sum("items__qty"), 0),
        )
        .order_by("sale_date")
    )
    sales_daily_map = {
        row["sale_date"]: {
            "revenue": row["revenue"],
            "units": int(row["units"] or 0),
        }
        for row in sales_daily_rows
    }
    actual_daily_labels = []
    actual_daily_revenue = []
    actual_daily_units = []
    cursor = since
    while cursor <= today:
        day_data = sales_daily_map.get(cursor, {})
        actual_daily_labels.append(cursor.strftime("%d %b"))
        actual_daily_revenue.append(float(day_data.get("revenue") or 0))
        actual_daily_units.append(int(day_data.get("units") or 0))
        cursor += timedelta(days=1)
    has_daily_sales = any(unit > 0 for unit in actual_daily_units)

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    sales_today_count = Sale.objects.filter(sale_date=today).count()
    sales_week_count = Sale.objects.filter(sale_date__gte=week_start, sale_date__lte=today).count()
    sales_month_count = Sale.objects.filter(sale_date__gte=month_start, sale_date__lte=today).count()
    sales_today_revenue = Sale.objects.filter(sale_date=today).aggregate(total=Coalesce(Sum("total"), Decimal("0.00")))["total"]
    sales_week_revenue = Sale.objects.filter(sale_date__gte=week_start, sale_date__lte=today).aggregate(total=Coalesce(Sum("total"), Decimal("0.00")))["total"]
    sales_month_revenue = Sale.objects.filter(sale_date__gte=month_start, sale_date__lte=today).aggregate(total=Coalesce(Sum("total"), Decimal("0.00")))["total"]

    best_this_week = list(
        SaleItem.objects.filter(sale__sale_date__gte=week_start, sale__sale_date__lte=today)
        .values("product__sku", "product__product_name", "product__category__category_name")
        .annotate(qty=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
        .order_by("-qty", "-revenue")[:6]
    )
    best_this_month = list(
        SaleItem.objects.filter(sale__sale_date__gte=month_start, sale__sale_date__lte=today)
        .values("product__sku", "product__product_name", "product__category__category_name")
        .annotate(qty=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
        .order_by("-qty", "-revenue")[:6]
    )
    best_selected_range = list(
        SaleItem.objects.filter(sale__sale_date__gte=since, sale__sale_date__lte=today)
        .values("product__sku", "product__product_name", "product__category__category_name")
        .annotate(qty=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
        .order_by("-qty", "-revenue")[:6]
    )

    category_current_rows = list(
        dps_range.values("product__category__category_name")
        .annotate(revenue=Coalesce(Sum("sales_amt"), Decimal("0.00")), units=Coalesce(Sum("qty_sold"), 0))
        .order_by("-revenue")[:6]
    )
    category_current_labels = [row["product__category__category_name"] or "Uncategorized" for row in category_current_rows]
    category_current_values = [float(row["revenue"]) for row in category_current_rows]

    monthly_window_start = today.replace(day=1)
    for _ in range(9):
        monthly_window_start = (monthly_window_start - timedelta(days=1)).replace(day=1)
    monthly_category_rows = list(
        DailyProductSales.objects.filter(date__gte=monthly_window_start, date__lte=today)
        .annotate(month_bucket=TruncMonth("date"))
        .values("month_bucket", "product__category__category_name")
        .annotate(units=Coalesce(Sum("qty_sold"), 0))
        .order_by("month_bucket", "product__category__category_name")
    )
    month_buckets = []
    month_cursor = monthly_window_start
    while month_cursor <= today.replace(day=1):
        month_buckets.append(month_cursor)
        month_cursor = next_month_start(month_cursor)

    monthly_rows_by_bucket = {month.strftime("%Y-%m"): [] for month in month_buckets}
    for row in monthly_category_rows:
        month_key = row["month_bucket"].strftime("%Y-%m")
        monthly_rows_by_bucket.setdefault(month_key, []).append({
            "category": row["product__category__category_name"] or "Uncategorized",
            "units": int(row["units"] or 0),
        })

    monthly_category_labels = [month.strftime("%b") for month in month_buckets]
    monthly_top_category_datasets = [
        {"label": "Top 1", "data": [], "categories": []},
        {"label": "Top 2", "data": [], "categories": []},
        {"label": "Top 3", "data": [], "categories": []},
    ]
    monthly_top_category_summary = []
    for month in month_buckets:
        month_key = month.strftime("%Y-%m")
        ranked_rows = sorted(
            monthly_rows_by_bucket.get(month_key, []),
            key=lambda item: item["units"],
            reverse=True,
        )[:3]
        monthly_top_category_summary.append({
            "month": month.strftime("%b"),
            "categories": [row["category"] for row in ranked_rows],
        })
        for rank_index in range(3):
            if rank_index < len(ranked_rows):
                monthly_top_category_datasets[rank_index]["data"].append(ranked_rows[rank_index]["units"])
                monthly_top_category_datasets[rank_index]["categories"].append(ranked_rows[rank_index]["category"])
            else:
                monthly_top_category_datasets[rank_index]["data"].append(0)
                monthly_top_category_datasets[rank_index]["categories"].append("")

    weekly_category_rows = list(
        DailyProductSales.objects.filter(date__gte=week_start, date__lte=today)
        .values("product__category__category_name")
        .annotate(units=Coalesce(Sum("qty_sold"), 0))
        .order_by("-units")
    )
    weekly_category_labels = [row["product__category__category_name"] or "Uncategorized" for row in weekly_category_rows]
    weekly_category_values = [int(row["units"] or 0) for row in weekly_category_rows]

    forecast_run = ForecastRun.objects.order_by("-created_at").first()
    forecast_anchor = max(
        (forecast_run.period_start if forecast_run else today),
        today + timedelta(days=1),
    )
    forecast_week_end = forecast_anchor + timedelta(days=6)
    forecast_30_end = forecast_anchor + timedelta(days=29)
    forecast_chart_end = forecast_30_end

    forecast_daily_labels = []
    forecast_daily_units = []
    forecast_next_week_units = Decimal("0.00")
    forecast_next_30_units = Decimal("0.00")
    predicted_next_week = []
    upcoming_closed_days = []
    forecast_category_labels = []
    forecast_category_values = []
    at_risk_products = []
    next_festival = None
    stock_attention_next_week = []
    stock_attention_next_30_days = []
    festival_predicted_units = Decimal("0.00")
    festival_predicted_best_sellers = []
    festival_top_category = ""

    # Helper for building stock-risk rows shown on the dashboard tables.
    def build_stock_attention_rows(forecast_end_date):
        at_risk = []
        forecast_by_sku = {}
        if forecast_run:
            risk_rows = list(
                ForecastDaily.objects.filter(
                    run=forecast_run,
                    forecast_date__gte=forecast_anchor,
                    forecast_date__lte=forecast_end_date,
                )
                .values(
                    "product__sku",
                    "product__product_name",
                    "product__category__category_name",
                    "product__qty_in_stock",
                    "product__reorder_level",
                )
                .annotate(forecast_qty=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
                .order_by("-forecast_qty")
            )
            for row in risk_rows:
                forecast_qty = float(row["forecast_qty"] or 0)
                stock_qty = int(row["product__qty_in_stock"] or 0)
                sku = row["product__sku"]
                forecast_by_sku[sku] = {
                    "forecast_qty": forecast_qty,
                    "gap": round(forecast_qty - stock_qty, 2),
                }
                gap = forecast_by_sku[sku]["gap"]
                if gap <= 0:
                    continue
                at_risk.append({
                    "sku": sku,
                    "product": row["product__product_name"],
                    "category": row["product__category__category_name"] or "Uncategorized",
                    "stock": stock_qty,
                    "forecast_qty": forecast_qty,
                    "gap": gap,
                    "reorder_level": row["product__reorder_level"] or 0,
                })

        rows = []
        row_map = {}

        for product in low_stock_items:
            status = "Out of stock" if int(product.qty_in_stock or 0) == 0 else "Low stock"
            forecast_data = forecast_by_sku.get(product.sku, {})
            gap = forecast_data.get("gap")
            status_tone = "danger" if status == "Out of stock" else "warn"
            if gap is not None and gap > 0:
                if status == "Low stock":
                    status = "Low + at risk"
                    status_tone = "warn"
                elif status == "Out of stock":
                    status = "Out + at risk"
                    status_tone = "danger"
            row = {
                "sku": product.sku,
                "product": product.product_name,
                "category": product.category.category_name if product.category else "Uncategorized",
                "stock": int(product.qty_in_stock or 0),
                "forecast_qty": forecast_data.get("forecast_qty"),
                "gap": gap,
                "status": status,
                "status_tone": status_tone,
                "sort_rank": 0 if status == "Out of stock" else 1,
            }
            row_map[row["sku"]] = row
            rows.append(row)

        for risk in at_risk[:6]:
            existing = row_map.get(risk["sku"])
            if existing:
                existing["forecast_qty"] = risk["forecast_qty"]
                existing["gap"] = risk["gap"]
                if existing["status"] == "Low stock":
                    existing["status"] = "Low + at risk"
                    existing["status_tone"] = "warn"
                elif existing["status"] == "Out of stock":
                    existing["status"] = "Out + at risk"
                    existing["status_tone"] = "danger"
                continue

            row = {
                "sku": risk["sku"],
                "product": risk["product"],
                "category": risk.get("category") or "Uncategorized",
                "stock": risk["stock"],
                "forecast_qty": risk["forecast_qty"],
                "gap": risk["gap"],
                "status": "At risk",
                "status_tone": "warn",
                "sort_rank": 2,
            }
            row_map[row["sku"]] = row
            rows.append(row)

        rows.sort(key=lambda item: (item["sort_rank"], item["stock"], item["product"]))
        return rows, at_risk

    if forecast_run:
        next_festival = (
            FestiveCalendar.objects.filter(end_date__gte=forecast_anchor)
            .order_by("start_date", "festival_name")
            .first()
        )
        if next_festival:
            forecast_chart_end = min(next_festival.end_date, forecast_run.period_end)
        else:
            forecast_chart_end = min(forecast_chart_end, forecast_run.period_end)

        forecast_chart_rows = list(
            ForecastDaily.objects.filter(run=forecast_run, forecast_date__gte=forecast_anchor, forecast_date__lte=forecast_chart_end)
            .values("forecast_date")
            .annotate(total=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
            .order_by("forecast_date")
        )
        forecast_daily_labels = [row["forecast_date"].strftime("%d %b") for row in forecast_chart_rows]
        forecast_daily_units = [float(row["total"]) for row in forecast_chart_rows]
        forecast_next_week_units = (
            ForecastDaily.objects.filter(run=forecast_run, forecast_date__gte=forecast_anchor, forecast_date__lte=forecast_week_end)
            .aggregate(total=Coalesce(Sum("predicted_qty"), Decimal("0.00")))["total"]
        )
        forecast_next_30_units = (
            ForecastDaily.objects.filter(run=forecast_run, forecast_date__gte=forecast_anchor, forecast_date__lte=forecast_30_end)
            .aggregate(total=Coalesce(Sum("predicted_qty"), Decimal("0.00")))["total"]
        )

        predicted_next_week = list(
            ForecastDaily.objects.filter(run=forecast_run, forecast_date__gte=forecast_anchor, forecast_date__lte=forecast_week_end)
            .values("product__sku", "product__product_name", "product__category__category_name")
            .annotate(qty=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
            .order_by("-qty", "product__product_name")[:6]
        )
        upcoming_closed_days = [
            row["forecast_date"].strftime("%d %b %Y")
            for row in forecast_chart_rows
            if row["total"] == 0
        ][:6]
        forecast_category_rows = list(
            ForecastDaily.objects.filter(run=forecast_run, forecast_date__gte=forecast_anchor, forecast_date__lte=forecast_30_end)
            .values("product__category__category_name")
            .annotate(total=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
            .order_by("-total")[:6]
        )
        forecast_category_labels = [row["product__category__category_name"] or "Uncategorized" for row in forecast_category_rows]
        forecast_category_values = [float(row["total"]) for row in forecast_category_rows]
        stock_attention_next_week, at_risk_products = build_stock_attention_rows(forecast_week_end)
        stock_attention_next_30_days, _ = build_stock_attention_rows(forecast_30_end)

        if next_festival:
            festival_forecast_start = max(forecast_anchor, next_festival.start_date)
            festival_forecast_end = next_festival.end_date
            festival_rows = ForecastDaily.objects.filter(
                run=forecast_run,
                forecast_date__gte=festival_forecast_start,
                forecast_date__lte=festival_forecast_end,
            )
            festival_predicted_units = festival_rows.aggregate(
                total=Coalesce(Sum("predicted_qty"), Decimal("0.00"))
            )["total"]
            festival_predicted_best_sellers = list(
                festival_rows
                .values("product__sku", "product__product_name", "product__category__category_name")
                .annotate(qty=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
                .order_by("-qty", "product__product_name")[:3]
            )
            festival_top_category_row = (
                festival_rows
                .values("product__category__category_name")
                .annotate(total=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
                .order_by("-total")
                .first()
            )
            festival_top_category = (
                festival_top_category_row["product__category__category_name"]
                if festival_top_category_row and festival_top_category_row["product__category__category_name"]
                else ""
            )
    else:
        stock_attention_next_week, at_risk_products = build_stock_attention_rows(forecast_week_end)
        stock_attention_next_30_days, _ = build_stock_attention_rows(forecast_30_end)

    context = {
        "since_date": since,
        "today_date": today,
        "range_days": range_days,
        "start_date": since,
        "end_date": today,
        "has_daily_sales": has_daily_sales,
        "forecast_available": bool(forecast_run),
        "forecast_run": forecast_run,
        "forecast_anchor": forecast_anchor,
        "forecast_chart_end": forecast_chart_end,
        "orders_count": orders_count,
        "total_revenue": total_revenue,
        "total_units": total_units,
        "avg_order_value": avg_order_value,
        "avg_basket_size": avg_basket_size,
        "sales_today_count": sales_today_count,
        "sales_week_count": sales_week_count,
        "sales_month_count": sales_month_count,
        "sales_today_revenue": sales_today_revenue,
        "sales_week_revenue": sales_week_revenue,
        "sales_month_revenue": sales_month_revenue,
        "forecast_next_week_units": forecast_next_week_units,
        "forecast_next_30_units": forecast_next_30_units,
        "next_festival": next_festival,
        "total_skus": total_skus,
        "total_units_on_hand": total_units_on_hand,
        "stock_value": stock_value,
        "out_of_stock_count": out_of_stock_count,
        "low_stock_count": low_stock_count,
        "ok_stock_count": ok_stock_count,
        "stock_attention_count": stock_attention_count,
        "low_stock_items": low_stock_items,
        "at_risk_products": at_risk_products,
        "stock_attention_rows": stock_attention_next_week,
        "stock_attention_next_week": stock_attention_next_week,
        "stock_attention_next_30_days": stock_attention_next_30_days,
        "ok_pct": ok_pct,
        "low_pct": low_pct,
        "out_pct": out_pct,
        "best_this_week": best_this_week,
        "best_this_month": best_this_month,
        "best_selected_range": best_selected_range,
        "predicted_next_week": predicted_next_week,
        "upcoming_closed_days": upcoming_closed_days,
        "actual_daily_labels": actual_daily_labels,
        "actual_daily_revenue": actual_daily_revenue,
        "actual_daily_units": actual_daily_units,
        "forecast_daily_labels": forecast_daily_labels,
        "forecast_daily_units": forecast_daily_units,
        "category_current_labels": category_current_labels,
        "category_current_values": category_current_values,
        "monthly_category_labels": monthly_category_labels,
        "monthly_top_category_datasets": monthly_top_category_datasets,
        "monthly_top_category_summary": monthly_top_category_summary,
        "weekly_category_labels": weekly_category_labels,
        "weekly_category_values": weekly_category_values,
        "forecast_category_labels": forecast_category_labels,
        "forecast_category_values": forecast_category_values,
        "festival_predicted_units": festival_predicted_units,
        "festival_predicted_best_sellers": festival_predicted_best_sellers,
        "festival_top_category": festival_top_category,
    }
    return render(request, "retail/dashboard.html", context)
## Assistant API and chat views.
@staff_required
def assistant(request):
    return render(request, "retail/assistant.html")

# Serializes a chat thread for JSON responses.
def _serialize_thread(thread: ChatThread) -> dict:
    return {
        "id": thread.id,
        "title": thread.title,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
    }

# Serializes a chat message for JSON responses.
def _serialize_message(msg: ChatMessage) -> dict:
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "payload": msg.payload_json or {},
        "created_at": msg.created_at.isoformat(),
    }


@staff_required
def assistant_threads(request):
    # Lists existing threads or creates a new thread.
    if request.method == "GET":
        threads = ChatThread.objects.filter(user=request.user, is_archived=False).order_by("-updated_at")[:100]
        return JsonResponse({"ok": True, "threads": [_serialize_thread(t) for t in threads]})

    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        if not title:
            title = "New Chat"
        thread = ChatThread.objects.create(user=request.user, title=title, context_json={})
        return JsonResponse({"ok": True, "thread": _serialize_thread(thread)})

    return JsonResponse({"ok": False, "error": "Method not allowed."}, status=405)


@staff_required
def assistant_thread_detail(request, thread_id: int):
    # Returns one thread with all of its messages.
    thread = get_object_or_404(ChatThread, id=thread_id, user=request.user, is_archived=False)
    messages = thread.messages.order_by("created_at")
    return JsonResponse({"ok": True, "thread": _serialize_thread(thread), "messages": [_serialize_message(m) for m in messages]})


@staff_required
@require_POST
def assistant_thread_message(request, thread_id: int):
    # Appends a user message to a thread and stores the assistant reply.
    thread = get_object_or_404(ChatThread, id=thread_id, user=request.user, is_archived=False)
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    query = (body.get("query") or "").strip()
    if not query:
        return JsonResponse({"ok": False, "error": "Query is required."}, status=400)

    ChatMessage.objects.create(thread=thread, role=ChatMessage.Role.USER, content=query, payload_json={})
    memory = dict(thread.context_json or {})
    result = process_query(query, memory)

    assistant_text = result.get("answer_text") or result.get("summary") or "No response."
    ChatMessage.objects.create(thread=thread, role=ChatMessage.Role.ASSISTANT, content=assistant_text, payload_json=result)

    if thread.title == "New Chat":
        thread.title = (query[:70] + "...") if len(query) > 70 else query
    thread.context_json = memory
    thread.save(update_fields=["title", "context_json", "updated_at"])

    return JsonResponse({"ok": True, "thread": _serialize_thread(thread), "message": result})


@staff_required
@require_POST
def assistant_query(request):
    # Handles the older assistant endpoint with optional session-backed memory.
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    query = (body.get("query") or "").strip()
    if not query:
        return JsonResponse({"ok": False, "error": "Query is required."}, status=400)

    thread_id = body.get("thread_id")
    if thread_id:
        thread = ChatThread.objects.filter(id=thread_id, user=request.user, is_archived=False).first()
        if thread:
            ChatMessage.objects.create(thread=thread, role=ChatMessage.Role.USER, content=query, payload_json={})
            memory = dict(thread.context_json or {})
            result = process_query(query, memory)
            ChatMessage.objects.create(
                thread=thread,
                role=ChatMessage.Role.ASSISTANT,
                content=result.get("answer_text") or result.get("summary") or "No response.",
                payload_json=result,
            )
            if thread.title == "New Chat":
                thread.title = (query[:70] + "...") if len(query) > 70 else query
            thread.context_json = memory
            thread.save(update_fields=["title", "context_json", "updated_at"])
            return JsonResponse({"ok": True, "thread": _serialize_thread(thread), **result})

    result = process_query(query, request.session)
    request.session.modified = True
    return JsonResponse({"ok": True, **result})

# Two-factor verification and trusted-device views.
def two_factor_verify(request):
    # Verifies the emailed login code and optionally remembers the device.
    user_id = request.session.get("pending_2fa_user_id")
    code = request.session.get("pending_2fa_code")
    exp = request.session.get("pending_2fa_exp")
    remember = request.session.get("pending_2fa_remember", False)

    if not (user_id and code and exp):
        return redirect("admin_login")

    now_ts = int(timezone.now().timestamp())
    expired = now_ts > int(exp)

    if request.GET.get("resend") == "1":
        user = get_object_or_404(get_user_model(), id=user_id)
        new_code = f"{secrets.randbelow(1000000):06d}"
        request.session["pending_2fa_code"] = new_code
        request.session["pending_2fa_exp"] = now_ts + 600
        try:
            send_mail(
                subject="Your login verification code",
                message=f"Your verification code is {new_code}. It expires in 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception:
            pass
        expired = False

    form = TwoFactorForm(request.POST or None)
    error = None

    if request.method == "POST" and form.is_valid():
        entered = form.cleaned_data["code"]
        if expired:
            error = "Code expired. Please request a new one."
        elif entered != str(code):
            error = "Invalid code. Try again."
        else:
            user = get_object_or_404(get_user_model(), id=user_id)
            login(request, user)
            request.session.pop("pending_2fa_user_id", None)
            request.session.pop("pending_2fa_code", None)
            request.session.pop("pending_2fa_exp", None)
            request.session.pop("pending_2fa_remember", None)

            if remember:
                request.session.set_expiry(60 * 60 * 24 * TRUSTED_DEVICE_DAYS)
            else:
                request.session.set_expiry(0)

            response = redirect("home")
            if remember:
                _create_trusted_device(response, request, user)
            return response

    context = {
        "form": form,
        "expired": expired,
        "error": error,
    }
    return render(request, "registration/two_factor_verify.html", context)


@staff_required
def trusted_devices(request):
    # Lists and removes trusted devices for the current user.
    if request.method == "POST":
        device_id = request.POST.get("device_id")
        if device_id:
            TrustedDevice.objects.filter(user=request.user, device_id=device_id).delete()
            raw = request.COOKIES.get(TRUSTED_DEVICE_COOKIE, "")
            if raw.startswith(f"{device_id}:"):
                response = redirect("trusted_devices")
                response.delete_cookie(TRUSTED_DEVICE_COOKIE)
                return response
        return redirect("trusted_devices")

    devices = (
        TrustedDevice.objects
        .filter(user=request.user)
        .order_by("-last_used_at", "-created_at")
    )
    context = {
        "devices": devices,
        "now": timezone.now(),
    }
    return render(request, "registration/trusted_devices.html", context)
