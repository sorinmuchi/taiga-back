# Copyright (C) 2014 Andrey Antukh <niwi@niwi.be>
# Copyright (C) 2014 Jesús Espino <jespinog@gmail.com>
# Copyright (C) 2014 David Barragán <bameda@dbarragan.com>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Examples:
# python manage.py rebuild_timeline_for_user_creation --settings=settings.local_timeline

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand
from django.db.models import Model
from django.db import reset_queries

from taiga.timeline.service import (_get_impl_key_from_model,
    _timeline_impl_map, extract_user_info)
from taiga.timeline.models import Timeline
from taiga.timeline.signals import _push_to_timelines
from taiga.users.models import User

from unittest.mock import patch

import gc

class BulkCreator(object):
    def __init__(self):
        self.timeline_objects = []
        self.created = None

    def createElement(self, element):
        self.timeline_objects.append(element)
        if len(self.timeline_objects) > 1000:
            self.flush()

    def flush(self):
        Timeline.objects.bulk_create(self.timeline_objects, batch_size=1000)
        del self.timeline_objects
        self.timeline_objects = []
        gc.collect()

bulk_creator = BulkCreator()


def custom_add_to_object_timeline(obj:object, instance:object, event_type:str, namespace:str="default", extra_data:dict={}):
    assert isinstance(obj, Model), "obj must be a instance of Model"
    assert isinstance(instance, Model), "instance must be a instance of Model"
    event_type_key = _get_impl_key_from_model(instance.__class__, event_type)
    impl = _timeline_impl_map.get(event_type_key, None)

    bulk_creator.createElement(Timeline(
        content_object=obj,
        namespace=namespace,
        event_type=event_type_key,
        project=None,
        data=impl(instance, extra_data=extra_data),
        data_content_type = ContentType.objects.get_for_model(instance.__class__),
        created = bulk_creator.created,
    ))


def generate_timeline():
    with patch('taiga.timeline.service._add_to_object_timeline', new=custom_add_to_object_timeline):
        # Users api wasn't a HistoryResourceMixin so we can't interate on the HistoryEntries in this case
        users = User.objects.order_by("date_joined")
        for user in users.iterator():
            bulk_creator.created = user.date_joined
            print("User:", user.date_joined)
            extra_data = {
                "values_diff": {},
                "user": extract_user_info(user),
            }
            _push_to_timelines(None, user, user, "create", extra_data=extra_data)
            del extra_data

    bulk_creator.flush()

class Command(BaseCommand):
    help = 'Regenerate project timeline'

    def handle(self, *args, **options):
        debug_enabled = settings.DEBUG
        if debug_enabled:
            print("Please, execute this script only with DEBUG mode disabled (DEBUG=False)")
            return

        generate_timeline()
