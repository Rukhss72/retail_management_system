from django.urls import path
from django.contrib.auth.views import LogoutView
from . import views
from .views import home, inventory, sales, dashboard, assistant, suppliers, AdminLoginView, two_factor_verify, trusted_devices
from django.contrib.auth import views as auth_views

urlpatterns = [
   # path("", AdminLoginView.as_view(), name="admin_login"),   # default = login
   # path("admin-login/", AdminLoginView.as_view(), name="admin_login"),
   # path("home/", home, name="home"),
    path('home/', views.home, name='home'),    
    path("inventory/", views.inventory, name="inventory"),
    path("sales/", views.sales, name="sales"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("assistant/", views.assistant, name="assistant"),
    path("assistant/query/", views.assistant_query, name="assistant_query"),
    path("assistant/threads/", views.assistant_threads, name="assistant_threads"),
    path("assistant/threads/<int:thread_id>/", views.assistant_thread_detail, name="assistant_thread_detail"),
    path("assistant/threads/<int:thread_id>/messages/", views.assistant_thread_message, name="assistant_thread_message"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("admin-login/verify/", two_factor_verify, name="two_factor_verify"),
    path("admin-login/trusted-devices/", trusted_devices, name="trusted_devices"),


     # Admin login/logout page inside your app
    path("logout/", auth_views.LogoutView.as_view(next_page="admin_login"), name="logout"),

    path("reorder-request/draft/", views.reorder_request_create_draft, name="reorder_request_create_draft"),
    path("reorder-request/<int:rr_id>/submit/", views.reorder_request_submit, name="reorder_request_submit"),
    path("reorder-request/<int:rr_id>/approve/", views.reorder_request_approve, name="reorder_request_approve"),
    path("reorder-request/<int:rr_id>/reject/", views.reorder_request_reject, name="reorder_request_reject"),


]
