from django.shortcuts import redirect
from .models import FantasyTeam


class FantasyTeamAuthMiddleware:
    EXEMPT_PATHS = ['/login/', '/players/', '/admin/', '/api/ingest/']

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        team_id = request.session.get('fantasy_team_id')
        request.fantasy_team = None
        if team_id:
            try:
                request.fantasy_team = FantasyTeam.objects.get(pk=team_id)
            except FantasyTeam.DoesNotExist:
                del request.session['fantasy_team_id']

        if not request.fantasy_team:
            path = request.path
            if not any(path.startswith(ep) for ep in self.EXEMPT_PATHS):
                if path != '/':
                    return redirect(f'/login/?next={path}')
                return redirect('/login/')

        return self.get_response(request)
