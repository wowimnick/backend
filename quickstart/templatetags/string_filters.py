from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter(name='replace')
def replace_string(value, args):
    """
    Replaces all occurrences of arg1 with arg2 in the given string.
    Usage: {{ some_string|replace:"old,new" }}
    """
    if not isinstance(value, str):
        return value # Or handle error appropriately
    
    arg_list = [arg.strip() for arg in args.split(',')]
    if len(arg_list) != 2:
        return value # Or raise an error / return original value

    old_str, new_str = arg_list[0], arg_list[1]
    return value.replace(old_str, new_str)

@register.filter(name='humanize_timezone')
def humanize_timezone(value):
    """
    Replaces underscores with spaces in a timezone string.
    Usage: {{ timezone_string|humanize_timezone }}
    """
    if isinstance(value, str):
        return value.replace('_', ' ')
    return value