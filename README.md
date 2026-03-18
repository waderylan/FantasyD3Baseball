# Fantasy D3 Baseball

**Live site: [rylanwwade.pythonanywhere.com](https://rylanwwade.pythonanywhere.com/)**

This is a full-stack fantasy baseball web app built specifically for a league that drafts players from D3 college baseball programs. It runs on Django with a SQLite database and uses Bootstrap for the frontend. Stats are pulled automatically using a custom scraper built on Playwright and BeautifulSoup. The commissioner can also enter or correct stats manually through the web interface.

## What it does

The app handles everything you would expect from a fantasy league platform. Teams are organized into weekly head-to-head matchups, and points are scored based on individual player performance in real games. The scoring system is fully configurable by the commissioner — every stat category for both hitting and pitching has its own point value that can be adjusted at any time.

Each fantasy team has a roster of 18 slots: one catcher, four infielders, three outfielders, one DH, five pitchers, and four bench spots. Bench players are on the roster but do not score points during the week. Members can set their lineup manually, moving players between active and bench slots according to position eligibility rules.

## Authentication

The app uses a simple custom authentication system, not Django's built-in auth. Each team has a login name and password stored with Django's password hashing. There are no user accounts in the traditional sense — each account represents a fantasy team. Teams can set a separate display name that shows up throughout the site while their login name stays fixed.

## Member features

After logging in, members land on a dashboard with their current week matchup score and roster summary. From there they can:

- View their full roster with season stats and current week points
- Set their lineup by moving players into and out of active slots
- Browse all players in the league, filter by position, real team, or fantasy team, and add free agents or drop players from their roster
- View the current week matchup in detail, with each team's active players broken down by position alongside their stats and points for the week
- See the full season schedule with links to every completed matchup
- Check league standings showing wins, losses, ties, points for, and points against
- View a public transaction log of all adds and drops
- Submit stat disputes if they believe a game log was entered incorrectly, and track the status of those disputes

## Commissioner features

The commissioner account has a separate panel with tools for running the league:

- Create and manage fantasy teams (login names, display names, passwords)
- Create and manage real teams (the actual D3 programs in the league)
- Create and manage players, including their position, class year, and real team assignment
- Enter games by creating a game record and then entering hitting and pitching stats for any rostered player who appeared
- Edit existing game logs if a mistake was made during entry
- Generate the weekly matchup schedule
- Review and adjudicate stat disputes submitted by members — the commissioner can edit the proposed stats before approving, or deny the dispute with a note
- Configure the point values for every scoring category
- Manage rosters directly, including assigning free agents to teams or moving players between teams

## Stat scraper

One of the more significant pieces of this project is the automated stat scraper, implemented as a Django management command. It uses Playwright to load the Liberty League athletics calendar, which is a JavaScript-rendered page, and then follows every box score link it finds within a given date window. From each box score page it parses the batting and pitching HTML tables and maps the players and teams back to the database records. If a game only has a PDF box score rather than an HTML one, it falls back to pdfplumber to extract the data from the PDF. At the end of each run it writes hitting and pitching game logs directly into the database and refreshes all cached player point totals.

The command supports a few useful flags: you can pass a date range, point it at a single box score URL, run it in dry-run mode to preview what it would import without writing anything, or use --force to overwrite logs that already exist. By default it looks back three days, so running it daily keeps the league current without any manual stat entry.

This means the commissioner does not have to enter stats by hand after every game. The scraper does it automatically, and the manual entry interface exists as a fallback for corrections or edge cases the scraper misses.

## Stat storage and scoring

Game logs are stored per-player per-game. Hitting logs track at-bats, runs, hits, doubles, triples, home runs, RBI, walks, strikeouts, stolen bases, caught stealing, and HBP. Pitching logs track outs recorded (stored as total outs rather than innings to avoid fractional math), hits, runs, earned runs, walks, strikeouts, home runs allowed, and win/loss/save decisions.

Points are calculated live from the raw game logs every time they are needed. There is also a cached points system on the Player model (season points, weekly points, games played, PPG) that gets refreshed whenever stats are entered or edited, which is used for sorting and display on the players list.

## Dispute system

If a member believes their player's stats were entered incorrectly, they can file a dispute. They search for the player, pick the specific game, fill out what they think the correct stats should be along with an optional message, and submit. The commissioner sees pending disputes on their panel and can pull up a side-by-side view of the current stats versus the proposed stats, edit the proposed stats if needed, approve and apply them, or deny the request. All dispute activity is recorded in the activity log.

## Sending stats to the hosted server

The scraper writes stats directly to the local SQLite database. Because the hosted server does not have Playwright installed, the scraper cannot run there. Instead, you run the scraper locally and then use the export_stats command to send the results to the server over HTTP.

The workflow is two commands: scrape first, then export. They can be chained:

```
uv run python manage.py scrape_stats --start-date 03/10/2026 --end-date 03/16/2026 && \
uv run python manage.py export_stats --start-date 03/10/2026 --end-date 03/16/2026 \
  --send --url https://yourdomain.com --secret <token>
```

The export_stats command queries the local database for all games in the given date range and builds a JSON payload matching the server's ingest API format. Without --send it prints the JSON to stdout, which is useful for inspection:

```
uv run python manage.py export_stats --start-date 03/10/2026 --end-date 03/16/2026
```

With --send it POSTs the payload to the server's /api/ingest/ endpoint. The --url and --secret arguments can be omitted if you set the INGEST_URL and INGEST_SECRET environment variables instead, which saves typing when running the command repeatedly:

```
export INGEST_URL=https://yourdomain.com
export INGEST_SECRET=<token>
uv run python manage.py export_stats --start-date 03/10/2026 --end-date 03/16/2026 --send
```

To preview the full request before sending, add --dry-run alongside --send. This prints the target URL, the masked auth token, and the formatted payload without making any network request:

```
uv run python manage.py export_stats --start-date 03/10/2026 --end-date 03/16/2026 \
  --send --url https://yourdomain.com --secret <token> --dry-run
```

The ingest endpoint is idempotent. Sending the same data twice will not create duplicate game logs — games are matched by source URL, and existing logs are overwritten rather than duplicated. The server returns a list of warnings in the response for any players or teams it could not match, which the command prints after a successful import.

## Tech stack

- Django 4.x
- SQLite
- Bootstrap 5
- Standard Django templating (no JavaScript framework)
- Playwright and BeautifulSoup for the stat scraper
- pdfplumber for PDF box score parsing
- PythonAnywhere (hosting)
