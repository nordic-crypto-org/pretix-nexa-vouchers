# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils.crypto import get_random_string
from django.utils.timezone import now
from django_scopes import scope
from pretix.base.models import Event, Item, Order, OrderPosition, Organizer, Quota, Team, User

from tests.fake_nexa import FakeNexa

ISSUER = "nexatest:nqtsq5g5w5hjzj7ukkup37h8y2mhuhnp7u8fw4gzauzdjhgl"
RECIPIENT = "nexatest:nqtsq5g5czjrzurmkp342rk3ztv3h40c6afvjcz7w8rxh8zn"
BURN = "nexatest:nqtsq5g5hj8cysf4kfhdd0dr945mavyv5anmja9emw88j7fr"
MAINNET_ADDRESS = "nexa:nqtsq5g5w5hjzj7ukkup37h8y2mhuhnp7u8fw4gzauzdjhgl"


@pytest.fixture
def fake_nexa(settings):
    fake = FakeNexa()
    settings.NEXA_VOUCHERS_CLIENT_FACTORY = fake.bind
    return fake


@pytest.fixture
def event():
    organizer = Organizer.objects.create(name="Nordic Crypto", slug="nordic")
    with scope(organizer=organizer):
        created = Event.objects.create(
            organizer=organizer,
            name="Voucher night",
            slug="vouchers",
            date_from=now(),
            plugins="pretix_nexa_vouchers",
            live=True,
        )
        created.settings.nexa_vouchers_network = "nexatest"
        created.settings.nexa_vouchers_issuer_address = ISSUER
        created.settings.nexa_vouchers_burn_address = BURN
        created.settings.nexa_vouchers_source_url = "https://example.com/pretix-nexa-vouchers"
        yield created


@pytest.fixture
def emit_paid(django_capture_on_commit_callbacks):
    def _emit(created_event, order):
        from pretix.base.signals import order_paid

        with django_capture_on_commit_callbacks(execute=True):
            order_paid.send(created_event, order=order)

    return _emit


@pytest.fixture
def staff(event):
    user = User.objects.create_user("ops@example.test", "correct-horse")
    team = Team.objects.create(
        organizer=event.organizer,
        name="Ops",
        all_events=True,
        all_event_permissions=True,
        all_organizer_permissions=True,
    )
    team.members.add(user)
    return user


def make_item(event, name, price="10.00", admission=False):
    return Item.objects.create(event=event, name=name, default_price=Decimal(price), admission=admission)


def make_order(event, item, voucher=None, code=None):
    order = Order.objects.create(
        code=code or ("N" + get_random_string(6)),
        status=Order.STATUS_PAID,
        organizer=event.organizer,
        event=event,
        email="buyer@example.test",
        datetime=now(),
        expires=now() + timedelta(days=7),
        total=item.default_price,
        locale="en",
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    OrderPosition.objects.create(
        order=order,
        organizer=event.organizer,
        item=item,
        price=item.default_price,
        tax_rate=Decimal("0.00"),
        tax_value=Decimal("0.00"),
        positionid=1,
        voucher=voucher,
    )
    return order


def make_quota(event, name, *items):
    quota = Quota.objects.create(event=event, name=name, size=100)
    quota.items.add(*items)
    return quota
