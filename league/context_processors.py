from .models import FantasyTeam


def demo_context(request):
    """Inject demo mode flag and team name anonymization map into every template."""
    if not getattr(request, 'is_demo', False):
        return {'is_demo': False, 'demo_name_map': {}}
    teams = list(FantasyTeam.objects.filter(is_commissioner=False).order_by('name'))
    letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    name_map = {team.id: f'Team {letters[i]}' for i, team in enumerate(teams)}
    demo_teams = [{'id': team.id, 'name': name_map[team.id]} for team in teams]
    return {
        'is_demo': True,
        'demo_name_map': name_map,
        'demo_teams': demo_teams,
    }
