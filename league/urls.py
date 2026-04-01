from django.urls import path
from . import api_views, views

app_name = 'league'

urlpatterns = [
    # Ingest API (no session auth — uses Bearer token)
    path('api/ingest/', api_views.ingest, name='ingest'),
    path('api/ingest/schedule/', api_views.ingest_schedule, name='ingest_schedule'),

    # Auth
    path('', views.login_view, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Home dashboard
    path('home/', views.home_view, name='home_dashboard'),

    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),
    path('settings/', views.team_settings, name='team_settings'),

    # Standings
    path('standings/', views.standings_view, name='standings'),

    # Schedule
    path('schedule/', views.schedule_view, name='schedule'),
    path('schedule/week/<int:week_id>/matchup/<int:matchup_id>/',
         views.weekly_matchup_view, name='weekly_matchup'),
    path('ll-schedule/', views.ll_schedule_view, name='ll_schedule'),

    # Matchup page
    path('matchup/', views.matchup_view, name='matchup'),

    # Roster
    path('roster/<int:team_id>/', views.roster_view, name='roster'),
    path('roster/<int:team_id>/lineup/', views.set_lineup, name='set_lineup'),

    # Games & Stat Entry
    path('games/', views.game_list, name='game_list'),
    path('games/create/', views.game_create, name='game_create'),
    path('games/<int:game_id>/stats/', views.stat_entry_select, name='stat_entry_select'),
    path('games/<int:game_id>/stats/hitting/<int:player_id>/',
         views.hitting_entry, name='hitting_entry'),
    path('games/<int:game_id>/stats/pitching/<int:player_id>/',
         views.pitching_entry, name='pitching_entry'),

    # Game Logs
    path('player/<int:player_id>/logs/', views.game_log_list, name='game_log_list'),

    # Transaction Log
    path('transactions/', views.transaction_log, name='transaction_log'),

    # Disputes
    path('disputes/', views.dispute_list, name='dispute_list'),
    path('disputes/new/', views.dispute_select_player, name='dispute_select_player'),
    path('disputes/new/<int:player_id>/', views.dispute_select_game, name='dispute_select_game'),
    path('disputes/<int:player_id>/<int:game_id>/submit/', views.submit_dispute, name='submit_dispute'),
    path('disputes/<int:player_id>/missing/', views.submit_missing_game_dispute, name='submit_missing_game_dispute'),
    path('disputes/<int:dispute_id>/cancel/', views.cancel_dispute, name='cancel_dispute'),
    path('disputes/coach/<int:coach_id>/', views.coach_dispute_select_game, name='coach_dispute_select_game'),
    path('disputes/coach/<int:coach_id>/<int:game_id>/submit/', views.submit_coach_win_dispute, name='submit_coach_win_dispute'),
    path('commissioner/disputes/', views.commissioner_disputes, name='commissioner_disputes'),
    path('commissioner/disputes/<int:dispute_id>/review/', views.review_dispute, name='review_dispute'),
    path('commissioner/disputes/<int:dispute_id>/review-missing/', views.review_missing_game, name='review_missing_game'),
    path('commissioner/disputes/<int:dispute_id>/review-coach-win/', views.review_coach_win_dispute, name='review_coach_win_dispute'),

    # Coaches
    path('coaches/<int:coach_id>/', views.coach_detail, name='coach_detail'),
    path('coaches/<int:coach_id>/add/', views.member_add_coach, name='member_add_coach'),
    path('coaches/<int:coach_id>/drop/', views.member_drop_coach, name='member_drop_coach'),

    # Players / Free Agents (public)
    path('players/', views.players_list, name='players'),
    path('players/<int:player_id>/', views.player_detail, name='player_detail'),
    path('players/<int:player_id>/add/', views.member_add_player, name='member_add_player'),
    path('players/<int:player_id>/drop/', views.member_drop_player, name='member_drop_player'),

    # Trades
    path('trades/', views.trade_select_team, name='trade_select_team'),
    path('trades/create/<int:team_id>/', views.trade_create, name='trade_create'),
    path('trades/<int:trade_id>/', views.trade_detail, name='trade_detail'),
    path('trades/<int:trade_id>/cancel/', views.trade_cancel, name='trade_cancel'),
    path('trades/<int:trade_id>/respond/', views.trade_respond, name='trade_respond'),

    # Commissioner
    path('commissioner/', views.commissioner_panel, name='commissioner_panel'),
    path('commissioner/scraper/', views.run_scraper, name='run_scraper'),
    path('commissioner/recalculate-coaches/', views.recalculate_coaches, name='recalculate_coaches'),
    path('commissioner/teams/', views.manage_teams, name='manage_teams'),
    path('commissioner/teams/create/', views.team_create, name='team_create'),
    path('commissioner/teams/<int:team_id>/edit/', views.team_edit, name='team_edit'),
    path('commissioner/teams/<int:team_id>/delete/', views.team_delete, name='team_delete'),
    path('commissioner/teams/<int:team_id>/roster/', views.commissioner_team_roster, name='commissioner_team_roster'),
    path('commissioner/players/', views.manage_players, name='manage_players'),
    path('commissioner/players/create/', views.player_create, name='player_create'),
    path('commissioner/players/<int:player_id>/edit/', views.player_edit, name='player_edit'),
    path('commissioner/players/<int:player_id>/delete/', views.player_delete, name='player_delete'),
    path('commissioner/real-teams/', views.manage_real_teams, name='manage_real_teams'),
    path('commissioner/real-teams/create/', views.real_team_create, name='real_team_create'),
    path('commissioner/real-teams/<int:team_id>/edit/', views.real_team_edit, name='real_team_edit'),
    path('commissioner/point-settings/', views.point_settings_view, name='point_settings'),
    path('commissioner/schedule/', views.generate_schedule_view, name='generate_schedule'),
    path('commissioner/game-log/<str:log_type>/<int:log_id>/edit/',
         views.edit_game_log, name='edit_game_log'),

    # Free Agent Board
    path('commissioner/weeks/', views.commissioner_week_list, name='commissioner_week_list'),
    path('commissioner/weeks/<int:week_id>/days/', views.commissioner_week_days, name='commissioner_week_days'),
    path('commissioner/lock-settings/', views.lock_settings, name='lock_settings'),
    path('commissioner/reset/', views.reset_league, name='reset_league'),
    path('commissioner/free-agents/', views.free_agent_board, name='free_agent_board'),
    path('commissioner/free-agents/<int:player_id>/assign/', views.assign_player, name='assign_player'),
    path('commissioner/free-agents/coach/<int:coach_id>/assign/', views.assign_coach, name='assign_coach'),
    path('commissioner/players/<int:player_id>/drop/', views.drop_player, name='drop_player'),
    path('commissioner/players/<int:player_id>/reassign-slot/', views.commissioner_reassign_slot, name='commissioner_reassign_slot'),
]
