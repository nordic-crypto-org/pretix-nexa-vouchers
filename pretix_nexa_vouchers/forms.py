# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Item, Question, Quota

from pretix_nexa_vouchers.models import VoucherList, VoucherProduct
from pretix_nexa_vouchers.nexa import (
    TESTNET,
    MainnetNotCleared,
    NexaRpcError,
    assert_address_for_network,
    mainnet_allowed,
    resolve_network,
)
from pretix_nexa_vouchers.services import MAX_VOUCHERS


class NexaProductForm(forms.Form):
    """Extra product fields rendered by pretix's item editor."""

    title = _("Nexa vouchers")

    enabled = forms.BooleanField(
        label=_("Issue Nexa vouchers when this product is paid"),
        required=False,
    )
    voucher_count = forms.IntegerField(
        label=_("Vouchers per item sold"),
        min_value=1,
        max_value=MAX_VOUCHERS,
        initial=1,
    )
    tag = forms.CharField(
        label=_("Voucher tag"),
        required=False,
        max_length=255,
        help_text=_("Stored on each pretix voucher so the batch can be filtered."),
    )
    validity_days = forms.IntegerField(
        label=_("Valid for this many days after issue"),
        required=False,
        min_value=1,
        max_value=3650,
    )
    valid_until = forms.DateTimeField(
        label=_("Valid until"),
        required=False,
        help_text=_("Leave empty to skip a fixed end. If both are set, the earlier one wins."),
    )
    target_items = forms.MultipleChoiceField(
        label=_("Target products"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_(
            "Pretix itself stores one product or one quota on a voucher. "
            "One product and no quotas uses that native limit. Several products are enforced by this plugin."
        ),
    )
    target_quotas = forms.MultipleChoiceField(
        label=_("Target quotas"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    block_quota = forms.BooleanField(
        label=_("Reserve quota"),
        required=False,
        help_text=_("Only available when exactly one product or exactly one quota is selected."),
    )
    allow_ignore_quota = forms.BooleanField(label=_("Allow bypassing quota"), required=False)
    price_mode = forms.ChoiceField(label=_("Price effect"), choices=VoucherProduct.PRICE_CHOICES)
    value = forms.DecimalField(label=_("Voucher value"), required=False, max_digits=13, decimal_places=2)
    max_usages = forms.IntegerField(label=_("Maximum usages"), min_value=1, initial=1)
    holder_mode = forms.ChoiceField(label=_("First token holder"), choices=VoucherProduct.HOLDER_CHOICES)
    address_question = forms.ChoiceField(label=_("Question that contains the Nexa address"), required=False)

    def __init__(self, event, item, *args, **kwargs):
        self.event = event
        self.item = item
        super().__init__(*args, **kwargs)
        self.fields["target_items"].choices = [
            (str(row.pk), str(row.name)) for row in Item.objects.filter(event=event)
        ]
        self.fields["target_quotas"].choices = [
            (str(row.pk), row.name) for row in Quota.objects.filter(event=event)
        ]
        self.fields["address_question"].choices = [("", "---------")] + [
            (str(row.pk), str(row.question)) for row in Question.objects.filter(event=event)
        ]
        config = VoucherProduct.objects.filter(item=item).first()
        self.config = config
        if config is not None and not self.is_bound:
            self.initial.update(
                {
                    "enabled": config.enabled,
                    "voucher_count": config.voucher_count,
                    "tag": config.tag,
                    "validity_days": config.validity_days,
                    "valid_until": config.valid_until,
                    "target_items": [str(pk) for pk in config.target_items.values_list("pk", flat=True)],
                    "target_quotas": [str(pk) for pk in config.target_quotas.values_list("pk", flat=True)],
                    "block_quota": config.block_quota,
                    "allow_ignore_quota": config.allow_ignore_quota,
                    "price_mode": config.price_mode,
                    "value": config.value,
                    "max_usages": config.max_usages,
                    "holder_mode": config.holder_mode,
                    "address_question": str(config.address_question_id or ""),
                }
            )

    def clean(self):
        data = super().clean()
        items = data.get("target_items") or []
        quotas = data.get("target_quotas") or []
        if data.get("enabled") and not items and not quotas:
            raise forms.ValidationError(_("Choose at least one target product or quota."))
        if data.get("block_quota") and not ((len(items) == 1 and not quotas) or (len(quotas) == 1 and not items)):
            raise forms.ValidationError(_("Reserving quota needs exactly one target product or one target quota."))
        if data.get("price_mode") not in (None, "", "none") and data.get("value") is None:
            raise forms.ValidationError(_("Enter a voucher value for this price effect."))
        if data.get("holder_mode") == VoucherProduct.HOLDER_BUYER and not data.get("address_question"):
            raise forms.ValidationError(_("Choose the question that collects the buyer's Nexa address."))
        return data

    def save(self):
        config, _created = VoucherProduct.objects.get_or_create(item=self.item)
        data = self.cleaned_data
        config.enabled = data["enabled"]
        config.voucher_count = data["voucher_count"]
        config.tag = data.get("tag") or "nexa"
        config.validity_days = data.get("validity_days")
        config.valid_until = data.get("valid_until")
        config.block_quota = data["block_quota"]
        config.allow_ignore_quota = data["allow_ignore_quota"]
        config.price_mode = data["price_mode"]
        config.value = None if data["price_mode"] == "none" else data.get("value")
        config.max_usages = data["max_usages"]
        config.holder_mode = data["holder_mode"]
        question_id = data.get("address_question") or ""
        config.address_question_id = int(question_id) if question_id else None
        config.save()
        config.target_items.set(Item.objects.filter(event=self.event, pk__in=[int(pk) for pk in data["target_items"]]))
        config.target_quotas.set(Quota.objects.filter(event=self.event, pk__in=[int(pk) for pk in data["target_quotas"]]))
        return config


class EventNexaSettingsForm(forms.Form):
    issuer_address = forms.CharField(
        label=_("Issuer address"),
        required=False,
        help_text=_("Testnet address that receives newly minted voucher tokens. The node wallet must be able to spend it."),
    )
    burn_address = forms.CharField(
        label=_("Burn address"),
        required=False,
        help_text=_("If a voucher token is sent here, redemption records that transfer instead of melting it."),
    )
    network = forms.ChoiceField(
        label=_("Network"),
        choices=[(TESTNET, _("Nexa testnet (nexatest)"))],
    )
    source_url = forms.URLField(
        label=_("Source code URL"),
        required=False,
        help_text=_("Shown to shop visitors so they can obtain the corresponding source (AGPL-3.0)."),
    )
    rotate_queue_token = forms.BooleanField(
        label=_("Rotate the queue organiser token"),
        required=False,
    )

    def __init__(self, event, *args, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)
        if mainnet_allowed():
            self.fields["network"].choices = [
                (TESTNET, _("Nexa testnet (nexatest)")),
                ("nexa", _("Nexa mainnet (not cleared)")),
            ]
        else:
            self.fields["network"].choices = [(TESTNET, _("Nexa testnet (nexatest)"))]
            self.fields["network"].help_text = _(
                "Mainnet is not cleared. NEXA_VOUCHERS_ALLOW_MAINNET is off, so this event stays on testnet."
            )
        if not self.is_bound:
            self.initial.update(
                {
                    "issuer_address": event.settings.nexa_vouchers_issuer_address or "",
                    "burn_address": event.settings.nexa_vouchers_burn_address or "",
                    "network": event.settings.nexa_vouchers_network or TESTNET,
                    "source_url": event.settings.nexa_vouchers_source_url or "",
                }
            )

    def clean(self):
        data = super().clean()
        requested = data.get("network") or TESTNET
        try:
            network = resolve_network(requested)
        except MainnetNotCleared as exc:
            raise forms.ValidationError(str(exc)) from exc
        data["network"] = network
        for field in ("issuer_address", "burn_address"):
            value = (data.get(field) or "").strip()
            if not value:
                data[field] = ""
                continue
            try:
                data[field] = assert_address_for_network(value, network)
            except (NexaRpcError, MainnetNotCleared) as exc:
                self.add_error(field, str(exc))
        source = (data.get("source_url") or "").strip()
        if source and not source.startswith(("https://", "http://")):
            self.add_error("source_url", _("Source URL must start with http:// or https://."))
        return data

    def save(self):
        self.event.settings.nexa_vouchers_issuer_address = self.cleaned_data["issuer_address"]
        self.event.settings.nexa_vouchers_burn_address = self.cleaned_data["burn_address"]
        self.event.settings.nexa_vouchers_network = self.cleaned_data["network"]
        self.event.settings.nexa_vouchers_source_url = self.cleaned_data.get("source_url") or ""
        from pretix_nexa_vouchers.services import queue_token

        queue_token(self.event, rotate=bool(self.cleaned_data.get("rotate_queue_token")))
        if not (self.event.settings.nexa_vouchers_queue_token or ""):
            queue_token(self.event, rotate=True)


class VoucherListForm(forms.ModelForm):
    class Meta:
        model = VoucherList
        fields = ["name", "slug", "description", "organiser_address", "active"]

    def __init__(self, event, *args, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)

    def clean_organiser_address(self):
        value = (self.cleaned_data.get("organiser_address") or "").strip()
        if not value:
            return ""
        try:
            network = resolve_network(self.event.settings.nexa_vouchers_network or TESTNET)
            return assert_address_for_network(value, network)
        except (NexaRpcError, MainnetNotCleared) as exc:
            raise forms.ValidationError(str(exc)) from exc

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.event = self.event
        if commit:
            obj.save()
        return obj
