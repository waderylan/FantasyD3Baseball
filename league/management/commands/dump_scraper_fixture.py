import json
from django.core.management.base import BaseCommand
from league.models import RealTeam, Player


class Command(BaseCommand):
    help = 'Export RealTeam + Player fixture safe for committing (no fantasy team data).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', default='league/fixtures/bootstrap.json',
            help='Output path (default: league/fixtures/bootstrap.json)',
        )

    def handle(self, *args, **options):
        records = []

        for team in RealTeam.objects.all():
            records.append({
                'model': 'league.realteam',
                'pk': team.pk,
                'fields': {
                    'name': team.name,
                    'abbreviation': team.abbreviation,
                },
            })

        for player in Player.objects.select_related('real_team').all():
            records.append({
                'model': 'league.player',
                'pk': player.pk,
                'fields': {
                    'first_name': player.first_name,
                    'last_name': player.last_name,
                    'position': player.position,
                    'class_year': player.class_year,
                    'real_team': player.real_team_id,
                    'fantasy_team': None,
                    'fantasy_team_since': None,
                    'cached_season_points': '0.00',
                    'cached_weekly_points': '0.00',
                    'cached_games_played': 0,
                    'cached_ppg': '0.00',
                },
            })

        out_path = options['output']
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2)

        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(records)} records to {out_path}'
        ))
