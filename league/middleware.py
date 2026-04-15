from django.contrib import messages
from django.shortcuts import redirect
from .models import FantasyTeam


class FantasyTeamAuthMiddleware:
    EXEMPT_PATHS = ['/login/', '/demo/', '/api/ingest/']
    # Note: /demo/ prefix covers /demo/, /demo/start/, and /demo/team/... all at once

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

        request.is_demo = bool(request.session.get('is_demo', False))

        if not request.fantasy_team:
            path = request.path
            if not any(path.startswith(ep) for ep in self.EXEMPT_PATHS):
                if path != '/':
                    return redirect(f'/login/?next={path}')
                return redirect('/login/')

        # Block commissioner paths and all write actions in demo mode
        if request.is_demo:
            if request.path.startswith('/commissioner/'):
                return redirect('league:home_dashboard')
            if request.method == 'POST' and request.path != '/logout/':
                messages.error(request, 'Actions are disabled in demo mode.')
                referer = request.META.get('HTTP_REFERER')
                return redirect(referer) if referer else redirect('/home/')

        return self.get_response(request)
