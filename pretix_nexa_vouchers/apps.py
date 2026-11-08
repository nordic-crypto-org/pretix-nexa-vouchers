# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from pretix.base.plugins import PLUGIN_LEVEL_EVENT, PluginConfig

from pretix_nexa_vouchers.version import __version__


class NexaVouchersConfig(PluginConfig):
    name = "pretix_nexa_vouchers"
    verbose_name = _("Nexa vouchers")

    class PretixPluginMeta:
        name = _("Nexa vouchers")
        author = "Nordic Crypto"
        version = __version__
        category = "FEATURE"
        experimental = True
        featured = False
        visible = True
        level = PLUGIN_LEVEL_EVENT
        description = _(
            "When a configured product is paid, create pretix vouchers and matching "
            "Nexa testnet tokens (one NFT-like subgroup per voucher). Community queues "
            "can pass those tokens on. Mainnet is not cleared. Maintained by Nordic Crypto."
        )
        compatibility = "pretix>=2025.7"
        settings_links = [
            (
                (_("Nexa vouchers"), _("Settings")),
                "plugins:pretix_nexa_vouchers:settings",
                {},
            ),
        ]
        navigation_links = [
            (
                (_("Nexa vouchers"),),
                "plugins:pretix_nexa_vouchers:index",
                {},
            ),
        ]

    def ready(self):
        from pretix_nexa_vouchers import signals  # noqa: F401

    @cached_property
    def compatibility_warnings(self):
        from pretix_nexa_vouchers.nexa import mainnet_allowed

        if mainnet_allowed():
            return [
                _(
                    "NEXA_VOUCHERS_ALLOW_MAINNET is enabled. Mainnet is not cleared "
                    "for this plugin; leave the flag off unless you are doing an explicit review."
                )
            ]
        return []
