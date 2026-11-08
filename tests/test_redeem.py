# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from pretix.base.services.orders import OrderError
from pretix.base.signals import validate_order

from pretix_nexa_vouchers.models import IssuedVoucher, VoucherProduct
from tests.conftest import BURN, ISSUER, RECIPIENT, make_item, make_order


def _pack(event):
    pack = make_item(event, "Pack")
    ticket = make_item(event, "Ticket", admission=True)
    config = VoucherProduct.objects.create(item=pack, enabled=True, voucher_count=1, tag="40c3", validity_days=7)
    config.target_items.add(ticket)
    return pack, ticket


@pytest.mark.django_db
def test_redemption_melts_a_custodied_token_and_blocks_reuse(event, fake_nexa, emit_paid):
    pack, ticket = _pack(event)
    emit_paid(event, make_order(event, pack, code="PACK1"))
    issued = IssuedVoucher.objects.get()
    assert issued.status == IssuedVoucher.STATUS_ISSUED
    assert fake_nexa.holds_token(ISSUER, issued.subgroup_id)

    redeem_order = make_order(event, ticket, voucher=issued.voucher, code="REDEEM")
    emit_paid(event, redeem_order)
    issued.refresh_from_db()
    assert issued.status == IssuedVoucher.STATUS_REDEEMED
    assert issued.redeem_kind == "melt"
    assert issued.redeem_txid.startswith("tx-melt-")
    assert ("melt", issued.subgroup_id, 1) in [
        (call[0], call[1], call[2]) for call in fake_nexa.calls if call[0] == "melt"
    ]

    position = redeem_order.positions.get()
    with pytest.raises(OrderError):
        validate_order.send(event, payments=[], positions=[position], email="buyer@example.test", locale="en")


@pytest.mark.django_db
def test_burn_address_is_observed_without_a_second_melt(event, fake_nexa, emit_paid):
    pack, ticket = _pack(event)
    emit_paid(event, make_order(event, pack, code="PACK2"))
    issued = IssuedVoucher.objects.get()
    fake_nexa.held[(BURN, issued.subgroup_id)] = True
    emit_paid(event, make_order(event, ticket, voucher=issued.voucher, code="BURN1"))
    issued.refresh_from_db()
    assert issued.redeem_kind == "burn-address"
    assert issued.status == IssuedVoucher.STATUS_REDEEMED
    assert not any(call[0] == "melt" for call in fake_nexa.calls)


@pytest.mark.django_db
def test_transferred_token_is_marked_instead_of_melted(event, fake_nexa, emit_paid):
    pack, ticket = _pack(event)
    emit_paid(event, make_order(event, pack, code="PACK3"))
    issued = IssuedVoucher.objects.get()
    fake_nexa.held[(ISSUER, issued.subgroup_id)] = False
    issued.holder_address = RECIPIENT
    issued.save(update_fields=["holder_address"])
    emit_paid(event, make_order(event, ticket, voucher=issued.voucher, code="MARK1"))
    issued.refresh_from_db()
    assert issued.redeem_kind == "marker"
    assert issued.redeem_txid.startswith("tx-mark-")


@pytest.mark.django_db
def test_wrong_product_is_rejected(event, fake_nexa, emit_paid):
    pack, ticket = _pack(event)
    other = make_item(event, "Hoodie")
    emit_paid(event, make_order(event, pack, code="PACK4"))
    issued = IssuedVoucher.objects.get()
    foreign = make_order(event, other, voucher=issued.voucher, code="NOPE1")
    with pytest.raises(OrderError):
        validate_order.send(
            event,
            payments=[],
            positions=[foreign.positions.get()],
            email="buyer@example.test",
            locale="en",
        )
