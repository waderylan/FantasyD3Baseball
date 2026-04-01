import json
import os
import urllib.request
import urllib.error

from django.core.management.base import BaseCommand, CommandError

from league.models import ScheduledGame


ENDPOINT_PATH = '/api/ingest/schedule/'


class Command(BaseCommand):
    help = 'Export scraped schedule from local DB as JSON, optionally POSTing to the ingest API.'

    def add_arguments(self, parser):
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
        if options['send']:
            if not options['url']:
                raise CommandError('--url is required with --send (or set INGEST_URL env var).')
            if not options['secret']:
                raise CommandError('--secret is required with --send (or set INGEST_SECRET env var).')

        games = ScheduledGame.objects.all().order_by('date', 'id')

        if not games.exists():
            self.stderr.write(self.style.WARNING('No scheduled games found in DB.'))

        payload = {
            'schedule': [
                {
                    'source_event_id': g.source_event_id,
                    'date':            g.date.strftime('%Y-%m-%d'),
                    'away_team_name':  g.away_team_name,
                    'home_team_name':  g.home_team_name,
                    'game_time':       g.game_time,
                    'away_score':      g.away_score,
                    'home_score':      g.home_score,
                    'status':          g.status,
                    'box_score_url':   g.box_score_url,
                }
                for g in games
            ]
        }

        if not options['send']:
            self.stdout.write(json.dumps(payload, indent=2))
            return

        base_url = options['url'].rstrip('/')
        url = base_url + ENDPOINT_PATH
        secret = options['secret']
        body = json.dumps(payload).encode('utf-8')

        if options['dry_run']:
            masked = secret[:4] + '...' + (secret[-4:] if len(secret) > 8 else '')
            self.stdout.write(f'[dry-run] POST {url}')
            self.stdout.write(f'[dry-run] Authorization: Bearer {masked}')
            self.stdout.write(f'[dry-run] Payload ({len(body)} bytes, {len(payload["schedule"])} games):')
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
