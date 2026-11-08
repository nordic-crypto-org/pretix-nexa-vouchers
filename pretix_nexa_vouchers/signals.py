# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging

from django.db import transaction
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import item_copy_data, order_paid, periodic_task, validate_order
from pretix.control.signals import item_forms, nav_event, nav_event_settings
from pretix.control.signals import order_info as control_order_info
from pretix.presale.signals import html_footer
from pretix.presale.signals import order_info as presale_order_info

from pretix_nexa_vouchers.services import (
    assert_voucher_redeemable,
    copy_product_config,
    issue_for_order,
    mark_paid_order,
    retry_pending,
    source_url,
)

logger = logging.getLogger("pretix_nexa_vouchers")

settings_hierarkey.add_default("nexa_vouchers_network", "nexatest", str)
settings_hierarkey.add_default("nexa_vouchers_issuer_address", "", str)
settings_hierarkey.add_default("nexa_vouchers_burn_address", "", str)
settings_hierarkey.add_default("nexa_vouchers_queue_token", "", str)
settings_hierarkey.add_default("nexa_vouchers_source_url", "", str)


@receiver(order_paid, dispatch_uid="pretix_nexa_vouchers_order_paid")
def on_order_paid(sender, order=None, **kwargs):
    if order is None:
        return
    order_id = order.pk

    def _run():
        from pretix.base.models import Order

        fresh = Order.objects.filter(pk=order_id).first()
        if fresh is None:
            return
        try:
            issue_for_order(fresh)
            mark_paid_order(fresh)
        except Exception:
            logger.exception("pretix-nexa-vouchers could not finish a paid order")

    transaction.on_commit(_run)


@receiver(validate_order, dispatch_uid="pretix_nexa_vouchers_validate_order")
def on_validate_order(sender, positions=None, **kwargs):
    assert_voucher_redeemable(sender, positions)


@receiver(periodic_task, dispatch_uid="pretix_nexa_vouchers_periodic")
def on_periodic(sender, **kwargs):
    retry_pending()


@receiver(item_forms, dispatch_uid="pretix_nexa_vouchers_item_form")
def product_form(sender, request, item, **kwargs):
    from pretix_nexa_vouchers.forms import NexaProductForm

    return NexaProductForm(
        event=sender,
        item=item,
        prefix="nexavouchers",
        data=request.POST if request.method == "POST" else None,
    )


@receiver(item_copy_data, dispatch_uid="pretix_nexa_vouchers_item_copy")
def on_item_copy(sender, source, target, **kwargs):
    copy_product_config(source, target)


@receiver(nav_event, dispatch_uid="pretix_nexa_vouchers_nav")
def navbar(sender, request, **kwargs):
    if not request.user.has_event_permission(request.organizer, request.event, "event.vouchers:read", request=request):
        return []
    url = resolve(request.path_info)
    return [
        {
            "label": _("Nexa vouchers"),
            "icon": "ticket",
            "url": reverse(
                "plugins:pretix_nexa_vouchers:index",
                kwargs={"event": request.event.slug, "organizer": request.organizer.slug},
            ),
            "active": url.namespace == "plugins:pretix_nexa_vouchers",
        }
    ]


@receiver(nav_event_settings, dispatch_uid="pretix_nexa_vouchers_nav_settings")
def navbar_settings(sender, request, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("Nexa vouchers"),
            "url": reverse(
                "plugins:pretix_nexa_vouchers:settings",
                kwargs={"event": request.event.slug, "organizer": request.organizer.slug},
            ),
            "active": url.namespace == "plugins:pretix_nexa_vouchers" and url.url_name == "settings",
        }
    ]


def _order_html(order):
    from pretix_nexa_vouchers.models import IssuedVoucher

    rows = list(IssuedVoucher.objects.filter(order=order).select_related("voucher"))
    if not rows:
        return ""
    return render_to_string("pretix_nexa_vouchers/order_info.html", {"rows": rows})


@receiver(control_order_info, dispatch_uid="pretix_nexa_vouchers_control_order_info")
def control_order_panel(sender, order, **kwargs):
    return _order_html(order)


@receiver(presale_order_info, dispatch_uid="pretix_nexa_vouchers_presale_order_info")
def presale_order_panel(sender, order, **kwargs):
    return _order_html(order)


@receiver(html_footer, dispatch_uid="pretix_nexa_vouchers_footer")
def footer(sender, request, **kwargs):
    return render_to_string(
        "pretix_nexa_vouchers/footer.html",
        {"source_url": source_url(sender)},
    )
