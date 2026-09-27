from django.apps import AppConfig


class BakeryConfig(AppConfig):
    name = 'bakery'

    def ready(self):
        from bakery import signals  # noqa: F401
