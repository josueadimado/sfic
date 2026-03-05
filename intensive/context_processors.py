from django.conf import settings


def static_version(request):
    return {"static_version": getattr(settings, "STATIC_VERSION", "1")}
