"""
Quick sitemap test script - run this before deploying to catch issues
Usage: python test_sitemap.py
"""

import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'CEBackend.settings')
django.setup()

from quickstart.sitemaps import StaticViewSitemap

def test_sitemaps():
    print("🔍 Testing Sitemap Generation...\n")
    
    issues = []
    total_urls = 0
    
    sitemaps = {
        'Static Pages': StaticViewSitemap(),
    }
    
    for name, sitemap in sitemaps.items():
        print(f"📂 Testing {name}...")
        try:
            items = list(sitemap.items())
            count = len(items)
            total_urls += count
            
            print(f"   ✅ {count:,} URLs generated")
            
            # Show first 3 URLs as examples
            for i, item in enumerate(items[:3], 1):
                if isinstance(item, dict):
                    url = item.get('url', str(item))
                    lastmod = item.get('lastmod', 'No date')
                else:
                    url = sitemap.location(item)
                    lastmod = getattr(sitemap, 'lastmod', lambda x: 'No date')(item)
                
                print(f"      {i}. {url}")
                
                # Check for spaces in URL (this would break everything)
                if ' ' in url and '?' in url:
                    # Only after the query string should be encoded
                    query_part = url.split('?', 1)[1]
                    if ' ' in query_part:
                        issues.append(f"❌ UNENCODED SPACE in {name}: {url}")
            
            if count > 3:
                print(f"      ... and {count - 3:,} more\n")
            else:
                print()
                
        except Exception as e:
            issues.append(f"❌ Error in {name}: {str(e)}")
            print(f"   ❌ ERROR: {str(e)}\n")
    
    print(f"{'='*60}")
    print(f"📊 SUMMARY")
    print(f"{'='*60}")
    print(f"Total URLs: {total_urls:,}")
    
    if total_urls > 50000:
        issues.append(f"⚠️  WARNING: Total URLs ({total_urls:,}) exceeds 50,000 limit!")
    
    if issues:
        print(f"\n❌ {len(issues)} ISSUE(S) FOUND:\n")
        for issue in issues:
            print(f"  {issue}")
        return False
    else:
        print(f"\n✅ All tests passed! Sitemap is ready to deploy.\n")
        return True

if __name__ == '__main__':
    success = test_sitemaps()
    sys.exit(0 if success else 1)
