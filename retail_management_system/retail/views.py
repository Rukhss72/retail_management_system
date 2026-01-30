#from django.contrib.auth.decorators import login_required
#from django.db.models import F, Value, IntegerField, Case, When, Sum, Q
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib.auth.decorators import login_required

import secrets
from django.db import models

from django.db import transaction
#from urllib3 import request
from .models import Product, StockMovement, Category, SKUCounter, Supplier, ReorderRequest, ReorderRequestItem, PurchaseOrder, PurchaseOrderItem, ProductSupplier
from .forms import RoleAuthenticationForm, StockInForm, StockOutForm, AdjustmentForm, HistoryFilterForm, ProductCreateForm, SupplierForm, PurchaseOrderCreateForm

from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.urls import reverse_lazy

from django.contrib.auth.decorators import user_passes_test

from django.core.exceptions import ValidationError
from .services import apply_stock_movement

from django.core.paginator import Paginator
from .models import Product, StockMovement

from datetime import datetime, time, timedelta

from .permissions import is_admin, is_staff_or_admin, is_staff_user
from .services import get_reorder_queryset, compute_reorder_row


#sales import
import json
from django.contrib import messages
from .models import Sale
from .services import create_sale

from decimal import Decimal, InvalidOperation


# Create your views here.

# views.py
class AdminLoginView(LoginView):
    template_name = "registration/admin_login.html"
    authentication_form = RoleAuthenticationForm
    redirect_authenticated_user = True  # IMPORTANT: prevents loop

    def form_valid(self, form):
        response = super().form_valid(form)
        user = self.request.user

        if not (is_admin(user) or is_staff_user(user)):          # only owner allowed
            logout(self.request)
            return redirect("admin_login")

        return response

    def get_success_url(self):
        return reverse_lazy("home") # or "home/dashboard"
    

#def is_admin(user):
#    return user.is_authenticated and (user.is_superuser or user.is_staff)

admin_required = user_passes_test(is_admin, login_url="/admin-login/")
staff_required = user_passes_test(is_staff_or_admin, login_url="/admin-login/")

#SKUGenerator
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

        # extra Safety check (should be unique already)
        if Product.objects.filter(sku=sku).exists():
            raise ValueError("SKU collision detected (unexpected).")

        return sku

@staff_required
#@admin_required
def home(request):
    # ===== STOCK COUNTS (match your Product model fields) =====
    low_stock_count = Product.objects.filter(
        qty_in_stock__gt=0,
        qty_in_stock__lte=models.F("reorder_level"),
        is_active=True
    ).count()

    out_of_stock_count = Product.objects.filter(
        qty_in_stock=0,
        is_active=True
    ).count()

    # ===== REORDERS PENDING (match your ReorderRequest constants if present) =====
    # If you have STATUS_SUBMITTED, use that as "pending admin decision"
    if hasattr(ReorderRequest, "STATUS_SUBMITTED"):
        reorders_pending_count = ReorderRequest.objects.filter(status=ReorderRequest.STATUS_SUBMITTED).count()
    else:
        # fallback
        reorders_pending_count = ReorderRequest.objects.filter(status__in=["SUBMITTED", "PENDING", "Pending"]).count()

    # ===== RECENT ACTIVITY (use StockMovement.movement_date + movement_type) =====
    recent_activity = []

    movements = (
        StockMovement.objects
        .select_related("product")
        .order_by("-movement_date")[:5]
    )

    for m in movements:
        # map movement_type => dot color
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

        # label for UI
        type_label = {
            "IN": "Stock In",
            "OUT": "Stock Out",
            "ADJUST": "Adjustment",
            "SALE": "Sale",
            "PO": "PO Created",
        }.get(m.movement_type, m.movement_type)

        t = m.movement_date
        t_str = timezone.localtime(t).strftime("%I:%M %p") if t else "—"

        item_text = f"{m.product.product_name}"
        if m.product.sku:
            item_text += f" (SKU: {m.product.sku})"

        recent_activity.append({
            "kind": kind,
            "type": type_label,
            "item": item_text,
            "time": t_str,
        })

    now = timezone.localtime()

    context = {
        "store_name": "Nasnmay Boutique",
        "today_date": now.strftime("%A, %d %B %Y"),
        "now_time": now.strftime("%I:%M %p"),

        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
        "reorders_pending_count": reorders_pending_count,

        "recent_activity": recent_activity,
    }
    return render(request, "retail/home.html", context)


@staff_required
#@login_required
#@admin_required
def inventory(request):
    # ---------- Filters (GET) ----------
    categories = Category.objects.order_by("category_name")

    category_id = request.GET.get("category", "")
    section = request.GET.get("section", "")
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "")
    tab = request.GET.get("tab", "products")

    # Pagination params (two independent paginations on one page)
    ppage = request.GET.get("ppage", 1)  # products page
    hpage = request.GET.get("hpage", 1)  # history page

    # Build base querystring for links (exclude tab + pagination params)
    qs = request.GET.copy()
    for k in ("tab", "ppage", "hpage"):
        qs.pop(k, None)
    base_qs = qs.urlencode()

    # ---------- Products queryset ----------
    products_qs = Product.objects.filter(is_active=True)

    if category_id:
        products_qs = products_qs.filter(category_id=category_id)

    if section:
        products_qs = products_qs.filter(section=section)

    if q:
        products_qs = products_qs.filter(
            Q(product_name__icontains=q) | Q(sku__icontains=q)
        )

    products_qs = products_qs.select_related("category").order_by("product_name")
    all_products = Product.objects.filter(is_active=True).select_related("category").order_by("product_name")

    # Compute status in python + counts
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

        # Apply product-list status filter (OK/Low/Out)
        if status and p.stock_status != status:
            continue

        products.append(p)

    # Paginate PRODUCTS (paginate the filtered python list)
    product_paginator = Paginator(products, 8)  # change page size if you want
    products_page = product_paginator.get_page(ppage)

    # ---------- Handle actions (POST) ----------
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
            adjust_form = AdjustmentForm(request.POST, prefix="adj")
            if adjust_form.is_valid():
                cd = adjust_form.cleaned_data
                with transaction.atomic():
                    product = Product.objects.select_for_update().get(pk=cd["product"].pk)

                    target = int(cd["set_stock_to"])
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
                        product.save(update_fields=["qty_in_stock", "updated_at"])

                return redirect("inventory")

        elif action == "CREATE_PRODUCT":
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

                    # ----------------------------
                    # CASE 1: Duplicate product exists
                    # ----------------------------
                    if existing:
                        add_opening = product_create_form.cleaned_data.get("add_opening_stock", False)
                        opening_qty = product_create_form.cleaned_data.get("opening_qty", 0)

                        # Add opening stock to EXISTING product (optional)
                        if add_opening and opening_qty and opening_qty > 0:
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

                        # Set / update PRIMARY supplier for EXISTING product (optional)
                        supplier = product_create_form.cleaned_data.get("primary_supplier")
                        if supplier:
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

                    # ----------------------------
                    # CASE 2: New product
                    # ----------------------------
                    else:
                        # Generate and save new product
                        new_obj.sku = generate_sku_for(new_obj.section, new_obj.category)
                        new_obj.save()

                        # bind instance back to form (so save_with_opening_stock uses correct product)
                        product_create_form.instance = new_obj

                        # Set / update PRIMARY supplier for NEW product (optional)
                        supplier = product_create_form.cleaned_data.get("primary_supplier")
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

                        # Add opening stock to NEW product (optional)
                        product_create_form.save_with_opening_stock(created_by=request.user)

                return redirect("inventory")


    # ---------- History panel (GET filters + pagination) ----------
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

    # IMPORTANT: no slicing here if you want proper pagination
    history_qs = history_qs.order_by("-movement_date")

    movement_paginator = Paginator(history_qs, 10)  # change page size if you want
    history_page = movement_paginator.get_page(hpage)

    context = {
        "categories": categories,
        "selected_category": category_id,
        "section": section,
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
    }
    return render(request, "retail/inventory.html", context)



def generate_sku():
    return f"SKU-{secrets.token_hex(4).upper()}"

def generate_unique_sku():
    while True:
        sku = generate_sku()
        if not Product.objects.filter(sku=sku).exists():
            return sku
'''
remove this
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

@staff_required
def sales(request):
    tab = (request.GET.get("tab") or "new").strip()  # new | history | details

    # Product filters (for New Sale product picker)
    q = (request.GET.get("q") or "").strip()
    category_id = (request.GET.get("category") or "").strip()

    products_qs = Product.objects.filter(is_active=True).select_related("category").order_by("product_name")
    categories = Category.objects.order_by("category_name")
    section = (request.GET.get("section") or "").strip()

    if q:
        products_qs = products_qs.filter(Q(product_name__icontains=q) | Q(sku__icontains=q))
    if category_id:
        products_qs = products_qs.filter(category_id=category_id)
    if section:
        products_qs = products_qs.filter(section=section)

    # Save Sale (POST)
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

    # History tab data
    sales_qs = Sale.objects.select_related("cashier").order_by("-created_at")
    page_obj = Paginator(sales_qs, 10).get_page(request.GET.get("page"))

    # Details tab data
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

        "section_choices": Product._meta.get_field("section").choices,
        "selected_section": section,

    })


#@login_required
@admin_required
def suppliers(request):
    tab = request.GET.get("tab", "suppliers")
    if tab not in ("suppliers", "purchase-orders", "reorders"):
        tab = "suppliers"

    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")
    page_number = request.GET.get("page", 1)

    supplier_form = SupplierForm()
    po_form = PurchaseOrderCreateForm()

    # ----------------------------
    # POST HANDLERS (ALL here)
    # ----------------------------
    if request.method == "POST":
        form_type = request.POST.get("form_type", "")

        # CREATE SUPPLIER
        if form_type == "create_supplier":
            supplier_form = SupplierForm(request.POST)
            if supplier_form.is_valid():
                supplier_form.save()
                messages.success(request, "Supplier created successfully.")
                return redirect(f"{request.path}?tab=suppliers")
            messages.error(request, "Please fix the errors in the supplier form.")
            tab = "suppliers"

        # UPDATE SUPPLIER
        elif form_type == "update_supplier":
            supplier_id = request.POST.get("supplier_id")
            s = get_object_or_404(Supplier, pk=supplier_id)

            s.supplier_name = request.POST.get("supplier_name", "").strip()
            s.email = request.POST.get("email", "").strip()
            s.phone = request.POST.get("phone", "").strip()

            lead = request.POST.get("lead_time_days")
            s.lead_time_days = int(lead) if lead not in (None, "") else 0

            s.notes = request.POST.get("notes", "").strip()
            s.is_active = (request.POST.get("is_active") == "on")

            if not s.supplier_name:
                messages.error(request, "Company Name is required.")
                return redirect(f"{request.path}?tab=suppliers")

            s.save()
            messages.success(request, "Supplier updated.")
            return redirect(f"{request.path}?tab=suppliers")

        # DELETE SUPPLIER
        elif form_type == "delete_supplier":
            supplier_id = request.POST.get("supplier_id")
            s = get_object_or_404(Supplier, pk=supplier_id)
            s.delete()
            messages.success(request, "Supplier deleted.")
            return redirect(f"{request.path}?tab=suppliers")

                # CREATE PO
        elif form_type == "create_po":
            po_form = PurchaseOrderCreateForm(request.POST)
            if po_form.is_valid():
                po = po_form.save(commit=False)
                po.created_by = request.user
                # Ensure Draft unless user picks otherwise (optional)
                if not po.status:
                    po.status = PurchaseOrder.STATUS_DRAFT
                po.save()

                messages.success(request, f"PO created: {po}")
                return redirect(f"{request.path}?tab=purchase-orders")

            messages.error(request, "Please fix the errors in the purchase order form.")
            tab = "purchase-orders"


                # UPDATE PO (modal edit)
        elif form_type == "update_po":
            po_id = request.POST.get("po_id")
            po = get_object_or_404(PurchaseOrder, pk=po_id)

            # Basic fields
            po.po_number = request.POST.get("po_number", "").strip()
            po.notes = request.POST.get("notes", "").strip()

            # supplier
            supplier_id = request.POST.get("supplier_id")
            if supplier_id:
                po.supplier = get_object_or_404(Supplier, pk=int(supplier_id))

            # dates (Django can parse YYYY-MM-DD; if blank -> None)
            po.order_date = request.POST.get("order_date") or None
            po.delivery_date = request.POST.get("delivery_date") or None

            # amount
            amt = request.POST.get("total_amount", "").strip()
            try:
                po.total_amount = Decimal(amt) if amt else Decimal("0.00")
            except InvalidOperation:
                po.total_amount = Decimal("0.00")

            # status
            status_val = request.POST.get("status")
            if status_val in dict(PurchaseOrder.STATUS_CHOICES):
                po.status = status_val

            if not po.po_number:
                messages.error(request, "PO Number is required.")
                return redirect(f"{request.path}?tab=purchase-orders")

            # add hard check: only DRAFT can be edited            
            if po.status != PurchaseOrder.STATUS_DRAFT:
                messages.error(request, "Only DRAFT purchase orders can be edited.")
                return redirect(f"{request.path}?tab=purchase-orders")

            po.save()
            messages.success(request, "Purchase order updated.")
            return redirect(f"{request.path}?tab=purchase-orders")


        # DELETE PO
        elif form_type == "delete_po":
            po_id = request.POST.get("po_id")
            po = get_object_or_404(PurchaseOrder, pk=po_id)

            # hard check: only DRAFT can be deleted
            if po.status != PurchaseOrder.STATUS_DRAFT:
                messages.error(request, "Only DRAFT purchase orders can be deleted.")
                return redirect(f"{request.path}?tab=purchase-orders")

            po.delete()
            messages.success(request, "Purchase order deleted.")
            return redirect(f"{request.path}?tab=purchase-orders")
        
        
        # CREATE PO FROM RR (set RR to APPROVED because PO has been created (converted))
        elif form_type == "create_po_from_rr":
            rr_id = request.POST.get("rr_id")
            rr = get_object_or_404(ReorderRequest, pk=rr_id)

            if rr.status != ReorderRequest.STATUS_DRAFT:
                messages.error(request, "Only DRAFT reorder requests can be converted to a PO.")
                return redirect(f"{request.path}?tab=purchase-orders")

            if not rr.supplier_id:
                messages.error(request, "This reorder request has no supplier.")
                return redirect(f"{request.path}?tab=purchase-orders")

            # prevent duplicates (if already converted)
            if rr.purchase_orders.exists():
                messages.error(request, "A PO was already created from this request.")
                return redirect(f"{request.path}?tab=purchase-orders")

            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    supplier_id=rr.supplier_id,
                    status=PurchaseOrder.STATUS_DRAFT,
                    created_by=request.user,
                    reorder_request=rr,
                )

                items = rr.items.select_related("product")
                for it in items:
                    PurchaseOrderItem.objects.create(
                        po=po,
                        product=it.product,
                        qty_ordered=it.qty_requested,
                        unit_cost=None,
                    )

                rr.status = ReorderRequest.STATUS_APPROVED
                rr.decided_at = timezone.now()
                rr.decided_by = request.user
                rr.save(update_fields=["status", "decided_at", "decided_by"])

            messages.success(request, f"Draft PO created from RR-{rr.id}: {po}")
            return redirect(f"{request.path}?tab=purchase-orders")




    # ----------------------------
    # BASE CONTEXT
    # ----------------------------
    context = {
        "tab": tab,
        "q": q,
        "status": status,
        "is_admin": is_admin(request.user),
        "form": supplier_form,
        "po_form": po_form,
    }

    context["suppliers_all"] = Supplier.objects.filter(is_active=True).order_by("supplier_name")

    # ----------------------------
    # TAB: SUPPLIERS
    # ----------------------------
    if tab == "suppliers":
        suppliers_qs = Supplier.objects.all().order_by("-created_at")

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

    # ----------------------------
    # TAB: PURCHASE ORDERS
    # ----------------------------
    if tab == "purchase-orders":
        context["rr_drafts"] = ReorderRequest.objects.select_related("supplier").filter(
            status=ReorderRequest.STATUS_DRAFT
        ).order_by("-created_at")[:50]

        po_qs = PurchaseOrder.objects.select_related("supplier").prefetch_related("items").all().order_by("-created_at")

        if q:
            po_qs = po_qs.filter(
                Q(po_number__icontains=q) |
                Q(supplier__supplier_name__icontains=q)
            )

        if status != "all":
            po_qs = po_qs.filter(status=status)

        paginator = Paginator(po_qs, 10)
        context["purchase_orders"] = paginator.get_page(page_number)
        return render(request, "retail/suppliers.html", context)


    # ----------------------------
    # TAB: REORDERS
    # ----------------------------
    qs, since = get_reorder_queryset()

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
            "qty_in_stock": p.qty_in_stock,
            "reorder_level": p.reorder_level,
            **extra,
        })

    if is_admin(request.user):
        requests_qs = ReorderRequest.objects.all().order_by("-created_at")[:50]
    else:
        requests_qs = ReorderRequest.objects.filter(created_by=request.user).order_by("-created_at")[:50]

    context.update({
        "grouped": grouped,
        "since": since,
        "safety_stock": 5,
        "requests": requests_qs,
    })
    return render(request, "retail/suppliers.html", context)

# Create draft request (staff or admin)
#@login_required
def reorder_request_create_draft(request):
    if request.method != "POST":
        return redirect("suppliers")

    product_ids = request.POST.getlist("product_ids")
    if not product_ids:
        messages.error(request, "Select at least one item to create a request.")
        return redirect("/suppliers/?tab=reorders")

    # group selected items by supplier_id (1 RR per supplier)
    by_supplier = {}

    for pid in product_ids:
        supplier_id = request.POST.get(f"supplier_{pid}")
        qty = int(request.POST.get(f"qty_{pid}", "0") or 0)
        suggested = int(request.POST.get(f"suggested_{pid}", "0") or 0)

        if qty <= 0:
            continue

        if not supplier_id:
            # enforce 1-supplier RR; no supplier means cannot draft correctly
            messages.error(request, "Some selected items have no supplier assigned. Please set a primary supplier.")
            return redirect("/suppliers/?tab=reorders")

        by_supplier.setdefault(int(supplier_id), []).append({
            "pid": int(pid),
            "qty": qty,
            "suggested": suggested,
        })

    if not by_supplier:
        messages.error(request, "All selected items had quantity 0. Please enter a quantity.")
        return redirect("/suppliers/?tab=reorders")

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
    return redirect("/suppliers/?tab=reorders")


# Submit request (only owner of draft)
#@login_required
def reorder_request_submit(request, rr_id):
    if request.method != "POST":
        return redirect("/suppliers/?tab=reorders")
    
    rr = get_object_or_404(ReorderRequest, id=rr_id, created_by=request.user)

    if rr.status != ReorderRequest.STATUS_DRAFT:
        messages.error(request, "Only draft requests can be submitted.")
        return redirect("/suppliers/?tab=reorders")

    rr.status = ReorderRequest.STATUS_SUBMITTED
    rr.submitted_at = timezone.now()
    rr.save(update_fields=["status", "submitted_at"])

    messages.success(request, f"RR-{rr.id} submitted to admin.")
    return redirect("/suppliers/?tab=reorders")

#Approve request (admin only) → creates Draft PO(s)
#@login_required
def reorder_request_approve(request, rr_id):
    if request.method != "POST":
        return redirect("/suppliers/?tab=reorders")

    if not is_admin(request.user):
        messages.error(request, "Not authorized.")
        return redirect("/suppliers/?tab=reorders")

    rr = get_object_or_404(ReorderRequest, id=rr_id)

    if rr.status != ReorderRequest.STATUS_SUBMITTED:
        messages.error(request, "Only submitted requests can be approved.")
        return redirect("/suppliers/?tab=reorders")

    items = rr.items.select_related("product", "supplier")

    with transaction.atomic():
        by_supplier = {}
        for it in items:
            # If supplier missing, try primary supplier
            if not it.supplier_id:
                link = it.product.supplier_links.filter(is_primary=True).select_related("supplier").first()
                if link:
                    it.supplier = link.supplier
                    it.save(update_fields=["supplier"])

            if it.supplier_id:
                by_supplier.setdefault(it.supplier_id, []).append(it)

        for supplier_id, its in by_supplier.items():
            po = PurchaseOrder.objects.create(
                supplier_id=supplier_id,
                status=PurchaseOrder.STATUS_DRAFT,
                created_by=request.user,
            )
            for it in its:
                PurchaseOrderItem.objects.create(
                    po=po,
                    product=it.product,
                    qty_ordered=it.qty_requested,
                    unit_cost=None,
                )

        rr.status = ReorderRequest.STATUS_APPROVED
        rr.decided_at = timezone.now()
        rr.decided_by = request.user
        rr.save(update_fields=["status", "decided_at", "decided_by"])

    messages.success(request, f"RR-{rr.id} approved. Draft PO(s) created.")
    return redirect("/suppliers/?tab=reorders")

# Reject request (admin only)
#@login_required
def reorder_request_reject(request, rr_id):
    if request.method != "POST":
        return redirect("/suppliers/?tab=reorders")

    if not is_admin(request.user):
        messages.error(request, "Not authorized.")
        return redirect("/suppliers/?tab=reorders")

    rr = get_object_or_404(ReorderRequest, id=rr_id)

    if rr.status != ReorderRequest.STATUS_SUBMITTED:
        messages.error(request, "Only submitted requests can be rejected.")
        return redirect("/suppliers/?tab=reorders")

    rr.status = ReorderRequest.STATUS_REJECTED
    rr.decided_at = timezone.now()
    rr.decided_by = request.user
    rr.decision_note = request.POST.get("decision_note", "").strip()
    rr.save(update_fields=["status", "decided_at", "decided_by", "decision_note"])

    messages.success(request, f"RR-{rr.id} rejected.")
    return redirect("/suppliers/?tab=reorders")


@admin_required
def dashboard(request):
    return render(request, "retail/dashboard.html")

@admin_required
def assistant(request):
    return render(request, "retail/assistant.html")
