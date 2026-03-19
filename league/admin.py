from django.contrib import admin
from .models import (
    RealTeam, FantasyTeam, Player, RealGame,
    HittingGameLog, PitchingGameLog, PointSettings, Week, Matchup, Coach
)

admin.site.register(RealTeam)
admin.site.register(FantasyTeam)
admin.site.register(Player)


@admin.register(RealGame)
class RealGameAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'date', 'home_team', 'away_team', 'winner')
    list_filter = ('date',)


admin.site.register(HittingGameLog)
admin.site.register(PitchingGameLog)
admin.site.register(PointSettings)
admin.site.register(Week)
admin.site.register(Matchup)
admin.site.register(Coach)
