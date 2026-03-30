# Event program PDF belongs to Session (per intensive), not site-wide.

from django.db import migrations, models


def copy_site_program_to_sessions(apps, schema_editor):
    SiteSetting = apps.get_model("intensive", "SiteSetting")
    Session = apps.get_model("intensive", "Session")
    ss = SiteSetting.objects.order_by("pk").first()
    if not ss:
        return
    pdf = getattr(ss, "event_program_pdf", "") or ""
    if pdf:
        Session.objects.all().update(event_program_pdf=pdf)


def restore_site_program_from_first_session(apps, schema_editor):
    SiteSetting = apps.get_model("intensive", "SiteSetting")
    Session = apps.get_model("intensive", "Session")
    ss = SiteSetting.objects.order_by("pk").first()
    if not ss:
        return
    sess = Session.objects.exclude(event_program_pdf="").order_by("start_date").first()
    if sess and sess.event_program_pdf:
        ss.event_program_pdf = sess.event_program_pdf
        ss.save(update_fields=["event_program_pdf"])


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0020_rename_intensive_fr_is_used_9f5e2a_idx_intensive_f_is_used_b348f9_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="event_program_pdf",
            field=models.FileField(
                blank=True,
                help_text="Event program for this intensive: public homepage link only while this session is the next upcoming live date; confirmation email attachment; learning hub download for paid registrants when downloads unlock.",
                upload_to="registration_materials/",
            ),
        ),
        migrations.RunPython(copy_site_program_to_sessions, restore_site_program_from_first_session),
        migrations.RemoveField(
            model_name="sitesetting",
            name="event_program_pdf",
        ),
    ]
