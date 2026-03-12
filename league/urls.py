from django.urls import path
from . import views

app_name = 'league'

urlpatterns = [
    # Auth
    path('', views.login_view, name='home'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),

    # Standings
    path('standings/', views.standings_view, name='standings'),

    # Schedule
    path('schedule/', views.schedule_view, name='schedule'),
    path('schedule/week/<int:week_id>/matchup/<int:matchup_id>/',
         views.weekly_matchup_view, name='weekly_matchup'),

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

    # Players / Free Agents (public)
    path('players/', views.players_list, name='players'),
    path('players/<int:player_id>/', views.player_detail, name='player_detail'),

    # Commissioner
    path('commissioner/', views.commissioner_panel, name='commissioner_panel'),
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
    path('commissioner/free-agents/', views.free_agent_board, name='free_agent_board'),
    path('commissioner/free-agents/<int:player_id>/assign/', views.assign_player, name='assign_player'),
    path('commissioner/players/<int:player_id>/drop/', views.drop_player, name='drop_player'),
]
