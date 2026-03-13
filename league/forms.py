import re
from django import forms
from .models import (
    FantasyTeam, Player, RealTeam, RealGame, PointSettings,
    HittingGameLog, PitchingGameLog, POSITION_CHOICES,
)


class LoginForm(forms.Form):
    team_name = forms.CharField(max_length=100)
    password = forms.CharField(widget=forms.PasswordInput)


class FantasyTeamForm(forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput,
                               help_text='Leave blank to keep current password')

    class Meta:
        model = FantasyTeam
        fields = ['name', 'display_name']

    def save(self, commit=True):
        team = super().save(commit=False)
        pw = self.cleaned_data.get('password')
        if pw:
            team.set_password(pw)
        if commit:
            team.save()
        return team


class PlayerForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = ['first_name', 'last_name', 'position', 'real_team', 'fantasy_team']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['fantasy_team'].required = False
        self.fields['fantasy_team'].queryset = FantasyTeam.objects.filter(
            is_commissioner=False
        )


class RealTeamForm(forms.ModelForm):
    class Meta:
        model = RealTeam
        fields = ['name', 'abbreviation']


class RealGameForm(forms.ModelForm):
    class Meta:
        model = RealGame
        fields = ['date', 'home_team', 'away_team', 'source_url']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'source_url': forms.URLInput(attrs={'placeholder': 'https://...'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['source_url'].required = True
        self.fields['source_url'].label = 'Box Score URL'

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('home_team') == cleaned.get('away_team'):
            raise forms.ValidationError('Home and away teams must be different.')
        return cleaned


def parse_ip_to_outs(ip_str):
    """Convert IP notation (e.g. '6.2') to total outs (e.g. 20)."""
    ip_str = str(ip_str).strip()
    match = re.match(r'^(\d+)\.?(\d)?$', ip_str)
    if not match:
        raise forms.ValidationError('Invalid IP format. Use format like 6.2')
    full = int(match.group(1))
    partial = int(match.group(2)) if match.group(2) else 0
    if partial > 2:
        raise forms.ValidationError('Partial innings must be 0, 1, or 2.')
    return full * 3 + partial


class HittingGameLogForm(forms.ModelForm):
    class Meta:
        model = HittingGameLog
        fields = ['ab', 'runs', 'hits', 'doubles', 'triples', 'hr',
                  'rbi', 'bb', 'so', 'sb', 'cs', 'hbp']

    def clean(self):
        cleaned = super().clean()
        hits = cleaned.get('hits', 0)
        doubles = cleaned.get('doubles', 0)
        triples = cleaned.get('triples', 0)
        hr = cleaned.get('hr', 0)
        ab = cleaned.get('ab', 0)

        if doubles + triples + hr > hits:
            raise forms.ValidationError(
                'Doubles + Triples + HR cannot exceed total hits.'
            )
        if hits > ab:
            raise forms.ValidationError('Hits cannot exceed at-bats.')
        return cleaned


class PitchingGameLogForm(forms.Form):
    ip = forms.CharField(max_length=10, label='IP',
                         help_text='Innings pitched (e.g. 6.2)')
    hits = forms.IntegerField(min_value=0, initial=0, label='H')
    runs = forms.IntegerField(min_value=0, initial=0, label='R')
    er = forms.IntegerField(min_value=0, initial=0, label='ER')
    bb = forms.IntegerField(min_value=0, initial=0, label='BB')
    so = forms.IntegerField(min_value=0, initial=0, label='SO')
    hr = forms.IntegerField(min_value=0, initial=0, label='HR')
    win = forms.BooleanField(required=False, label='W')
    loss = forms.BooleanField(required=False, label='L')
    save = forms.BooleanField(required=False, label='SV')

    def clean_ip(self):
        return parse_ip_to_outs(self.cleaned_data['ip'])

    def clean(self):
        cleaned = super().clean()
        er = cleaned.get('er', 0)
        runs = cleaned.get('runs', 0)
        if er > runs:
            raise forms.ValidationError('Earned runs cannot exceed total runs.')
        win = cleaned.get('win', False)
        loss = cleaned.get('loss', False)
        if win and loss:
            raise forms.ValidationError('A pitcher cannot have both a win and a loss.')
        return cleaned


class PointSettingsForm(forms.ModelForm):
    class Meta:
        model = PointSettings
        exclude = []


class GenerateScheduleForm(forms.Form):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text='Must be a Monday'
    )
    num_weeks = forms.IntegerField(min_value=1, max_value=52, label='Number of weeks')

    def clean_start_date(self):
        d = self.cleaned_data['start_date']
        if d.weekday() != 0:
            raise forms.ValidationError('Start date must be a Monday.')
        return d
