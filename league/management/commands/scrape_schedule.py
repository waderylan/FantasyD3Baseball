"""
Management command: scrape_schedule

Scrapes the full Liberty League baseball schedule from the Sidearm calendar
and writes results into the ScheduledGame model. Runs idempotently via
update_or_create on source_event_id.

Usage
-----
  python manage.py scrape_schedule           # scrape full season
  python manage.py scrape_schedule --dry-run # print games without DB writes

Dependencies
------------
  playwright install chromium
"""

import datetime
import hashlib
import logging
import re
import urllib.parse

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

CALENDAR_URL = 'https://libertyleagueathletics.com/calendar.aspx?path=baseball'
CALENDAR_WAIT_MS = 20_000

# Sidearm calendar TR text:
# "Wed 3/11/2026  Skidmore (Away) 8  Rockford (Home) 6  Final Box Score"
# "Tue 4/01/2026  Union (Away)  Vassar (Home)  3:00 PM"
# "Sat 4/05/2026  Hobart (Away)  RPI (Home)  Postponed"
_GAME_ROW_RE = re.compile(
    r'(?P<away>[A-Za-z][A-Za-z0-9\s\.\'\-]*?)\s+\(Away\)\s*(?P<away_score>\d+)?\s*'
    r'(?P<home>[A-Za-z][A-Za-z0-9\s\.\'\-]*?)\s+\(Home\)\s*(?P<home_score>\d+)?',
    re.I,
)

_DATE_PATTERNS = [
    re.compile(
        r'\b(?:January|February|March|April|May|June|July|August|'
        r'September|October|November|December|'
        r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s*\d{4}',
        re.I,
    ),
    re.compile(r'\d{1,2}/\d{1,2}/\d{4}'),
    re.compile(r'\d{4}-\d{2}-\d{2}'),
]
_DATE_FMTS = [
    '%B %d %Y', '%b %d %Y', '%B. %d %Y', '%b. %d %Y',
    '%m/%d/%Y', '%Y-%m-%d',
]
_TIME_RE = re.compile(r'\b(\d{1,2}:\d{2}\s*(?:A\.M\.|P\.M\.|AM|PM))', re.I)


def _parse_date(text):
    cleaned = re.sub(r'\s+', ' ', text.replace(',', '')).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _find_date_in_text(text):
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            d = _parse_date(m.group(0))
            if d:
                return d
    return None


def _normalize_time(raw):
    """Convert '9:00 A.M.' / '11:00 A.M.' to '9:00 AM' / '11:00 AM'."""
    t = re.sub(r'A\.M\.', 'AM', raw, flags=re.I)
    t = re.sub(r'P\.M\.', 'PM', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip()


def _detect_status_and_time(tr_text):
    """Return (status, game_time) from TR text."""
    low = tr_text.lower()
    time_m = _TIME_RE.search(tr_text)
    game_time = _normalize_time(time_m.group(1)) if time_m else ''
    if 'postponed' in low:
        return 'POSTPONED', game_time
    if 'cancel' in low:
        return 'CANCELLED', game_time
    if 'final' in low:
        return 'FINAL', game_time
    if game_time:
        return 'UPCOMING', game_time
    return 'UPCOMING', ''


def _source_event_id_from_row(tr, date, away, home, game_time=''):
    """Extract a stable ID from any link in the TR, falling back to a hash."""
    for a in tr.find_all('a', href=True):
        href = a['href']
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'id' in qs:
            return qs['id'][0]
        for key in ('event_id', 'eventid', 'gameid', 'game_id'):
            if key in qs:
                return qs[key][0]
    # Fallback: include time so doubleheaders (same date/teams, different times) get distinct IDs
    raw = f"{date}|{away.lower()}|{home.lower()}|{game_time.lower()}"
    return hashlib.md5(raw.encode()).hexdigest()[:20]


class Command(BaseCommand):
    help = 'Scrape the Liberty League baseball schedule and store in ScheduledGame.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print scraped games without writing to DB.',
        )
        parser.add_argument(
            '--debug', action='store_true',
            help='Print raw TR text for every game row found.',
        )

    def handle(self, *args, **options):
        import os
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CommandError(
                'playwright is required.\n'
                '  uv run playwright install chromium'
            )
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise CommandError('beautifulsoup4 is required.')

        from league.models import ScheduledGame

        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no database writes'))

        today = datetime.date.today()
        season_start = datetime.date(today.year, 2, 1)
        season_end = datetime.date(today.year, 7, 31)

        totals = {'scraped': 0, 'created': 0, 'updated': 0, 'errors': 0}

        # Scrape month by month to handle calendar pagination
        months = []
        cur = season_start.replace(day=1)
        while cur <= season_end:
            months.append(cur)
            # advance to next month
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)

        all_games = {}  # source_event_id -> game dict (dedup across months)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                extra_http_headers={
                    'referer': CALENDAR_URL,
                    'accept-language': 'en-US,en;q=0.9',
                },
            )
            page = ctx.new_page()

            for month_start in months:
                # last day of month
                if month_start.month == 12:
                    month_end = month_start.replace(year=month_start.year + 1, month=1, day=1) - datetime.timedelta(days=1)
                else:
                    month_end = month_start.replace(month=month_start.month + 1, day=1) - datetime.timedelta(days=1)

                cal_url = (
                    f'{CALENDAR_URL}'
                    f'&startdate={month_start.strftime("%m/%d/%Y")}'
                    f'&enddate={month_end.strftime("%m/%d/%Y")}'
                )
                self.stdout.write(f'Loading {month_start.strftime("%B %Y")}...')

                try:
                    page.goto(cal_url, wait_until='networkidle', timeout=60_000)
                    page.wait_for_timeout(3_000)
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f'  Failed to load {month_start.strftime("%B %Y")}: {exc}'))
                    totals['errors'] += 1
                    continue

                soup = BeautifulSoup(page.content(), 'html.parser')

                for tr in soup.find_all('tr'):
                    txt = tr.get_text(' ', strip=True)
                    m = _GAME_ROW_RE.search(txt)
                    if not m:
                        continue

                    away_name = m.group('away').strip()
                    home_name = m.group('home').strip()

                    # Skip degenerate matches where away == home
                    if away_name.lower() == home_name.lower():
                        continue

                    if options.get('debug'):
                        self.stdout.write(f'\n[DEBUG TR] {repr(txt[:300])}')

                    away_score_str = m.group('away_score')
                    home_score_str = m.group('home_score')
                    away_score = int(away_score_str) if away_score_str else None
                    home_score = int(home_score_str) if home_score_str else None

                    game_date = _find_date_in_text(txt)
                    if not game_date:
                        # Try walking up to parent rows/sections for date
                        for parent in tr.parents:
                            parent_txt = parent.get_text(' ', strip=True)[:400]
                            game_date = _find_date_in_text(parent_txt)
                            if game_date:
                                break
                    if not game_date:
                        continue

                    status, game_time = _detect_status_and_time(txt)
                    if away_score is not None and home_score is not None:
                        status = 'FINAL'

                    event_id = _source_event_id_from_row(tr, game_date, away_name, home_name, game_time)

                    all_games[event_id] = {
                        'date': game_date,
                        'away_team_name': away_name,
                        'home_team_name': home_name,
                        'game_time': game_time,
                        'away_score': away_score,
                        'home_score': home_score,
                        'status': status,
                        'source_event_id': event_id,
                    }

            browser.close()

        totals['scraped'] = len(all_games)
        self.stdout.write(f'Found {totals["scraped"]} unique game(s) on calendar.')

        if dry_run:
            for g in sorted(all_games.values(), key=lambda x: x['date']):
                score = (
                    f"{g['away_score']}-{g['home_score']}"
                    if g['away_score'] is not None else g.get('game_time', '')
                )
                self.stdout.write(
                    f"  {g['date']}  {g['away_team_name']} @ {g['home_team_name']}"
                    f"  {score}  [{g['status']}]  id={g['source_event_id']}"
                )
            return

        for event_id, g in all_games.items():
            try:
                _, created = ScheduledGame.objects.update_or_create(
                    source_event_id=event_id,
                    defaults={
                        'date':           g['date'],
                        'away_team_name': g['away_team_name'],
                        'home_team_name': g['home_team_name'],
                        'game_time':      g['game_time'],
                        'away_score':     g['away_score'],
                        'home_score':     g['home_score'],
                        'status':         g['status'],
                    },
                )
                if created:
                    totals['created'] += 1
                else:
                    totals['updated'] += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'  DB error for {event_id}: {exc}'))
                totals['errors'] += 1

        self.stdout.write(self.style.SUCCESS(
            f'\n{"─"*50}\n'
            f'  Scraped  : {totals["scraped"]}\n'
            f'  Created  : {totals["created"]}\n'
            f'  Updated  : {totals["updated"]}\n'
            f'  Errors   : {totals["errors"]}\n'
            f'{"─"*50}'
        ))
