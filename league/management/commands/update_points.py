"""
Management command: update_points

Recomputes and caches season/weekly points for all players.
Run this after bulk DB changes or if cached values look stale.

Usage:
  python manage.py update_points
"""
from django.core.management.base import BaseCommand
from league.scoring import refresh_all_players
from league.models import Player


class Command(BaseCommand):
    help = 'Recompute and cache season/weekly points for all players'

    def handle(self, *args, **options):
        count = Player.objects.count()
        self.stdout.write(f'Refreshing points for {count} players...')
        refresh_all_players()
        self.stdout.write(self.style.SUCCESS('Done.'))
