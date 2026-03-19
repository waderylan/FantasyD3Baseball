import datetime
from decimal import Decimal
from django.db.models import Q
from .models import (
    PointSettings, HittingGameLog, PitchingGameLog,
    Player, FantasyTeam, Matchup, Week, RosterSlot, WeeklyLineupSlot, ExcludedDay, PITCHING_POSITIONS,
    Coach, RealGame,
)


def calc_hitting_points(log, ps):
    return (
        Decimal(log.singles) * ps.single
        + Decimal(log.doubles) * ps.double
        + Decimal(log.triples) * ps.triple
        + Decimal(log.hr) * ps.hr
        + Decimal(log.rbi) * ps.rbi
        + Decimal(log.runs) * ps.run
        + Decimal(log.bb) * ps.bb
        + Decimal(log.sb) * ps.sb
        + Decimal(log.cs) * ps.cs
        + Decimal(log.hbp) * ps.hbp
        + Decimal(log.so) * ps.so_hitting
    )


def calc_pitching_points(log, ps):
    pts = (
        Decimal(log.outs) * ps.ip_out
        + Decimal(log.so) * ps.so_pitching
        + Decimal(log.er) * ps.er
        + Decimal(log.hits) * ps.hit_against
        + Decimal(log.bb) * ps.bb_pitching
    )
    if log.win:
        pts += ps.win
    if log.loss:
        pts += ps.loss
    if log.save_game:
        pts += ps.save_pts
    return pts


def calc_player_points_for_period(player, start_date, end_date, ps=None, excluded_dates=()):
    if ps is None:
        ps = PointSettings.load()
    total = Decimal('0')
    if player.is_pitcher:
        logs = PitchingGameLog.objects.filter(
            player=player,
            game__date__gte=start_date,
            game__date__lte=end_date
        )
        if excluded_dates:
            logs = logs.exclude(game__date__in=excluded_dates)
        for log in logs:
            total += calc_pitching_points(log, ps)
    else:
        logs = HittingGameLog.objects.filter(
            player=player,
            game__date__gte=start_date,
            game__date__lte=end_date
        )
        if excluded_dates:
            logs = logs.exclude(game__date__in=excluded_dates)
        for log in logs:
            total += calc_hitting_points(log, ps)
    return total


def _owned_start(player, week_start):
    """Return the effective start date for scoring: max(week_start, fantasy_team_since)."""
    if player.fantasy_team_since and player.fantasy_team_since > week_start:
        return player.fantasy_team_since
    return week_start


def _active_players(fantasy_team, week=None):
    """Return players in active (non-bench) slots for a team.

    For past weeks with a snapshot, uses WeeklyLineupSlot; otherwise falls
    back to the live RosterSlots.
    """
    today = datetime.date.today()
    if week is not None and week.end_date < today:
        ids = WeeklyLineupSlot.objects.filter(
            fantasy_team=fantasy_team, week=week, player__isnull=False
        ).exclude(slot_type='BN').values_list('player_id', flat=True)
        if ids.exists():
            return Player.objects.filter(pk__in=ids)
    # Current week or no snapshot yet — fall back to live RosterSlots
    qs = RosterSlot.objects.filter(
        fantasy_team=fantasy_team, player__isnull=False
    ).exclude(slot_type='BN')
    if week is not None and week.end_date < today:
        # Exclude players added after this week ended so past weeks aren't
        # inflated by players who weren't rostered yet.
        qs = qs.filter(player__fantasy_team_since__lte=week.end_date)
    ids = qs.values_list('player_id', flat=True)
    return Player.objects.filter(pk__in=ids)


def calc_coach_points_for_period(coach, start_date, end_date, ps=None, excluded_dates=()):
    if ps is None:
        ps = PointSettings.load()
    qs = RealGame.objects.filter(
        winner=coach.real_team,
        date__gte=start_date,
        date__lte=end_date,
    )
    if excluded_dates:
        qs = qs.exclude(date__in=excluded_dates)
    return Decimal(qs.count()) * ps.coach_win


def calc_team_weekly_points(fantasy_team, week, ps=None):
    if ps is None:
        ps = PointSettings.load()
    excluded = list(ExcludedDay.objects.filter(week=week).values_list('date', flat=True))
    total = Decimal('0')
    for player in _active_players(fantasy_team, week=week):
        total += calc_player_points_for_period(player, week.start_date, week.end_date, ps, excluded_dates=excluded)
    for coach in Coach.objects.filter(fantasy_team=fantasy_team):
        total += calc_coach_points_for_period(coach, week.start_date, week.end_date, ps, excluded_dates=excluded)
    return total


def calc_team_season_points(fantasy_team, ps=None):
    if ps is None:
        ps = PointSettings.load()
    total = Decimal('0')
    weeks = Week.objects.all()
    for week in weeks:
        total += calc_team_weekly_points(fantasy_team, week, ps)
    return total


def get_player_weekly_breakdown(fantasy_team, week, ps=None):
    if ps is None:
        ps = PointSettings.load()
    excluded = list(ExcludedDay.objects.filter(week=week).values_list('date', flat=True))
    breakdown = []
    for player in _active_players(fantasy_team, week=week):
        pts = calc_player_points_for_period(player, week.start_date, week.end_date, ps, excluded_dates=excluded)
        breakdown.append({'player': player, 'points': pts})
    for coach in Coach.objects.filter(fantasy_team=fantasy_team):
        pts = calc_coach_points_for_period(coach, week.start_date, week.end_date, ps, excluded_dates=excluded)
        breakdown.append({'player': None, 'coach': coach, 'points': pts})
    breakdown.sort(key=lambda x: x['points'], reverse=True)
    return breakdown


def resolve_matchup(matchup, ps=None):
    if ps is None:
        ps = PointSettings.load()
    t1_pts = calc_team_weekly_points(matchup.team_1, matchup.week, ps)
    if matchup.team_2 is None:
        return t1_pts, Decimal('0'), None  # bye = tie
    t2_pts = calc_team_weekly_points(matchup.team_2, matchup.week, ps)
    if t1_pts > t2_pts:
        winner = matchup.team_1
    elif t2_pts > t1_pts:
        winner = matchup.team_2
    else:
        winner = None  # tie
    return t1_pts, t2_pts, winner


def _current_week():
    today = datetime.date.today()
    w = Week.objects.filter(start_date__lte=today, end_date__gte=today).first()
    return w or Week.objects.filter(end_date__lt=today).last()


def refresh_player_points(player, current_week=None, ps=None):
    """Recompute and store cached point fields on a single Player."""
    if ps is None:
        ps = PointSettings.load()
    if current_week is None:
        current_week = _current_week()

    season_pts = Decimal('0')
    weekly_pts = Decimal('0')
    for w in Week.objects.all():
        excluded = list(ExcludedDay.objects.filter(week=w).values_list('date', flat=True))
        pts = calc_player_points_for_period(player, w.start_date, w.end_date, ps, excluded_dates=excluded)
        season_pts += pts
        if current_week and w.pk == current_week.pk:
            weekly_pts = pts

    if player.is_pitcher:
        games_played = PitchingGameLog.objects.filter(player=player).count()
    else:
        games_played = HittingGameLog.objects.filter(player=player).count()

    ppg = (season_pts / games_played).quantize(Decimal('0.01')) if games_played else Decimal('0')

    Player.objects.filter(pk=player.pk).update(
        cached_season_points=season_pts,
        cached_weekly_points=weekly_pts,
        cached_games_played=games_played,
        cached_ppg=ppg,
    )


def refresh_all_players(ps=None):
    """Recompute cached point fields for every player. Called after point settings change."""
    if ps is None:
        ps = PointSettings.load()
    current_week = _current_week()
    for player in Player.objects.all():
        refresh_player_points(player, current_week=current_week, ps=ps)


def refresh_coach_points(coach, current_week=None, ps=None):
    if ps is None:
        ps = PointSettings.load()
    if current_week is None:
        current_week = _current_week()
    season_pts = Decimal('0')
    weekly_pts = Decimal('0')
    for w in Week.objects.all():
        excluded = list(ExcludedDay.objects.filter(week=w).values_list('date', flat=True))
        pts = calc_coach_points_for_period(coach, w.start_date, w.end_date, ps, excluded_dates=excluded)
        season_pts += pts
        if current_week and w.pk == current_week.pk:
            weekly_pts = pts
    Coach.objects.filter(pk=coach.pk).update(
        cached_season_points=season_pts,
        cached_weekly_points=weekly_pts,
    )


def refresh_all_coaches(ps=None):
    if ps is None:
        ps = PointSettings.load()
    current_week = _current_week()
    for coach in Coach.objects.all():
        refresh_coach_points(coach, current_week=current_week, ps=ps)


def get_standings():
    ps = PointSettings.load()
    teams = FantasyTeam.objects.filter(is_commissioner=False)
    records = {}
    for team in teams:
        records[team.id] = {
            'team': team,
            'wins': 0,
            'losses': 0,
            'ties': 0,
            'points_for': Decimal('0'),
            'points_against': Decimal('0'),
        }

    today = datetime.date.today()
    matchups = Matchup.objects.select_related('week', 'team_1', 'team_2').filter(
        week__end_date__lt=today
    )
    for matchup in matchups:
        t1_id = matchup.team_1_id
        if t1_id not in records:
            continue
        t1_pts, t2_pts, winner = resolve_matchup(matchup, ps)

        if matchup.team_2 is None:
            # bye week — counts as a tie
            records[t1_id]['ties'] += 1
            records[t1_id]['points_for'] += t1_pts
            continue

        t2_id = matchup.team_2_id
        if t2_id not in records:
            continue

        records[t1_id]['points_for'] += t1_pts
        records[t1_id]['points_against'] += t2_pts
        records[t2_id]['points_for'] += t2_pts
        records[t2_id]['points_against'] += t1_pts

        if winner is None:
            records[t1_id]['ties'] += 1
            records[t2_id]['ties'] += 1
        elif winner.id == t1_id:
            records[t1_id]['wins'] += 1
            records[t2_id]['losses'] += 1
        else:
            records[t2_id]['wins'] += 1
            records[t1_id]['losses'] += 1

    standings = list(records.values())
    for s in standings:
        total_games = s['wins'] + s['losses'] + s['ties']
        s['pct'] = s['wins'] / total_games if total_games > 0 else 0
    standings.sort(key=lambda x: (x['wins'], x['points_for']), reverse=True)
    return standings
