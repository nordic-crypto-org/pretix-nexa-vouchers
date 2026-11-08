# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from django.urls import re_path
from pretix.multidomain import event_url

from pretix_nexa_vouchers import views

urlpatterns = [
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/nexa-vouchers/$",
        views.IndexView.as_view(),
        name="index",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/nexa-vouchers/settings/$",
        views.SettingsView.as_view(),
        name="settings",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/nexa-vouchers/lists/new/$",
        views.ListCreateView.as_view(),
        name="list.create",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/nexa-vouchers/lists/(?P<slug>[-a-zA-Z0-9_]+)/$",
        views.ListDetailView.as_view(),
        name="list.detail",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/nexa-vouchers/lists/(?P<slug>[-a-zA-Z0-9_]+)/assign/$",
        views.AssignNextView.as_view(),
        name="list.assign",
    ),
]

_UUID = r"(?P<public_id>[0-9a-fA-F-]{32,36})"
_SLUG = r"(?P<slug>[-a-zA-Z0-9_]+)"

event_patterns = [
    event_url(r"^nexa/lists/$", views.public_lists, name="public.lists", require_live=False),
    event_url(rf"^nexa/lists/{_SLUG}/$", views.public_list, name="public.list", require_live=False),
    event_url(rf"^nexa/lists/{_SLUG}/join$", views.queue_join, name="queue.join", require_live=False),
    event_url(rf"^nexa/lists/{_SLUG}/assign-next$", views.api_assign_next, name="api.assign", require_live=False),
    event_url(rf"^nexa/lists/{_SLUG}/confirm-transfer$", views.api_confirm_transfer, name="api.confirm", require_live=False),
    event_url(
        rf"^nexa/lists/{_SLUG}/entries/{_UUID}/$",
        views.queue_entry,
        name="queue.entry",
        require_live=False,
    ),
    event_url(
        rf"^nexa/lists/{_SLUG}/entries/{_UUID}/address$",
        views.queue_entry_address,
        name="queue.address",
        require_live=False,
    ),
    event_url(rf"^nexa/claim/{_UUID}/$", views.claim, name="claim", require_live=False),
    event_url(rf"^nexa/token/{_UUID}\.json$", views.token_document, name="token.document", require_live=False),
    event_url(r"^nexa/qr\.svg$", views.qr_svg, name="qr", require_live=False),
]
