from django.apps import AppConfig


class IntensiveConfig(AppConfig):
    name = "intensive"

    def ready(self) -> None:
        import intensive.signals  # noqa: F401
