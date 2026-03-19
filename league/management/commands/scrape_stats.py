"""
Management command: scrape_stats

Loads the Liberty League baseball calendar via Playwright (JS-rendered),
follows every "Box Score" link found in the given date window, parses the
batting and pitching HTML tables, and writes results into the existing
HittingGameLog / PitchingGameLog models.

Usage
-----
  python manage.py scrape_stats                         # last 3 days
  python manage.py scrape_stats --start-date 03/01/2026 --end-date 03/31/2026
  python manage.py scrape_stats --dry-run               # preview, no DB writes
  python manage.py scrape_stats --force                 # overwrite existing logs
  python manage.py scrape_stats --url <boxscore-url>    # single game

Dependencies
------------
  pip install playwright beautifulsoup4 pdfplumber
  playwright install chromium
"""

import datetime
import logging
import re
import io
import urllib.parse

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.models import (
    RealTeam, Player, RealGame,
    HittingGameLog, PitchingGameLog,
)

logger = logging.getLogger(__name__)

CALENDAR_URL = 'https://libertyleagueathletics.com/calendar.aspx?path=baseball'
DEFAULT_LOOKBACK_DAYS = 3

BOX_SCORE_SELECTORS = [
    'a:has-text("Box Score")',
    'a:has-text("box score")',
    'a:has-text("Boxscore")',
    'a[href*="boxscore"]',
    'a[href*="box_score"]',
]

PDF_LINK_SELECTORS = [
    'a:has-text("View PDF")',
    'a:has-text("View pdf")',
    'a:has-text("PDF")',
    'a[href$=".pdf"]',
    'a[href*="pdf"]',
]

CALENDAR_WAIT_MS = 15_000
TABLE_RENDER_WAIT_MS = 2_500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Stats listed in the PDF notes section that map to HittingGameLog fields.
# Skipped: SH (not in model), PO/E (fielding, not batting).
_PDF_NOTES_STAT_MAP = {
    '2b':  'doubles',
    '3b':  'triples',
    'hr':  'hr',
    'sb':  'sb',
    'cs':  'cs',
    'hbp': 'hbp',
}


def _parse_pdf_notes(text):
    """
    Parse the notes section below batting tables in PDF box scores.
    Lines look like:
      2B: Eddie Galvao (1); Jack Collins (2); Nate Vandersea (1)
      HR: Eddie Galvao (1)
    Handles wrapped lines (name split across lines) by joining any continuation
    line that does not start with a stat code back onto the previous line.
    Returns {(first_lower, last_lower): {field: count}}.
    Covers both teams — callers look up by player key.
    """
    # Join wrapped lines: a line that doesn't start with "WORD:" is a continuation
    stat_start = re.compile(r'^[A-Z0-9]{1,4}\s*:', re.MULTILINE)
    joined_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            joined_lines.append('')
            continue
        if stat_start.match(line) or not joined_lines:
            joined_lines.append(line)
        else:
            joined_lines[-1] += ' ' + line   # continuation — join to previous

    joined_text = '\n'.join(joined_lines)

    result = {}
    stat_line_re = re.compile(r'^([A-Z0-9]{1,4})\s*:\s*(.+)$', re.MULTILINE)
    player_re    = re.compile(r'([A-Za-z][A-Za-z\s\'\-\.]+?)\s*\((\d+)\)')
    for m in stat_line_re.finditer(joined_text):
        field = _PDF_NOTES_STAT_MAP.get(m.group(1).lower())
        if not field:
            continue
        for pm in player_re.finditer(m.group(2)):
            first, last = _parse_name(pm.group(1).strip())
            if first and last:
                result.setdefault((first.lower(), last.lower()), {})[field] = int(pm.group(2))
    return result


def _parse_ip(ip_str):
    """Convert Sidearm IP notation ('6.2') to total outs (20). Returns None on failure."""
    s = str(ip_str or '').strip()
    m = re.match(r'^(\d+)\.?(\d)?$', s)
    if not m:
        return None
    full = int(m.group(1))
    partial = int(m.group(2)) if m.group(2) else 0
    if partial > 2:
        return None
    return full * 3 + partial


def _int(val, default=0):
    try:
        return int(str(val or '').strip() or default)
    except (ValueError, TypeError):
        return default


def _bool_flag(val):
    v = str(val or '').strip().lower()
    return v not in ('', '0', '-', 'no')


def _parse_name(raw):
    """
    'Last, First'  -> ('First', 'Last')
    'First Last'   -> ('First', 'Last')
    Returns (None, None) on failure.
    """
    raw = str(raw or '').strip()
    raw = re.sub(r'\s*[\(\[#].*$', '', raw).strip()
    raw = re.sub(r'\s+(?:jr\.?|sr\.?|ii+|iv|vi?)$', '', raw, flags=re.I).strip()
    if not raw:
        return None, None
    if ',' in raw:
        parts = raw.split(',', 1)
        return parts[1].strip(), parts[0].strip()
    parts = raw.split()
    if len(parts) < 2:
        return None, None
    return parts[0], ' '.join(parts[1:])


def _norm_name(name):
    """Normalize a team name: lowercase, strip punctuation, collapse whitespace."""
    if not name:
        return ''
    n = name.lower().strip()
    n = re.sub(r'[^a-z0-9\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _clean_label(label):
    """Clean a raw table caption / heading for team matching."""
    if not label:
        return ''
    lab = label.lower().strip()
    # Strip sport/event suffix: "Union (N.Y.) - Baseball - 3/8/2026 - Box Score - ..."
    lab = re.sub(
        r'\s*[-–—]\s*(?:baseball|softball|basketball|football|soccer|lacrosse|tennis|'
        r'swimming|diving|track|cross[\s\-]?country|volleyball|golf|wrestling|'
        r'field hockey|rowing|crew|rugby).*$',
        '', lab, flags=re.I,
    ).strip()
    lab = re.sub(r'[\:\-–—]\s*(pitching|batting|stats|box score|boxscore).*$', '', lab, flags=re.I).strip()
    # Strip date patterns before the generic digit removal
    lab = re.sub(r'\b\d{1,2}/\d{1,2}/\d{4}\b', '', lab)
    lab = re.sub(r'\b\d{1,3}\b', '', lab).strip()
    lab = re.sub(r'[^a-z0-9\s]', ' ', lab)
    lab = re.sub(r'\s+', ' ', lab).strip()
    return lab


def _score_team_match(lab, lab_tokens, team):
    """
    Return a match score for how well `team` matches a cleaned label.

    Scores (higher = better):
      4 — exact normalized name match, or exact abbreviation match
      3 — team abbreviation appears as a token inside the label
          (handles "RIT - Baseball..." → "rit" token → RIT abbr)
      2 — bidirectional token-subset:
            team name tokens ⊆ label tokens  (label contains team name)
            OR label tokens ⊆ team name tokens (label is a short form)
      1 — prefix / startswith (normalized)
      0 — no match
    """
    name_norm = _norm_name(team.name)
    abbr_norm = (team.abbreviation or '').lower().strip()
    name_tokens = set(t for t in re.split(r'\s+', name_norm) if t) if name_norm else set()

    if name_norm == lab:
        return 4
    if abbr_norm and abbr_norm == lab:
        return 4
    if abbr_norm and abbr_norm in lab_tokens:
        return 3
    if name_tokens and name_tokens.issubset(lab_tokens):
        return 2  # team name fully contained in label — strong match
    if name_tokens and lab_tokens and lab_tokens.issubset(name_tokens):
        # label is a short form of the team name; score by coverage so that
        # "rochester" scores higher against "University of Rochester" (1/3 coverage)
        # than against "Rochester Institute of Technology" (1/4 coverage)
        return 1 + len(lab_tokens) / len(name_tokens)
    if name_norm and (lab.startswith(name_norm) or name_norm.startswith(lab)):
        return 1
    # Partial overlap: majority of the team's meaningful tokens appear in the label.
    # Strip generic education words so "Washington College" never matches "Ithaca College"
    # just because both contain "college".
    _GENERIC = {'college', 'university', 'institute', 'technology', 'polytechnic',
                'school', 'of', 'the', 'at'}
    sig_name = {t for t in name_tokens if len(t) > 2 and t not in _GENERIC}
    sig_lab  = {t for t in lab_tokens  if len(t) > 2 and t not in _GENERIC}
    if sig_name and sig_lab:
        overlap = sig_name & sig_lab
        if len(overlap) >= max(1, len(sig_name) * 0.5):
            return 1
    return 0


def _resolve_team(label, all_teams, create_placeholder=False):
    """
    Find the best matching RealTeam for a label string.

    Teams with actual players always beat lower-scoring matches.
    Within the same player-count tier, higher match score wins.

    Never creates new RealTeam objects — the team list is fixed.
    Returns None if no match is found.
    """
    lab = _clean_label(label)
    if not lab:
        return None

    lab_tokens = set(t for t in re.split(r'\s+', lab) if t)

    candidates = []
    for team in all_teams:
        score = _score_team_match(lab, lab_tokens, team)
        if score > 0:
            candidates.append((score, team))

    if candidates:
        # Primary: player count desc (real rosters beat empty ones)
        # Secondary: match score desc
        # Tertiary: name length desc (prefer more specific names)
        candidates.sort(key=lambda x: (-x[1].players.count(), -x[0], -len(x[1].name or '')))
        return candidates[0][1]

    return None

    return None


def _match_player(first, last, real_team):
    """
    Three-tier fuzzy match against the Player table for a given real team,
    then a global fallback when the name is unique across all teams.
    Tier 1: exact first + last (team-scoped)
    Tier 2: first initial + exact last (team-scoped, only if unique)
    Tier 3: exact last only (team-scoped, only if unique)
    Tier 4 (fallback): exact first + last across all teams (only if unique)
    Tier 5 (fallback): first initial + exact last across all teams (only if unique)
    Returns Player or None.
    """
    if not first or not last:
        return None

    qs = Player.objects.filter(real_team=real_team)

    p = qs.filter(last_name__iexact=last, first_name__iexact=first).first()
    if p:
        return p

    matches = qs.filter(last_name__iexact=last, first_name__istartswith=first[0])
    if matches.count() == 1:
        return matches.first()

    matches = qs.filter(last_name__iexact=last)
    if matches.count() == 1:
        return matches.first()

    # Hardcoded fallback for Rochester / RIT: these two teams are often mis-assigned
    # to each other because both names contain "rochester". Since no player names
    # overlap between the two rosters, a globally unique name match is safe.
    _ROC_RIT = {
        'rochester', 'university of rochester',
        'rit', 'rochester institute of technology',
    }
    if real_team and _norm_name(real_team.name or '') in _ROC_RIT:
        global_qs = Player.objects.filter(last_name__iexact=last, first_name__iexact=first)
        if global_qs.count() == 1:
            return global_qs.first()
        global_qs = Player.objects.filter(last_name__iexact=last, first_name__istartswith=first[0])
        if global_qs.count() == 1:
            return global_qs.first()

    # Suffix fallback: retry tiers 1-3 stripping known suffixes from last name
    # (handles players whose DB last_name includes III/IV/Jr. but box score omits it,
    #  or vice-versa — _parse_name already strips from the scraped side)
    _SUFFIX_RE = re.compile(r'\s+(?:jr\.?|sr\.?|ii+|iv|vi?)$', re.I)
    last_stripped = _SUFFIX_RE.sub('', last).strip()
    if last_stripped != last:
        # DB has suffix, box score didn't → try matching with stripped last
        p = qs.filter(last_name__iexact=last_stripped, first_name__iexact=first).first()
        if p:
            return p
        matches = qs.filter(last_name__iexact=last_stripped, first_name__istartswith=first[0])
        if matches.count() == 1:
            return matches.first()
    else:
        # Scraped name had no suffix → DB might have one; match where last starts with our value
        matches = qs.filter(last_name__istartswith=last, first_name__iexact=first)
        if matches.count() == 1:
            return matches.first()
        matches = qs.filter(last_name__istartswith=last, first_name__istartswith=first[0])
        if matches.count() == 1:
            return matches.first()

    # Compound last name: DB stores middle name + last name (e.g. 'Jay Walker III')
    # but box score only shows the last part ('Walker' or 'Walker III').
    # Try contains match on both the scraped last and its suffix-stripped form.
    for try_last in dict.fromkeys([last_stripped, last]):  # deduplicated, stripped first
        if not try_last:
            continue
        matches = qs.filter(last_name__icontains=try_last, first_name__istartswith=first[0])
        if matches.count() == 1:
            return matches.first()

    return None


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'Scrape Liberty League baseball box scores and load stats into the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--start-date', default=None,
            help='Start date MM/DD/YYYY (default: 3 days ago)',
        )
        parser.add_argument(
            '--end-date', default=None,
            help='End date MM/DD/YYYY (default: today)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be written without touching the DB',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Overwrite existing logs even if they already exist for a game',
        )
        parser.add_argument(
            '--url', default=None,
            help='Single boxscore URL to process (bypasses calendar).',
        )

    def handle(self, *args, **options):
        import os
        os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise CommandError(
                'playwright is required.\n'
                '  pip install playwright beautifulsoup4 pdfplumber\n'
                '  playwright install chromium'
            )
        try:
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError:
            raise CommandError('beautifulsoup4 is required.  pip install beautifulsoup4')
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            raise CommandError('pdfplumber is required.  pip install pdfplumber')

        today = datetime.date.today()

        def _parse_date_arg(val, label):
            for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y'):
                try:
                    return datetime.datetime.strptime(val, fmt).date()
                except ValueError:
                    continue
            raise CommandError(f'--{label} must be MM/DD/YYYY, got: {val!r}')

        single_url = options.get('url')

        if single_url:
            start = _parse_date_arg(options['start_date'], 'start-date') if options.get('start_date') else None
            end = _parse_date_arg(options['end_date'], 'end-date') if options.get('end_date') else None
        else:
            start = _parse_date_arg(options['start_date'], 'start-date') if options.get('start_date') \
                else today - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS)
            end = _parse_date_arg(options['end_date'], 'end-date') if options.get('end_date') \
                else today

        dry_run = options.get('dry_run', False)
        force = options.get('force', False)

        calendar_url = (
            f'{CALENDAR_URL}'
            f'&startdate={start.strftime("%m/%d/%Y") if start else ""}'
            f'&enddate={end.strftime("%m/%d/%Y") if end else ""}'
        )

        if not single_url:
            self.stdout.write(f'Date range : {start}  ->  {end}')
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN -- no database writes'))

        totals = {'imported': 0, 'skipped': 0, 'errors': 0, 'unmatched': 0}

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
                }
            )
            page = ctx.new_page()

            if single_url:
                self.stdout.write(f'Processing single URL: {single_url}')
                try:
                    result = self._scrape_boxscore(page, single_url, dry_run, force, start, end)
                    totals['imported'] += result.get('imported', 0)
                    totals['skipped'] += result.get('skipped', 0)
                    totals['unmatched'] += result.get('unmatched', 0)
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f'  [ERROR] {exc}'))
                    logger.exception('scrape_boxscore failed for %s', single_url)
                    totals['errors'] += 1
                browser.close()
                self._print_totals(totals, dry_run)
                if not dry_run and totals['imported'] > 0:
                    self.stdout.write('Refreshing cached player points...')
                    from league.scoring import refresh_all_players
                    refresh_all_players()
                    self.stdout.write(self.style.SUCCESS('Player points updated.'))
                return

            # ---- Load the calendar ----
            self.stdout.write('\nLoading calendar...')
            page.goto(calendar_url, wait_until='networkidle', timeout=60_000)

            # Wait for at least one box score link to appear
            for selector in BOX_SCORE_SELECTORS:
                try:
                    page.wait_for_selector(selector, timeout=CALENDAR_WAIT_MS)
                    break
                except Exception:
                    continue

            from bs4 import BeautifulSoup as _BSCal
            cal_soup = _BSCal(page.content(), 'html.parser')

            # Date patterns used to find the game date next to each link on the calendar
            _cal_date_res = [
                re.compile(
                    r'\b(?:January|February|March|April|May|June|July|August|'
                    r'September|October|November|December|'
                    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s*\d{4}',
                    re.I,
                ),
                re.compile(r'\d{1,2}/\d{1,2}/\d{4}'),
                re.compile(r'\d{4}-\d{2}-\d{2}'),
            ]
            _cal_date_fmts = [
                '%B %d %Y', '%b %d %Y', '%B. %d %Y', '%b. %d %Y',
                '%m/%d/%Y', '%Y-%m-%d',
            ]

            def _parse_cal_date(text):
                cleaned = re.sub(r'\s+', ' ', text.replace(',', '')).strip()
                for fmt in _cal_date_fmts:
                    try:
                        return datetime.datetime.strptime(cleaned, fmt).date()
                    except ValueError:
                        continue
                return None

            def _find_date_for_link(a_tag):
                """Walk up the DOM to find the date near a link.
                At each level: check heading children first (catches the date header
                of the current section before prev-siblings can return a stale date
                from the previous section), then prev-siblings, then own short text.
                """
                node = a_tag.parent
                while node and getattr(node, 'name', None):
                    # 1. Direct heading children of this node — the date label for this section
                    for heading in node.find_all(
                        ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'span'],
                        recursive=False,
                    ):
                        txt = heading.get_text(' ', strip=True)
                        if len(txt) > 100:
                            continue
                        for pat in _cal_date_res:
                            m = pat.search(txt)
                            if m:
                                d = _parse_cal_date(m.group(0))
                                if d:
                                    return d
                    # 2. Previous siblings
                    for sib in node.find_previous_siblings(limit=10):
                        txt = sib.get_text(' ', strip=True)
                        for pat in _cal_date_res:
                            m = pat.search(txt)
                            if m:
                                d = _parse_cal_date(m.group(0))
                                if d:
                                    return d
                    # 3. Own text when short enough
                    node_txt = node.get_text(' ', strip=True)
                    if len(node_txt) < 300:
                        for pat in _cal_date_res:
                            m = pat.search(node_txt)
                            if m:
                                d = _parse_cal_date(m.group(0))
                                if d:
                                    return d
                    node = node.parent
                return None

            # Matchup pattern: "Team A vs Team B" or "Team A at Team B" or "Team A @ Team B"
            _matchup_pat = re.compile(
                r'(?P<t1>[A-Za-z][A-Za-z0-9\s\.\'\-]+?)\s*(?:\([^)]*\))?\s*'
                r'(?:vs\.?|@|at)\s*'
                r'(?P<t2>[A-Za-z][A-Za-z0-9\s\.\'\-]+?)(?:\s*\([^)]*\))?\s*$',
                re.I,
            )

            # Sidearm calendar TR text: "Wed 3/11/2026  Skidmore (Away) 8  Rockford (Home) 6  Final ..."
            _sidearm_away_home = re.compile(
                r'(?P<away>[A-Za-z][A-Za-z0-9\s\.\'\-]*?)\s+\(Away\).*?'
                r'(?P<home>[A-Za-z][A-Za-z0-9\s\.\'\-]*?)\s+\(Home\)',
                re.I,
            )

            def _find_matchup_for_link(a_tag):
                """Extract away/home team names from the Sidearm game row containing this link."""
                tr = a_tag.find_parent('tr')
                if tr:
                    txt = tr.get_text(' ', strip=True)
                    m = _sidearm_away_home.search(txt)
                    if m:
                        return m.group('away').strip(), m.group('home').strip()
                return None, None

            # Ensure OOC placeholder exists for out-of-conference opponents
            ooc_team, _ = RealTeam.objects.get_or_create(
                abbreviation='OOC',
                defaults={'name': 'Out of Conference'},
            )
            all_teams_cal = list(RealTeam.objects.exclude(abbreviation='OOC'))

            # Collect (date_or_None, href, raw_t1, raw_t2) for every box score link.
            # Do NOT deduplicate here — the same URL can appear for two different dates
            # (website bug) and we need to keep both so date filtering works correctly.
            cal_links = []
            for a in cal_soup.find_all('a'):
                href = a.get('href') or ''
                text = a.get_text(strip=True)
                if not href:
                    continue
                is_boxscore = (
                    re.search(r'box\s*score|boxscore', text, re.I)
                    or 'boxscore' in href.lower()
                    or 'box_score' in href.lower()
                )
                if not is_boxscore:
                    continue
                if not href.startswith('http'):
                    href = urllib.parse.urljoin('https://libertyleagueathletics.com', href)
                raw_t1, raw_t2 = _find_matchup_for_link(a)
                cal_links.append((_find_date_for_link(a), href, raw_t1, raw_t2))

            if not cal_links:
                self.stdout.write(self.style.WARNING(
                    'No "Box Score" links found on the calendar page. '
                    'Either no games have been played yet or the page structure changed.'
                ))
                browser.close()
                return

            # Filter by date range; resolve known teams from schedule matchup text.
            # Games are in ascending date order so we can break early at end.
            # Dedup is done HERE (after filtering) so that a URL appearing on both
            # an out-of-range date and an in-range date is still processed.
            box_games = []  # list of (href, known_teams_list | None)
            seen_in_range = set()
            for link_date, href, raw_t1, raw_t2 in cal_links:
                if link_date is not None:
                    if link_date < start:
                        continue
                    if link_date > end:
                        break   # ascending order: nothing later can be in range
                if href in seen_in_range:
                    continue
                seen_in_range.add(href)

                # Resolve the two teams from the schedule text so box score parsing
                # is constrained to exactly these two teams (avoids cross-team confusion
                # e.g. "Rochester" matching RIT instead of University of Rochester).
                known = []
                for raw in (raw_t1, raw_t2):
                    if raw:
                        t = _resolve_team(raw, all_teams_cal, create_placeholder=False)
                        if t:
                            known.append(t)
                box_games.append((href, known if len(known) == 2 and known[0].pk != known[1].pk else None))

            if not box_games:
                self.stdout.write(self.style.WARNING(
                    f'No box scores found within {start} – {end}. '
                    f'({len(cal_links)} game(s) found outside this range on the calendar page.)'
                ))
                browser.close()
                return

            self.stdout.write(f'Found {len(box_games)} box score link(s) in range\n')

            for box_url, known_teams in box_games:
                label = (
                    f'{known_teams[0].name} vs {known_teams[1].name}'
                    if known_teams else box_url
                )
                self.stdout.write(f'  [ ] {label}')
                try:
                    result = self._scrape_boxscore(page, box_url, dry_run, force, start, end, known_teams)
                    totals['imported'] += result.get('imported', 0)
                    totals['skipped'] += result.get('skipped', 0)
                    totals['unmatched'] += result.get('unmatched', 0)
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f'  [ERROR] {exc}'))
                    logger.exception('scrape_boxscore failed for %s', box_url)
                    totals['errors'] += 1

            browser.close()

        self._print_totals(totals, dry_run)
        if not dry_run and totals['imported'] > 0:
            self.stdout.write('Refreshing cached player points...')
            from league.scoring import refresh_all_players, refresh_all_coaches
            refresh_all_players()
            self.stdout.write(self.style.SUCCESS('Player points updated.'))
            refresh_all_coaches()
            self.stdout.write(self.style.SUCCESS('Coach points updated.'))

    def _print_totals(self, totals, dry_run):
        self.stdout.write(self.style.SUCCESS(
            f'\n{"-"*50}\n'
            f'  Stat lines written : {totals["imported"]}\n'
            f'  Games skipped      : {totals["skipped"]}  (already logged; use --force to overwrite)\n'
            f'  Unmatched players  : {totals["unmatched"]}  (not in DB; check name spelling / roster import)\n'
            f'  Errors             : {totals["errors"]}\n'
            f'{"-"*50}'
        ))

    # -----------------------------------------------------------------------
    # Box score scraping
    # -----------------------------------------------------------------------

    def _scrape_boxscore(self, page, url, dry_run, force, start=None, end=None, known_teams=None):
        """
        Load one box score page and write all batting/pitching logs.
        Attempts PDF parsing first, falls back to HTML.
        Returns {'imported': N, 'skipped': N, 'unmatched': N}.
        """
        from bs4 import BeautifulSoup
        try:
            import pdfplumber
        except Exception:
            pdfplumber = None

        page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        page.wait_for_timeout(TABLE_RENDER_WAIT_MS)
        html_content = page.content()

        pdf_tables_html = ''
        pdf_notes = {}  # {(first_lower, last_lower): {field: count}}

        try:
            pdf_url = None
            for sel in PDF_LINK_SELECTORS:
                try:
                    el = page.query_selector(sel)
                    if el:
                        href = el.get_attribute('href') or ''
                        if href:
                            pdf_url = urllib.parse.urljoin(url, href)
                            break
                except Exception:
                    continue

            if not pdf_url:
                for a in page.query_selector_all('a'):
                    try:
                        href = a.get_attribute('href') or ''
                        if '.pdf' in href.lower() or 'document.aspx' in href.lower():
                            pdf_url = urllib.parse.urljoin(url, href)
                            break
                    except Exception:
                        continue

            if pdf_url and pdfplumber:
                final_pdf_bytes = None

                try:
                    nav_resp = page.goto(pdf_url, wait_until='load', timeout=60_000)
                except Exception:
                    nav_resp = None

                try:
                    if nav_resp and 'application/pdf' in (nav_resp.headers.get('content-type') or '').lower():
                        final_pdf_bytes = nav_resp.body()
                except Exception:
                    pass

                if final_pdf_bytes is None:
                    try:
                        iframe = page.query_selector('iframe[src*=".pdf"], embed[src*=".pdf"], object[data*=".pdf"]')
                        if iframe:
                            src = iframe.get_attribute('src') or iframe.get_attribute('data') or ''
                            if src:
                                src = urllib.parse.urljoin(page.url, src)
                                resp = page.context.request.get(src, timeout=60_000)
                                if resp.status == 200 and 'application/pdf' in (resp.headers.get('content-type') or '').lower():
                                    final_pdf_bytes = resp.body()

                        if final_pdf_bytes is None:
                            for a in page.query_selector_all('a'):
                                try:
                                    ahref = a.get_attribute('href') or ''
                                    if '.pdf' in ahref.lower():
                                        candidate = urllib.parse.urljoin(page.url, ahref)
                                        resp = page.context.request.get(candidate, timeout=60_000)
                                        if resp.status == 200 and 'application/pdf' in (resp.headers.get('content-type') or '').lower():
                                            final_pdf_bytes = resp.body()
                                            break
                                except Exception:
                                    continue
                    except Exception:
                        pass

                if final_pdf_bytes is None:
                    try:
                        resp = page.context.request.get(pdf_url, timeout=60_000)
                        if resp.status == 200 and 'application/pdf' in (resp.headers.get('content-type') or '').lower():
                            final_pdf_bytes = resp.body()
                    except Exception:
                        pass

                if final_pdf_bytes:
                    try:
                        with pdfplumber.open(io.BytesIO(final_pdf_bytes)) as pdf:
                            tbl_fragments = []
                            full_text_parts = []
                            for page_obj in pdf.pages:
                                page_text = page_obj.extract_text() or ''
                                if page_text:
                                    full_text_parts.append(page_text)
                                for tbl in (page_obj.extract_tables() or []):
                                    if not tbl or len(tbl) < 2:
                                        continue
                                    norm_rows = [[(cell or '').strip() for cell in row] for row in tbl]
                                    header_row = norm_rows[0]
                                    body_rows = norm_rows[1:]
                                    ths = ''.join(f'<th>{h}</th>' for h in header_row)
                                    trs = ''.join(
                                        '<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>'
                                        for row in body_rows
                                    )
                                    tbl_fragments.append(f'<table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>')
                            if tbl_fragments:
                                pdf_tables_html = '\n'.join(tbl_fragments)
                            if full_text_parts:
                                pdf_notes = _parse_pdf_notes('\n'.join(full_text_parts))
                    except Exception:
                        pass
        except Exception:
            pass

        if pdf_tables_html:
            soup = BeautifulSoup(pdf_tables_html + '\n' + html_content, 'html.parser')
        else:
            soup = BeautifulSoup(html_content, 'html.parser')

        # ---- Identify game date ----
        game_date = self._extract_date(soup, url)
        if not game_date:
            self.stdout.write(self.style.WARNING('  --> Could not determine game date -- skipping'))
            return {'imported': 0, 'skipped': 0, 'unmatched': 0}

        if start and game_date < start:
            self.stdout.write(f'  --> {game_date} is before {start} -- skipping')
            return {'imported': 0, 'skipped': 0, 'unmatched': 0}
        if end and game_date > end:
            self.stdout.write(f'  --> {game_date} is after {end} -- skipping')
            return {'imported': 0, 'skipped': 0, 'unmatched': 0}

        # ---- Find and assign stat tables to teams ----
        batting_tables = self._find_stat_tables(soup, 'batting')
        pitching_tables = self._find_stat_tables(soup, 'pitching')
        team_tables = self._assign_tables(soup, batting_tables, pitching_tables, known_teams)

        if not team_tables:
            self.stdout.write(self.style.WARNING('  --> No known-team tables found -- skipping'))
            return {'imported': 0, 'skipped': 0, 'unmatched': 0}

        # ---- Determine home/away order ----
        page_order = self._extract_teams(soup)

        def _page_idx(t):
            try:
                return page_order.index(t)
            except ValueError:
                return 999

        actual_teams = sorted(team_tables.keys(), key=_page_idx)

        if len(actual_teams) == 1:
            def _label_text(table):
                cap = table.find('caption')
                if cap:
                    return cap.get_text(separator=' ', strip=True)
                prev = table.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'strong'])
                return prev.get_text(separator=' ', strip=True) if prev else ''

            opponent_label = None
            for tbl in (list(batting_tables) + list(pitching_tables)):
                lbl = _label_text(tbl)
                if not lbl:
                    continue
                if actual_teams[0].name.lower() in lbl.lower():
                    continue
                opponent_label = lbl
                break

            if opponent_label:
                all_known = list(RealTeam.objects.exclude(abbreviation='OOC'))
                opponent = _resolve_team(opponent_label, all_known)
                if opponent and opponent not in actual_teams:
                    actual_teams.append(opponent)

            # Still only 1 team — opponent is OOC
            if len(actual_teams) == 1:
                ooc_team = RealTeam.objects.get(abbreviation='OOC')
                actual_teams.append(ooc_team)
            else:
                for t in page_order:
                    if t not in actual_teams:
                        actual_teams.append(t)
                        break

        if len(actual_teams) < 2:
            self.stdout.write(self.style.WARNING('  --> Could not identify two teams -- skipping'))
            return {'imported': 0, 'skipped': 0, 'unmatched': 0}

        away_team, home_team = actual_teams[0], actual_teams[1]
        self.stdout.write(f'{away_team.name} @ {home_team.name}  ({game_date})')

        # Look up by source URL first — ensures re-runs find the same game
        # and doubleheaders (same date+teams, different URL) get separate entries.
        game = RealGame.objects.filter(source_url=url).first()
        if not game:
            existing_count = RealGame.objects.filter(
                date=game_date, home_team=home_team, away_team=away_team
            ).count()
            game = RealGame.objects.create(
                date=game_date,
                home_team=home_team,
                away_team=away_team,
                game_number=existing_count + 1,
                source_url=url,
            )

        if not force:
            already = (
                HittingGameLog.objects.filter(game=game).exists()
                or PitchingGameLog.objects.filter(game=game).exists()
            )
            if already:
                self.stdout.write('  --> Already logged -- skipping  (--force to overwrite)')
                return {'imported': 0, 'skipped': 1, 'unmatched': 0}

        total_imported = 0
        total_unmatched = 0

        with transaction.atomic():
            for team, tables in team_tables.items():
                if tables.get('batting'):
                    imp, unm = self._import_batting(tables['batting'], game, team, dry_run, force, pdf_notes)
                    total_imported += imp
                    total_unmatched += unm
                if tables.get('pitching'):
                    imp, unm = self._import_pitching(tables['pitching'], game, team, dry_run, force)
                    total_imported += imp
                    total_unmatched += unm

        if not dry_run:
            from league.models import PitchingGameLog as _PGL
            winning_log = _PGL.objects.filter(game=game, win=True).select_related('player__real_team').first()
            if winning_log:
                game.winner = winning_log.player.real_team
                game.save(update_fields=['winner'])

        verb = 'Would write' if dry_run else 'Wrote'
        self.stdout.write(
            f'  --> {verb} {total_imported} stat line(s)'
            + (f', {total_unmatched} unmatched player(s)' if total_unmatched else '')
        )

        return {'imported': total_imported, 'skipped': 0, 'unmatched': total_unmatched}

    # -----------------------------------------------------------------------
    # Date extraction
    # -----------------------------------------------------------------------

    def _extract_date(self, soup, url):
        candidate_texts = []

        title = soup.find('title')
        if title:
            candidate_texts.append(title.get_text())

        for tag in soup.find_all(['h1', 'h2', 'h3', 'div'], limit=30):
            t = tag.get_text(' ', strip=True)
            if t:
                candidate_texts.append(t)

        date_patterns = [
            (r'(\w+\.?\s+\d{1,2},?\s*\d{4})', ['%B %d %Y', '%b %d %Y', '%B. %d %Y']),
            (r'(\d{1,2}/\d{1,2}/\d{4})', ['%m/%d/%Y']),
            (r'(\d{4}-\d{2}-\d{2})', ['%Y-%m-%d']),
        ]

        for text in candidate_texts:
            for pat, fmts in date_patterns:
                m = re.search(pat, text)
                if m:
                    raw = re.sub(r'\s+', ' ', m.group(1).replace(',', '')).strip()
                    for fmt in fmts:
                        try:
                            return datetime.datetime.strptime(raw, fmt).date()
                        except ValueError:
                            continue

        for pat, fmt in [(r'(\d{8})', '%Y%m%d'), (r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d')]:
            m = re.search(pat, url)
            if m:
                try:
                    return datetime.datetime.strptime(m.group(1), fmt).date()
                except ValueError:
                    pass

        return None

    # -----------------------------------------------------------------------
    # Team extraction
    # -----------------------------------------------------------------------

    def _extract_teams(self, soup):
        """
        Return a list of RealTeam objects [away, home] for this box score.
        Uses _resolve_team so it will find real roster teams before creating
        placeholders, preventing garbage team rows from polluting the DB.
        """
        all_teams = list(RealTeam.objects.exclude(abbreviation='OOC'))

        try:
            text_with_lines = soup.get_text('\n', strip=True)
            non_empty = [ln.strip() for ln in text_with_lines.splitlines() if ln.strip()]
            for line in non_empty[:3]:
                m = re.match(
                    r'^\s*(?P<t1>.*?)\s*\([^)]*\)\s*(?:[-–—]?\s*vs\.?-?|\bvs\.?\b|@)\s*(?P<t2>.*?)\s*\([^)]*\)\s*$',
                    line, flags=re.I
                )
                if not m:
                    m = re.match(
                        r'^\s*(?P<t1>.*?)\s*(?:\([^)]*\))?\s*(?:[-–—]?\s*vs\.?-?|\bvs\.?\b|@)\s*(?P<t2>.*?)\s*(?:\([^)]*\))?\s*$',
                        line, flags=re.I
                    )
                if m:
                    raw_t1 = m.group('t1').strip()
                    raw_t2 = m.group('t2').strip()
                    if not raw_t1 or not raw_t2:
                        continue
                    t1 = _resolve_team(raw_t1, all_teams, create_placeholder=False)
                    t2 = _resolve_team(raw_t2, all_teams, create_placeholder=False)
                    if t1 and t2:
                        return [t1, t2]
        except Exception:
            pass

        def find_team_in_text(text):
            text_l = text.lower()
            for team in all_teams:
                if team.name and team.name.lower() in text_l:
                    return team
            return None

        candidates = []
        title_tag = soup.find('title')
        if title_tag:
            candidates.append(title_tag.get_text())
        for tag in soup.find_all(['h1', 'h2', 'h3'], limit=15):
            t = tag.get_text(' ', strip=True)
            if t:
                candidates.append(t)

        for text in candidates:
            for sep_pat in [r'\bat\b', r'\bvs\.?\b', r'@']:
                parts = re.split(sep_pat, text, maxsplit=1, flags=re.I)
                if len(parts) == 2:
                    away = find_team_in_text(parts[0])
                    home = find_team_in_text(parts[1])
                    if away and home and away.pk != home.pk:
                        return [away, home]

        page_text = soup.get_text(' ', strip=True)
        hits = []
        for team in all_teams:
            if not team.name:
                continue
            idx = page_text.lower().find(team.name.lower())
            if idx != -1:
                hits.append((idx, team))

        hits.sort(key=lambda x: x[0])
        seen = set()
        ordered = []
        for _, team in hits:
            if team.pk not in seen:
                seen.add(team.pk)
                ordered.append(team)

        return ordered

    # -----------------------------------------------------------------------
    # Table detection
    # -----------------------------------------------------------------------

    def _find_stat_tables(self, soup, table_type):
        batting_keywords = {'batting', 'hitting', 'batters', 'offense'}
        pitching_keywords = {'pitching', 'pitchers', 'hurlers'}
        keywords = batting_keywords if table_type == 'batting' else pitching_keywords

        found = []

        for table in soup.find_all('table'):
            caption = table.find('caption')
            if caption and any(k in caption.get_text().lower() for k in keywords):
                if table not in found:
                    found.append(table)
                continue

            prev_heading = table.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'th', 'strong'])
            if prev_heading:
                heading_text = prev_heading.get_text().lower()
                if any(k in heading_text for k in keywords):
                    if table not in found:
                        found.append(table)
                    continue

            headers = {th.get_text(strip=True).lower() for th in table.find_all('th')}
            if table_type == 'batting' and {'ab', 'h', 'rbi'}.issubset(headers):
                cap = table.find('caption')
                cap_text = cap.get_text().lower() if cap else ''
                if 'composite' not in cap_text and 'season' not in cap_text:
                    if table not in found:
                        found.append(table)
            elif table_type == 'pitching' and {'ip', 'er'}.issubset(headers):
                if table not in found:
                    found.append(table)

        return found

    # -----------------------------------------------------------------------
    # Table-to-team assignment
    # -----------------------------------------------------------------------

    def _assign_tables(self, soup, batting_tables, pitching_tables, known_teams=None):
        """
        Assign each stat table to a RealTeam.
        If known_teams is provided (resolved from the schedule page), matching is
        constrained to only those two teams — no placeholders are created and there
        is no risk of cross-team confusion (e.g. "Rochester" matching RIT).
        Falls back to searching all teams when known_teams is None.
        Returns {team: {'batting': table, 'pitching': table}, ...}.
        """
        candidate_teams = known_teams if known_teams else list(RealTeam.objects.exclude(abbreviation='OOC'))
        create_ph = False
        result = {}

        def _get_label(table):
            caption = table.find('caption')
            if caption:
                return caption.get_text(separator=' ', strip=True)
            prev = table.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'strong'])
            return prev.get_text(separator=' ', strip=True) if prev else ''

        for table in batting_tables:
            team = _resolve_team(_get_label(table), candidate_teams, create_placeholder=create_ph)
            if team:
                result.setdefault(team, {})['batting'] = table

        for table in pitching_tables:
            team = _resolve_team(_get_label(table), candidate_teams, create_placeholder=create_ph)
            if team:
                result.setdefault(team, {})['pitching'] = table

        return result

    # -----------------------------------------------------------------------
    # Column header parsing
    # -----------------------------------------------------------------------

    def _col_map(self, table):
        thead = table.find('thead')
        header_cells = thead.find_all('th') if thead else table.find_all('th')
        return {th.get_text(strip=True).lower(): idx for idx, th in enumerate(header_cells)}

    def _cell(self, cells, col_map, *col_names, default=''):
        for name in col_names:
            idx = col_map.get(name)
            if idx is not None:
                try:
                    return cells[idx].get_text(strip=True)
                except IndexError:
                    pass
        return default

    # -----------------------------------------------------------------------
    # Batting import
    # -----------------------------------------------------------------------

    def _import_batting(self, table, game, real_team, dry_run, force, pdf_notes=None):
        imported = 0
        unmatched = 0
        col_map = self._col_map(table)

        candidate_name_cols = [col_map.get(k) for k in ('player', 'name', 'batters', 'batter') if k in col_map]
        if not candidate_name_cols:
            candidate_name_cols = [0]

        tbody = table.find('tbody') or table

        pos_pat = re.compile(r'^(?:p|c|1b|2b|3b|ss|rf|lf|cf|of|dh|ph|inf|ut|pr|pinch|pinch-hitter|pinch-runner)$', re.I)

        name_patterns = [
            re.compile(r'\b([A-Z][A-Za-z\-\']{1,}\s*,\s*[A-Z][A-Za-z\-\']{1,})\b'),
            re.compile(r'\b([A-Z][a-z]+\s+[A-Z][a-zA-Z\-\']{1,})\b'),
            re.compile(r'\b([A-Z]\.\s*[A-Z][a-zA-Z\-\']{1,})\b'),
            re.compile(r'\b([A-Z][a-zA-Z\-\']{1,}\s+[A-Z]\.)\b'),
        ]

        for row in tbody.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            if not cells:
                continue

            name_raw = ''
            for idx in candidate_name_cols:
                if idx is None or idx >= len(cells):
                    continue
                cell = cells[idx]
                anchor = cell.find('a') or cell.find('span')
                txt = (anchor.get_text(strip=True) if anchor else cell.get_text(strip=True)).strip()
                if txt and not pos_pat.match(txt.lower()):
                    name_raw = txt
                    break

            if not name_raw:
                candidate = None
                for c in cells:
                    a = c.find('a')
                    if a:
                        t = a.get_text(strip=True)
                        if t and not pos_pat.match(t.lower()):
                            candidate = t
                            break
                if not candidate:
                    for c in cells:
                        t_all = c.get_text(separator=' ').strip()
                        if not t_all:
                            continue
                        for p in re.split(r'[\/\|\(\)\-–—]', t_all):
                            t = p.strip()
                            if not t or pos_pat.match(t.lower()):
                                continue
                            for pat in name_patterns:
                                m = pat.search(t)
                                if m:
                                    candidate = m.group(1).strip()
                                    break
                            if candidate:
                                break
                        if candidate:
                            break
                if not candidate:
                    row_text = row.get_text(separator=' ', strip=True)
                    for pat in name_patterns:
                        m = pat.search(row_text)
                        if m:
                            candidate = m.group(1).strip()
                            break
                if candidate:
                    name_raw = candidate

            if not name_raw or re.match(r'^(totals?|player|name|batters?|-+)$', name_raw, re.I):
                continue

            first, last = _parse_name(name_raw)
            if not first or not last:
                continue

            player = _match_player(first, last, real_team)
            if not player:
                self.stdout.write(self.style.WARNING(f'[NO MATCH] batter: {name_raw!r}  ({real_team.name})'))
                unmatched += 1
                continue
            if player.real_team_id != real_team.pk:
                real_team = player.real_team

            if not force and HittingGameLog.objects.filter(player=player, game=game).exists():
                continue

            ab      = _int(self._cell(cells, col_map, 'ab', 'atbats'))
            runs    = _int(self._cell(cells, col_map, 'r', 'runs'))
            hits    = _int(self._cell(cells, col_map, 'h', 'hits'))
            doubles = _int(self._cell(cells, col_map, '2b', 'doubles'))
            triples = _int(self._cell(cells, col_map, '3b', 'triples'))
            hr      = _int(self._cell(cells, col_map, 'hr', 'homers'))
            rbi     = _int(self._cell(cells, col_map, 'rbi'))
            bb      = _int(self._cell(cells, col_map, 'bb', 'walks', 'bases on balls'))
            so      = _int(self._cell(cells, col_map, 'so', 'k', 'ks', 'strikeouts'))
            sb      = _int(self._cell(cells, col_map, 'sb', 'sba'))
            cs      = _int(self._cell(cells, col_map, 'cs'))
            hbp     = _int(self._cell(cells, col_map, 'hbp', 'hp'))

            # Supplement with PDF notes section (e.g. "2B: Name (1); ...")
            # Notes are authoritative for these fields — take max so we don't
            # lose a value already found in the table.
            if pdf_notes:
                note_key = (player.first_name.lower(), player.last_name.lower())
                for field, val in pdf_notes.get(note_key, {}).items():
                    if   field == 'doubles': doubles = max(doubles, val)
                    elif field == 'triples': triples = max(triples, val)
                    elif field == 'hr':      hr      = max(hr,      val)
                    elif field == 'sb':      sb      = max(sb,      val)
                    elif field == 'cs':      cs      = max(cs,      val)
                    elif field == 'hbp':     hbp     = max(hbp,     val)

            extras = []
            if doubles: extras.append(f'{doubles}2B')
            if triples: extras.append(f'{triples}3B')
            if hr:      extras.append(f'{hr}HR')
            if sb:      extras.append(f'{sb}SB')
            if cs:      extras.append(f'{cs}CS')
            if hbp:     extras.append(f'{hbp}HBP')
            self.stdout.write(
                f'BAT   {real_team.name:20}  {last}, {first}: '
                f'{ab}AB {hits}H {hr}HR {rbi}RBI {runs}R'
                + (f'  [{", ".join(extras)}]' if extras else '')
            )

            if not dry_run:
                HittingGameLog.objects.update_or_create(
                    player=player, game=game,
                    defaults=dict(
                        ab=ab, runs=runs, hits=hits,
                        doubles=doubles, triples=triples, hr=hr,
                        rbi=rbi, bb=bb, so=so, sb=sb, cs=cs, hbp=hbp,
                        entered_by=None,
                    ),
                )
            imported += 1

        return imported, unmatched

    # -----------------------------------------------------------------------
    # Pitching import
    # -----------------------------------------------------------------------

    def _import_pitching(self, table, game, real_team, dry_run, force):
        imported = 0
        unmatched = 0
        col_map = self._col_map(table)

        tbody = table.find('tbody') or table
        for row in tbody.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            if len(cells) < 3:
                continue

            name_raw = cells[0].get_text(strip=True)
            if not name_raw or re.match(r'^(totals?|player|name|pitchers?|-+)$', name_raw, re.I):
                continue

            # Check for decision embedded in name: "Nick Palovich (W, 1-0)"
            name_decision = None
            name_dec_match = re.search(r'\(\s*([WLS])\s*[,\)]', name_raw, re.I)
            if name_dec_match:
                name_decision = name_dec_match.group(1).upper()
                name_raw = name_raw[:name_dec_match.start()].strip()

            first, last = _parse_name(name_raw)
            if not first or not last:
                continue

            player = _match_player(first, last, real_team)
            if not player:
                self.stdout.write(self.style.WARNING(f'[NO MATCH] pitcher: {name_raw!r}  ({real_team.name})'))
                unmatched += 1
                continue
            if player.real_team_id != real_team.pk:
                real_team = player.real_team

            if not force and PitchingGameLog.objects.filter(player=player, game=game).exists():
                continue

            ip_str = self._cell(cells, col_map, 'ip', 'inn', default='0')
            outs = _parse_ip(ip_str)
            if outs is None:
                self.stdout.write(self.style.WARNING(f'Bad IP "{ip_str}" for {name_raw} -- defaulting to 0'))
                outs = 0

            hits = _int(self._cell(cells, col_map, 'h', 'hits'))
            runs = _int(self._cell(cells, col_map, 'r', 'runs'))
            er   = _int(self._cell(cells, col_map, 'er'))
            bb   = _int(self._cell(cells, col_map, 'bb', 'walks'))
            so   = _int(self._cell(cells, col_map, 'so', 'k', 'ks'))
            hr   = _int(self._cell(cells, col_map, 'hr'))

            if name_decision:
                win  = name_decision == 'W'
                loss = name_decision == 'L'
                save = name_decision == 'S'
            else:
                dec_raw = self._cell(cells, col_map, 'dec', 'decision', 'result', default='')
                if dec_raw:
                    dec = dec_raw.upper()
                    win  = dec.startswith('W')
                    loss = dec.startswith('L')
                    save = 'SV' in dec or dec.startswith('S')
                else:
                    win  = _bool_flag(self._cell(cells, col_map, 'w', 'win'))
                    loss = _bool_flag(self._cell(cells, col_map, 'l', 'loss'))
                    save = _bool_flag(self._cell(cells, col_map, 'sv', 'save', 's'))

            self.stdout.write(
                f'PITCH {real_team.name:20}  {last}, {first}: '
                f'{ip_str} IP  {so}K  {er}ER'
                + (' W' if win else '') + (' L' if loss else '')
                + (' SV' if save else '')
            )

            if not dry_run:
                PitchingGameLog.objects.update_or_create(
                    player=player, game=game,
                    defaults=dict(
                        outs=outs, hits=hits, runs=runs, er=er,
                        bb=bb, so=so, hr=hr,
                        win=win, loss=loss, save_game=save,
                        entered_by=None,
                    ),
                )
            imported += 1

        return imported, unmatched
