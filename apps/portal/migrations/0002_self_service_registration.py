# HAND-WRITTEN — no local Django was available when this was authored.
#
# What it changes (apps/portal/models.py):
#   · OrgMembership.status      choices += pending_verification
#   · PortalLoginEvent.outcome  choices += registered, verified, verify_failed
#   · EmailVerificationToken    new model; FK names "portal.orgmembership",
#                               never settings.AUTH_USER_MODEL
#
# Verified by CI, not by trust: the dual `makemigrations --check` steps fail if
# this file and the models disagree in any way. If they do, run the
# `generate-migrations` workflow_dispatch job, and replace this file with the
# generated one.

import apps.portal.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='orgmembership',
            name='status',
            field=models.CharField(choices=[('active', 'Active'), ('invited', 'Invited'), ('suspended', 'Suspended'), ('pending_verification', 'Pending email verification')], default='invited', max_length=20),
        ),
        migrations.AlterField(
            model_name='portalloginevent',
            name='outcome',
            field=models.CharField(choices=[('success', 'Success'), ('bad_credentials', 'Bad credentials'), ('inactive', 'Account inactive'), ('no_membership', 'No active membership'), ('rate_limited', 'Rate limited'), ('registered', 'Registered (unverified)'), ('verified', 'Email verified'), ('verify_failed', 'Verification failed')], max_length=32),
        ),
        migrations.CreateModel(
            name='EmailVerificationToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token_hash', models.CharField(max_length=128, unique=True)),
                ('expires_at', models.DateTimeField(default=apps.portal.models._verification_expiry)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('invalidated_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('membership', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='verification_tokens', to='portal.orgmembership')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
    ]
