from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("donate/", views.donate, name="donate"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap_xml"),
    path("checkout/", views.create_checkout, name="create_checkout"),
    path("checkout/student-code/", views.request_student_discount_code, name="request_student_discount_code"),
    path("donate/checkout/", views.create_donation_checkout, name="create_donation_checkout"),
    path("checkout/resume/<uuid:registration_id>/", views.resume_checkout, name="resume_checkout"),
    path("success", views.success, name="success"),
    path("cancel", views.cancel, name="cancel"),
    path("donation/success", views.donation_success, name="donation_success"),
    path("donation/cancel", views.donation_cancel, name="donation_cancel"),
    path("donation/manage/<str:token>/", views.donation_manage, name="donation_manage"),
    path("webhooks/stripe", views.stripe_webhook, name="stripe_webhook"),
    path("webhooks/donorelf", views.donor_elf_webhook, name="donor_elf_webhook"),
    path("dashboard/login/", auth_views.LoginView.as_view(template_name="intensive/login.html"), name="login"),
    path("dashboard/logout/", views.dashboard_logout, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path(
        "dashboard/registrations/sync-pending/",
        views.dashboard_registrations_sync_pending,
        name="dashboard_registrations_sync_pending",
    ),
    path("dashboard/donations/", views.dashboard_donations, name="dashboard_donations"),
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
