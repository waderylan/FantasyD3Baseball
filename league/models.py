from django.db import models
from django.contrib.auth.hashers import make_password, check_password


class RealTeam(models.Model):
    name = models.CharField(max_length=100, unique=True)
    abbreviation = models.CharField(max_length=10, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class FantasyTeam(models.Model):
    name = models.CharField(max_length=100, unique=True)
    password_hash = models.CharField(max_length=256)
    is_commissioner = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password_hash)


POSITION_CHOICES = [
    ('C',  'Catcher'),
    ('IF', 'Infield'),
    ('OF', 'Outfield'),
    ('P',  'Pitcher'),
]

CLASS_YEAR_CHOICES = [
    ('FY', 'First Year'),
    ('SO', 'Sophomore'),
    ('JR', 'Junior'),
    ('SR', 'Senior'),
    ('GR', 'Grad'),
    ('N/A', 'N/A'),
]

PITCHING_POSITIONS = {'P'}

# Roster slot configuration
SLOT_CHOICES = [
    ('C',  'Catcher'),
    ('IF', 'Infield'),
    ('OF', 'Outfield'),
    ('DH', 'Designated Hitter'),
    ('P',  'Pitcher'),
    ('BN', 'Bench'),
]

# How many of each slot type per team
SLOT_LIMITS = {'C': 1, 'IF': 4, 'OF': 3, 'DH': 1, 'P': 5, 'BN': 4}

# Which player positions are eligible for each slot type
SLOT_ELIGIBLE = {
    'C':  {'C'},
    'IF': {'IF'},
    'OF': {'OF'},
    'DH': {'C', 'IF', 'OF'},
    'P':  {'P'},
    'BN': {'C', 'IF', 'OF', 'P'},
}

# Display order for slots
SLOT_ORDER = {'C': 0, 'IF': 1, 'OF': 2, 'DH': 3, 'P': 4, 'BN': 5}


class Player(models.Model):
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    position = models.CharField(max_length=10, choices=POSITION_CHOICES)
    class_year = models.CharField(max_length=5, blank=True, default='',
                                  choices=CLASS_YEAR_CHOICES)
    real_team = models.ForeignKey(
        RealTeam, on_delete=models.CASCADE, related_name='players'
    )
    fantasy_team = models.ForeignKey(
        FantasyTeam, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='players'
    )

    class Meta:
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.position})"

    @property
    def is_pitcher(self):
        return self.position in PITCHING_POSITIONS


class RealGame(models.Model):
    date = models.DateField()
    home_team = models.ForeignKey(
        RealTeam, on_delete=models.CASCADE, related_name='home_games'
    )
    away_team = models.ForeignKey(
        RealTeam, on_delete=models.CASCADE, related_name='away_games'
    )

    class Meta:
        ordering = ['-date']
        unique_together = ['date', 'home_team', 'away_team']

    def __str__(self):
        return f"{self.away_team.abbreviation} @ {self.home_team.abbreviation} ({self.date})"


class HittingGameLog(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='hitting_logs'
    )
    game = models.ForeignKey(
        RealGame, on_delete=models.CASCADE, related_name='hitting_logs'
    )
    ab = models.PositiveIntegerField(default=0, verbose_name='AB')
    runs = models.PositiveIntegerField(default=0, verbose_name='R')
    hits = models.PositiveIntegerField(default=0, verbose_name='H')
    doubles = models.PositiveIntegerField(default=0, verbose_name='2B')
    triples = models.PositiveIntegerField(default=0, verbose_name='3B')
    hr = models.PositiveIntegerField(default=0, verbose_name='HR')
    rbi = models.PositiveIntegerField(default=0, verbose_name='RBI')
    bb = models.PositiveIntegerField(default=0, verbose_name='BB')
    so = models.PositiveIntegerField(default=0, verbose_name='SO')
    sb = models.PositiveIntegerField(default=0, verbose_name='SB')
    cs = models.PositiveIntegerField(default=0, verbose_name='CS')
    hbp = models.PositiveIntegerField(default=0, verbose_name='HBP')
    entered_by = models.ForeignKey(
        FantasyTeam, on_delete=models.SET_NULL, null=True, blank=True
    )
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['player', 'game']
        ordering = ['-game__date']

    def __str__(self):
        return f"{self.player} - {self.game}"

    @property
    def singles(self):
        return self.hits - self.doubles - self.triples - self.hr


class PitchingGameLog(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='pitching_logs'
    )
    game = models.ForeignKey(
        RealGame, on_delete=models.CASCADE, related_name='pitching_logs'
    )
    outs = models.PositiveIntegerField(default=0, verbose_name='Outs',
                                       help_text='Total outs recorded (IP stored as outs)')
    hits = models.PositiveIntegerField(default=0, verbose_name='H')
    runs = models.PositiveIntegerField(default=0, verbose_name='R')
    er = models.PositiveIntegerField(default=0, verbose_name='ER')
    bb = models.PositiveIntegerField(default=0, verbose_name='BB')
    so = models.PositiveIntegerField(default=0, verbose_name='SO')
    hr = models.PositiveIntegerField(default=0, verbose_name='HR')
    win = models.BooleanField(default=False, verbose_name='W')
    loss = models.BooleanField(default=False, verbose_name='L')
    save_game = models.BooleanField(default=False, verbose_name='SV')
    hold = models.BooleanField(default=False, verbose_name='HLD')
    entered_by = models.ForeignKey(
        FantasyTeam, on_delete=models.SET_NULL, null=True, blank=True
    )
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['player', 'game']
        ordering = ['-game__date']

    def __str__(self):
        return f"{self.player} - {self.game}"

    @property
    def ip_display(self):
        full = self.outs // 3
        remainder = self.outs % 3
        return f"{full}.{remainder}"


class PointSettings(models.Model):
    # Hitting points
    single = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    double = models.DecimalField(max_digits=5, decimal_places=2, default=2)
    triple = models.DecimalField(max_digits=5, decimal_places=2, default=3)
    hr = models.DecimalField(max_digits=5, decimal_places=2, default=4)
    rbi = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    run = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    bb = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    sb = models.DecimalField(max_digits=5, decimal_places=2, default=2)
    cs = models.DecimalField(max_digits=5, decimal_places=2, default=-1)
    hbp = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    so_hitting = models.DecimalField(max_digits=5, decimal_places=2, default=-0.5,
                                     verbose_name='SO (hitting)')

    # Pitching points
    ip_out = models.DecimalField(max_digits=5, decimal_places=2, default=1,
                                 verbose_name='Per out recorded',
                                 help_text='Points per out (3 per full IP)')
    so_pitching = models.DecimalField(max_digits=5, decimal_places=2, default=2,
                                      verbose_name='SO (pitching)')
    win = models.DecimalField(max_digits=5, decimal_places=2, default=5)
    loss = models.DecimalField(max_digits=5, decimal_places=2, default=-3)
    save_pts = models.DecimalField(max_digits=5, decimal_places=2, default=5,
                                   verbose_name='Save')
    hold_pts = models.DecimalField(max_digits=5, decimal_places=2, default=3,
                                   verbose_name='Hold')
    er = models.DecimalField(max_digits=5, decimal_places=2, default=-1,
                             verbose_name='ER')
    hit_against = models.DecimalField(max_digits=5, decimal_places=2, default=-0.5,
                                      verbose_name='H against')
    bb_pitching = models.DecimalField(max_digits=5, decimal_places=2, default=-0.5,
                                      verbose_name='BB (pitching)')
    hr_against = models.DecimalField(max_digits=5, decimal_places=2, default=-1,
                                     verbose_name='HR against')

    class Meta:
        verbose_name = 'Point Settings'
        verbose_name_plural = 'Point Settings'

    def __str__(self):
        return 'Point Settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Week(models.Model):
    week_number = models.PositiveIntegerField(unique=True)
    start_date = models.DateField(help_text='Monday')
    end_date = models.DateField(help_text='Sunday')

    class Meta:
        ordering = ['week_number']

    def __str__(self):
        return f"Week {self.week_number} ({self.start_date} - {self.end_date})"


class RosterSlot(models.Model):
    fantasy_team = models.ForeignKey(
        FantasyTeam, on_delete=models.CASCADE, related_name='roster_slots'
    )
    slot_type = models.CharField(max_length=3, choices=SLOT_CHOICES)
    slot_number = models.PositiveSmallIntegerField()
    player = models.OneToOneField(
        'Player', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='roster_slot'
    )

    class Meta:
        unique_together = [['fantasy_team', 'slot_type', 'slot_number']]
        ordering = ['slot_type', 'slot_number']

    def __str__(self):
        return f"{self.fantasy_team} – {self.slot_label}"

    @property
    def slot_label(self):
        if self.slot_type in ('C', 'DH'):
            return self.slot_type
        return f"{self.slot_type}{self.slot_number}"

    @classmethod
    def create_for_team(cls, team):
        """Ensure all 18 roster slots exist for a team."""
        for slot_type, count in SLOT_LIMITS.items():
            for n in range(1, count + 1):
                cls.objects.get_or_create(
                    fantasy_team=team,
                    slot_type=slot_type,
                    slot_number=n,
                )


class Matchup(models.Model):
    week = models.ForeignKey(Week, on_delete=models.CASCADE, related_name='matchups')
    team_1 = models.ForeignKey(
        FantasyTeam, on_delete=models.CASCADE, related_name='matchups_as_team1'
    )
    team_2 = models.ForeignKey(
        FantasyTeam, on_delete=models.CASCADE, related_name='matchups_as_team2',
        null=True, blank=True, help_text='Null = bye week'
    )

    class Meta:
        unique_together = [['week', 'team_1']]

    def __str__(self):
        if self.team_2:
            return f"Week {self.week.week_number}: {self.team_1} vs {self.team_2}"
        return f"Week {self.week.week_number}: {self.team_1} (BYE)"
