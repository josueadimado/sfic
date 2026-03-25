from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import FreeRegistrationCode, Registration


@receiver(pre_delete, sender=Registration)
def release_free_registration_code_when_registration_deleted(sender, instance, **kwargs):
    """
    If a registration is deleted (e.g. from Django admin), mark its free code as
    unused again. Otherwise the code stays is_used=True while used_registration is
    cleared, and no one can use that code again.
    """
    code_id = instance.free_registration_code_id
    if code_id:
        FreeRegistrationCode.objects.filter(pk=code_id).update(
            is_used=False,
            used_at=None,
            used_registration=None,
        )
