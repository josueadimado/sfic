from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap_xml"),
    path("checkout/", views.create_checkout, name="create_checkout"),
    path("success", views.success, name="success"),
    path("cancel", views.cancel, name="cancel"),
    path("webhooks/stripe", views.stripe_webhook, name="stripe_webhook"),
    path("dashboard/login/", auth_views.LoginView.as_view(template_name="intensive/login.html"), name="login"),
    path("dashboard/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/registrations/<uuid:item_id>/", views.dashboard_registration_detail, name="dashboard_registration_detail"),
    path("dashboard/sessions/", views.dashboard_sessions, name="dashboard_sessions"),
    path("dashboard/sessions/new/", views.dashboard_session_create, name="dashboard_session_create"),
    path(
        "dashboard/sessions/<uuid:item_id>/edit/",
        views.dashboard_session_edit,
        name="dashboard_session_edit",
    ),
    path(
        "dashboard/sessions/<uuid:item_id>/delete/",
        views.dashboard_session_delete,
        name="dashboard_session_delete",
    ),
    path("dashboard/schedule/", views.dashboard_schedule, name="dashboard_schedule"),
    path("dashboard/schedule/new/", views.dashboard_schedule_create, name="dashboard_schedule_create"),
    path(
        "dashboard/schedule/<int:item_id>/edit/",
        views.dashboard_schedule_edit,
        name="dashboard_schedule_edit",
    ),
    path(
        "dashboard/schedule/<int:item_id>/delete/",
        views.dashboard_schedule_delete,
        name="dashboard_schedule_delete",
    ),
    path("dashboard/settings/", views.dashboard_site_settings, name="dashboard_site_settings"),
    path("dashboard/transactions/", views.dashboard_transactions, name="dashboard_transactions"),
    path("dashboard/transactions/backfill/", views.dashboard_transactions_backfill, name="dashboard_transactions_backfill"),
    path("dashboard/speakers/", views.dashboard_speakers, name="dashboard_speakers"),
    path("dashboard/speakers/new/", views.dashboard_speaker_create, name="dashboard_speaker_create"),
    path(
        "dashboard/speakers/<int:item_id>/edit/",
        views.dashboard_speaker_edit,
        name="dashboard_speaker_edit",
    ),
    path(
        "dashboard/speakers/<int:item_id>/delete/",
        views.dashboard_speaker_delete,
        name="dashboard_speaker_delete",
    ),
    path("dashboard/export.csv", views.export_csv, name="export_csv"),
]
