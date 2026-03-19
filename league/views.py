import datetime
from functools import wraps
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.html import format_html
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum, Q

from .models import (
    FantasyTeam, Player, RealTeam, RealGame,
    HittingGameLog, PitchingGameLog, PointSettings,
    Week, Matchup, RosterSlot, WeeklyLineupSlot, Transaction, PendingRequest, ActivityEntry,
    LeagueSettings, Trade, TradeItem, ExcludedDay, Coach,
    PITCHING_POSITIONS, POSITION_CHOICES, SLOT_LIMITS, SLOT_ELIGIBLE, SLOT_ORDER,
)
from .forms import (
    LoginForm, FantasyTeamForm, PlayerForm, RealTeamForm,
    RealGameForm, HittingGameLogForm, PitchingGameLogForm,
    PointSettingsForm, GenerateScheduleForm, MissingGameDisputeForm,
    ScrapeStatsForm,
)
from .scoring import (
    calc_team_weekly_points, calc_team_season_points,
    resolve_matchup, get_standings, get_player_weekly_breakdown,
    calc_hitting_points, calc_pitching_points, calc_player_points_for_period,
    refresh_player_points, refresh_all_players, calc_coach_points_for_period,
    refresh_all_coaches,
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


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.fantasy_team:
            messages.error(request, 'You must be logged in to do that.')
            return redirect('league:login')
        return view_func(request, *args, **kwargs)
    return wrapper


_ROSTER_MAX = sum(SLOT_LIMITS.values())


# --- Lock helper ---

import zoneinfo as _zoneinfo

_ET = _zoneinfo.ZoneInfo('America/New_York')


def _is_locked():
    """Return True if team transactions/lineup changes are currently locked.

    Automatic unlock window: Sunday 9:01 PM ET through Monday 3:29 PM ET.
    """
    settings = LeagueSettings.load()
    if settings.normal_mode:
        now = datetime.datetime.now(_ET)
        weekday = now.weekday()
        if weekday == 6:  # Sunday — unlocked from 9:01 PM onward
            unlock_start = now.replace(hour=21, minute=1, second=0, microsecond=0)
            return now < unlock_start
        if weekday == 0:  # Monday — unlocked until 3:29 PM
            unlock_end = now.replace(hour=15, minute=29, second=59, microsecond=999999)
            return now > unlock_end
        return True  # locked all other days
    return settings.manual_locked


def _is_sunday_unlock_window():
    """Return True if it's Sunday at or after 9:01 PM ET.

    Changes made during this window belong to the next week, not the current one.
    """
    settings = LeagueSettings.load()
    if not settings.normal_mode:
        return False
    now = datetime.datetime.now(_ET)
    if now.weekday() == 6:
        unlock_start = now.replace(hour=21, minute=1, second=0, microsecond=0)
        return now >= unlock_start
    return False


def _ensure_week_snapshot(team, week):
    """If no lineup snapshot exists for this team+week, create one from live roster slots.

    Called before drops/trades so the current week's roster is preserved even
    if the player is about to leave the team.
    """
    if WeeklyLineupSlot.objects.filter(fantasy_team=team, week=week).exists():
        return
    slots = RosterSlot.objects.filter(fantasy_team=team, player__isnull=False)
    WeeklyLineupSlot.objects.bulk_create([
        WeeklyLineupSlot(
            fantasy_team=team,
            week=week,
            slot_type=slot.slot_type,
            slot_number=slot.slot_number,
            player=slot.player,
        )
        for slot in slots
    ])


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
            if team.is_commissioner:
                return redirect('league:commissioner_panel')
            return redirect('league:roster', team_id=team.id)
        messages.error(request, 'Invalid team name or password.')
    return render(request, 'league/login.html', {'form': form})


def logout_view(request):
    request.session.flush()
    return redirect('league:login')


# --- Dashboard ---

@login_required
def team_settings(request):
    team = request.fantasy_team
    if request.method == 'POST':
        form_type = request.POST.get('form_type')
        if form_type == 'password':
            old_pw = request.POST.get('old_password', '')
            new_pw = request.POST.get('new_password', '').strip()
            confirm_pw = request.POST.get('confirm_password', '').strip()
            if not team.check_password(old_pw):
                messages.error(request, 'Current password is incorrect.')
            elif not new_pw:
                messages.error(request, 'New password cannot be blank.')
            elif new_pw != confirm_pw:
                messages.error(request, 'New passwords do not match.')
            else:
                team.set_password(new_pw)
                team.save()
                messages.success(request, 'Password updated.')
        else:
            display_name = request.POST.get('display_name', '').strip()
            team.display_name = display_name
            team.save()
            messages.success(request, 'Display name updated.')
        return redirect('league:team_settings')
    ps = PointSettings.load()
    league_settings = LeagueSettings.load()
    return render(request, 'league/team_settings.html', {
        'team': team,
        'ps': ps,
        'currently_locked': _is_locked(),
        'normal_mode': league_settings.normal_mode,
    })


def dashboard(request):
    team = request.fantasy_team
    if team:
        if team.is_commissioner:
            return redirect('league:commissioner_panel')
        return redirect('league:roster', team_id=team.id)
    return redirect('league:login')


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
    excluded_dates = list(ExcludedDay.objects.filter(week=matchup.week).values_list('date', flat=True))
    excluded_days = ExcludedDay.objects.filter(week=matchup.week).order_by('date')
    t1_pts, t2_pts, winner = resolve_matchup(matchup, ps)
    t1_breakdown = _matchup_team_breakdown(matchup.team_1, matchup.week, ps, excluded_dates=excluded_dates)
    t2_breakdown = _matchup_team_breakdown(matchup.team_2, matchup.week, ps, excluded_dates=excluded_dates) if matchup.team_2 else []

    return render(request, 'league/matchup.html', {
        'matchup': matchup,
        'current_week': matchup.week,
        'all_matchups': Matchup.objects.filter(week=matchup.week).select_related('team_1', 'team_2'),
        't1_pts': t1_pts,
        't2_pts': t2_pts,
        'winner': winner,
        't1_breakdown': t1_breakdown,
        't2_breakdown': t2_breakdown,
        'excluded_days': excluded_days,
        'back_url': reverse('league:schedule'),
    })


def _week_player_stats(player, start_date, end_date, excluded_dates=()):
    """Aggregate game log stats for a player within a date range."""
    if player.is_pitcher:
        qs = PitchingGameLog.objects.filter(
            player=player, game__date__gte=start_date, game__date__lte=end_date
        )
        if excluded_dates:
            qs = qs.exclude(game__date__in=excluded_dates)
        agg = qs.aggregate(
            total_outs=Sum('outs'), total_h=Sum('hits'), total_er=Sum('er'),
            total_bb=Sum('bb'), total_so=Sum('so'), total_hr=Sum('hr'),
            total_w=Sum('win'), total_l=Sum('loss'),
            total_sv=Sum('save_game'),
        )
    else:
        qs = HittingGameLog.objects.filter(
            player=player, game__date__gte=start_date, game__date__lte=end_date
        )
        if excluded_dates:
            qs = qs.exclude(game__date__in=excluded_dates)
        agg = qs.aggregate(
            total_ab=Sum('ab'), total_h=Sum('hits'), total_2b=Sum('doubles'),
            total_3b=Sum('triples'), total_hr=Sum('hr'), total_rbi=Sum('rbi'),
            total_r=Sum('runs'), total_bb=Sum('bb'), total_so=Sum('so'),
            total_sb=Sum('sb'), total_cs=Sum('cs'), total_hbp=Sum('hbp'),
        )
    return {k: (v or 0) for k, v in agg.items()}


def _matchup_team_breakdown(team, week, ps, excluded_dates=()):
    """Return slot-ordered breakdown: hitter_slots, pitcher_slots, bench_hitting, bench_pitching."""
    HITTER_ORDER = {'C': 0, 'IF': 1, 'OF': 2, 'DH': 3}
    today = datetime.date.today()
    use_snapshot = (
        week.end_date < today and
        WeeklyLineupSlot.objects.filter(fantasy_team=team, week=week).exists()
    )
    if use_snapshot:
        slots_qs = WeeklyLineupSlot.objects.filter(
            fantasy_team=team, week=week
        ).select_related('player__real_team').order_by('slot_type', 'slot_number')
    else:
        slots_qs = RosterSlot.objects.filter(
            fantasy_team=team
        ).select_related('player__real_team').order_by('slot_type', 'slot_number')

    hitter_slots = []
    pitcher_slots = []
    bench_hitting = []
    bench_pitching = []
    slotted_player_ids = set()

    for slot in slots_qs:
        player = slot.player
        if player:
            pts = calc_player_points_for_period(player, week.start_date, week.end_date, ps, excluded_dates=excluded_dates)
            stats = _week_player_stats(player, week.start_date, week.end_date, excluded_dates=excluded_dates)
            slotted_player_ids.add(player.pk)
        else:
            pts = Decimal('0')
            stats = {}
        row = {'slot_type': slot.slot_type, 'player': player, 'points': pts, 'stats': stats}

        if slot.slot_type in HITTER_ORDER:
            hitter_slots.append(row)
        elif slot.slot_type == 'P':
            pitcher_slots.append(row)
        elif slot.slot_type == 'BN':
            if player and player.is_pitcher:
                bench_pitching.append(row)
            elif player:
                bench_hitting.append(row)

    hitter_slots.sort(key=lambda x: HITTER_ORDER.get(x['slot_type'], 99))

    if not use_snapshot:
        # Include rostered players not assigned to any slot (live view only)
        unslotted = Player.objects.filter(fantasy_team=team).select_related('real_team').exclude(pk__in=slotted_player_ids)
        for player in unslotted:
            pts = calc_player_points_for_period(player, week.start_date, week.end_date, ps, excluded_dates=excluded_dates)
            stats = _week_player_stats(player, week.start_date, week.end_date, excluded_dates=excluded_dates)
            row = {'slot_type': 'BN', 'player': player, 'points': pts, 'stats': stats}
            if player.is_pitcher:
                bench_pitching.append(row)
            else:
                bench_hitting.append(row)

    coach_slots = []
    for coach in Coach.objects.filter(fantasy_team=team).select_related('real_team'):
        pts = calc_coach_points_for_period(coach, week.start_date, week.end_date, ps, excluded_dates=excluded_dates)
        wins = RealGame.objects.filter(
            winner=coach.real_team,
            date__gte=week.start_date,
            date__lte=week.end_date,
        ).count()
        coach_slots.append({'coach': coach, 'points': pts, 'wins': wins})

    coach_total_wins = sum(c['wins'] for c in coach_slots)
    coach_total_points = sum(c['points'] for c in coach_slots)

    return {
        'hitter_slots': hitter_slots,
        'pitcher_slots': pitcher_slots,
        'bench_hitting': bench_hitting,
        'bench_pitching': bench_pitching,
        'coach_slots': coach_slots,
        'coach_total_wins': coach_total_wins,
        'coach_total_points': coach_total_points,
    }


def matchup_view(request):
    today = datetime.date.today()
    current_week = (
        Week.objects.filter(start_date__lte=today, end_date__gte=today).first()
        or Week.objects.filter(end_date__lt=today).last()
    )
    if not current_week:
        return render(request, 'league/matchup.html', {'no_week': True, 'current_week': None})

    all_matchups = Matchup.objects.filter(week=current_week).select_related(
        'week', 'team_1', 'team_2'
    )

    # Determine which matchup to show
    selected_id = request.GET.get('matchup_id')
    matchup = None
    if selected_id:
        matchup = all_matchups.filter(pk=selected_id).first()
    if matchup is None and request.fantasy_team:
        team = request.fantasy_team
        matchup = all_matchups.filter(
            Q(team_1=team) | Q(team_2=team)
        ).first()
    if matchup is None:
        matchup = all_matchups.first()
    if matchup is None:
        return render(request, 'league/matchup.html', {'no_week': True, 'current_week': current_week})

    ps = PointSettings.load()
    excluded_dates = list(ExcludedDay.objects.filter(week=matchup.week).values_list('date', flat=True))
    excluded_days = ExcludedDay.objects.filter(week=matchup.week).order_by('date')
    t1_pts, t2_pts, winner = resolve_matchup(matchup, ps)
    t1_breakdown = _matchup_team_breakdown(matchup.team_1, matchup.week, ps, excluded_dates=excluded_dates)
    t2_breakdown = _matchup_team_breakdown(matchup.team_2, matchup.week, ps, excluded_dates=excluded_dates) if matchup.team_2 else []

    return render(request, 'league/matchup.html', {
        'matchup': matchup,
        'all_matchups': all_matchups,
        'current_week': current_week,
        't1_pts': t1_pts,
        't2_pts': t2_pts,
        'winner': winner,
        't1_breakdown': t1_breakdown,
        't2_breakdown': t2_breakdown,
        'excluded_days': excluded_days,
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

    all_weeks = Week.objects.order_by('start_date')

    # Week selector: ?week_id= shows a past week's snapshot (read-only)
    selected_week = None
    is_locked_week = False
    week_id_param = request.GET.get('week_id')
    if week_id_param:
        selected_week = Week.objects.filter(pk=week_id_param).first()
        if selected_week and selected_week.end_date < today:
            is_locked_week = True
        else:
            selected_week = None  # invalid or current/future week — ignore

    stat_range = request.GET.get('range', 'season')
    if stat_range == 'today':
        stat_start, stat_end = today, today
    elif stat_range == 'week' and current_week:
        stat_start, stat_end = current_week.start_date, current_week.end_date
    else:
        stat_range = 'season'
        first_week = Week.objects.order_by('start_date').first()
        stat_start = first_week.start_date if first_week else today
        stat_end = today

    def player_row(player):
        pts = calc_player_points_for_period(player, stat_start, stat_end, ps)
        stats = _week_player_stats(player, stat_start, stat_end)
        return {
            'player': player,
            'points': pts,
            'is_pitcher': player.is_pitcher,
            'stats': stats,
        }

    if is_locked_week:
        # Show frozen snapshot for this past week
        snapshot_slots = list(
            WeeklyLineupSlot.objects.filter(fantasy_team=team, week=selected_week)
            .select_related('player__real_team')
            .order_by('slot_type', 'slot_number')
        )
        slot_rows = []
        bench_player_ids = set()
        for snap in snapshot_slots:
            if snap.slot_type == 'BN':
                bench_player_ids.add(snap.player_id) if snap.player_id else None
                continue
            row = player_row(snap.player) if snap.player else None
            # Wrap in a dict that mimics the live slot_rows structure
            slot_rows.append({'slot': snap, 'row': row})
        unslotted = []
        for snap in snapshot_slots:
            if snap.slot_type == 'BN' and snap.player:
                unslotted.append(player_row(snap.player))
        incoming_trades = []
        outgoing_trades = []
    else:
        # Build ordered slot list from live RosterSlots
        slots_qs = RosterSlot.objects.filter(fantasy_team=team).select_related('player__real_team')
        slots_by_key = {(s.slot_type, s.slot_number): s for s in slots_qs}
        slot_rows = []
        slotted_ids = set()
        for slot_type, count in sorted(SLOT_LIMITS.items(), key=lambda x: SLOT_ORDER[x[0]]):
            if slot_type == 'BN':
                continue  # bench shown via unslotted; BN slots must not hide players
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

        incoming_trades = list(Trade.objects.filter(receiver=team, status='pending'))
        outgoing_trades = list(Trade.objects.filter(sender=team, status='pending'))

    # Coaches for this team
    coach_qs = Coach.objects.filter(fantasy_team=team).select_related('real_team')
    coach_rows = []
    for c in coach_qs:
        pts = calc_coach_points_for_period(c, stat_start, stat_end, ps)
        wins = RealGame.objects.filter(winner=c.real_team, date__gte=stat_start, date__lte=stat_end).count()
        coach_rows.append({'coach': c, 'points': pts, 'wins': wins})

    return render(request, 'league/roster.html', {
        'viewed_team': team,
        'slot_rows': slot_rows,
        'unslotted': unslotted,
        'coach_rows': coach_rows,
        'current_week': current_week,
        'stat_range': stat_range,
        'incoming_trades': incoming_trades,
        'outgoing_trades': outgoing_trades,
        'all_weeks': all_weeks,
        'selected_week': selected_week,
        'is_locked_week': is_locked_week,
        'today': today,
    })


def set_lineup(request, team_id):
    team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)

    # Only the team owner or commissioner can edit
    if not request.fantasy_team.is_commissioner and request.fantasy_team != team:
        messages.error(request, 'You can only edit your own lineup.')
        return redirect('league:roster', team_id=team_id)
    if not request.fantasy_team.is_commissioner and _is_locked():
        messages.error(request, format_html(
            'Lineup changes are currently locked. <a href="{}">View info</a>',
            '/settings/'
        ))
        return redirect('league:roster', team_id=team_id)

    RosterSlot.create_for_team(team)
    rostered = list(Player.objects.filter(fantasy_team=team).select_related('real_team').order_by('position', 'last_name'))

    if request.method == 'POST':
        # Clear all slot assignments first to avoid OneToOne conflicts
        RosterSlot.objects.filter(fantasy_team=team).update(player=None)

        player_map = {p.id: p for p in rostered}
        assigned_ids = []
        errors = []
        pending = []

        # Process active slots (C/IF/OF/DH/P)
        for slot_type, count in SLOT_LIMITS.items():
            if slot_type == 'BN':
                continue
            for n in range(1, count + 1):
                field = f'slot_{slot_type}_{n}'
                raw = request.POST.get(field, '').strip()
                if not raw:
                    continue
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

        # Process bench slots dynamically (variable count from form)
        for key in request.POST:
            if not key.startswith('slot_BN_'):
                continue
            raw = request.POST[key].strip()
            if not raw:
                continue
            try:
                n = int(key[len('slot_BN_'):])
                pid = int(raw)
            except ValueError:
                continue
            player = player_map.get(pid)
            if not player:
                errors.append('Unknown player for bench slot.')
                continue
            if pid in assigned_ids:
                errors.append(f'{player} assigned to multiple slots.')
                continue
            assigned_ids.append(pid)
            pending.append(('BN', n, pid))

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            with transaction.atomic():
                for slot_type, n, pid in pending:
                    RosterSlot.objects.update_or_create(
                        fantasy_team=team, slot_type=slot_type, slot_number=n,
                        defaults={'player_id': pid},
                    )
                # Snapshot lineup for the appropriate week.
                # If it's the Sunday PM unlock window, snapshot to next week
                # so the current (ending) week's roster is preserved.
                today = datetime.date.today()
                if _is_sunday_unlock_window():
                    snapshot_week = Week.objects.filter(
                        start_date=today + datetime.timedelta(days=1)
                    ).first()
                else:
                    snapshot_week = Week.objects.filter(
                        start_date__lte=today, end_date__gte=today
                    ).first()
                if snapshot_week:
                    WeeklyLineupSlot.objects.filter(fantasy_team=team, week=snapshot_week).delete()
                    WeeklyLineupSlot.objects.bulk_create([
                        WeeklyLineupSlot(
                            fantasy_team=team, week=snapshot_week,
                            slot_type=slot_type, slot_number=n, player_id=pid
                        )
                        for slot_type, n, pid in pending
                    ])
            messages.success(request, 'Lineup saved.')
            return redirect('league:roster', team_id=team_id)
        return redirect('league:set_lineup', team_id=team_id)

    # Build active slot sections (C/IF/OF/DH/P only)
    slots_qs = RosterSlot.objects.filter(fantasy_team=team).select_related('player')
    slots_by_key = {(s.slot_type, s.slot_number): s for s in slots_qs}
    slot_sections = []
    section_labels = {'C': 'Catcher', 'IF': 'Infield', 'OF': 'Outfield',
                      'DH': 'Designated Hitter', 'P': 'Pitchers'}
    active_player_ids = set()
    for slot_type, count in sorted(SLOT_LIMITS.items(), key=lambda x: SLOT_ORDER[x[0]]):
        if slot_type == 'BN':
            continue
        eligible = [p for p in rostered if p.position in SLOT_ELIGIBLE[slot_type]]
        rows = []
        for n in range(1, count + 1):
            slot = slots_by_key.get((slot_type, n))
            if slot and slot.player_id:
                active_player_ids.add(slot.player_id)
            rows.append({'slot': slot, 'eligible_players': eligible})
        slot_sections.append({
            'label': section_labels[slot_type],
            'slot_type': slot_type,
            'rows': rows,
        })

    # Bench: every rostered player not in an active slot, pre-filled + one empty
    bench_players = [p for p in rostered if p.id not in active_player_ids]
    bench_rows = [{'n': i + 1, 'player': p} for i, p in enumerate(bench_players)]
    bench_next_n = len(bench_rows) + 2  # number JS will use for the next dynamic slot

    return render(request, 'league/set_lineup.html', {
        'team': team,
        'slot_sections': slot_sections,
        'bench_rows': bench_rows,
        'bench_next_n': bench_next_n,
        'rostered': rostered,
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
        refresh_player_points(player)
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
        log.entered_by = request.fantasy_team
        log.save()
        refresh_player_points(player)
        messages.success(request, f'Pitching stats saved for {player}.')
        return redirect('league:stat_entry_select', game_id=game.id)

    return render(request, 'league/stat_entry_pitching.html', {
        'form': form,
        'game': game,
        'player': player,
        'editing': existing is not None,
    })


def coach_detail(request, coach_id):
    coach = get_object_or_404(
        Coach.objects.select_related('real_team', 'fantasy_team'), pk=coach_id
    )
    ps = PointSettings.load()
    games = RealGame.objects.filter(
        winner=coach.real_team
    ).select_related('home_team', 'away_team').order_by('-date', '-game_number')

    log_data = []
    for game in games:
        log_data.append({'game': game, 'points': ps.coach_win})

    total_wins = len(log_data)
    total_points = ps.coach_win * total_wins

    user_has_coach = (
        request.fantasy_team is not None and
        not request.fantasy_team.is_commissioner and
        Coach.objects.filter(fantasy_team=request.fantasy_team).exists()
    )

    return render(request, 'league/coach_detail.html', {
        'coach': coach,
        'log_data': log_data,
        'total_wins': total_wins,
        'total_points': total_points,
        'user_has_coach': user_has_coach,
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

def transaction_log(request):
    add_drops = Transaction.objects.select_related('fantasy_team', 'player__real_team', 'coach__real_team').all()
    dispute_entries = ActivityEntry.objects.filter(
        entry_type__in=['dispute_submitted', 'dispute_approved', 'dispute_denied', 'dispute_cancelled']
    ).select_related('fantasy_team', 'player__real_team', 'coach__real_team')
    accepted_trades = Trade.objects.filter(status='accepted').prefetch_related(
        'items__player__real_team'
    ).select_related('sender', 'receiver')

    feed = []
    for t in add_drops:
        feed.append({'kind': t.transaction_type, 'timestamp': t.timestamp,
                     'player': t.player, 'coach': t.coach, 'fantasy_team': t.fantasy_team,
                     'description': t.notes})
    for e in dispute_entries:
        feed.append({'kind': e.entry_type, 'timestamp': e.created_at,
                     'player': e.player, 'coach': e.coach, 'fantasy_team': e.fantasy_team,
                     'description': e.description})
    for trade in accepted_trades:
        give_names = ', '.join(
            str(i.player or i.coach) for i in trade.items.all() if i.direction == 'give'
        ) or 'nothing'
        receive_names = ', '.join(
            str(i.player or i.coach) for i in trade.items.all() if i.direction == 'receive'
        ) or 'nothing'
        feed.append({
            'kind': 'trade',
            'timestamp': trade.created_at,
            'player': None,
            'coach': None,
            'fantasy_team': trade.sender,
            'description': f'{trade.sender} traded {give_names} to {trade.receiver} for {receive_names}',
            'trade_id': trade.pk,
        })
    feed.sort(key=lambda x: x['timestamp'], reverse=True)
    return render(request, 'league/transaction_log.html', {'feed': feed})


_SORT_FIELD_MAP = {
    'season_points': 'cached_season_points',
    'weekly_points': 'cached_weekly_points',
    'ppg':           'cached_ppg',
}
_PER_PAGE_OPTIONS = [25, 50, 100, 250]


def players_list(request):
    real_team_id     = request.GET.get('real_team', '')
    position         = request.GET.get('position', '')
    fantasy_team_id  = request.GET.get('fantasy_team', '')
    show_all         = request.GET.get('show_all', '')
    search           = request.GET.get('search', '').strip()
    sort             = request.GET.get('sort', 'season_points')
    order            = request.GET.get('order', 'desc')
    try:
        per_page = int(request.GET.get('per_page', 50))
        if per_page not in _PER_PAGE_OPTIONS:
            per_page = 50
    except ValueError:
        per_page = 50

    today = datetime.date.today()
    current_week = Week.objects.filter(
        start_date__lte=today, end_date__gte=today
    ).first()
    if not current_week:
        current_week = Week.objects.filter(end_date__lt=today).last()

    real_teams    = RealTeam.objects.exclude(abbreviation='OOC')
    fantasy_teams = FantasyTeam.objects.filter(is_commissioner=False)

    # --- Coach mode ---
    if position == 'coaches':
        coaches = Coach.objects.select_related('real_team', 'fantasy_team').all()
        if real_team_id:
            coaches = coaches.filter(real_team_id=real_team_id)
        if fantasy_team_id:
            coaches = coaches.filter(fantasy_team_id=fantasy_team_id)
        if not show_all and not fantasy_team_id:
            coaches = coaches.filter(fantasy_team__isnull=True)
        if search:
            coaches = coaches.filter(
                Q(first_name__icontains=search) | Q(last_name__icontains=search)
            )
        if sort == 'weekly_points':
            coaches = coaches.order_by(
                'cached_weekly_points' if order == 'asc' else '-cached_weekly_points',
                'last_name'
            )
        elif sort == 'season_points':
            coaches = coaches.order_by(
                'cached_season_points' if order == 'asc' else '-cached_season_points',
                'last_name'
            )
        else:
            coaches = coaches.order_by('last_name', 'first_name')

        paginator = Paginator(coaches, per_page)
        page_obj = paginator.get_page(request.GET.get('page', 1))
        coach_data = [{'coach': c} for c in page_obj]

        user_has_coach = (
            request.fantasy_team is not None and
            not request.fantasy_team.is_commissioner and
            Coach.objects.filter(fantasy_team=request.fantasy_team).exists()
        )

        return render(request, 'league/verification.html', {
            'coach_data':           coach_data,
            'player_data':          [],
            'page_obj':             page_obj,
            'per_page':             per_page,
            'per_page_options':     _PER_PAGE_OPTIONS,
            'real_teams':           real_teams,
            'fantasy_teams':        fantasy_teams,
            'position_choices':     POSITION_CHOICES,
            'selected_real_team':   real_team_id,
            'selected_position':    position,
            'selected_fantasy_team':fantasy_team_id,
            'show_all':             show_all,
            'search':               search,
            'current_week':         current_week,
            'sort':                 sort,
            'order':                order,
            'user_has_coach':       user_has_coach,
        })

    # --- Player mode ---
    players = Player.objects.select_related('real_team', 'fantasy_team').all()

    if real_team_id:
        players = players.filter(real_team_id=real_team_id)
    if position == 'hitters':
        players = players.exclude(position='P')
    elif position:
        players = players.filter(position=position)
    if fantasy_team_id:
        players = players.filter(fantasy_team_id=fantasy_team_id)
    if not show_all and not fantasy_team_id:
        players = players.filter(fantasy_team__isnull=True)
    if search:
        players = players.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
        )

    db_field = _SORT_FIELD_MAP.get(sort, 'cached_season_points')
    players = players.order_by(db_field if order == 'asc' else f'-{db_field}')

    paginator = Paginator(players, per_page)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    player_data = [{
        'player':        p,
        'season_points': p.cached_season_points,
        'weekly_points': p.cached_weekly_points,
        'games_played':  p.cached_games_played,
        'ppg':           p.cached_ppg,
    } for p in page_obj]

    return render(request, 'league/verification.html', {
        'player_data':          player_data,
        'page_obj':             page_obj,
        'per_page':             per_page,
        'per_page_options':     _PER_PAGE_OPTIONS,
        'real_teams':           real_teams,
        'fantasy_teams':        fantasy_teams,
        'position_choices':     POSITION_CHOICES,
        'selected_real_team':   real_team_id,
        'selected_position':    position,
        'selected_fantasy_team':fantasy_team_id,
        'show_all':             show_all,
        'search':               search,
        'current_week':         current_week,
        'sort':                 sort,
        'order':                order,
    })


def player_detail(request, player_id):
    player = get_object_or_404(
        Player.objects.select_related('real_team', 'fantasy_team'), pk=player_id
    )
    ps = PointSettings.load()

    if player.is_pitcher:
        logs = PitchingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by').order_by('-game__date', '-game__game_number')
        log_data = []
        totals = {'outs': 0, 'hits': 0, 'er': 0, 'bb': 0, 'so': 0, 'hr': 0,
                  'w': 0, 'l': 0, 'sv': 0, 'points': Decimal('0')}
        for log in logs:
            pts = calc_pitching_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'pitching'})
            totals['outs']   += log.outs
            totals['hits']   += log.hits
            totals['er']     += log.er
            totals['bb']     += log.bb
            totals['so']     += log.so
            totals['hr']     += log.hr
            totals['w']      += int(log.win)
            totals['l']      += int(log.loss)
            totals['sv']     += int(log.save_game)
            totals['points'] += pts
    else:
        logs = HittingGameLog.objects.filter(
            player=player
        ).select_related('game__home_team', 'game__away_team', 'entered_by').order_by('-game__date', '-game__game_number')
        log_data = []
        totals = {'ab': 0, 'runs': 0, 'hits': 0, 'doubles': 0, 'triples': 0,
                  'hr': 0, 'rbi': 0, 'bb': 0, 'so': 0, 'sb': 0, 'cs': 0,
                  'hbp': 0, 'points': Decimal('0')}
        for log in logs:
            pts = calc_hitting_points(log, ps)
            log_data.append({'log': log, 'points': pts, 'type': 'hitting'})
            totals['ab']      += log.ab
            totals['runs']    += log.runs
            totals['hits']    += log.hits
            totals['doubles'] += log.doubles
            totals['triples'] += log.triples
            totals['hr']      += log.hr
            totals['rbi']     += log.rbi
            totals['bb']      += log.bb
            totals['so']      += log.so
            totals['sb']      += log.sb
            totals['cs']      += log.cs
            totals['hbp']     += log.hbp
            totals['points']  += pts

    return render(request, 'league/verification_detail.html', {
        'player': player,
        'log_data': log_data,
        'totals': totals,
    })


# --- Member Add/Drop Views ---

@login_required
def member_add_player(request, player_id):
    if request.method != 'POST':
        return redirect('league:players')
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Use the commissioner panel to manage rosters.')
        return redirect('league:players')
    if _is_locked():
        messages.error(request, format_html(
            'Team transactions are currently locked. <a href="{}">View info</a>',
            '/settings/'
        ))
        return redirect('league:roster', team_id=team.pk)
    player = get_object_or_404(Player, pk=player_id)
    if player.fantasy_team is not None:
        messages.error(request, f'{player.first_name} {player.last_name} is already on a team.')
        return redirect('league:roster', team_id=team.pk)
    roster_count = Player.objects.filter(fantasy_team=team).count()
    if roster_count >= _ROSTER_MAX:
        messages.error(
            request,
            f'Your roster is full ({roster_count}/{_ROSTER_MAX}). Drop a player first.'
        )
        return redirect('league:roster', team_id=team.pk)
    player.fantasy_team = team
    # If it's the Sunday PM unlock window, attribute the add to next Monday
    # so it doesn't affect the current week's scoring.
    if _is_sunday_unlock_window():
        player.fantasy_team_since = datetime.date.today() + datetime.timedelta(days=1)
    else:
        player.fantasy_team_since = datetime.date.today()
    player.save()
    Transaction.objects.create(transaction_type='add', fantasy_team=team, player=player)
    refresh_player_points(player)
    messages.success(request, f'{player.first_name} {player.last_name} added to your roster.')
    return redirect(request.POST.get('next', 'league:players'))


@login_required
def member_drop_player(request, player_id):
    if request.method != 'POST':
        return redirect('league:players')
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Use the commissioner panel to manage rosters.')
        return redirect('league:players')
    if _is_locked():
        messages.error(request, format_html(
            'Team transactions are currently locked. <a href="{}">View info</a>',
            '/settings/'
        ))
        return redirect('league:player_detail', player_id=player_id)
    player = get_object_or_404(Player, pk=player_id)
    if player.fantasy_team != team:
        messages.error(request, 'That player is not on your roster.')
        return redirect('league:player_detail', player_id=player_id)
    # Snapshot current week's roster before the drop so the dropped player's
    # stats still count for this week even if no lineup was saved yet.
    today = datetime.date.today()
    current_week = Week.objects.filter(start_date__lte=today, end_date__gte=today).first()
    if current_week:
        _ensure_week_snapshot(team, current_week)
    Transaction.objects.create(transaction_type='drop', fantasy_team=team, player=player)
    # Remove from any roster slot
    RosterSlot.objects.filter(fantasy_team=team, player=player).update(player=None)
    player.fantasy_team = None
    player.fantasy_team_since = None
    player.save()
    refresh_player_points(player)
    messages.success(request, f'{player.first_name} {player.last_name} dropped to free agency.')
    return redirect(request.POST.get('next', 'league:roster'), team.id)


@login_required
def member_add_coach(request, coach_id):
    if request.method != 'POST':
        return redirect('league:players')
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Use the commissioner panel to manage rosters.')
        return redirect('league:players')
    if _is_locked():
        messages.error(request, format_html(
            'Team transactions are currently locked. <a href="{}">View info</a>',
            '/settings/'
        ))
        return redirect('league:roster', team_id=team.pk)
    if Coach.objects.filter(fantasy_team=team).exists():
        messages.error(request, 'You already have a coach on your roster. Drop them before adding another.')
        return redirect(request.POST.get('next', 'league:players'))
    coach = get_object_or_404(Coach, pk=coach_id)
    if coach.fantasy_team is not None:
        messages.error(request, f'{coach} is already on a team.')
        return redirect('league:roster', team_id=team.pk)
    coach.fantasy_team = team
    if _is_sunday_unlock_window():
        coach.fantasy_team_since = datetime.date.today() + datetime.timedelta(days=1)
    else:
        coach.fantasy_team_since = datetime.date.today()
    coach.save()
    Transaction.objects.create(transaction_type='add', fantasy_team=team, coach=coach)
    from league.scoring import refresh_coach_points
    refresh_coach_points(coach)
    messages.success(request, f'{coach} added to your roster.')
    return redirect(request.POST.get('next', 'league:players'))


@login_required
def member_drop_coach(request, coach_id):
    if request.method != 'POST':
        return redirect('league:players')
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Use the commissioner panel to manage rosters.')
        return redirect('league:players')
    if _is_locked():
        messages.error(request, format_html(
            'Team transactions are currently locked. <a href="{}">View info</a>',
            '/settings/'
        ))
        return redirect('league:coach_detail', coach_id=coach_id)
    coach = get_object_or_404(Coach, pk=coach_id)
    if coach.fantasy_team != team:
        messages.error(request, 'That coach is not on your roster.')
        return redirect('league:coach_detail', coach_id=coach_id)
    Transaction.objects.create(transaction_type='drop', fantasy_team=team, coach=coach)
    coach.fantasy_team = None
    coach.fantasy_team_since = None
    coach.save()
    from league.scoring import refresh_coach_points
    refresh_coach_points(coach)
    messages.success(request, f'{coach} dropped to free agency.')
    return redirect(request.POST.get('next', 'league:roster'), team.id)


# --- Dispute Views ---

@login_required
def dispute_list(request):
    team = request.fantasy_team
    disputes = PendingRequest.objects.filter(
        request_type__in=['stat_modify', 'missing_game', 'coach_win'], submitted_by=team
    ).select_related('player__real_team', 'game', 'reviewed_by', 'coach__real_team').order_by('-submitted_at')
    my_coaches = Coach.objects.filter(fantasy_team=team)
    return render(request, 'league/disputes.html', {'disputes': disputes, 'my_coaches': my_coaches})


@login_required
def dispute_select_player(request):
    team = request.fantasy_team
    if team.is_commissioner:
        return redirect('league:commissioner_disputes')
    q = request.GET.get('q', '').strip()
    players = []
    if q:
        players = Player.objects.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
        ).select_related('real_team').order_by('last_name', 'first_name')
    return render(request, 'league/dispute_select_player.html', {'q': q, 'players': players})


@login_required
def dispute_select_game(request, player_id):
    team = request.fantasy_team
    if team.is_commissioner:
        return redirect('league:commissioner_disputes')
    player = get_object_or_404(Player, pk=player_id)
    if player.is_pitcher:
        logs = PitchingGameLog.objects.filter(player=player).select_related('game').order_by('-game__date')
    else:
        logs = HittingGameLog.objects.filter(player=player).select_related('game').order_by('-game__date')
    return render(request, 'league/dispute_select_game.html', {'player': player, 'logs': logs})


@login_required
def submit_dispute(request, player_id, game_id):
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Commissioners cannot submit disputes.')
        return redirect('league:players')
    player = get_object_or_404(Player, pk=player_id)
    game = get_object_or_404(RealGame, pk=game_id)

    existing = PendingRequest.objects.filter(
        request_type='stat_modify', submitted_by=team,
        player=player, game=game, status='pending'
    ).first()
    if existing:
        messages.warning(request, 'You already have a pending dispute for this game.')
        return redirect('league:dispute_list')

    if player.is_pitcher:
        log = PitchingGameLog.objects.filter(player=player, game=game).first()
        if not log:
            messages.error(request, 'No stats have been entered for this player in that game yet.')
            return redirect('league:player_detail', player_id=player.id)
        stat_type = 'pitching'
        initial = {
            'ip': log.ip_display, 'hits': log.hits, 'runs': log.runs,
            'er': log.er, 'bb': log.bb, 'so': log.so, 'hr': log.hr,
            'win': log.win, 'loss': log.loss, 'save': log.save_game,
        }
        FormClass = PitchingGameLogForm
    else:
        log = HittingGameLog.objects.filter(player=player, game=game).first()
        if not log:
            messages.error(request, 'No stats have been entered for this player in that game yet.')
            return redirect('league:player_detail', player_id=player.id)
        stat_type = 'hitting'
        initial = None
        FormClass = HittingGameLogForm

    if request.method == 'POST':
        if request.POST.get('action') == 'remove_game':
            existing_remove = PendingRequest.objects.filter(
                request_type='stat_modify', stat_type='remove',
                submitted_by=team, player=player, game=game, status='pending'
            ).first()
            if existing_remove:
                messages.warning(request, 'You already have a pending removal request for this game.')
                return redirect('league:dispute_list')
            PendingRequest.objects.create(
                request_type='stat_modify', submitted_by=team,
                player=player, game=game, stat_type='remove',
                user_message=request.POST.get('remove_message', '').strip(),
            )
            ActivityEntry.objects.create(
                entry_type='dispute_submitted', fantasy_team=team, player=player,
                description=f'{team.name} requested removal of {player} stats for {game}',
            )
            messages.success(request, 'Game removal request submitted.')
            return redirect('league:dispute_list')

        if stat_type == 'hitting':
            form = FormClass(request.POST, instance=log)
        else:
            form = FormClass(request.POST, initial=initial)
        if form.is_valid():
            user_message = request.POST.get('user_message', '').strip()
            if stat_type == 'hitting':
                cd = form.cleaned_data
                proposed = {
                    'ab': cd['ab'], 'runs': cd['runs'], 'hits': cd['hits'],
                    'doubles': cd['doubles'], 'triples': cd['triples'], 'hr': cd['hr'],
                    'rbi': cd['rbi'], 'bb': cd['bb'], 'so': cd['so'],
                    'sb': cd['sb'], 'cs': cd['cs'], 'hbp': cd['hbp'],
                }
            else:
                cd = form.cleaned_data
                proposed = {
                    'outs': cd['ip'], 'hits': cd['hits'], 'runs': cd['runs'],
                    'er': cd['er'], 'bb': cd['bb'], 'so': cd['so'], 'hr': cd['hr'],
                    'win': cd['win'], 'loss': cd['loss'],
                    'save_game': cd['save'],
                }
            PendingRequest.objects.create(
                request_type='stat_modify', submitted_by=team,
                player=player, game=game, stat_type=stat_type,
                proposed_data=proposed, user_message=user_message,
            )
            ActivityEntry.objects.create(
                entry_type='dispute_submitted', fantasy_team=team, player=player,
                description=f'{team.name} disputed stats for {player} vs {game}',
            )
            messages.success(request, 'Dispute submitted successfully.')
            return redirect('league:dispute_list')
    else:
        if stat_type == 'hitting':
            form = FormClass(instance=log)
        else:
            form = FormClass(initial=initial)

    return render(request, 'league/submit_dispute.html', {
        'player': player, 'game': game, 'log': log,
        'stat_type': stat_type, 'form': form,
    })


@login_required
def cancel_dispute(request, dispute_id):
    if request.method != 'POST':
        return redirect('league:dispute_list')
    team = request.fantasy_team
    dispute = get_object_or_404(PendingRequest, pk=dispute_id, request_type__in=['stat_modify', 'missing_game', 'coach_win'], submitted_by=team)
    if dispute.status != 'pending':
        messages.error(request, 'Only pending disputes can be cancelled.')
        return redirect('league:dispute_list')
    dispute.status = 'cancelled'
    dispute.save()
    if dispute.request_type == 'coach_win':
        description = f'{team.name} cancelled coach win dispute for {dispute.coach} in {dispute.game}'
    else:
        description = f'{team.name} cancelled dispute for {dispute.player} in {dispute.game}'
    ActivityEntry.objects.create(
        entry_type='dispute_cancelled', fantasy_team=team,
        player=dispute.player,
        coach=dispute.coach if dispute.request_type == 'coach_win' else None,
        description=description,
    )
    messages.success(request, 'Dispute cancelled.')
    return redirect('league:dispute_list')


@login_required
def submit_missing_game_dispute(request, player_id):
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Commissioners cannot submit disputes.')
        return redirect('league:players')
    player = get_object_or_404(Player, pk=player_id)

    stat_type = 'pitching' if player.is_pitcher else 'hitting'
    StatFormClass = PitchingGameLogForm if player.is_pitcher else HittingGameLogForm

    if request.method == 'POST':
        form = MissingGameDisputeForm(request.POST)
        stat_form = StatFormClass(request.POST)
        if form.is_valid() and stat_form.is_valid():
            date_val = form.cleaned_data['date']
            opponent = form.cleaned_data['opponent']
            user_message = request.POST.get('user_message', '').strip()

            existing = PendingRequest.objects.filter(
                request_type='missing_game', submitted_by=team, player=player,
                status='pending',
                proposed_data__date=str(date_val),
                proposed_data__opponent=opponent,
            ).first()
            if existing:
                messages.warning(request, 'You already have a pending dispute for this game.')
                return redirect('league:dispute_list')

            cd = stat_form.cleaned_data
            if stat_type == 'hitting':
                proposed = {
                    'date': str(date_val), 'opponent': opponent,
                    'ab': cd['ab'], 'runs': cd['runs'], 'hits': cd['hits'],
                    'doubles': cd['doubles'], 'triples': cd['triples'], 'hr': cd['hr'],
                    'rbi': cd['rbi'], 'bb': cd['bb'], 'so': cd['so'],
                    'sb': cd['sb'], 'cs': cd['cs'], 'hbp': cd['hbp'],
                }
            else:
                proposed = {
                    'date': str(date_val), 'opponent': opponent,
                    'outs': cd['ip'], 'hits': cd['hits'], 'runs': cd['runs'],
                    'er': cd['er'], 'bb': cd['bb'], 'so': cd['so'], 'hr': cd['hr'],
                    'win': cd['win'], 'loss': cd['loss'],
                    'save_game': cd['save'],
                }

            PendingRequest.objects.create(
                request_type='missing_game', submitted_by=team,
                player=player, stat_type=stat_type,
                proposed_data=proposed,
                user_message=user_message,
            )
            ActivityEntry.objects.create(
                entry_type='dispute_submitted', fantasy_team=team, player=player,
                description=f'{team.name} reported a missing game for {player} on {date_val} vs {opponent}',
            )
            messages.success(request, 'Missing game dispute submitted successfully.')
            return redirect('league:dispute_list')
    else:
        form = MissingGameDisputeForm()
        stat_form = StatFormClass()

    return render(request, 'league/submit_missing_game_dispute.html', {
        'player': player, 'form': form, 'stat_form': stat_form, 'stat_type': stat_type,
    })


@commissioner_required
def commissioner_disputes(request):
    disputes = PendingRequest.objects.filter(
        request_type__in=['stat_modify', 'missing_game', 'coach_win']
    ).select_related('submitted_by', 'player__real_team', 'game__home_team', 'game__away_team', 'reviewed_by', 'coach__real_team')
    pending_count = disputes.filter(status='pending').count()
    return render(request, 'league/commissioner/disputes.html', {
        'disputes': disputes,
        'pending_count': pending_count,
    })


@commissioner_required
def review_dispute(request, dispute_id):
    dispute = get_object_or_404(
        PendingRequest.objects.select_related('player', 'game', 'submitted_by'),
        pk=dispute_id, request_type='stat_modify'
    )

    if dispute.stat_type == 'remove':
        if dispute.player.is_pitcher:
            current_log = PitchingGameLog.objects.filter(player=dispute.player, game=dispute.game).first()
        else:
            current_log = HittingGameLog.objects.filter(player=dispute.player, game=dispute.game).first()
        if request.method == 'POST' and dispute.status == 'pending':
            action = request.POST.get('action')
            commissioner_note = request.POST.get('commissioner_note', '').strip()
            with transaction.atomic():
                if action == 'approve' and current_log:
                    current_log.delete()
                    refresh_player_points(dispute.player)
                    dispute.status = 'approved'
                    entry_type = 'dispute_approved'
                else:
                    dispute.status = 'denied'
                    entry_type = 'dispute_denied'
                dispute.commissioner_note = commissioner_note
                dispute.reviewed_by = request.fantasy_team
                dispute.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
                dispute.save()
                ActivityEntry.objects.create(
                    entry_type=entry_type, fantasy_team=request.fantasy_team,
                    player=dispute.player,
                    description=f'Game removal request by {dispute.submitted_by} for {dispute.player} was {dispute.status}',
                )
            messages.success(request, f'Dispute {dispute.status}.')
            return redirect('league:commissioner_disputes')
        return render(request, 'league/commissioner/review_dispute.html', {
            'dispute': dispute, 'current_log': current_log, 'form': None,
        })

    if dispute.stat_type == 'pitching':
        current_log = get_object_or_404(PitchingGameLog, player=dispute.player, game=dispute.game)
        pd = dispute.proposed_data or {}
        initial = {
            'ip': f"{pd.get('outs', 0) // 3}.{pd.get('outs', 0) % 3}",
            'hits': pd.get('hits', 0), 'runs': pd.get('runs', 0),
            'er': pd.get('er', 0), 'bb': pd.get('bb', 0),
            'so': pd.get('so', 0), 'hr': pd.get('hr', 0),
            'win': pd.get('win', False), 'loss': pd.get('loss', False),
            'save': pd.get('save_game', False),
        }
        FormClass = PitchingGameLogForm
    else:
        current_log = get_object_or_404(HittingGameLog, player=dispute.player, game=dispute.game)
        pd = dispute.proposed_data or {}
        initial = {
            'ab': pd.get('ab', 0), 'runs': pd.get('runs', 0),
            'hits': pd.get('hits', 0), 'doubles': pd.get('doubles', 0),
            'triples': pd.get('triples', 0), 'hr': pd.get('hr', 0),
            'rbi': pd.get('rbi', 0), 'bb': pd.get('bb', 0),
            'so': pd.get('so', 0), 'sb': pd.get('sb', 0),
            'cs': pd.get('cs', 0), 'hbp': pd.get('hbp', 0),
        }
        FormClass = HittingGameLogForm

    if request.method == 'POST':
        action = request.POST.get('action')
        commissioner_note = request.POST.get('commissioner_note', '').strip()
        if action == 'approve':
            form = FormClass(request.POST,
                             instance=current_log if dispute.stat_type == 'hitting' else None)
            if form.is_valid():
                with transaction.atomic():
                    if dispute.stat_type == 'hitting':
                        form.save()
                    else:
                        cd = form.cleaned_data
                        current_log.outs = cd['ip']
                        current_log.hits = cd['hits']
                        current_log.runs = cd['runs']
                        current_log.er = cd['er']
                        current_log.bb = cd['bb']
                        current_log.so = cd['so']
                        current_log.hr = cd['hr']
                        current_log.win = cd['win']
                        current_log.loss = cd['loss']
                        current_log.save_game = cd['save']
                        current_log.save()
                    refresh_player_points(dispute.player)
                    dispute.status = 'approved'
                    entry_type = 'dispute_approved'
                    dispute.commissioner_note = commissioner_note
                    dispute.reviewed_by = request.fantasy_team
                    dispute.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
                    dispute.save()
                    ActivityEntry.objects.create(
                        entry_type=entry_type, fantasy_team=request.fantasy_team,
                        player=dispute.player,
                        description=f'Dispute by {dispute.submitted_by} for {dispute.player} was {dispute.status}',
                    )
                messages.success(request, f'Dispute {dispute.status}.')
                return redirect('league:commissioner_disputes')
            else:
                return render(request, 'league/commissioner/review_dispute.html', {
                    'dispute': dispute, 'current_log': current_log, 'form': form,
                })
        else:
            with transaction.atomic():
                dispute.status = 'denied'
                dispute.commissioner_note = commissioner_note
                dispute.reviewed_by = request.fantasy_team
                dispute.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
                dispute.save()
                ActivityEntry.objects.create(
                    entry_type='dispute_denied', fantasy_team=request.fantasy_team,
                    player=dispute.player,
                    description=f'Dispute by {dispute.submitted_by} for {dispute.player} was denied',
                )
            messages.success(request, 'Dispute denied.')
            return redirect('league:commissioner_disputes')

    form = FormClass(initial=initial)

    return render(request, 'league/commissioner/review_dispute.html', {
        'dispute': dispute, 'current_log': current_log, 'form': form,
    })


@commissioner_required
def review_missing_game(request, dispute_id):
    dispute = get_object_or_404(
        PendingRequest.objects.select_related('player', 'submitted_by'),
        pk=dispute_id, request_type='missing_game'
    )

    if request.method == 'POST':
        action = request.POST.get('action')
        commissioner_note = request.POST.get('commissioner_note', '').strip()
        with transaction.atomic():
            dispute.status = 'approved' if action == 'approve' else 'denied'
            entry_type = 'dispute_approved' if action == 'approve' else 'dispute_denied'
            dispute.commissioner_note = commissioner_note
            dispute.reviewed_by = request.fantasy_team
            dispute.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
            dispute.save()
            ActivityEntry.objects.create(
                entry_type=entry_type, fantasy_team=request.fantasy_team,
                player=dispute.player,
                description=f'Missing game dispute by {dispute.submitted_by} for {dispute.player} was {dispute.status}',
            )
        messages.success(request, f'Dispute {dispute.status}.')
        return redirect('league:commissioner_disputes')

    return render(request, 'league/commissioner/review_missing_game.html', {
        'dispute': dispute,
    })


@login_required
def coach_dispute_select_game(request, coach_id):
    team = request.fantasy_team
    if team.is_commissioner:
        return redirect('league:commissioner_disputes')
    coach = get_object_or_404(Coach, pk=coach_id, fantasy_team=team)
    games = RealGame.objects.filter(
        Q(home_team=coach.real_team) | Q(away_team=coach.real_team)
    ).select_related('home_team', 'away_team').order_by('-date')
    return render(request, 'league/coach_dispute_select_game.html', {
        'coach': coach, 'games': games,
    })


@login_required
def submit_coach_win_dispute(request, coach_id, game_id):
    team = request.fantasy_team
    if team.is_commissioner:
        messages.error(request, 'Commissioners cannot submit disputes.')
        return redirect('league:dashboard')
    coach = get_object_or_404(Coach, pk=coach_id, fantasy_team=team)
    game = get_object_or_404(RealGame.objects.select_related('home_team', 'away_team', 'winner'), pk=game_id)

    if game.home_team != coach.real_team and game.away_team != coach.real_team:
        messages.error(request, "This game does not involve your coach's team.")
        return redirect('league:coach_dispute_select_game', coach_id=coach_id)

    existing = PendingRequest.objects.filter(
        request_type='coach_win', submitted_by=team,
        coach=coach, game=game, status='pending'
    ).first()
    if existing:
        messages.warning(request, 'You already have a pending dispute for this game.')
        return redirect('league:dispute_list')

    if request.method == 'POST':
        dispute_type = request.POST.get('dispute_type')
        user_message = request.POST.get('user_message', '').strip()
        if dispute_type not in ('add_win', 'remove_win'):
            messages.error(request, 'Please select a dispute type.')
        else:
            PendingRequest.objects.create(
                request_type='coach_win', submitted_by=team,
                coach=coach, game=game, stat_type=dispute_type,
                user_message=user_message,
            )
            ActivityEntry.objects.create(
                entry_type='dispute_submitted', fantasy_team=team,
                coach=coach,
                description=f'{team.name} submitted coach win dispute for {coach} in {game}',
            )
            messages.success(request, 'Coach win dispute submitted.')
            return redirect('league:dispute_list')

    return render(request, 'league/submit_coach_win_dispute.html', {
        'coach': coach, 'game': game,
    })


@commissioner_required
def review_coach_win_dispute(request, dispute_id):
    dispute = get_object_or_404(
        PendingRequest.objects.select_related(
            'coach__real_team', 'game__home_team', 'game__away_team',
            'game__winner', 'submitted_by'
        ),
        pk=dispute_id, request_type='coach_win'
    )

    if request.method == 'POST':
        action = request.POST.get('action')
        commissioner_note = request.POST.get('commissioner_note', '').strip()
        with transaction.atomic():
            if action == 'approve':
                game = dispute.game
                if dispute.stat_type == 'add_win':
                    game.winner = dispute.coach.real_team
                else:
                    game.winner = None
                game.save(update_fields=['winner'])
                from league.scoring import refresh_all_coaches
                refresh_all_coaches()
                dispute.status = 'approved'
                entry_type = 'dispute_approved'
            else:
                dispute.status = 'denied'
                entry_type = 'dispute_denied'
            dispute.commissioner_note = commissioner_note
            dispute.reviewed_by = request.fantasy_team
            dispute.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
            dispute.save()
            ActivityEntry.objects.create(
                entry_type=entry_type, fantasy_team=dispute.submitted_by,
                coach=dispute.coach,
                description=f'Coach win dispute by {dispute.submitted_by} for {dispute.coach} was {dispute.status}',
            )
        messages.success(request, f'Dispute {dispute.status}.')
        return redirect('league:commissioner_disputes')

    return render(request, 'league/commissioner/review_coach_win_dispute.html', {
        'dispute': dispute,
    })


# --- Trade Views ---

@login_required
@login_required
def trade_select_team(request):
    team = request.fantasy_team
    if team.is_commissioner:
        return redirect('league:dashboard')
    teams = FantasyTeam.objects.filter(is_commissioner=False).exclude(pk=team.pk)
    return render(request, 'league/trade_select_team.html', {'teams': teams})


@login_required
def trade_create(request, team_id):
    my_team = request.fantasy_team
    if my_team.is_commissioner:
        return redirect('league:dashboard')
    other_team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)
    if other_team == my_team:
        return redirect('league:trade_select_team')

    counter_to_id = request.GET.get('counter_to') or request.POST.get('counter_to')
    counter_to = None
    prefill_give = []
    prefill_receive = []
    if counter_to_id:
        counter_to = Trade.objects.filter(pk=counter_to_id, status='pending', receiver=my_team).first()
        if counter_to:
            for item in counter_to.items.filter(direction='receive'):
                if item.player_id:
                    prefill_give.append(f'p_{item.player_id}')
                elif item.coach_id:
                    prefill_give.append(f'c_{item.coach_id}')
            for item in counter_to.items.filter(direction='give'):
                if item.player_id:
                    prefill_receive.append(f'p_{item.player_id}')
                elif item.coach_id:
                    prefill_receive.append(f'c_{item.coach_id}')

    my_players = list(Player.objects.filter(fantasy_team=my_team).order_by('last_name', 'first_name'))
    their_players = list(Player.objects.filter(fantasy_team=other_team).order_by('last_name', 'first_name'))
    my_coach = Coach.objects.filter(fantasy_team=my_team).first()
    their_coach = Coach.objects.filter(fantasy_team=other_team).first()

    if request.method == 'POST':
        raw_give = [v for v in request.POST.getlist('give_players') if v]
        raw_receive = [v for v in request.POST.getlist('receive_players') if v]

        give_player_ids, give_coach_ids = [], []
        for v in raw_give:
            if v.startswith('p_'):
                give_player_ids.append(int(v[2:]))
            elif v.startswith('c_'):
                give_coach_ids.append(int(v[2:]))

        receive_player_ids, receive_coach_ids = [], []
        for v in raw_receive:
            if v.startswith('p_'):
                receive_player_ids.append(int(v[2:]))
            elif v.startswith('c_'):
                receive_coach_ids.append(int(v[2:]))

        if not (give_player_ids or give_coach_ids) or not (receive_player_ids or receive_coach_ids):
            messages.error(request, 'A trade must include at least one player or coach on each side.')
            return redirect(request.path + (f'?counter_to={counter_to_id}' if counter_to_id else ''))

        if len(give_player_ids) != len(set(give_player_ids)) or len(receive_player_ids) != len(set(receive_player_ids)):
            messages.error(request, 'The same player cannot appear more than once in a trade.')
            return redirect(request.path + (f'?counter_to={counter_to_id}' if counter_to_id else ''))

        my_player_ids = {p.id for p in my_players}
        their_player_ids = {p.id for p in their_players}
        my_coach_id = my_coach.id if my_coach else None
        their_coach_id = their_coach.id if their_coach else None

        invalid = [i for i in give_player_ids if i not in my_player_ids]
        invalid += [i for i in receive_player_ids if i not in their_player_ids]
        invalid += [i for i in give_coach_ids if i != my_coach_id]
        invalid += [i for i in receive_coach_ids if i != their_coach_id]
        if invalid:
            messages.error(request, 'Invalid player or coach selection.')
            return redirect(request.path + (f'?counter_to={counter_to_id}' if counter_to_id else ''))

        conflicting = TradeItem.objects.filter(
            trade__sender=my_team, trade__status='pending', direction='give',
            player_id__in=give_player_ids,
        ).exists()
        if not conflicting and give_coach_ids:
            conflicting = TradeItem.objects.filter(
                trade__sender=my_team, trade__status='pending', direction='give',
                coach_id__in=give_coach_ids,
            ).exists()
        if conflicting:
            messages.error(request, 'One or more of your players or coaches are already included in another pending trade you sent.')
            return redirect(request.path + (f'?counter_to={counter_to_id}' if counter_to_id else ''))

        with transaction.atomic():
            trade = Trade.objects.create(sender=my_team, receiver=other_team, counter_to=counter_to)
            for pid in give_player_ids:
                TradeItem.objects.create(trade=trade, player_id=pid, direction='give')
            for cid in give_coach_ids:
                TradeItem.objects.create(trade=trade, coach_id=cid, direction='give')
            for pid in receive_player_ids:
                TradeItem.objects.create(trade=trade, player_id=pid, direction='receive')
            for cid in receive_coach_ids:
                TradeItem.objects.create(trade=trade, coach_id=cid, direction='receive')
            if counter_to:
                counter_to.status = 'amended'
                counter_to.save()

        messages.success(request, f'Trade offer sent to {other_team}.')
        return redirect('league:roster', team_id=my_team.pk)

    return render(request, 'league/trade_create.html', {
        'other_team': other_team,
        'my_players': my_players,
        'their_players': their_players,
        'my_coach': my_coach,
        'their_coach': their_coach,
        'counter_to': counter_to,
        'prefill_give': prefill_give,
        'prefill_receive': prefill_receive,
    })


@login_required
def trade_detail(request, trade_id):
    team = request.fantasy_team
    trade = get_object_or_404(Trade, pk=trade_id)
    if team != trade.sender and team != trade.receiver and not team.is_commissioner:
        messages.error(request, 'You do not have access to this trade.')
        return redirect('league:dashboard')
    give_items = trade.items.filter(direction='give').select_related('player__real_team', 'coach__real_team')
    receive_items = trade.items.filter(direction='receive').select_related('player__real_team', 'coach__real_team')
    return render(request, 'league/trade_detail.html', {
        'trade': trade,
        'give_items': give_items,
        'receive_items': receive_items,
        'is_sender': team == trade.sender,
        'is_receiver': team == trade.receiver,
    })


@login_required
def trade_cancel(request, trade_id):
    team = request.fantasy_team
    trade = get_object_or_404(Trade, pk=trade_id, sender=team, status='pending')
    if request.method == 'POST':
        trade.status = 'cancelled'
        trade.save()
        messages.success(request, 'Trade cancelled.')
    return redirect('league:roster', team_id=team.pk)


@login_required
def trade_respond(request, trade_id):
    team = request.fantasy_team
    trade = get_object_or_404(Trade, pk=trade_id, receiver=team, status='pending')
    if request.method != 'POST':
        return redirect('league:trade_detail', trade_id=trade_id)

    action = request.POST.get('action')

    if action == 'accept':
        if _is_locked():
            messages.error(request, format_html(
                'Trades can only be accepted during the transaction window. <a href="{}">View info</a>',
                '/settings/'
            ))
            return redirect('league:trade_detail', trade_id=trade_id)
        give_items = list(trade.items.filter(direction='give').select_related('player', 'coach'))
        receive_items = list(trade.items.filter(direction='receive').select_related('player', 'coach'))
        # Validate players/coaches still belong to the right teams
        for item in give_items:
            if item.player and item.player.fantasy_team_id != trade.sender_id:
                trade.delete()
                messages.error(request, f'{item.player} is no longer on {trade.sender}\'s roster. The trade has been cancelled.')
                return redirect('league:roster', team_id=team.pk)
            if item.coach and item.coach.fantasy_team_id != trade.sender_id:
                trade.delete()
                messages.error(request, f'{item.coach} is no longer on {trade.sender}\'s roster. The trade has been cancelled.')
                return redirect('league:roster', team_id=team.pk)
        for item in receive_items:
            if item.player and item.player.fantasy_team_id != trade.receiver_id:
                trade.delete()
                messages.error(request, f'{item.player} is no longer on your roster. The trade has been cancelled.')
                return redirect('league:roster', team_id=team.pk)
            if item.coach and item.coach.fantasy_team_id != trade.receiver_id:
                trade.delete()
                messages.error(request, f'{item.coach} is no longer on your roster. The trade has been cancelled.')
                return redirect('league:roster', team_id=team.pk)
        today = datetime.date.today()
        current_week = Week.objects.filter(start_date__lte=today, end_date__gte=today).first()
        if current_week:
            _ensure_week_snapshot(trade.sender, current_week)
            _ensure_week_snapshot(trade.receiver, current_week)
        if _is_sunday_unlock_window():
            new_since = today + datetime.timedelta(days=1)
        else:
            new_since = today
        from league.scoring import refresh_coach_points as _refresh_coach
        with transaction.atomic():
            for item in give_items:
                if item.player:
                    p = item.player
                    RosterSlot.objects.filter(player=p).update(player=None)
                    p.fantasy_team = trade.receiver
                    p.fantasy_team_since = new_since
                    p.save()
                    refresh_player_points(p)
                elif item.coach:
                    c = item.coach
                    c.fantasy_team = trade.receiver
                    c.fantasy_team_since = new_since
                    c.save()
                    _refresh_coach(c)
            for item in receive_items:
                if item.player:
                    p = item.player
                    RosterSlot.objects.filter(player=p).update(player=None)
                    p.fantasy_team = trade.sender
                    p.fantasy_team_since = new_since
                    p.save()
                    refresh_player_points(p)
                elif item.coach:
                    c = item.coach
                    c.fantasy_team = trade.sender
                    c.fantasy_team_since = new_since
                    c.save()
                    _refresh_coach(c)
            trade.status = 'accepted'
            trade.save()
        messages.success(request, 'Trade accepted.')
        return redirect('league:roster', team_id=team.pk)

    elif action == 'deny':
        trade.status = 'denied'
        trade.save()
        messages.success(request, 'Trade denied.')
        return redirect('league:roster', team_id=team.pk)

    elif action == 'amend':
        return redirect(
            reverse('league:trade_create', kwargs={'team_id': trade.sender_id})
            + f'?counter_to={trade.pk}'
        )

    return redirect('league:trade_detail', trade_id=trade_id)


# --- Commissioner Views ---

@commissioner_required
def commissioner_panel(request):
    team_count = FantasyTeam.objects.filter(is_commissioner=False).count()
    player_count = Player.objects.count()
    real_team_count = RealTeam.objects.exclude(abbreviation='OOC').count()
    week_count = Week.objects.count()
    free_agent_count = Player.objects.filter(fantasy_team__isnull=True).count()
    pending_disputes = PendingRequest.objects.filter(request_type__in=['stat_modify', 'missing_game', 'coach_win'], status='pending').count()
    return render(request, 'league/commissioner/panel.html', {
        'team_count': team_count,
        'player_count': player_count,
        'real_team_count': real_team_count,
        'week_count': week_count,
        'free_agent_count': free_agent_count,
        'pending_disputes': pending_disputes,
    })


@commissioner_required
def recalculate_coaches(request):
    if request.method != 'POST':
        return redirect('league:commissioner_panel')
    # Backfill RealGame.winner from existing pitching logs
    updated = 0
    for game in RealGame.objects.filter(winner__isnull=True):
        winning_log = PitchingGameLog.objects.filter(
            game=game, win=True
        ).select_related('player__real_team').first()
        if winning_log:
            game.winner = winning_log.player.real_team
            game.save(update_fields=['winner'])
            updated += 1
    refresh_all_coaches()
    messages.success(request, f'Backfilled {updated} game winner(s) and recalculated all coach points.')
    return redirect('league:commissioner_panel')


@commissioner_required
def run_scraper(request):
    if request.method == 'POST':
        form = ScrapeStatsForm(request.POST)
        if form.is_valid():
            from io import StringIO
            from django.core.management import call_command
            start = form.cleaned_data['start_date'].strftime('%m/%d/%Y')
            end = form.cleaned_data['end_date'].strftime('%m/%d/%Y')
            stdout_buf = StringIO()
            stderr_buf = StringIO()
            error = None
            try:
                call_command(
                    'scrape_stats',
                    start_date=start,
                    end_date=end,
                    stdout=stdout_buf,
                    stderr=stderr_buf,
                )
            except Exception as e:
                error = str(e)
            return render(request, 'league/commissioner/run_scraper.html', {
                'form': form,
                'output': stdout_buf.getvalue(),
                'error': error or stderr_buf.getvalue(),
                'start': start,
                'end': end,
                'done': True,
            })
    else:
        form = ScrapeStatsForm()
    latest = (
        RealGame.objects.filter(hitting_logs__isnull=False)
        .order_by('-date')
        .values_list('date', flat=True)
        .first()
    )
    return render(request, 'league/commissioner/run_scraper.html', {'form': form, 'done': False, 'latest_date': latest})


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
        prev_team = player.fantasy_team
        team_name = prev_team.name if prev_team else 'no team'
        Transaction.objects.create(transaction_type='drop', fantasy_team=prev_team, player=player, notes='Removed by commissioner')
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
    teams = RealTeam.objects.exclude(abbreviation='OOC')
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
        ps = form.save()
        refresh_all_players(ps=ps)
        refresh_all_coaches(ps=ps)
        messages.success(request, 'Point settings updated and player/coach points recalculated.')
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
def lock_settings(request):
    settings = LeagueSettings.load()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_mode':
            settings.normal_mode = not settings.normal_mode
            settings.save()
        elif action == 'lock_all':
            settings.manual_locked = True
            settings.save()
        elif action == 'unlock_all':
            settings.manual_locked = False
            settings.save()
        return redirect('league:lock_settings')
    now_et = datetime.datetime.now(_ET)
    return render(request, 'league/commissioner/lock_settings.html', {
        'settings': settings,
        'currently_locked': _is_locked(),
        'now_et': now_et,
    })


@commissioner_required
def reset_league(request):
    if request.method == 'POST':
        if request.POST.get('confirm') != 'RESET':
            messages.error(request, 'You must type RESET to confirm.')
            return redirect('league:reset_league')
        with transaction.atomic():
            PendingRequest.objects.all().delete()
            Transaction.objects.all().delete()
            ActivityEntry.objects.all().delete()
            Week.objects.all().delete()  # cascades Matchup
            FantasyTeam.objects.filter(is_commissioner=False).delete()  # cascades RosterSlot
            Player.objects.update(fantasy_team=None, fantasy_team_since=None)
        messages.success(request, 'League has been reset. All teams, matchups, and transactions have been cleared.')
        return redirect('league:commissioner_panel')
    return render(request, 'league/commissioner/reset_league.html')


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
    real_teams = RealTeam.objects.exclude(abbreviation='OOC')

    # Unassigned coaches
    free_coaches = Coach.objects.filter(fantasy_team__isnull=True).select_related('real_team').order_by('last_name', 'first_name')
    coach_data = []
    for c in free_coaches:
        wins = RealGame.objects.filter(winner=c.real_team).count()
        coach_data.append({'coach': c, 'wins': wins, 'season_points': ps.coach_win * wins})

    return render(request, 'league/commissioner/free_agent_board.html', {
        'player_data': player_data,
        'coach_data': coach_data,
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
            Transaction.objects.create(transaction_type='add', fantasy_team=team, player=player, notes='Added by commissioner')
            messages.success(request, f'{player} assigned to {team.name}.')
        else:
            messages.error(request, 'No team selected.')
    return redirect(request.POST.get('next', 'league:free_agent_board'))


@commissioner_required
def assign_coach(request, coach_id):
    coach = get_object_or_404(Coach, pk=coach_id)
    if request.method == 'POST':
        team_id = request.POST.get('team_id')
        if team_id:
            team = get_object_or_404(FantasyTeam, pk=team_id, is_commissioner=False)
            coach.fantasy_team = team
            coach.fantasy_team_since = datetime.date.today()
            coach.save()
            Transaction.objects.create(transaction_type='add', fantasy_team=team, coach=coach, notes='Added by commissioner')
            from league.scoring import refresh_coach_points
            refresh_coach_points(coach)
            messages.success(request, f'{coach} assigned to {team.name}.')
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
            refresh_player_points(log.player)
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
            log.entered_by = request.fantasy_team
            log.save()
            refresh_player_points(log.player)
            messages.success(request, 'Pitching game log updated.')
            return redirect('league:player_detail', player_id=log.player.id)
        template = 'league/commissioner/edit_game_log.html'

    return render(request, template, {
        'form': form,
        'log': log,
        'log_type': log_type,
    })


@commissioner_required
def commissioner_week_list(request):
    weeks = Week.objects.all().order_by('week_number')
    return render(request, 'league/commissioner/week_list.html', {'weeks': weeks})


@commissioner_required
def commissioner_week_days(request, week_id):
    week = get_object_or_404(Week, pk=week_id)

    # Build the list of all 7 days in this week
    all_dates = [week.start_date + datetime.timedelta(days=i) for i in range(7)]

    # Dates that have any game logs (informational)
    dates_with_logs = set(
        RealGame.objects.filter(
            date__gte=week.start_date, date__lte=week.end_date
        ).filter(
            Q(hitting_logs__isnull=False) | Q(pitching_logs__isnull=False)
        ).values_list('date', flat=True)
    )

    if request.method == 'POST':
        # Checked boxes = active (not excluded); unchecked = excluded
        active_dates = set()
        for d in all_dates:
            key = d.strftime('%Y-%m-%d')
            if request.POST.get(key):
                active_dates.add(d)

        excluded_dates = [d for d in all_dates if d not in active_dates]

        ExcludedDay.objects.filter(week=week).delete()
        for d in excluded_dates:
            ExcludedDay.objects.create(week=week, date=d)

        # Refresh points for all affected players
        affected_players = Player.objects.filter(
            Q(hitting_logs__game__date__gte=week.start_date, hitting_logs__game__date__lte=week.end_date) |
            Q(pitching_logs__game__date__gte=week.start_date, pitching_logs__game__date__lte=week.end_date)
        ).distinct()
        for player in affected_players:
            refresh_player_points(player)

        messages.success(request, f'Week {week.week_number} active days updated.')
        return redirect('league:commissioner_week_days', week_id=week.pk)

    current_excluded = set(ExcludedDay.objects.filter(week=week).values_list('date', flat=True))
    day_rows = []
    for d in all_dates:
        day_rows.append({
            'date': d,
            'key': d.strftime('%Y-%m-%d'),
            'active': d not in current_excluded,
            'has_logs': d in dates_with_logs,
        })

    return render(request, 'league/commissioner/week_days.html', {
        'week': week,
        'day_rows': day_rows,
    })
