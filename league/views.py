import datetime
from functools import wraps
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Sum, Q

from .models import (
    FantasyTeam, Player, RealTeam, RealGame,
    HittingGameLog, PitchingGameLog, PointSettings,
    Week, Matchup, RosterSlot,
    PITCHING_POSITIONS, POSITION_CHOICES, SLOT_LIMITS, SLOT_ELIGIBLE, SLOT_ORDER,
)
from .forms import (
    LoginForm, FantasyTeamForm, PlayerForm, RealTeamForm,
    RealGameForm, HittingGameLogForm, PitchingGameLogForm,
    PointSettingsForm, GenerateScheduleForm,
)
from .scoring import (
    calc_team_weekly_points, calc_team_season_points,
    resolve_matchup, get_standings, get_player_weekly_breakdown,
    calc_hitting_points, calc_pitching_points, calc_player_points_for_period,
)
from .schedule import generate_round_robin


# --- Decorators ---

def commissioner_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.fantasy_team or not request.fantasy_team.is_commissioner:
            messages.error(request, 'Commissioner access required.')
            return redirect('league:dashboard')
        return view_func(request, *args, **kwargs)
    return wrapper


# --- Auth Views ---

def login_view(request):
    if request.fantasy_team:
        return redirect('league:dashboard')
    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        name = form.cleaned_data['team_name']
        password = form.cleaned_data['password']
        try:
            team = FantasyTeam.objects.get(name__iexact=name)
        except FantasyTeam.DoesNotExist:
            messages.error(request, 'Invalid team name or password.')
            return render(request, 'league/login.html', {'form': form})
        if team.check_password(password):
            request.session['fantasy_team_id'] = team.id
            next_url = request.GET.get('next', '/dashboard/')
            return redirect(next_url)
        messages.error(request, 'Invalid team name or password.')
    return render(request, 'league/login.html', {'form': form})


def logout_view(request):
    request.session.flush()
    return redirect('league:login')


# --- Dashboard ---

def dashboard(request):
    team = request.fantasy_team
    today = datetime.date.today()
    ps = PointSettings.load()

    # Find current week
    current_week = Week.objects.filter(
        start_date__lte=today, end_date__gte=today
    ).first()
    if not current_week:
        current_week = Week.objects.filter(end_date__lt=today).last()

    matchup_data = None
    if current_week:
        matchup = Matchup.objects.filter(
            Q(team_1=team) | Q(team_2=team),
            week=current_week
        ).select_related('team_1', 'team_2', 'week').first()
        if matchup:
            t1_pts, t2_pts, winner = resolve_matchup(matchup, ps)
            if matchup.team_1 == team:
                my_pts, opp_pts = t1_pts, t2_pts
                opponent = matchup.team_2
            else:
                my_pts, opp_pts = t2_pts, t1_pts
                opponent = matchup.team_1
            matchup_data = {
                'week': current_week,
                'opponent': opponent,
                'my_points': my_pts,
                'opp_points': opp_pts,
                'winner': winner,
            }

    # Roster with season points
    players = Player.objects.filter(fantasy_team=team).select_related('real_team')
    roster = []
    for p in players:
        weeks = Week.objects.all()
        season_pts = Decimal('0')
        weekly_pts = Decimal('0')
        for w in weeks:
            pts = calc_player_points_for_period(p, w.start_date, w.end_date, ps)
            season_pts += pts
            if current_week and w.id == current_week.id:
                weekly_pts = pts
        roster.append({
            'player': p,
            'weekly_points': weekly_pts,
            'season_points': season_pts,
        })
    roster.sort(key=lambda x: x['season_points'], reverse=True)

    return render(request, 'league/dashboard.html', {
        'team': team,
        'matchup': matchup_data,
        'roster': roster,
        'current_week': current_week,
    })


# --- Standings ---

def standings_view(request):
    standing_list = get_standings()
    return render(request, 'league/standings.html', {
        'standings': standing_list,
    })


# --- Schedule ---

def schedule_view(request):
    weeks = Week.objects.prefetch_related(
        'matchups__team_1', 'matchups__team_2'
    ).all()
    today = datetime.date.today()
    current_week = None
    ps = PointSettings.load()

    week_data = []
    for week in weeks:
        is_current = week.start_date <= today <= week.end_date
        if is_current:
            current_week = week
        matchups = []
        for m in week.matchups.all():
            t1_pts, t2_pts, winner = resolve_matchup(m, ps)
            matchups.append({
                'matchup': m,
                't1_pts': t1_pts,
                't2_pts': t2_pts,
                'winner': winner,
            })
        week_data.append({
            'week': week,
            'is_current': is_current,
            'matchups': matchups,
        })

    return render(request, 'league/schedule.html', {
        'week_data': week_data,
        'current_week': current_week,
    })


def weekly_matchup_view(request, week_id, matchup_id):
    matchup = get_object_or_404(
        Matchup.objects.select_related('week', 'team_1', 'team_2'),
        pk=matchup_id
    )
    ps = PointSettings.load()
    t1_pts, t2_pts, winner = resolve_matchup(matchup, ps)
    t1_breakdown = get_player_weekly_breakdown(matchup.team_1, matchup.week, ps)
    t2_breakdown = []
    if matchup.team_2:
        t2_breakdown = get_player_weekly_breakdown(matchup.team_2, matchup.week, ps)

    return render(request, 'league/weekly_matchup.html', {
        'matchup': matchup,
        't1_pts': t1_pts,
        't2_pts': t2_pts,
        'winner': winner,
        't1_breakdown': t1_breakdown,
        't2_breakdown': t2_breakdown,
    })


# --- Roster ---

def _player_stats(player):
    """Return aggregated season stats dict for a player."""
    if player.is_pitcher:
        return PitchingGameLog.objects.filter(player=player).aggregate(
            total_outs=Sum('outs'), total_h=Sum('hits'), total_er=Sum('er'),
            total_bb=Sum('bb'), total_so=Sum('so'), total_hr=Sum('hr'),
            total_runs=Sum('runs'),
        )
    return HittingGameLog.objects.filter(player=player).aggregate(
        total_ab=Sum('ab'), total_h=Sum('hits'), total_hr=Sum('hr'),
        total_rbi=Sum('rbi'), total_r=Sum('runs'), total_bb=Sum('bb'),
        total_so=Sum('so'), total_sb=Sum('sb'),
    )


def roster_view(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id)
    RosterSlot.create_for_team(team)

    ps = PointSettings.load()
    today = datetime.date.today()
    current_week = Week.objects.filter(
        start_date__lte=today, end_date__gte=today
    ).first() or Week.objects.filter(end_date__lt=today).last()
    weeks = list(Week.objects.all())

    def player_row(player):
        season_pts = Decimal('0')
        weekly_pts = Decimal('0')
        for w in weeks:
            pts = calc_player_points_for_period(player, w.start_date, w.end_date, ps)
            season_pts += pts
            if current_week and w.id == current_week.id:
                weekly_pts = pts
        return {
            'player': player,
            'season_points': season_pts,
            'weekly_points': weekly_pts,
            'is_pitcher': player.is_pitcher,
            'stats': _player_stats(player),
        }

    # Build ordered slot list
    slots_qs = RosterSlot.objects.filter(fantasy_team=team).select_related('player__real_team')
    slots_by_key = {(s.slot_type, s.slot_number): s for s in slots_qs}
    slot_rows = []
    slotted_ids = set()
    for slot_type, count in sorted(SLOT_LIMITS.items(), key=lambda x: SLOT_ORDER[x[0]]):
        for n in range(1, count + 1):
            slot = slots_by_key.get((slot_type, n))
            if slot and slot.player:
                slotted_ids.add(slot.player_id)
                row = player_row(slot.player)
            else:
                row = None
            slot_rows.append({'slot': slot, 'row': row})

    # Players on team but not in any slot
    all_rostered = Player.objects.filter(fantasy_team=team).select_related('real_team')
    unslotted = [player_row(p) for p in all_rostered if p.id not in slotted_ids]

    return render(request, 'league/roster.html', {
        'viewed_team': team,
        'slot_rows': slot_rows,
        'unslotted': unslotted,
        'current_week': current_week,
    })


def set_lineup(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)

    # Only the team owner or commissioner can edit
    if not request.fantasy_team.is_commissioner and request.fantasy_team != team:
        messages.error(request, 'You can only edit your own lineup.')
        return redirect('league:roster', team_id=team_id)

    RosterSlot.create_for_team(team)
    rostered = list(Player.objects.filter(fantasy_team=team).select_related('real_team').order_by('position', 'last_name'))

    if request.method == 'POST':
        # Clear all slot assignments first to avoid OneToOne conflicts
        RosterSlot.objects.filter(fantasy_team=team).update(player=None)

        player_map = {p.id: p for p in rostered}
        assigned_ids = []
        errors = []

        # Collect assignments
        pending = []
        for slot_type, count in SLOT_LIMITS.items():
            for n in range(1, count + 1):
                field = f'slot_{slot_type}_{n}'
                raw = request.POST.get(field, '').strip()
                if raw:
                    try:
                        pid = int(raw)
                    except ValueError:
                        continue
                    player = player_map.get(pid)
                    if not player:
                        errors.append(f'Unknown player for {slot_type}{n}.')
                        continue
                    if player.position not in SLOT_ELIGIBLE[slot_type]:
                        errors.append(f'{player} is not eligible for the {slot_type} slot.')
                        continue
                    if pid in assigned_ids:
                        errors.append(f'{player} assigned to multiple slots.')
                        continue
                    assigned_ids.append(pid)
                    pending.append((slot_type, n, pid))

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            for slot_type, n, pid in pending:
                RosterSlot.objects.filter(
                    fantasy_team=team, slot_type=slot_type, slot_number=n
                ).update(player_id=pid)
            messages.success(request, 'Lineup saved.')
        return redirect('league:set_lineup', team_id=team_id)

    # Build slot sections grouped by type for template
    slots_qs = RosterSlot.objects.filter(fantasy_team=team).select_related('player')
    slots_by_key = {(s.slot_type, s.slot_number): s for s in slots_qs}
    slot_sections = []
    section_labels = {'C': 'Catcher', 'IF': 'Infield', 'OF': 'Outfield',
                      'DH': 'Designated Hitter', 'P': 'Pitchers', 'BN': 'Bench'}
    for slot_type, count in sorted(SLOT_LIMITS.items(), key=lambda x: SLOT_ORDER[x[0]]):
        eligible = [p for p in rostered if p.position in SLOT_ELIGIBLE[slot_type]]
        rows = []
        for n in range(1, count + 1):
            slot = slots_by_key.get((slot_type, n))
            rows.append({'slot': slot, 'eligible_players': eligible})
        slot_sections.append({
            'label': section_labels[slot_type],
            'slot_type': slot_type,
            'rows': rows,
        })

    return render(request, 'league/set_lineup.html', {
        'team': team,
        'slot_sections': slot_sections,
    })


# --- Stat Entry ---

def game_list(request):
    games = RealGame.objects.select_related('home_team', 'away_team').all()
    return render(request, 'league/game_list.html', {'games': games})


def game_create(request):
    form = RealGameForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Game created.')
        return redirect('league:game_list')
    return render(request, 'league/commissioner/real_team_form.html', {
        'form': form,
        'title': 'Add Game',
    })


def stat_entry_select(request, game_id):
    game = get_object_or_404(RealGame.objects.select_related('home_team', 'away_team'), pk=game_id)
    team = request.fantasy_team

    # Show players on this team that play in this game
    players = Player.objects.filter(
        fantasy_team=team,
        real_team__in=[game.home_team, game.away_team]
    ).select_related('real_team')

    # Mark which already have logs
    player_data = []
    for p in players:
        if p.is_pitcher:
            has_log = PitchingGameLog.objects.filter(player=p, game=game).exists()
        else:
            has_log = HittingGameLog.objects.filter(player=p, game=game).exists()
        player_data.append({'player': p, 'has_log': has_log})

    return render(request, 'league/stat_entry.html', {
        'game': game,
        'player_data': player_data,
    })


def hitting_entry(request, game_id, player_id):
    game = get_object_or_404(RealGame, pk=game_id)
    player = get_object_or_404(Player, pk=player_id)

    existing = HittingGameLog.objects.filter(player=player, game=game).first()
    form = HittingGameLogForm(request.POST or None, instance=existing)

    if request.method == 'POST' and form.is_valid():
        log = form.save(commit=False)
        log.player = player
        log.game = game
        log.entered_by = request.fantasy_team
        log.save()
        messages.success(request, f'Hitting stats saved for {player}.')
        return redirect('league:stat_entry_select', game_id=game.id)

    return render(request, 'league/stat_entry.html', {
        'form': form,
        'game': game,
        'player': player,
        'stat_type': 'hitting',
        'editing': existing is not None,
    })


def pitching_entry(request, game_id, player_id):
    game = get_object_or_404(RealGame, pk=game_id)
    player = get_object_or_404(Player, pk=player_id)

    existing = PitchingGameLog.objects.filter(player=player, game=game).first()
    initial = {}
    if existing:
        initial = {
            'ip': existing.ip_display,
            'hits': existing.hits,
            'runs': existing.runs,
            'er': existing.er,
            'bb': existing.bb,
            'so': existing.so,
            'hr': existing.hr,
            'win': existing.win,
            'loss': existing.loss,
            'save': existing.save_game,
            'hold': existing.hold,
        }

    form = PitchingGameLogForm(request.POST or initial or None)

    if request.method == 'POST' and form.is_valid():
        if existing:
            log = existing
        else:
            log = PitchingGameLog(player=player, game=game)
        log.outs = form.cleaned_data['ip']
        log.hits = form.cleaned_data['hits']
        log.runs = form.cleaned_data['runs']
        log.er = form.cleaned_data['er']
        log.bb = form.cleaned_data['bb']
        log.so = form.cleaned_data['so']
        log.hr = form.cleaned_data['hr']
        log.win = form.cleaned_data['win']
        log.loss = form.cleaned_data['loss']
        log.save_game = form.cleaned_data['save']
        log.hold = form.cleaned_data['hold']
        log.entered_by = request.fantasy_team
        log.save()
        messages.success(request, f'Pitching stats saved for {player}.')
        return redirect('league:stat_entry_select', game_id=game.id)

    return render(request, 'league/stat_entry_pitching.html', {
        'form': form,
        'game': game,
        'player': player,
        'editing': existing is not None,
    })


def game_log_list(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    ps = PointSettings.load()

    if player.is_pitcher:
        logs = PitchingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by')
        log_data = []
        for log in logs:
            pts = calc_pitching_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'pitching'})
    else:
        logs = HittingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by')
        log_data = []
        for log in logs:
            pts = calc_hitting_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'hitting'})

    return render(request, 'league/game_log_list.html', {
        'player': player,
        'log_data': log_data,
    })


# --- Verification (public) ---

def players_list(request):
    players = Player.objects.select_related('real_team', 'fantasy_team').all()

    # Filters — default to free agents only
    real_team_id = request.GET.get('real_team', '')
    position = request.GET.get('position', '')
    fantasy_team_id = request.GET.get('fantasy_team', '')
    # 'all' shows everyone; anything else (including blank default) shows only FAs
    show_all = request.GET.get('show_all', '')

    if real_team_id:
        players = players.filter(real_team_id=real_team_id)
    if position:
        players = players.filter(position=position)
    if fantasy_team_id:
        players = players.filter(fantasy_team_id=fantasy_team_id)
    if not show_all and not fantasy_team_id:
        players = players.filter(fantasy_team__isnull=True)

    today = datetime.date.today()
    current_week = Week.objects.filter(
        start_date__lte=today, end_date__gte=today
    ).first()
    if not current_week:
        current_week = Week.objects.filter(end_date__lt=today).last()

    sort = request.GET.get('sort', 'season_points')
    order = request.GET.get('order', 'desc')

    ps = PointSettings.load()
    weeks = list(Week.objects.all())
    player_data = []
    for p in players:
        season_pts = Decimal('0')
        weekly_pts = Decimal('0')
        for w in weeks:
            pts = calc_player_points_for_period(p, w.start_date, w.end_date, ps)
            season_pts += pts
            if current_week and w.id == current_week.id:
                weekly_pts = pts
        if p.is_pitcher:
            games_played = PitchingGameLog.objects.filter(player=p).count()
        else:
            games_played = HittingGameLog.objects.filter(player=p).count()
        ppg = (season_pts / games_played).quantize(Decimal('0.01')) if games_played else Decimal('0')
        player_data.append({
            'player': p,
            'season_points': season_pts,
            'weekly_points': weekly_pts,
            'games_played': games_played,
            'ppg': ppg,
        })

    reverse = (order != 'asc')
    if sort == 'weekly_points':
        player_data.sort(key=lambda x: x['weekly_points'], reverse=reverse)
    elif sort == 'ppg':
        player_data.sort(key=lambda x: x['ppg'], reverse=reverse)
    else:
        player_data.sort(key=lambda x: x['season_points'], reverse=reverse)

    real_teams = RealTeam.objects.all()
    fantasy_teams = FantasyTeam.objects.filter(is_commissioner=False)

    return render(request, 'league/verification.html', {
        'player_data': player_data,
        'real_teams': real_teams,
        'fantasy_teams': fantasy_teams,
        'position_choices': POSITION_CHOICES,
        'selected_real_team': real_team_id,
        'selected_position': position,
        'selected_fantasy_team': fantasy_team_id,
        'show_all': show_all,
        'current_week': current_week,
        'sort': sort,
        'order': order,
    })


def player_detail(request, player_id):
    player = get_object_or_404(
        Player.objects.select_related('real_team', 'fantasy_team'), pk=player_id
    )
    ps = PointSettings.load()

    if player.is_pitcher:
        logs = PitchingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by')
        log_data = []
        for log in logs:
            pts = calc_pitching_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'pitching'})
    else:
        logs = HittingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by')
        log_data = []
        for log in logs:
            pts = calc_hitting_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'hitting'})

    return render(request, 'league/verification_detail.html', {
        'player': player,
        'log_data': log_data,
    })


# --- Commissioner Views ---

@commissioner_required
def commissioner_panel(request):
    team_count = FantasyTeam.objects.filter(is_commissioner=False).count()
    player_count = Player.objects.count()
    real_team_count = RealTeam.objects.count()
    week_count = Week.objects.count()
    free_agent_count = Player.objects.filter(fantasy_team__isnull=True).count()
    return render(request, 'league/commissioner/panel.html', {
        'team_count': team_count,
        'player_count': player_count,
        'real_team_count': real_team_count,
        'week_count': week_count,
        'free_agent_count': free_agent_count,
    })


@commissioner_required
def manage_teams(request):
    teams = FantasyTeam.objects.filter(is_commissioner=False)
    return render(request, 'league/commissioner/manage_teams.html', {
        'teams': teams,
    })


@commissioner_required
def team_create(request):
    form = FantasyTeamForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        team = form.save(commit=False)
        pw = form.cleaned_data.get('password')
        if pw:
            team.set_password(pw)
        else:
            team.set_password('changeme')
        team.save()
        messages.success(request, f'Team "{team.name}" created.')
        return redirect('league:manage_teams')
    return render(request, 'league/commissioner/team_form.html', {
        'form': form,
        'title': 'Create Fantasy Team',
    })


@commissioner_required
def team_edit(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id)
    form = FantasyTeamForm(request.POST or None, instance=team)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Team "{team.name}" updated.')
        return redirect('league:manage_teams')
    return render(request, 'league/commissioner/team_form.html', {
        'form': form,
        'title': f'Edit Team: {team.name}',
    })


@commissioner_required
def team_delete(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)
    if request.method == 'POST':
        team_name = team.name
        # Release all players to free agency before deleting
        Player.objects.filter(fantasy_team=team).update(fantasy_team=None)
        team.delete()
        messages.success(request, f'Team "{team_name}" deleted. All players moved to free agency.')
        return redirect('league:manage_teams')
    return render(request, 'league/commissioner/team_confirm_delete.html', {'team': team})


@commissioner_required
def commissioner_team_roster(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)
    players = Player.objects.filter(fantasy_team=team).select_related('real_team').order_by('last_name', 'first_name')
    other_teams = FantasyTeam.objects.filter(is_commissioner=False).exclude(pk=team_id)

    # All players not on this team (free agents + players on other teams) for the "add player" search
    available_players = Player.objects.exclude(fantasy_team=team).select_related('real_team', 'fantasy_team').order_by('last_name', 'first_name')
    search = request.GET.get('search', '').strip()
    if search:
        available_players = available_players.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
        )

    return render(request, 'league/commissioner/commissioner_team_roster.html', {
        'viewed_team': team,
        'players': players,
        'other_teams': other_teams,
        'available_players': available_players,
        'search': search,
    })


@commissioner_required
def drop_player(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    if request.method == 'POST':
        team_name = player.fantasy_team.name if player.fantasy_team else 'no team'
        player.fantasy_team = None
        player.fantasy_team_since = None
        player.save()
        messages.success(request, f'{player} dropped to free agency from {team_name}.')
    next_url = request.POST.get('next', 'league:free_agent_board')
    return redirect(next_url)


@commissioner_required
def player_delete(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    if request.method == 'POST':
        name = str(player)
        player.delete()
        messages.success(request, f'{name} has been removed from the league.')
        return redirect('league:manage_players')
    return redirect('league:manage_players')


@commissioner_required
def manage_players(request):
    players = Player.objects.select_related('real_team', 'fantasy_team').all()
    return render(request, 'league/commissioner/manage_players.html', {
        'players': players,
    })


@commissioner_required
def player_create(request):
    form = PlayerForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Player created.')
        return redirect('league:manage_players')
    return render(request, 'league/commissioner/player_form.html', {
        'form': form,
        'title': 'Add Player',
    })


@commissioner_required
def player_edit(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    form = PlayerForm(request.POST or None, instance=player)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Player "{player}" updated.')
        return redirect('league:manage_players')
    return render(request, 'league/commissioner/player_form.html', {
        'form': form,
        'title': f'Edit Player: {player}',
    })


@commissioner_required
def manage_real_teams(request):
    teams = RealTeam.objects.all()
    return render(request, 'league/commissioner/manage_real_teams.html', {
        'teams': teams,
    })


@commissioner_required
def real_team_create(request):
    form = RealTeamForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Real team created.')
        return redirect('league:manage_real_teams')
    return render(request, 'league/commissioner/real_team_form.html', {
        'form': form,
        'title': 'Add Real Team',
    })


@commissioner_required
def real_team_edit(request, team_id):
    team = get_object_or_404(RealTeam, pk=team_id)
    form = RealTeamForm(request.POST or None, instance=team)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Team "{team.name}" updated.')
        return redirect('league:manage_real_teams')
    return render(request, 'league/commissioner/real_team_form.html', {
        'form': form,
        'title': f'Edit Real Team: {team.name}',
    })


@commissioner_required
def point_settings_view(request):
    ps = PointSettings.load()
    form = PointSettingsForm(request.POST or None, instance=ps)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Point settings updated.')
        return redirect('league:commissioner_panel')
    return render(request, 'league/commissioner/point_settings.html', {
        'form': form,
    })


@commissioner_required
def generate_schedule_view(request):
    form = GenerateScheduleForm(request.POST or None)
    team_count = FantasyTeam.objects.filter(is_commissioner=False).count()
    existing_weeks = Week.objects.count()

    if request.method == 'POST' and form.is_valid():
        start_date = form.cleaned_data['start_date']
        num_weeks = form.cleaned_data['num_weeks']
        generate_round_robin(start_date, num_weeks)
        messages.success(request, f'Generated {num_weeks}-week schedule.')
        return redirect('league:schedule')

    return render(request, 'league/commissioner/generate_schedule.html', {
        'form': form,
        'team_count': team_count,
        'existing_weeks': existing_weeks,
    })


@commissioner_required
def free_agent_board(request):
    players = Player.objects.filter(fantasy_team__isnull=True).select_related('real_team')

    # Filters
    position = request.GET.get('position', '')
    school_id = request.GET.get('school', '')
    class_year = request.GET.get('class_year', '')
    search = request.GET.get('search', '').strip()
    sort = request.GET.get('sort', 'last_name')

    if position:
        players = players.filter(position=position)
    if school_id:
        players = players.filter(real_team_id=school_id)
    if class_year:
        players = players.filter(class_year=class_year)
    if search:
        players = players.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
        )

    # Sorting
    sort_options = {
        'last_name': 'last_name',
        'first_name': 'first_name',
        'position': 'position',
        'class_year': 'class_year',
        'school': 'real_team__name',
    }
    order_field = sort_options.get(sort, 'last_name')
    players = players.order_by(order_field, 'last_name', 'first_name')

    # Attach season stats for each free agent
    ps = PointSettings.load()
    player_data = []
    for p in players:
        season_pts = Decimal('0')
        for w in Week.objects.all():
            season_pts += calc_player_points_for_period(p, w.start_date, w.end_date, ps)

        if p.is_pitcher:
            agg = PitchingGameLog.objects.filter(player=p).aggregate(
                total_outs=Sum('outs'), total_so=Sum('so'),
                total_er=Sum('er'), total_bb=Sum('bb'),
            )
        else:
            agg = HittingGameLog.objects.filter(player=p).aggregate(
                total_ab=Sum('ab'), total_h=Sum('hits'), total_hr=Sum('hr'),
                total_rbi=Sum('rbi'), total_r=Sum('runs'), total_sb=Sum('sb'),
            )

        player_data.append({
            'player': p,
            'season_points': season_pts,
            'stats': agg,
        })

    # Sort by season points if requested
    if sort == 'points':
        player_data.sort(key=lambda x: x['season_points'], reverse=True)

    fantasy_teams = FantasyTeam.objects.filter(is_commissioner=False)
    real_teams = RealTeam.objects.all()

    return render(request, 'league/commissioner/free_agent_board.html', {
        'player_data': player_data,
        'fantasy_teams': fantasy_teams,
        'real_teams': real_teams,
        'position_choices': POSITION_CHOICES,
        'selected_position': position,
        'selected_school': school_id,
        'selected_class_year': class_year,
        'search': search,
        'sort': sort,
        'free_agent_count': len(player_data),
    })


@commissioner_required
def assign_player(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    if request.method == 'POST':
        team_id = request.POST.get('team_id')
        if team_id:
            team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)
            player.fantasy_team = team
            player.fantasy_team_since = datetime.date.today()
            player.save()
            messages.success(request, f'{player} assigned to {team.name}.')
        else:
            messages.error(request, 'No team selected.')
    return redirect(request.POST.get('next', 'league:free_agent_board'))


@commissioner_required
def edit_game_log(request, log_type, log_id):
    if log_type == 'hitting':
        log = get_object_or_404(
            HittingGameLog.objects.select_related('player', 'game'), pk=log_id
        )
        form = HittingGameLogForm(request.POST or None, instance=log)
        if request.method == 'POST' and form.is_valid():
            form.save()
            messages.success(request, 'Hitting game log updated.')
            return redirect('league:player_detail', player_id=log.player.id)
        template = 'league/commissioner/edit_game_log.html'
    else:
        log = get_object_or_404(
            PitchingGameLog.objects.select_related('player', 'game'), pk=log_id
        )
        initial = {
            'ip': log.ip_display,
            'hits': log.hits,
            'runs': log.runs,
            'er': log.er,
            'bb': log.bb,
            'so': log.so,
            'hr': log.hr,
            'win': log.win,
            'loss': log.loss,
            'save': log.save_game,
            'hold': log.hold,
        }
        form = PitchingGameLogForm(request.POST or None, initial=initial)
        if request.method == 'POST' and form.is_valid():
            log.outs = form.cleaned_data['ip']
            log.hits = form.cleaned_data['hits']
            log.runs = form.cleaned_data['runs']
            log.er = form.cleaned_data['er']
            log.bb = form.cleaned_data['bb']
            log.so = form.cleaned_data['so']
            log.hr = form.cleaned_data['hr']
            log.win = form.cleaned_data['win']
            log.loss = form.cleaned_data['loss']
            log.save_game = form.cleaned_data['save']
            log.hold = form.cleaned_data['hold']
            log.entered_by = request.fantasy_team
            log.save()
            messages.success(request, 'Pitching game log updated.')
            return redirect('league:player_detail', player_id=log.player.id)
        template = 'league/commissioner/edit_game_log.html'

    return render(request, template, {
        'form': form,
        'log': log,
        'log_type': log_type,
    })
