import datetime
from .models import Week, Matchup, FantasyTeam


def generate_round_robin(start_date, num_weeks):
    """Generate weeks and matchups for a round-robin schedule.

    start_date must be a Monday. Each week runs Mon-Sun.
    For odd team counts, one team gets a bye each week.
    """
    # Clear existing schedule
    Matchup.objects.all().delete()
    Week.objects.all().delete()

    teams = list(FantasyTeam.objects.filter(is_commissioner=False).order_by('id'))
    n = len(teams)
    if n < 2:
        return

    # For round-robin, if odd number of teams add a None placeholder for byes
    if n % 2 == 1:
        teams.append(None)
        n += 1

    # Generate all rounds using the circle method
    # Fix first team, rotate the rest
    fixed = teams[0]
    rotating = teams[1:]

    rounds = []
    for r in range(n - 1):
        round_matchups = []
        current = [fixed] + rotating
        for i in range(n // 2):
            t1 = current[i]
            t2 = current[n - 1 - i]
            round_matchups.append((t1, t2))
        rounds.append(round_matchups)
        # Rotate: move last element to front of rotating list
        rotating = [rotating[-1]] + rotating[:-1]

    # Create weeks and matchups, cycling through rounds if num_weeks > len(rounds)
    for wk in range(num_weeks):
        week_start = start_date + datetime.timedelta(weeks=wk)
        week_end = week_start + datetime.timedelta(days=6)
        week = Week.objects.create(
            week_number=wk + 1,
            start_date=week_start,
            end_date=week_end
        )
        round_idx = wk % len(rounds)
        for t1, t2 in rounds[round_idx]:
            if t1 is None:
                # t2 gets the bye
                Matchup.objects.create(week=week, team_1=t2, team_2=None)
            elif t2 is None:
                # t1 gets the bye
                Matchup.objects.create(week=week, team_1=t1, team_2=None)
            else:
                Matchup.objects.create(week=week, team_1=t1, team_2=t2)
