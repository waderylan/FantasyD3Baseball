#!/usr/bin/env python3
"""
send_data.py — POST scraped stats to the deployed Fantasy D3 Baseball ingest endpoint.

Usage
-----
    python send_data.py --url https://yourdomain.com --secret <INGEST_SECRET> --data data.json

    # Or set env vars so you don't repeat them every run:
    export INGEST_URL=https://yourdomain.com
    export INGEST_SECRET=<token>
    python send_data.py --data data.json

    # Pipe JSON directly:
    python scraper.py | python send_data.py --url https://yourdomain.com --secret <token>

Payload format (data.json)
--------------------------
{
  "games": [
    {
      "date":        "YYYY-MM-DD",
      "home_team":   "<RealTeam abbreviation>",
      "away_team":   "<RealTeam abbreviation>",
      "game_number": 1,
      "source_url":  "https://...",
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
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error


ENDPOINT_PATH = '/api/ingest/'


def parse_args():
    parser = argparse.ArgumentParser(description='Send scraped stats to the ingest endpoint.')
    parser.add_argument(
        '--url',
        default=os.environ.get('INGEST_URL', ''),
        help='Base URL of the deployed site (e.g. https://yourdomain.com). '
             'Also read from INGEST_URL env var.',
    )
    parser.add_argument(
        '--secret',
        default=os.environ.get('INGEST_SECRET', ''),
        help='Bearer token (INGEST_SECRET). Also read from INGEST_SECRET env var.',
    )
    parser.add_argument(
        '--data',
        default=None,
        help='Path to JSON file containing the payload. Reads from stdin if omitted.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print the request without actually sending it.',
    )
    return parser.parse_args()


def load_payload(data_path):
    if data_path:
        with open(data_path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    # Read from stdin
    raw = sys.stdin.read()
    return json.loads(raw)


def send(base_url, secret, payload, dry_run=False):
    url = base_url.rstrip('/') + ENDPOINT_PATH
    body = json.dumps(payload).encode('utf-8')

    if dry_run:
        print(f'[dry-run] POST {url}')
        print(f'[dry-run] Authorization: Bearer {secret[:4]}...{secret[-4:] if len(secret) > 8 else ""}')
        print(f'[dry-run] Payload ({len(body)} bytes):')
        print(json.dumps(payload, indent=2))
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
        print(f'ERROR: could not reach {url}: {exc.reason}', file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        data = {'raw': response_body}

    print(f'HTTP {status}')
    print(json.dumps(data, indent=2))

    if status != 200:
        sys.exit(1)

    warnings = data.get('warnings', [])
    if warnings:
        print(f'\n{len(warnings)} warning(s):')
        for w in warnings:
            print(f'  - {w}')


def main():
    args = parse_args()

    if not args.url:
        print('ERROR: --url is required (or set INGEST_URL env var)', file=sys.stderr)
        sys.exit(1)
    if not args.secret:
        print('ERROR: --secret is required (or set INGEST_SECRET env var)', file=sys.stderr)
        sys.exit(1)

    try:
        payload = load_payload(args.data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f'ERROR: could not parse JSON payload: {exc}', file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)

    send(args.url, args.secret, payload, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
