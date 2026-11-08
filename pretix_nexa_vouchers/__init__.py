# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from pretix_nexa_vouchers.apps import NexaVouchersConfig
from pretix_nexa_vouchers.version import __version__

PretixPluginMeta = NexaVouchersConfig.PretixPluginMeta

__all__ = ["NexaVouchersConfig", "PretixPluginMeta", "__version__"]
