# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
from datetime import timedelta

import pytest
from django.utils.timezone import now
from pretix.base.models import Voucher

from pretix_nexa_vouchers.forms import NexaProductForm
from pretix_nexa_vouchers.models import IssuedVoucher, VoucherProduct
from pretix_nexa_vouchers.services import retry_pending
from tests.conftest import ISSUER, MAINNET_ADDRESS, make_item, make_order, make_quota


def _enable(event, pack, targets, count=2, **extra):
    config = VoucherProduct.objects.create(
        item=pack,
        enabled=True,
        voucher_count=count,
        tag="40c3",
        validity_days=14,
        **extra,
    )
    config.target_items.set(targets)
    return config


@pytest.mark.django_db
def test_paid_order_creates_vouchers_and_mocked_tokens(event, fake_nexa, emit_paid):
    pack = make_item(event, "Community voucher pack")
    ticket = make_item(event, "Conference ticket", admission=True)
    _enable(event, pack, [ticket], count=3)
    order = make_order(event, pack)

    emit_paid(event, order)
    mints = [call for call in fake_nexa.calls if call[0] == "mint"]
    emit_paid(event, order)

    issued = list(IssuedVoucher.objects.filter(order=order).order_by("index"))
    assert len(issued) == 3
    assert len({row.voucher_id for row in issued}) == 3
    assert all(row.status == IssuedVoucher.STATUS_ISSUED for row in issued)
    assert all(row.subgroup_id.startswith("nexatest:") for row in issued)
    assert all(row.holder_address == ISSUER for row in issued)
    assert {row.voucher.tag for row in issued} == {"40c3"}
    assert all(row.voucher.item_id == ticket.pk for row in issued)
    assert all(row.voucher.valid_until is not None for row in issued)
    assert fake_nexa.calls[0][0] == "create_group"
    assert mints and len(mints) == 3
    assert [call for call in fake_nexa.calls if call[0] == "mint"] == mints
    assert Voucher.objects.filter(event=event).count() == 3


@pytest.mark.django_db
def test_multiple_targets_are_snapshotted_for_later_checks(event, fake_nexa, emit_paid):
    pack = make_item(event, "Pack")
    day = make_item(event, "Day ticket", admission=True)
    workshop = make_item(event, "Workshop")
    make_quota(event, "Hall", day)
    config = _enable(event, pack, [day, workshop], count=1)
    config.target_quotas.add(make_quota(event, "Rooms", workshop))
    order = make_order(event, pack)
    emit_paid(event, order)
    issued = IssuedVoucher.objects.get(order=order)
    assert issued.voucher.item_id is None
    assert issued.voucher.quota_id is None
    assert set(issued.allowed_item_ids) == {day.pk, workshop.pk}
    assert issued.allowed_quota_ids


@pytest.mark.django_db
def test_plugin_disabled_does_not_issue(event, fake_nexa, emit_paid):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    _enable(event, pack, [ticket], count=1)
    event.plugins = ""
    event.save()
    emit_paid(event, make_order(event, pack))
    assert IssuedVoucher.objects.count() == 0
    assert fake_nexa.calls == []


@pytest.mark.django_db
def test_mainnet_is_blocked_and_creates_no_spendable_voucher(event, fake_nexa, emit_paid, monkeypatch):
    monkeypatch.delenv("NEXA_VOUCHERS_ALLOW_MAINNET", raising=False)
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    _enable(event, pack, [ticket], count=2)
    event.settings.nexa_vouchers_network = "nexa"
    event.settings.nexa_vouchers_issuer_address = MAINNET_ADDRESS
    emit_paid(event, make_order(event, pack))
    rows = list(IssuedVoucher.objects.all())
    assert len(rows) == 2
    assert all(row.status == IssuedVoucher.STATUS_BLOCKED for row in rows)
    assert all(row.voucher_id is None for row in rows)
    assert all("not cleared" in row.error or "not enabled" in row.error for row in rows)
    assert fake_nexa.calls == []
    assert Voucher.objects.count() == 0


@pytest.mark.django_db
def test_mainnet_flag_reaches_the_client(event, fake_nexa, emit_paid, monkeypatch):
    monkeypatch.setenv("NEXA_VOUCHERS_ALLOW_MAINNET", "1")
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    _enable(event, pack, [ticket], count=1)
    event.settings.nexa_vouchers_network = "nexa"
    event.settings.nexa_vouchers_issuer_address = MAINNET_ADDRESS
    event.settings.nexa_vouchers_burn_address = ""
    emit_paid(event, make_order(event, pack))
    assert fake_nexa.networks == ["nexa"]
    assert IssuedVoucher.objects.get().subgroup_id.startswith("nexa:")


@pytest.mark.django_db
def test_chain_failure_is_retried(event, fake_nexa, emit_paid):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    _enable(event, pack, [ticket], count=1)
    fake_nexa.fail_mint = True
    emit_paid(event, make_order(event, pack))
    issued = IssuedVoucher.objects.get()
    assert issued.status == IssuedVoucher.STATUS_CHAIN_PENDING
    assert issued.voucher_id is not None
    assert issued.subgroup_id
    fake_nexa.fail_mint = False
    retry_pending()
    issued.refresh_from_db()
    assert issued.status == IssuedVoucher.STATUS_ISSUED
    assert issued.issuance_txid


@pytest.mark.django_db
def test_product_form_saves_targets_and_validity(event):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    quota = make_quota(event, "Hall", ticket)
    deadline = now() + timedelta(days=10)
    form = NexaProductForm(
        event,
        pack,
        data={
            "enabled": "on",
            "voucher_count": "4",
            "tag": "cold-north",
            "validity_days": "3",
            "valid_until": deadline.strftime("%Y-%m-%d %H:%M:%S"),
            "target_items": [str(ticket.pk)],
            "target_quotas": [str(quota.pk)],
            "price_mode": "none",
            "max_usages": "1",
            "holder_mode": "issuer",
            "address_question": "",
        },
    )
    assert form.is_valid(), form.errors
    config = form.save()
    assert config.enabled is True
    assert config.voucher_count == 4
    assert config.tag == "cold-north"
    assert list(config.target_items.values_list("pk", flat=True)) == [ticket.pk]
    assert list(config.target_quotas.values_list("pk", flat=True)) == [quota.pk]


@pytest.mark.django_db
def test_token_document_matches_the_hashed_body(event, fake_nexa, emit_paid, client):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    _enable(event, pack, [ticket], count=1)
    emit_paid(event, make_order(event, pack))
    issued = IssuedVoucher.objects.select_related("series").get()
    series = issued.series
    digest = hashlib.sha256(series.document_body.encode()).hexdigest()
    assert digest == series.document_hash
    response = client.get(
        f"/nordic/vouchers/nexa/token/{series.public_id}.json",
        HTTP_HOST="example.com",
    )
    assert response.status_code == 200
    assert response.content == series.document_body.encode()
    body = response.content.decode()
    assert "Nordic Crypto" in body
    assert "owns Nexa" not in body
