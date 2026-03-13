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
    display_name = models.CharField(max_length=100, blank=True)
    password_hash = models.CharField(max_length=256)
    is_commissioner = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.display_name or self.name

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
    fantasy_team_since = models.DateField(null=True, blank=True,
        help_text='Date this player was added to their current fantasy team'
    )
    cached_season_points = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    cached_weekly_points = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    cached_games_played  = models.PositiveIntegerField(default=0)
    cached_ppg           = models.DecimalField(max_digits=8, decimal_places=2, default=0)

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
    game_number = models.PositiveSmallIntegerField(default=1,
        help_text='1 for single game or first game of doubleheader, 2 for second game')
    source_url = models.URLField(max_length=500, null=True, blank=True, unique=True,
        help_text='Box score URL this game was scraped from')

    class Meta:
        ordering = ['-date', 'game_number']
        unique_together = ['date', 'home_team', 'away_team', 'game_number']

    def __str__(self):
        suffix = f' (G{self.game_number})' if self.game_number > 1 else ''
        return f"{self.away_team.abbreviation} @ {self.home_team.abbreviation} ({self.date}){suffix}"


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
    er = models.DecimalField(max_digits=5, decimal_places=2, default=-1,
                             verbose_name='ER')
    hit_against = models.DecimalField(max_digits=5, decimal_places=2, default=-0.5,
                                      verbose_name='H against')
    bb_pitching = models.DecimalField(max_digits=5, decimal_places=2, default=-0.5,
                                      verbose_name='BB (pitching)')

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


class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ('add', 'Add'),
        ('drop', 'Drop'),
    ]
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    fantasy_team = models.ForeignKey(
        FantasyTeam, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions'
    )
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions'
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.transaction_type.upper()} {self.player} — {self.fantasy_team} ({self.timestamp:%Y-%m-%d})"


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


class PendingRequest(models.Model):
    REQUEST_TYPES = [
        ('game_add', 'Add Game'),
        ('stat_modify', 'Modify Stats'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('denied', 'Denied'),
        ('cancelled', 'Cancelled'),
    ]
    request_type = models.CharField(max_length=20, choices=REQUEST_TYPES)
    submitted_by = models.ForeignKey(
        'FantasyTeam', on_delete=models.CASCADE, related_name='submitted_requests'
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(
        'FantasyTeam', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_requests'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    commissioner_note = models.TextField(blank=True)
    # game_add fields
    source_url = models.URLField(max_length=500, blank=True)
    # stat_modify fields
    player = models.ForeignKey('Player', on_delete=models.CASCADE, null=True, blank=True)
    game = models.ForeignKey('RealGame', on_delete=models.CASCADE, null=True, blank=True)
    stat_type = models.CharField(max_length=10, blank=True)  # 'hitting' or 'pitching'
    proposed_data = models.JSONField(null=True, blank=True)
    user_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.get_request_type_display()} by {self.submitted_by} ({self.status})"


class ActivityEntry(models.Model):
    ENTRY_TYPES = [
        ('add', 'Player Added'),
        ('drop', 'Player Dropped'),
        ('dispute_submitted', 'Dispute Submitted'),
        ('dispute_approved', 'Dispute Approved'),
        ('dispute_denied', 'Dispute Denied'),
        ('dispute_cancelled', 'Dispute Cancelled'),
    ]
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)
    fantasy_team = models.ForeignKey(
        'FantasyTeam', on_delete=models.SET_NULL, null=True, blank=True
    )
    player = models.ForeignKey(
        'Player', on_delete=models.SET_NULL, null=True, blank=True
    )
    description = models.TextField()

    class Meta:
        ordering = ['-created_at']
