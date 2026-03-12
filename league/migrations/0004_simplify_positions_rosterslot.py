from django.db import migrations, models
import django.db.models.deletion


def remap_positions(apps, schema_editor):
    """Collapse old granular positions into C / IF / OF / P."""
    Player = apps.get_model('league', 'Player')
    infield = ['1B', '2B', '3B', 'SS', 'DH', 'IF']
    pitching = ['SP', 'RP']
    Player.objects.filter(position__in=infield).update(position='IF')
    Player.objects.filter(position__in=pitching).update(position='P')
    # C and OF are already correct


def reverse_remap(apps, schema_editor):
    pass  # not reversible without extra data


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0003_player_class_year_position'),
    ]

    operations = [
        migrations.RunPython(remap_positions, reverse_remap),
        migrations.CreateModel(
            name='RosterSlot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('slot_type', models.CharField(
                    max_length=3,
                    choices=[('C', 'Catcher'), ('IF', 'Infield'), ('OF', 'Outfield'),
                             ('DH', 'Designated Hitter'), ('P', 'Pitcher'), ('BN', 'Bench')],
                )),
                ('slot_number', models.PositiveSmallIntegerField()),
                ('fantasy_team', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='roster_slots',
                    to='league.fantasyteam',
                )),
                ('player', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='roster_slot',
                    to='league.player',
                )),
            ],
            options={
                'ordering': ['slot_type', 'slot_number'],
                'unique_together': {('fantasy_team', 'slot_type', 'slot_number')},
            },
        ),
    ]
