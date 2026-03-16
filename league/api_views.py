"""
Ingest API — accepts scraped game/stat data from the local scraper script.

POST /api/ingest/
  Authorization: Bearer <INGEST_SECRET>
  Content-Type:  application/json

Payload schema
--------------
{
  "games": [
    {
      "date":        "YYYY-MM-DD",
      "home_team":   "<RealTeam abbreviation>",
      "away_team":   "<RealTeam abbreviation>",
      "game_number": 1,            // optional, defaults to 1
      "source_url":  "https://...",// optional but used as upsert key when present
      "hitting_logs": [
        {
          "player_first_name": "...",
          "player_last_name":  "...",
          "player_team":       "<RealTeam abbreviation>",
          "ab": 0, "runs": 0, "hits": 0, "doubles": 0, "triples": 0,
          "hr": 0, "rbi": 0, "bb": 0, "so": 0, "sb": 0, "cs": 0, "hbp": 0
        }
      ],
      "pitching_logs": [
        {
          "player_first_name": "...",
          "player_last_name":  "...",
          "player_team":       "<RealTeam abbreviation>",
          "outs": 0, "hits": 0, "runs": 0, "er": 0,
          "bb": 0, "so": 0, "hr": 0,
          "win": false, "loss": false, "save_game": false
        }
      ]
    }
  ]
}

Responses
---------
200  { "status": "ok", "games_processed": N, "warnings": [...] }
400  { "error": "<reason>" }
401  { "error": "unauthorized" }
"""

import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import HittingGameLog, PitchingGameLog, Player, RealGame, RealTeam
from .scoring import refresh_player_points

logger = logging.getLogger('league')

# Required top-level key; hitting and pitching are optional per game
_REQUIRED_GAME_FIELDS = {'date', 'home_team', 'away_team'}

_HITTING_INT_FIELDS = ('ab', 'runs', 'hits', 'doubles', 'triples', 'hr',
                        'rbi', 'bb', 'so', 'sb', 'cs', 'hbp')
_PITCHING_INT_FIELDS = ('outs', 'hits', 'runs', 'er', 'bb', 'so', 'hr')
_PITCHING_BOOL_FIELDS = ('win', 'loss', 'save_game')


@csrf_exempt
@require_POST
def ingest(request):
    # --- Authentication ---
    secret = settings.INGEST_SECRET
    if not secret:
        logger.error('INGEST_SECRET is not configured')
        return JsonResponse({'error': 'ingest endpoint not configured'}, status=500)

    auth_header = request.META.get('HTTP_AUTHORIZATION', '')
    if not auth_header.startswith('Bearer ') or auth_header[7:] != secret:
        logger.warning('Ingest auth failure from %s', request.META.get('REMOTE_ADDR'))
        return JsonResponse({'error': 'unauthorized'}, status=401)

    # --- Parse body ---
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonResponse({'error': f'invalid JSON: {exc}'}, status=400)

    if not isinstance(payload, dict) or 'games' not in payload:
        return JsonResponse({'error': 'payload must be {"games": [...]}'}, status=400)

    games_data = payload['games']
    if not isinstance(games_data, list):
        return JsonResponse({'error': '"games" must be a list'}, status=400)

    # --- Process each game ---
    warnings = []
    games_processed = 0
    affected_players = set()

    for idx, game_data in enumerate(games_data):
        prefix = f'games[{idx}]'

        missing = _REQUIRED_GAME_FIELDS - set(game_data.keys())
        if missing:
            warnings.append(f'{prefix}: missing fields {sorted(missing)}, skipped')
            continue

        # Resolve real teams
        home_abbr = game_data['home_team']
        away_abbr = game_data['away_team']
        try:
            home_team = RealTeam.objects.get(abbreviation=home_abbr)
        except RealTeam.DoesNotExist:
            warnings.append(f'{prefix}: unknown home_team abbreviation "{home_abbr}", skipped')
            continue
        try:
            away_team = RealTeam.objects.get(abbreviation=away_abbr)
        except RealTeam.DoesNotExist:
            warnings.append(f'{prefix}: unknown away_team abbreviation "{away_abbr}", skipped')
            continue

        # Parse date
        try:
            game_date = game_data['date']  # Django will validate format on save
        except Exception:
            warnings.append(f'{prefix}: bad date format, skipped')
            continue

        game_number = int(game_data.get('game_number', 1))
        source_url = game_data.get('source_url') or None

        # Upsert RealGame
        try:
            if source_url:
                game, _ = RealGame.objects.update_or_create(
                    source_url=source_url,
                    defaults={
                        'date': game_date,
                        'home_team': home_team,
                        'away_team': away_team,
                        'game_number': game_number,
                    },
                )
            else:
                game, _ = RealGame.objects.update_or_create(
                    date=game_date,
                    home_team=home_team,
                    away_team=away_team,
                    game_number=game_number,
                    defaults={},
                )
        except Exception as exc:
            warnings.append(f'{prefix}: could not upsert game record: {exc}, skipped')
            continue

        # Hitting logs
        for hlog in game_data.get('hitting_logs', []):
            player = _resolve_player(hlog, warnings, prefix + '.hitting')
            if player is None:
                continue
            hit_defaults = {f: int(hlog.get(f, 0)) for f in _HITTING_INT_FIELDS}
            try:
                HittingGameLog.objects.update_or_create(
                    player=player, game=game, defaults=hit_defaults
                )
                affected_players.add(player.pk)
            except Exception as exc:
                warnings.append(f'{prefix}.hitting {player}: {exc}')

        # Pitching logs
        for plog in game_data.get('pitching_logs', []):
            player = _resolve_player(plog, warnings, prefix + '.pitching')
            if player is None:
                continue
            pitch_defaults = {f: int(plog.get(f, 0)) for f in _PITCHING_INT_FIELDS}
            pitch_defaults.update({f: bool(plog.get(f, False)) for f in _PITCHING_BOOL_FIELDS})
            try:
                PitchingGameLog.objects.update_or_create(
                    player=player, game=game, defaults=pitch_defaults
                )
                affected_players.add(player.pk)
            except Exception as exc:
                warnings.append(f'{prefix}.pitching {player}: {exc}')

        games_processed += 1

    # Refresh cached points for every touched player
    for pk in affected_players:
        try:
            refresh_player_points(Player.objects.get(pk=pk))
        except Exception as exc:
            logger.warning('refresh_player_points failed for player %s: %s', pk, exc)

    logger.info('Ingest: %d games processed, %d players refreshed, %d warnings',
                games_processed, len(affected_players), len(warnings))

    return JsonResponse({
        'status': 'ok',
        'games_processed': games_processed,
        'players_refreshed': len(affected_players),
        'warnings': warnings,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_player(log_data, warnings, prefix):
    """Look up a Player by name + team abbreviation. Returns None and appends
    a warning if not found."""
    first = log_data.get('player_first_name', '').strip()
    last = log_data.get('player_last_name', '').strip()
    team_abbr = log_data.get('player_team', '').strip()

    if not first or not last or not team_abbr:
        warnings.append(f'{prefix}: missing player_first_name/player_last_name/player_team, skipped')
        return None

    try:
        team = RealTeam.objects.get(abbreviation=team_abbr)
    except RealTeam.DoesNotExist:
        warnings.append(f'{prefix}: unknown player_team abbreviation "{team_abbr}", skipped')
        return None

    try:
        return Player.objects.get(
            first_name__iexact=first,
            last_name__iexact=last,
            real_team=team,
        )
    except Player.DoesNotExist:
        warnings.append(f'{prefix}: player "{first} {last}" ({team_abbr}) not found, skipped')
        return None
    except Player.MultipleObjectsReturned:
        warnings.append(f'{prefix}: multiple players matched "{first} {last}" ({team_abbr}), skipped')
        return None
