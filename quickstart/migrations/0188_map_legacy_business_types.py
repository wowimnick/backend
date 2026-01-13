from django.db import migrations

def migrate_legacy_data(apps, schema_editor):
    # 1. Get the models
    BusinessInfo = apps.get_model('quickstart', 'BusinessInfo')
    ClassOption = apps.get_model('quickstart', 'ClassOption')
    
    # -----------------------------
    # PART A: Business Type Mapping
    # -----------------------------
    business_mapping = {
        'school': 'tour-operator',      
        'academy': 'experience-group',  
        'center': 'venue',              
        'studio': 'venue',              
        # 'individual' stays 'individual'
    }

    for old_type, new_type in business_mapping.items():
        count = BusinessInfo.objects.filter(businessType=old_type).update(businessType=new_type)
        if count > 0:
            print(f"Migrated {count} businesses from '{old_type}' to '{new_type}'")

    # -----------------------------
    # PART B: Class/Experience Option Mapping
    # -----------------------------
    # If you had 'expert' levels, map them to 'advanced'
    # If you had 'semipro', map them to 'intermediate', etc.
    option_mapping = {
        'expert': 'advanced',
        'professional': 'advanced',
    }

    for old_level, new_level in option_mapping.items():
        count = ClassOption.objects.filter(level=old_level).update(level=new_level)
        if count > 0:
            print(f"Migrated {count} class options from '{old_level}' to '{new_level}'")

def reverse_migration(apps, schema_editor):
    # We generally don't reverse this because we are losing granularity 
    # (e.g., we can't know which 'tour-operators' used to be 'schools')
    pass

class Migration(migrations.Migration):

    dependencies = [
        ('quickstart', '0187_alter_businessinfo_businesstype_and_more'),
    ]

    operations = [
        migrations.RunPython(migrate_legacy_data, reverse_migration),
    ]
