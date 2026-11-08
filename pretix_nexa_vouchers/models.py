# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_scopes import ScopedManager


class VoucherProduct(models.Model):
    """Per-product rules for how many vouchers a paid order position issues."""

    HOLDER_ISSUER = "issuer"
    HOLDER_BUYER = "buyer_address"
    HOLDER_CHOICES = (
        (HOLDER_ISSUER, _("Issuer / queue wallet from the event settings")),
        (HOLDER_BUYER, _("Nexa address answered on the order")),
    )
    PRICE_CHOICES = (
        ("none", _("No price effect")),
        ("set", _("Set product price to")),
        ("subtract", _("Subtract from product price")),
        ("percent", _("Reduce product price by (%)")),
    )

    item = models.OneToOneField(
        "pretixbase.Item",
        related_name="nexa_voucher_product",
        on_delete=models.CASCADE,
    )
    enabled = models.BooleanField(default=False)
    voucher_count = models.PositiveIntegerField(default=1)
    tag = models.CharField(max_length=255, blank=True, default="nexa")
    validity_days = models.PositiveIntegerField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    target_items = models.ManyToManyField(
        "pretixbase.Item",
        blank=True,
        related_name="+",
    )
    target_quotas = models.ManyToManyField(
        "pretixbase.Quota",
        blank=True,
        related_name="+",
    )
    block_quota = models.BooleanField(default=False)
    allow_ignore_quota = models.BooleanField(default=False)
    price_mode = models.CharField(max_length=32, choices=PRICE_CHOICES, default="none")
    value = models.DecimalField(max_digits=13, decimal_places=2, null=True, blank=True)
    max_usages = models.PositiveIntegerField(default=1)
    holder_mode = models.CharField(max_length=32, choices=HOLDER_CHOICES, default=HOLDER_ISSUER)
    address_question = models.ForeignKey(
        "pretixbase.Question",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = ScopedManager(organizer="item__event__organizer")

    def __str__(self):
        return f"Nexa vouchers for {self.item_id}"


class TokenSeries(models.Model):
    """Parent Nexa group for one product. Each voucher is a subgroup of this group."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.ForeignKey("pretixbase.Event", on_delete=models.CASCADE, related_name="nexa_token_series")
    product = models.OneToOneField(VoucherProduct, on_delete=models.CASCADE, related_name="series")
    parent_group_id = models.CharField(max_length=255, blank=True)
    genesis_txid = models.CharField(max_length=128, blank=True)
    ticker = models.CharField(max_length=16)
    token_name = models.CharField(max_length=128)
    document_hash = models.CharField(max_length=64, blank=True)
    document_url = models.URLField(max_length=500, blank=True)
    document_body = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)

    objects = ScopedManager(organizer="event__organizer")


class IssuedVoucher(models.Model):
    """One pretix voucher bound to one Nexa subgroup (quantity 1)."""

    STATUS_BLOCKED = "blocked"
    STATUS_CHAIN_PENDING = "chain_pending"
    STATUS_ISSUED = "issued"
    STATUS_ASSIGNED = "assigned"
    STATUS_TRANSFERRED = "transferred"
    STATUS_REDEEM_PENDING = "redeem_pending"
    STATUS_REDEEMED = "redeemed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_BLOCKED, _("Blocked")),
        (STATUS_CHAIN_PENDING, _("Waiting for the chain")),
        (STATUS_ISSUED, _("Issued")),
        (STATUS_ASSIGNED, _("Assigned to a queue entry")),
        (STATUS_TRANSFERRED, _("Transferred")),
        (STATUS_REDEEM_PENDING, _("Redemption marker pending")),
        (STATUS_REDEEMED, _("Redeemed")),
        (STATUS_FAILED, _("Failed")),
    )
    OPEN_STATUSES = (STATUS_ISSUED, STATUS_ASSIGNED, STATUS_TRANSFERRED)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.ForeignKey("pretixbase.Event", on_delete=models.CASCADE, related_name="nexa_issued_vouchers")
    order = models.ForeignKey("pretixbase.Order", on_delete=models.CASCADE, related_name="nexa_issued_vouchers")
    order_position = models.ForeignKey(
        "pretixbase.OrderPosition",
        on_delete=models.CASCADE,
        related_name="nexa_issued_vouchers",
    )
    product = models.ForeignKey(VoucherProduct, on_delete=models.PROTECT, related_name="issued")
    series = models.ForeignKey(TokenSeries, null=True, blank=True, on_delete=models.SET_NULL, related_name="issued")
    voucher = models.ForeignKey(
        "pretixbase.Voucher",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="nexa_issued",
    )
    index = models.PositiveIntegerField()
    parent_group_id = models.CharField(max_length=255, blank=True)
    subgroup_id = models.CharField(max_length=255, blank=True)
    commitment = models.CharField(max_length=80, blank=True)
    issuance_txid = models.CharField(max_length=128, blank=True)
    redeem_txid = models.CharField(max_length=128, blank=True)
    redeem_kind = models.CharField(max_length=32, blank=True)
    transfer_txid = models.CharField(max_length=128, blank=True)
    holder_address = models.CharField(max_length=255, blank=True)
    issuer_address = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_CHAIN_PENDING)
    allowed_item_ids = models.JSONField(default=list)
    allowed_quota_ids = models.JSONField(default=list)
    error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedManager(organizer="event__organizer")

    class Meta:
        unique_together = (("order_position", "index"),)
        ordering = ("id",)

    @property
    def chain_ready(self) -> bool:
        return bool(self.subgroup_id) and self.status in {
            self.STATUS_ISSUED,
            self.STATUS_ASSIGNED,
            self.STATUS_TRANSFERRED,
            self.STATUS_REDEEM_PENDING,
            self.STATUS_REDEEMED,
        }


class VoucherList(models.Model):
    """A community queue, such as a hackerspace or a habitat list."""

    event = models.ForeignKey("pretixbase.Event", on_delete=models.CASCADE, related_name="nexa_voucher_lists")
    name = models.CharField(max_length=190)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    organiser_address = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    objects = ScopedManager(organizer="event__organizer")

    class Meta:
        unique_together = (("event", "slug"),)
        ordering = ("name",)

    def __str__(self):
        return self.name


class QueueEntry(models.Model):
    STATUS_WAITING = "waiting"
    STATUS_ASSIGNED = "assigned"
    STATUS_CLAIMED = "claimed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = (
        (STATUS_WAITING, _("Waiting")),
        (STATUS_ASSIGNED, _("Assigned")),
        (STATUS_CLAIMED, _("Claimed")),
        (STATUS_SKIPPED, _("Skipped")),
    )

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    voucher_list = models.ForeignKey(VoucherList, on_delete=models.CASCADE, related_name="entries")
    display_name = models.CharField(max_length=80)
    nexa_address = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_WAITING)
    issued = models.OneToOneField(
        IssuedVoucher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="queue_entry",
    )
    created = models.DateTimeField(auto_now_add=True)
    assigned_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedManager(organizer="voucher_list__event__organizer")

    class Meta:
        ordering = ("created", "id")
