import signal
import threading
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from storage.service import cleanup


class Command(BaseCommand):
    help = "Delete expired anonymous files and abandoned uploads."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        if options["loop"]:
            signal.signal(signal.SIGTERM, lambda *args: stop.set())
            signal.signal(signal.SIGINT, lambda *args: stop.set())
        while True:
            close_old_connections()
            removed = cleanup()
            if removed or not options["loop"]:
                self.stdout.write(f"Cleanup: {removed} file(s) removed")
            if not options["loop"] or stop.wait(10):
                break
