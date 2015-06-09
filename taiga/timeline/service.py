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

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model
from django.db.models import Q
from django.db.models.query import QuerySet

from functools import partial, wraps

from taiga.base.utils.db import get_typename_for_model_class
from taiga.celery import app
from taiga.users.services import get_photo_or_gravatar_url, get_big_photo_or_gravatar_url

_timeline_impl_map = {}


def _get_impl_key_from_model(model:Model, event_type:str):
    if issubclass(model, Model):
        typename = get_typename_for_model_class(model)
        return _get_impl_key_from_typename(typename, event_type)
    raise Exception("Not valid model parameter")


def _get_impl_key_from_typename(typename:str, event_type:str):
    if isinstance(typename, str):
        return "{0}.{1}".format(typename, event_type)
    raise Exception("Not valid typename parameter")


def build_user_namespace(user:object):
    return "{0}:{1}".format("user", user.id)


def build_project_namespace(project:object):
    return "{0}:{1}".format("project", project.id)


def _add_to_object_timeline(obj:object, instance:object, event_type:str, namespace:str="default", extra_data:dict={}):
    assert isinstance(obj, Model), "obj must be a instance of Model"
    assert isinstance(instance, Model), "instance must be a instance of Model"
    from .models import Timeline
    event_type_key = _get_impl_key_from_model(instance.__class__, event_type)
    impl = _timeline_impl_map.get(event_type_key, None)

    project = None
    if hasattr(instance, "project"):
        project = instance.project

    Timeline.objects.create(
        content_object=obj,
        namespace=namespace,
        event_type=event_type_key,
        project=project,
        data=impl(instance, extra_data=extra_data),
        data_content_type = ContentType.objects.get_for_model(instance.__class__),
    )


def _add_to_objects_timeline(objects, instance:object, event_type:str, namespace:str="default", extra_data:dict={}):
    for obj in objects:
        _add_to_object_timeline(obj, instance, event_type, namespace, extra_data)


@app.task
def push_to_timeline(objects, instance:object, event_type:str, namespace:str="default", extra_data:dict={}):
    if isinstance(objects, Model):
        _add_to_object_timeline(objects, instance, event_type, namespace, extra_data)
    elif isinstance(objects, QuerySet) or isinstance(objects, list):
        _add_to_objects_timeline(objects, instance, event_type, namespace, extra_data)
    else:
        raise Exception("Invalid objects parameter")


def get_timeline(obj, namespace=None):
    assert isinstance(obj, Model), "obj must be a instance of Model"
    from .models import Timeline

    ct = ContentType.objects.get_for_model(obj.__class__)
    timeline = Timeline.objects.filter(content_type=ct, object_id=obj.pk)
    if namespace is not None:
        timeline = timeline.filter(namespace=namespace)

    timeline = timeline.order_by("-created")
    return timeline


def filter_timeline_for_user(timeline, user):
    # Filtering entities from public projects or entities without project
    tl_filter = Q(project__is_private=False) | Q(project=None)

    # Filtering private project with some public parts
    content_types = {
        "view_project": ContentType.objects.get(app_label="projects", model="project"),
        "view_milestones": ContentType.objects.get(app_label="milestones", model="milestone"),
        "view_us": ContentType.objects.get(app_label="userstories", model="userstory"),
        "view_tasks": ContentType.objects.get(app_label="tasks", model="task"),
        "view_issues": ContentType.objects.get(app_label="issues", model="issue"),
        "view_wiki_pages": ContentType.objects.get(app_label="wiki", model="wikipage"),
        "view_wiki_links": ContentType.objects.get(app_label="wiki", model="wikilink"),
    }

    for content_type_key, content_type in content_types.items():
        tl_filter |= Q(project__is_private=True,
                                            project__anon_permissions__contains=[content_type_key],
                                            data_content_type=content_type)

    # Filtering private projects where user is member
    if not user.is_anonymous():
        membership_model = apps.get_model('projects', 'Membership')
        memberships_qs = membership_model.objects.filter(user=user)
        for membership in memberships_qs:
            for content_type_key, content_type in content_types.items():
                if content_type_key in membership.role.permissions or membership.is_owner:
                    tl_filter |= Q(project=membership.project, data_content_type=content_type)

    timeline = timeline.filter(tl_filter)
    return timeline


def get_profile_timeline(user, accessing_user=None):
    timeline = get_timeline(user)
    if accessing_user is not None:
        timeline = filter_timeline_for_user(timeline, accessing_user)
    return timeline


def get_user_timeline(user, accessing_user=None):
    namespace = build_user_namespace(user)
    timeline = get_timeline(user, namespace)
    if accessing_user is not None:
        timeline = filter_timeline_for_user(timeline, accessing_user)
    return timeline


def get_project_timeline(project, accessing_user=None):
    namespace = build_project_namespace(project)
    timeline = get_timeline(project, namespace)
    if accessing_user is not None:
        timeline = filter_timeline_for_user(timeline, accessing_user)
    return timeline


def register_timeline_implementation(typename:str, event_type:str, fn=None):
    assert isinstance(typename, str), "typename must be a string"
    assert isinstance(event_type, str), "event_type must be a string"

    if fn is None:
        return partial(register_timeline_implementation, typename, event_type)

    @wraps(fn)
    def _wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    key = _get_impl_key_from_typename(typename, event_type)

    _timeline_impl_map[key] = _wrapper
    return _wrapper



def extract_project_info(instance):
    return {
        "id": instance.pk,
        "slug": instance.slug,
        "name": instance.name,
        "description": instance.description,
    }


def extract_user_info(instance):
    return {
        "id": instance.pk
    }


def extract_milestone_info(instance):
    return {
        "id": instance.pk,
        "slug": instance.slug,
        "name": instance.name,
    }


def extract_userstory_info(instance):
    return {
        "id": instance.pk,
        "ref": instance.ref,
        "subject": instance.subject,
    }


def extract_issue_info(instance):
    return {
        "id": instance.pk,
        "ref": instance.ref,
        "subject": instance.subject,
    }


def extract_task_info(instance):
    return {
        "id": instance.pk,
        "ref": instance.ref,
        "subject": instance.subject,
    }


def extract_wiki_page_info(instance):
    return {
        "id": instance.pk,
        "slug": instance.slug,
    }


def extract_role_info(instance):
    return {
        "id": instance.pk,
        "name": instance.name,
    }
