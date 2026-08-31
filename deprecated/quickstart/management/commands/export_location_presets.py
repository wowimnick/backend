import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from quickstart.services.location_preset_service import (
    get_location_presets_with_coverage,
    serialize_presets_for_api,
)


class Command(BaseCommand):
    help = (
        "Export explore location presets that have bookable classes inside "
        "census boundary polygons."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            help="Write JSON payload to this file path.",
        )
        parser.add_argument(
            "--min-count",
            type=int,
            default=1,
            help="Minimum active bookable classes inside the boundary (default: 1).",
        )

    def handle(self, *args, **options):
        min_count = max(1, int(options["min_count"] or 1))
        presets = serialize_presets_for_api(
            get_location_presets_with_coverage(min_count=min_count)
        )
        payload = {
            "generatedAt": timezone.now().isoformat(),
            "presets": presets,
        }

        output_path = options.get("output")
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            self.stdout.write(
                self.style.SUCCESS(
                    f"Wrote {len(presets)} location preset(s) to {path}"
                )
            )
            return

        self.stdout.write(json.dumps(payload, indent=2))
