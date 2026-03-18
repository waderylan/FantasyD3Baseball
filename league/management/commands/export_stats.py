import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from league.models import RealGame


ENDPOINT_PATH = '/api/ingest/'


class Command(BaseCommand):
    help = 'Export scraped stats from local DB as JSON, optionally POSTing to the ingest API.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--start-date',
            required=True,
            help='Start date in MM/DD/YYYY format (inclusive).',
        )
        parser.add_argument(
            '--end-date',
            required=True,
            help='End date in MM/DD/YYYY format (inclusive).',
        )
        parser.add_argument(
            '--send',
            action='store_true',
            help='POST the payload to the ingest API instead of printing JSON.',
        )
        parser.add_argument(
            '--url',
            default=os.environ.get('INGEST_URL', ''),
            help='Base URL of the deployed site. Also read from INGEST_URL env var.',
        )
        parser.add_argument(
            '--secret',
            default=os.environ.get('INGEST_SECRET', ''),
            help='Bearer token. Also read from INGEST_SECRET env var.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='With --send: print the payload without actually sending it.',
        )

    def handle(self, *args, **options):
        # Parse dates
        fmt = '%m/%d/%Y'
        try:
            start = datetime.strptime(options['start_date'], fmt).date()
            end = datetime.strptime(options['end_date'], fmt).date()
        except ValueError as exc:
            raise CommandError(f'Invalid date format: {exc}. Use MM/DD/YYYY.')

        if options['send']:
            if not options['url']:
                raise CommandError('--url is required with --send (or set INGEST_URL env var).')
            if not options['secret']:
                raise CommandError('--secret is required with --send (or set INGEST_SECRET env var).')

        # Query games
        games = (
            RealGame.objects
            .filter(date__gte=start, date__lte=end)
            .prefetch_related(
                'hitting_logs__player__real_team',
                'pitching_logs__player__real_team',
            )
            .order_by('date', 'id')
        )

        if not games.exists():
            self.stderr.write(
                self.style.WARNING(f'No games found between {start} and {end}.')
            )

        # Build payload
        payload = {
            'games': [
                {
                    'date': game.date.strftime('%Y-%m-%d'),
                    'home_team': game.home_team.abbreviation,
                    'away_team': game.away_team.abbreviation,
                    'game_number': game.game_number,
                    'source_url': game.source_url,
                    'hitting_logs': [
                        {
                            'player_first_name': log.player.first_name,
                            'player_last_name': log.player.last_name,
                            'player_team': log.player.real_team.abbreviation,
                            'ab': log.ab,
                            'runs': log.runs,
                            'hits': log.hits,
                            'doubles': log.doubles,
                            'triples': log.triples,
                            'hr': log.hr,
                            'rbi': log.rbi,
                            'bb': log.bb,
                            'so': log.so,
                            'sb': log.sb,
                            'cs': log.cs,
                            'hbp': log.hbp,
                        }
                        for log in game.hitting_logs.all()
                    ],
                    'pitching_logs': [
                        {
                            'player_first_name': log.player.first_name,
                            'player_last_name': log.player.last_name,
                            'player_team': log.player.real_team.abbreviation,
                            'outs': log.outs,
                            'hits': log.hits,
                            'runs': log.runs,
                            'er': log.er,
                            'bb': log.bb,
                            'so': log.so,
                            'hr': log.hr,
                            'win': log.win,
                            'loss': log.loss,
                            'save_game': log.save_game,
                        }
                        for log in game.pitching_logs.all()
                    ],
                }
                for game in games
            ]
        }

        if not options['send']:
            self.stdout.write(json.dumps(payload, indent=2))
            return

        # Send
        base_url = options['url'].rstrip('/')
        url = base_url + ENDPOINT_PATH
        secret = options['secret']
        body = json.dumps(payload).encode('utf-8')

        if options['dry_run']:
            masked = secret[:4] + '...' + (secret[-4:] if len(secret) > 8 else '')
            self.stdout.write(f'[dry-run] POST {url}')
            self.stdout.write(f'[dry-run] Authorization: Bearer {masked}')
            self.stdout.write(f'[dry-run] Payload ({len(body)} bytes):')
            self.stdout.write(json.dumps(payload, indent=2))
            return

        req = urllib.request.Request(
            url,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {secret}',
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status
                response_body = resp.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read().decode('utf-8')
        except urllib.error.URLError as exc:
            raise CommandError(f'Could not reach {url}: {exc.reason}')

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            data = {'raw': response_body}

        self.stdout.write(f'HTTP {status}')
        self.stdout.write(json.dumps(data, indent=2))

        if status != 200:
            raise CommandError(f'Ingest API returned HTTP {status}.')

        warnings = data.get('warnings', [])
        if warnings:
            self.stdout.write(f'\n{len(warnings)} warning(s):')
            for w in warnings:
                self.stdout.write(f'  - {w}')
