from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0016_transaction_notes'),
    ]

    operations = [
        migrations.CreateModel(
            name='WeeklyLineupSlot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slot_type', models.CharField(choices=[('C', 'Catcher'), ('IF', 'Infield'), ('OF', 'Outfield'), ('DH', 'Designated Hitter'), ('P', 'Pitcher'), ('BN', 'Bench')], max_length=4)),
                ('slot_number', models.PositiveSmallIntegerField()),
                ('fantasy_team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='weekly_slots', to='league.fantasyteam')),
                ('player', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='league.player')),
                ('week', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lineup_slots', to='league.week')),
            ],
            options={
                'unique_together': {('fantasy_team', 'week', 'slot_type', 'slot_number')},
            },
        ),
    ]
