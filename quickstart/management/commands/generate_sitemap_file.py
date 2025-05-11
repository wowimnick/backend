import os
from django.core.management.base import BaseCommand
from django.urls import reverse, NoReverseMatch # Keep reverse for Django URLs
from django.conf import settings
from django.utils import timezone
from django.contrib.sites.models import Site
from xml.etree.ElementTree import Element, SubElement, tostring as xml_tostring
from xml.dom import minidom

# ### ADJUST ###: Import your relevant models (Same as before)
try:
    from ...models import ClassesMain as Class
except ImportError:
    print("Warning: Could not import Class model.")
    Class = None

class Command(BaseCommand):
    help = 'Generates a static sitemap.xml file including known frontend routes.'

    # ### ADJUST ###: Define where the sitemap file should be saved. (Same as before)
    DEFAULT_OUTPUT_DIR = os.path.join(settings.BASE_DIR, '..', 'frontend', 'public')
    DEFAULT_OUTPUT_FILENAME = 'sitemap.xml'

    def add_arguments(self, parser):
        # Arguments remain the same
        parser.add_argument('--output-dir', type=str, default=self.DEFAULT_OUTPUT_DIR)
        parser.add_argument('--filename', type=str, default=self.DEFAULT_OUTPUT_FILENAME)

    def handle(self, *args, **options):
        output_dir = options['output_dir']
        output_filename = options['filename']
        output_path = os.path.join(output_dir, output_filename)

        self.stdout.write(f"Generating sitemap XML file at: {output_path}")

        # --- Get Site Domain --- (Same as before)
        try:
            current_site = Site.objects.get_current()
            base_url = f"https://{current_site.domain}"
        except Exception:
            base_url = getattr(settings, 'FRONTEND_URL', 'https://classeasily.com') # Fallback

        self.stdout.write(f"Using base URL: {base_url}")

        urlset = Element('urlset', xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

        # --- URLs List ---
        # Combine Django-reversed URLs and hardcoded Frontend URLs
        all_urls_data = []

        # 1. Django URLs (using reverse)
        django_routes = [

        ]
        for route in django_routes:
            try:
                all_urls_data.append({
                    'location': reverse(route['name']),
                    'priority': route.get('priority', '0.8'),
                    'changefreq': route.get('changefreq', 'weekly')
                })
            except NoReverseMatch:
                self.stderr.write(self.style.WARNING(f"Could not reverse Django URL named '{route['name']}'"))

        # 2. Frontend-Only Static URLs (hardcoded relative paths)
        # ### ADJUST ###: Add the exact paths used in your React Router <Route path="...">
        frontend_routes = [
            # Base pages
            {'path': '/', 'priority': '1.0', 'changefreq': 'daily'}, 
            {'path': '/explore', 'priority': '0.9', 'changefreq': 'daily'},
            {'path': '/about-us', 'priority': '0.7', 'changefreq': 'monthly'},
            {'path': '/careers', 'priority': '0.6', 'changefreq': 'monthly'},
            {'path': '/careers/positions', 'priority': '0.5', 'changefreq': 'weekly'},
            {'path': '/business', 'priority': '0.7', 'changefreq': 'monthly'},
            # User specific (lower priority maybe, depends on content)
            {'path': '/my-classes', 'priority': '0.5', 'changefreq': 'weekly'},
            {'path': '/my-favorites', 'priority': '0.5', 'changefreq': 'weekly'},
            {'path': '/my-tickets', 'priority': '0.5', 'changefreq': 'weekly'},
            # Legal/Info pages
            {'path': '/privacy-policy', 'priority': '0.3', 'changefreq': 'yearly'},
            {'path': '/terms-of-service', 'priority': '0.3', 'changefreq': 'yearly'},
            {'path': '/help', 'priority': '0.4', 'changefreq': 'monthly'},
             # Add others like '/gift-cards', '/referrals', etc. if they are static frontend pages
             # IMPORTANT: Do NOT add parameterized routes like '/class/:id' or '/my-tickets/:id' here.
             # Those should be handled by the dynamic model loops below.
        ]
        for route in frontend_routes:
            all_urls_data.append({
                'location': route['path'], # Use the hardcoded path directly
                'priority': route.get('priority', '0.8'),
                'changefreq': route.get('changefreq', 'weekly')
            })

        # --- Add URLs to XML ---
        for url_data in all_urls_data:
            url_element = SubElement(urlset, 'url')
            SubElement(url_element, 'loc').text = base_url + url_data['location'] # Combine base_url + location path
            SubElement(url_element, 'changefreq').text = url_data.get('changefreq', 'weekly')
            SubElement(url_element, 'priority').text = url_data.get('priority', '0.8')
            # SubElement(url_element, 'lastmod').text = timezone.now().strftime('%Y-%m-%d') # Add for static if needed


        # --- Dynamic URLs (e.g., Classes) --- (Same as before)
        if Class:
            class_qs = Class.objects.filter(
                status='active',  # Use the status field of ClassesMain
                businessId__isActive=True # Optionally, ensure the business is also active
            ).order_by('-updatedAt')

            for class_obj in class_qs:
                try:
                    # Ensure 'classpage' is the name of the URL pattern in your urls.py
                    # that resolves to the view handling /class/:id on the frontend.
                    # Since /class/:id is a frontend route parameter, it will be handled by the
                    # catch-all in Django, but we still need to construct the URL.
                    # We can't 'reverse' parameterized frontend routes from Django directly.
                    # So we construct it manually for the sitemap.
                    location = f'/class/{class_obj.pk}/' # Construct the path manually

                    url_element = SubElement(urlset, 'url')
                    SubElement(url_element, 'loc').text = base_url + location
                    if hasattr(class_obj, 'updatedAt') and class_obj.updatedAt:
                        SubElement(url_element, 'lastmod').text = class_obj.updatedAt.strftime('%Y-%m-%d')
                    SubElement(url_element, 'changefreq').text = 'weekly'
                    SubElement(url_element, 'priority').text = '0.8'
                except Exception as e: # Catch any other potential error during URL construction for a class
                    self.stderr.write(self.style.WARNING(f"Error processing Class object pk={class_obj.pk}: {e}"))

        # --- Format and Write XML File --- (Same as before)
        xml_str = xml_tostring(urlset, encoding='unicode')
        try:
            dom = minidom.parseString(xml_str)
            pretty_xml_str = dom.toprettyxml(indent="  ", encoding="utf-8").decode('utf-8')
            if not pretty_xml_str.startswith('<?xml'):
                 pretty_xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + pretty_xml_str
        except Exception as parse_error:
             self.stderr.write(self.style.WARNING(f"Could not prettify XML, writing raw: {parse_error}"))
             pretty_xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(pretty_xml_str)
            self.stdout.write(self.style.SUCCESS(f"Successfully generated sitemap at {output_path}"))
        except IOError as e:
            self.stderr.write(self.style.ERROR(f"Error writing sitemap file to {output_path}: {e}"))
        except Exception as e:
             self.stderr.write(self.style.ERROR(f"An unexpected error occurred: {e}"))