# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-06-18 03:40
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('storage', '0009_disk_disk_passwd'),
    ]

    operations = [
        migrations.DeleteModel(
            name='QuotaExcess',
        ),
    ]
