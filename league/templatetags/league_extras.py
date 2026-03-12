from django import template

register = template.Library()


@register.filter
def ip_display(outs):
    """Convert total outs to IP display format (e.g. 20 -> 6.2)."""
    try:
        outs = int(outs)
    except (TypeError, ValueError):
        return '0.0'
    full = outs // 3
    remainder = outs % 3
    return f"{full}.{remainder}"


@register.filter
def pct_format(value):
    """Format a win percentage like .750"""
    try:
        return f".{int(value * 1000):03d}"
    except (TypeError, ValueError):
        return '.000'


@register.filter
def pts_format(value):
    """Format points to 1 decimal."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return '0.0'


@register.filter
def batting_avg(hits, ab):
    """Calculate batting average."""
    try:
        if int(ab) == 0:
            return '.000'
        avg = int(hits) / int(ab)
        return f".{int(avg * 1000):03d}"
    except (TypeError, ValueError, ZeroDivisionError):
        return '.000'
