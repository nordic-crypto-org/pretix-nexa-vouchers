# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib

from pretix_nexa_vouchers.nexa import NexaRpcError


class FakeNexa:
    """In-memory stand-in for nexad and Rostrum. Tests never open a socket."""

    def __init__(self):
        self.calls = []
        self.network = "nexatest"
        self.networks = []
        self.held = {}
        self.fail_mint = False
        self.fail_create = False
        self._groups = 0

    def bind(self, network):
        self.network = network
        self.networks.append(network)
        return self

    def create_group(self, *, address, ticker, name, document_url, document_hash):
        self.calls.append(("create_group", address, ticker, name, document_url, document_hash))
        if self.fail_create:
            raise NexaRpcError("node down")
        self._groups += 1
        prefix = "nexatest:" if self.network != "nexa" else "nexa:"
        return f"{prefix}group{self._groups}", f"tx-gen-{self._groups}"

    def subgroup(self, parent_group_id, data_hex):
        self.calls.append(("subgroup", parent_group_id, data_hex))
        digest = hashlib.sha256(f"{parent_group_id}:{data_hex}".encode()).hexdigest()[:16]
        prefix = "nexatest:" if self.network != "nexa" else "nexa:"
        return f"{prefix}sub{digest}"

    def mint(self, group_id, address, quantity=1):
        self.calls.append(("mint", group_id, address, quantity))
        if self.fail_mint:
            raise NexaRpcError("mint rejected")
        self.held[(address, group_id)] = True
        return "tx-mint-" + group_id[-8:]

    def melt(self, group_id, quantity=1):
        self.calls.append(("melt", group_id, quantity))
        for key in list(self.held):
            if key[1] == group_id:
                self.held[key] = False
        return "tx-melt-" + group_id[-8:]

    def announce_marker(self, subgroup_id, public_id):
        self.calls.append(("marker", subgroup_id, public_id))
        return "tx-mark-" + public_id[:8]

    def verify_message(self, address, signature, message):
        self.calls.append(("verify", address, signature, message))
        return signature == "good-sig" and bool(message)

    def token_unspent(self, address, token_id):
        if self.held.get((address, token_id)):
            return [{"tx_hash": "utxo" + token_id[-6:], "tx_pos": 0, "value": 1}]
        return []

    def holds_token(self, address, token_id):
        return bool(self.held.get((address, token_id)))
