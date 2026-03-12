from django.contrib import admin
from .models import (
    RealTeam, FantasyTeam, Player, RealGame,
    HittingGameLog, PitchingGameLog, PointSettings, Week, Matchup
)

admin.site.register(RealTeam)
admin.site.register(FantasyTeam)
admin.site.register(Player)
admin.site.register(RealGame)
admin.site.register(HittingGameLog)
admin.site.register(PitchingGameLog)
admin.site.register(PointSettings)
admin.site.register(Week)
admin.site.register(Matchup)
