from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def demo_team_name(context, team):
    """Return anonymized team name in demo mode, real name otherwise.

    Usage: {% demo_team_name matchup.team_1 %}
    """
    if not team:
        return ''
    name_map = context.get('demo_name_map', {})
    if name_map:
        return name_map.get(team.id, str(team))
    return str(team)
