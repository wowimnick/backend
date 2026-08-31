import os
from django.core.management.base import BaseCommand
from django.contrib.gis.gdal import DataSource
from django.contrib.gis.geos import GEOSGeometry, Polygon, MultiPolygon
from django.db import transaction
from quickstart.models import GeographicBoundary

# --- HELPER FUNCTION: Placed outside the class for cleanliness ---
def get_province_name_from_pruid(pruid_str):
    """Converts a PRUID string to a province name, with a fallback."""
    pruid_to_province = {
        '10': 'Newfoundland and Labrador', '11': 'Prince Edward Island',
        '12': 'Nova Scotia', '13': 'New Brunswick', '24': 'Quebec',
        '35': 'Ontario', '46': 'Manitoba', '47': 'Saskatchewan',
        '48': 'Alberta', '59': 'British Columbia', '60': 'Yukon',
        '61': 'Northwest Territories', '62': 'Nunavut'
    }
    return pruid_to_province.get(pruid_str, f'Unknown Province (PRUID: {pruid_str})')

class Command(BaseCommand):
    help = 'Imports and simplifies geographic boundaries from a Statistics Canada Shapefile.'

    def add_arguments(self, parser):
        parser.add_argument('shapefile', type=str, help='The full path to the .shp file.')

    @transaction.atomic  # --- PERFORMANCE: Wrap the entire operation in one transaction
    def handle(self, *args, **options):
        shapefile_path = options['shapefile']

        if not os.path.exists(shapefile_path):
            self.stdout.write(self.style.ERROR(f"Shapefile not found at: {shapefile_path}"))
            return

        self.stdout.write(f"Opening shapefile: {shapefile_path}...")
        try:
            ds = DataSource(shapefile_path)
            layer = ds[0]
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error opening shapefile: {e}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Successfully opened. Found {len(layer)} features."))
        
        # These are the field names from the StatCan Shapefile
        field_mappings = {
            'csuid': 'CSDUID',
            'name': 'CSDNAME',
        }
        
        imported_count = 0
        updated_count = 0
        total_to_process = len(layer)

        for i, feature in enumerate(layer):
            try:
                geom = feature.geom.geos
                
                # 1. Transform to the standard WGS84 (GPS) coordinate system
                geom.transform(4326)
                
                # 2. Simplify the geometry to reduce complexity and improve query performance.
                # A tolerance of 0.001 degrees is a good balance of detail vs. performance.
                geom = geom.simplify(0.001, preserve_topology=True)
                
                # 3. --- IMPROVED: Safely ensure the geometry is a MultiPolygon ---
                # This correctly handles complex shapes with holes (interior rings).
                if isinstance(geom, Polygon):
                    geom = MultiPolygon(geom)
                
                if not isinstance(geom, MultiPolygon):
                    self.stdout.write(self.style.WARNING(f"Skipping feature with unsupported geometry type: {geom.geom_type}"))
                    continue

                # 4. Extract data using mappings and the helper function
                boundary_data = {key: feature.get(value) for key, value in field_mappings.items()}
                boundary_data['province'] = get_province_name_from_pruid(feature.get('PRUID'))
                boundary_data['geom'] = geom

                # 5. Ensure we have the required unique ID before saving
                if not boundary_data.get('csuid'):
                    self.stdout.write(self.style.WARNING(f"Feature at index {i} is missing a CSUID. Skipping."))
                    continue
                
                # 6. Use update_or_create to insert or update the record
                obj, created = GeographicBoundary.objects.update_or_create(
                    csuid=boundary_data['csuid'],
                    defaults=boundary_data
                )

                if created:
                    imported_count += 1
                else:
                    updated_count += 1
                
                # Provide progress feedback
                if (i + 1) % 500 == 0:
                     self.stdout.write(f"  Processed {i + 1}/{total_to_process}...")

            except Exception as e:
                csuid = feature.get('CSDUID', 'N/A')
                self.stdout.write(self.style.WARNING(f"Could not process feature with CSDUID {csuid}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Import complete!"))
        self.stdout.write(f"  - New boundaries imported: {imported_count}")
        self.stdout.write(f"  - Existing boundaries updated: {updated_count}")