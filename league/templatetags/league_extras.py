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
        v = int(round(value * 1000))
        return "1.000" if v >= 1000 else f".{v:03d}"
    except (TypeError, ValueError):
        return '.000'


@register.filter
def pts_format(value):
    """Format points to 1 decimal."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return '0.0'


_POSITION_COLORS = {
    'P':  'danger',   # red
    'C':  'primary',  # blue
    'IF': 'warning',  # orange
    'OF': 'success',  # green
}


@register.filter
def position_badge_color(position):
    """Return the Bootstrap color name for a player position."""
    return _POSITION_COLORS.get(position, 'secondary')


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


@register.filter
def format_excluded_dates(excluded_days):
    """Format a queryset of ExcludedDay objects as 'Mar 12, Mar 13'."""
    return ", ".join(d.date.strftime("%b ") + str(d.date.day) for d in excluded_days)
