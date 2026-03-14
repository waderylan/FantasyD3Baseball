from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0012_remove_hold_and_hr_against'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pendingrequest',
            name='request_type',
            field=models.CharField(
                choices=[
                    ('game_add', 'Add Game'),
                    ('stat_modify', 'Modify Stats'),
                    ('missing_game', 'Missing Game'),
                ],
                max_length=20,
            ),
        ),
    ]
