from django.core.management.base import BaseCommand
from league.models import FantasyTeam, PointSettings


class Command(BaseCommand):
    help = 'Creates commissioner team and default point settings'

    def handle(self, *args, **options):
        if not FantasyTeam.objects.filter(is_commissioner=True).exists():
            team = FantasyTeam(name='Commissioner', is_commissioner=True)
            team.set_password('admin')
            team.save()
            self.stdout.write(self.style.SUCCESS(
                'Created commissioner team (name: "Commissioner", password: "admin")'
            ))
        else:
            self.stdout.write('Commissioner team already exists.')

        PointSettings.load()
        self.stdout.write(self.style.SUCCESS('Point settings initialized.'))
