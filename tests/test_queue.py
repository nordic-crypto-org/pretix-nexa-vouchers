# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import json

import pytest
from django.test import Client
from django.urls import reverse

from pretix_nexa_vouchers.models import IssuedVoucher, QueueEntry, VoucherList, VoucherProduct
from pretix_nexa_vouchers.services import queue_token
from tests.conftest import ISSUER, RECIPIENT, make_item, make_order


def _issue_one(event, fake_nexa, emit_paid):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    config = VoucherProduct.objects.create(item=pack, enabled=True, voucher_count=1, tag="queue")
    config.target_items.add(ticket)
    emit_paid(event, make_order(event, pack, code="QUEUE1"))
    return IssuedVoucher.objects.get()


def _urls(event, slug, public_id=None):
    kwargs = {"organizer": event.organizer.slug, "event": event.slug, "slug": slug}
    join = reverse("plugins:pretix_nexa_vouchers:queue.join", kwargs=kwargs)
    assign = reverse("plugins:pretix_nexa_vouchers:api.assign", kwargs=kwargs)
    confirm = reverse("plugins:pretix_nexa_vouchers:api.confirm", kwargs=kwargs)
    entry = None
    if public_id:
        entry = reverse(
            "plugins:pretix_nexa_vouchers:queue.entry",
            kwargs={**kwargs, "public_id": public_id},
        )
    return join, assign, confirm, entry


@pytest.mark.django_db
def test_queue_join_assign_confirm_and_claim(event, fake_nexa, emit_paid):
    issued = _issue_one(event, fake_nexa, emit_paid)
    voucher_list = VoucherList.objects.create(
        event=event,
        name="Cold North",
        slug="cold-north",
        description="Habitat queue",
        organiser_address=ISSUER,
    )
    token = queue_token(event)
    api = Client(enforce_csrf_checks=True)
    join_url, assign_url, confirm_url, _entry_url = _urls(event, voucher_list.slug)

    first = api.post(
        join_url,
        data=json.dumps({"display_name": "Ada", "nexa_address": RECIPIENT}),
        content_type="application/json",
        HTTP_ACCEPT="application/json",
    )
    assert first.status_code == 201, first.content
    body = first.json()
    assert body["position"] == 1
    assert body["assigned_voucher"] is None

    second_address = "nexatest:nqtsq5g5w5hjzj7ukkup37h8y2mhuhnp7u8fw4gzauzdjhgl"
    second = api.post(
        join_url + "?format=json",
        data=json.dumps({"display_name": "Bea", "nexa_address": second_address}),
        content_type="application/json",
    )
    assert second.status_code == 201
    assert second.json()["position"] == 2

    status_url = reverse(
        "plugins:pretix_nexa_vouchers:queue.entry",
        kwargs={
            "organizer": event.organizer.slug,
            "event": event.slug,
            "slug": voucher_list.slug,
            "public_id": body["public_id"],
        },
    )
    looked_up = api.get(status_url + "?format=json")
    assert looked_up.status_code == 200
    assert looked_up.json()["position"] == 1

    denied = api.post(assign_url, data="{}", content_type="application/json")
    assert denied.status_code == 403

    assigned = api.post(
        assign_url,
        data="{}",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert assigned.status_code == 200, assigned.content
    payload = assigned.json()
    assert payload["display_name"] == "Ada"
    assert payload["status"] == QueueEntry.STATUS_ASSIGNED
    assert payload["assigned_voucher"]["subgroup_id"] == issued.subgroup_id
    assert payload["assigned_voucher"]["token_send_uri"].startswith("tdpp://")
    assert "chain=nexatest" in payload["assigned_voucher"]["token_send_uri"]
    assert "voucher_code" not in payload["assigned_voucher"]

    still_waiting = api.get(
        reverse(
            "plugins:pretix_nexa_vouchers:queue.entry",
            kwargs={
                "organizer": event.organizer.slug,
                "event": event.slug,
                "slug": voucher_list.slug,
                "public_id": second.json()["public_id"],
            },
        )
        + "?format=json"
    )
    assert still_waiting.json()["position"] == 1

    old_code = issued.voucher.code
    fake_nexa.held[(RECIPIENT, issued.subgroup_id)] = True
    confirmed = api.post(
        confirm_url,
        data=json.dumps({"entry": body["public_id"], "txid": "tx-handover"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert confirmed.status_code == 200, confirmed.content
    issued.refresh_from_db()
    issued.voucher.refresh_from_db()
    assert issued.holder_address == RECIPIENT
    assert issued.voucher.code != old_code
    assert issued.status == IssuedVoucher.STATUS_TRANSFERRED

    claim_url = reverse(
        "plugins:pretix_nexa_vouchers:claim",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "public_id": issued.public_id},
    )
    challenge = api.get(claim_url + "?format=json")
    assert challenge.status_code == 200
    assert "voucher_present_uri" in challenge.json()
    assert "voucher_code" not in challenge.json()
    bad = api.post(
        claim_url + "?format=json",
        data=json.dumps({"address": RECIPIENT, "signature": "nope"}),
        content_type="application/json",
    )
    assert bad.status_code == 403
    good = api.post(
        claim_url + "?format=json",
        data=json.dumps({"address": RECIPIENT, "signature": "good-sig"}),
        content_type="application/json",
    )
    assert good.status_code == 200, good.content
    assert good.json()["voucher_code"] == issued.voucher.code
    assert "voucher=" in good.json()["redeem_url"]


@pytest.mark.django_db
def test_join_rejects_a_mainnet_address_on_testnet(event):
    VoucherList.objects.create(event=event, name="List", slug="list", active=True)
    api = Client()
    url = reverse(
        "plugins:pretix_nexa_vouchers:queue.join",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "slug": "list"},
    )
    response = api.post(
        url,
        data=json.dumps({"display_name": "Ada", "nexa_address": "nexa:nqtsq5g5nottestnet"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert QueueEntry.objects.count() == 0


@pytest.mark.django_db
def test_public_list_page_names_nordic_crypto(event, client):
    VoucherList.objects.create(event=event, name="Cold North", slug="cold-north", description="Queue")
    response = client.get("/nordic/vouchers/nexa/lists/cold-north/")
    assert response.status_code == 200
    text = response.content.decode()
    assert "Cold North" in text
    assert "Nordic Crypto" in text
    assert "owns Nexa" not in text


@pytest.mark.django_db
def test_control_index_for_staff(event, staff, client):
    assert client.login(email="ops@example.test", password="correct-horse")
    response = client.get("/control/event/nordic/vouchers/nexa-vouchers/")
    assert response.status_code == 200
    text = response.content.decode()
    assert "testnet" in text.lower()
    assert "not cleared" in text.lower() or "Mainnet is not cleared" in text or "defaults off" in text
