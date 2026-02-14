from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from .models import Product, PurchaseOrder, StockMovement, Category,  Supplier, ProductSupplier
from decimal import Decimal


class RoleAuthenticationForm(AuthenticationForm):
    ROLE_CHOICES = (
        ("owner", "Owner"),
        ("staff", "Staff"),
    )

    role = forms.ChoiceField(choices=ROLE_CHOICES)
    remember_me = forms.BooleanField(required=False, initial=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["role"].widget.attrs.update({
            "class": "form-control role-select",
        })
        self.fields["username"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Enter your username",
        })
        self.fields["password"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Enter your password",
        })
        self.fields["remember_me"].widget.attrs.update({
            "class": "remember-input",
        })


class TwoFactorForm(forms.Form):
    code = forms.CharField(max_length=6, min_length=6)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].widget.attrs.update({
            "class": "input",
            "placeholder": "------",
            "inputmode": "numeric",
            "autocomplete": "one-time-code",
        })

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        selected_role = self.cleaned_data.get("role") # must be "owner" or "staff"

        # Ensure user has a profile (extra safety)
        if not hasattr(user, "profile"):
            raise ValidationError("Profile missing. Contact admin.", code="no_profile")
        
        if user.profile.role != selected_role:
            raise ValidationError("Role mismatch.", code="role_mismatch")
        
        # Admin: must be superuser (strict)
        #if selected_role == "admin" and not user.is_superuser:
         #   raise ValidationError("This account is not an Admin.", code="not_admin")

        # Staff: must be staff (strict)
        if selected_role == "staff" and not user.is_staff:
            raise ValidationError("This account is not Staff.", code="not_staff")

        # If you want Staff to exclude Admins, use this instead:
        # if selected_role == "staff" and (not user.is_staff or user.is_superuser):
        #     raise ValidationError("This account is not Staff.", code="not_staff")


class ProductCreateForm(forms.ModelForm):
    """
    Create Product (master data) + optional opening stock transaction.

    NOTE:
    - This form does NOT expose sku (it should be auto-generated in the view).
    - "Add opening stock now" is NOT stored in Product; it creates a StockMovement(IN).
    """

    add_opening_stock = forms.BooleanField(
        required=False,
        initial=False,
        label="Add opening stock now",
    )
    opening_qty = forms.IntegerField(
        required=False,
        min_value=0,
        label="Opening quantity",
    )
    opening_reference = forms.CharField(
        required=False,
        max_length=60,
        label="Reference (optional)",
    )
    opening_comments = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Comments (optional)",
    )

    primary_supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.filter(is_active=True).order_by("supplier_name"),
        required=False,
        empty_label="-- Select primary supplier (optional) --",
        label="Primary Supplier",
    )

    supplier_lead_time_days = forms.IntegerField(
        required=False,
        min_value=0,
        label="Lead time override (days)",
    )

    last_unit_cost = forms.DecimalField(
        required=False,
        min_value=Decimal("0.00"),
        decimal_places=2,
        max_digits=12,
        label="Last unit cost (optional)",
    )

    class Meta:
        model = Product
        # If your model field is still named `gender`, replace "section" with "gender"
        fields = [
            "product_name",
            "category",
            "section",
            "selling_price",
            "reorder_level",
            "is_active",
        ]
        widgets = {
            "product_name": forms.TextInput(attrs={"placeholder": "e.g., Cotton T-Shirt"}),
            "selling_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "reorder_level": forms.NumberInput(attrs={"min": "0"}),
        }

    def clean(self):
        cleaned = super().clean()

        add_opening = cleaned.get("add_opening_stock")
        opening_qty = cleaned.get("opening_qty")

        if add_opening:
            if opening_qty is None:
                self.add_error("opening_qty", "Enter an opening quantity.")
            elif opening_qty <= 0:
                self.add_error("opening_qty", "Opening quantity must be greater than 0.")
        else:
            # Normalize optional fields when not used
            cleaned["opening_qty"] = None
            cleaned["opening_reference"] = ""
            cleaned["opening_comments"] = ""
        
        if not cleaned.get("primary_supplier"):
            cleaned["supplier_lead_time_days"] = None
            cleaned["last_unit_cost"] = None


        return cleaned

    def save_with_opening_stock(self, *, created_by):
        """
         Assumes the Product is already saved by the caller/view (sku already set).
         If 'add_opening_stock' is checked, creates a StockMovement(IN) AND updates Product.qty_in_stock.
        """
        product = self.instance  # use the already-saved product from the view

        if not self.cleaned_data.get("add_opening_stock"):
            return product

        qty = int(self.cleaned_data.get("opening_qty") or 0)
        if qty <= 0:
            return product

        with transaction.atomic():
            # lock product row to avoid race conditions
            product_locked = Product.objects.select_for_update().get(pk=product.pk)

            StockMovement.objects.create(
                product=product_locked,
                movement_type="IN",
                qty_moved=qty,
                reference=self.cleaned_data.get("opening_reference") or None,
                comments=self.cleaned_data.get("opening_comments") or None,
                reason="Opening Stock",
                created_by=created_by,
            )

            product_locked.qty_in_stock += qty
            product_locked.save(update_fields=["qty_in_stock", "updated_at"])

        supplier = self.cleaned_data.get("primary_supplier")
        if supplier:
            ProductSupplier.objects.filter(product=product).update(is_primary=False)

            ProductSupplier.objects.update_or_create(
                product=product,
                supplier=supplier,
                defaults={
                    "is_primary": True,
                    "lead_time_days": self.cleaned_data.get("supplier_lead_time_days") or None,
                    "last_unit_cost": self.cleaned_data.get("last_unit_cost") or None,
                }
            )

        return product

class StockInForm(forms.Form):
    category = forms.ModelChoiceField(
        queryset=Category.objects.order_by("category_name"),
        required=False,
        empty_label="All categories",
    )
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True).select_related("category").order_by("product_name"),
        required=True,
        empty_label="---------",
    )
    qty = forms.IntegerField(min_value=1)
    reference = forms.CharField(required=False)
    comments = forms.CharField(widget=forms.Textarea, required=False)


class StockOutForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True).select_related("category").order_by("product_name"),
        required=True,
        empty_label="---------",
    )
    qty = forms.IntegerField(min_value=1)
    reason = forms.CharField(max_length=120)
    reference = forms.CharField(required=False, max_length=60)
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

   
class AdjustmentForm(forms.Form):
    """
    Set stock to a target level (like your UI idea).
    We convert it into an ADJUST movement (increase/decrease difference).
    """
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True).select_related("category").order_by("product_name"),
        required=True,
        empty_label="---------",
    )
    set_stock_to = forms.IntegerField(min_value=0)
    reason = forms.CharField(max_length=120)
    reference = forms.CharField(required=False, max_length=60)
    comments = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class HistoryFilterForm(forms.Form):
    from_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    to_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    movement_type = forms.ChoiceField(
        required=False,
        choices=[("", "All")] + list(StockMovement.MovementType.choices),
    )
    product = forms.ModelChoiceField(
        required=False,
        queryset=Product.objects.filter(is_active=True).select_related("category").order_by("product_name"),
        empty_label="All products",
    )


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            "supplier_name",
            "contact_person",
            "email",
            "phone",
            "address",
            "products_services",
            "lead_time_days",
            "is_active",
            "notes",
        ]
        widgets = {
            "supplier_name": forms.TextInput(attrs={
                "placeholder": "Company name",
            }),
            "contact_person": forms.TextInput(attrs={
                "placeholder": "Contact person",
            }),
            "email": forms.EmailInput(attrs={
                "placeholder": "Email address",
            }),
            "phone": forms.TextInput(attrs={
                "placeholder": "Phone number",
            }),
            "address": forms.Textarea(attrs={
                "placeholder": "Street, city, country…",
                "rows": 2,
            }),
            "products_services": forms.Textarea(attrs={
                "placeholder": "e.g. Fabrics, accessories, delivery…",
                "rows": 2,
            }),
            "lead_time_days": forms.NumberInput(attrs={
                "min": 0,
            }),
            "notes": forms.Textarea(attrs={
                "placeholder": "Internal notes about this supplier…",
                "rows": 2,
                "class": "ta-notes",
            }),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, f in self.fields.items():
            # keep checkbox clean
            if name == "is_active":
                continue
            existing = f.widget.attrs.get("class", "")
            f.widget.attrs["class"] = (existing + " scm-input").strip()


class PurchaseOrderCreateForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            "po_number",
            "supplier",
            "order_date",
            "delivery_date",
            "total_amount",
            "status",
            "notes",
        ]
        widgets = {
            "po_number": forms.TextInput(attrs={"placeholder": "PO-2026-001"}),
            "order_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_date": forms.DateInput(attrs={"type": "date"}),
            "total_amount": forms.NumberInput(attrs={"placeholder": "0.00", "min": "0", "step": "0.01"}),
            "notes": forms.Textarea(attrs={"rows": 2, "placeholder": "Notes (optional)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("supplier_name")
