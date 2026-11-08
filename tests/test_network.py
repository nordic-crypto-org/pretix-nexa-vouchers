# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from pretix_nexa_vouchers.nexa import (
    MAINNET,
    TESTNET,
    MainnetNotCleared,
    NexaClient,
    NexaRpcError,
    address_network,
    assert_address_for_network,
    build_tdpp,
    build_wallet_carrier,
    mainnet_allowed,
    resolve_network,
)


def test_mainnet_is_off_by_default(monkeypatch):
    monkeypatch.delenv("NEXA_VOUCHERS_ALLOW_MAINNET", raising=False)
    assert mainnet_allowed() is False
    assert resolve_network(None) == TESTNET
    assert resolve_network("nexatest") == TESTNET
    with pytest.raises(MainnetNotCleared):
        resolve_network(MAINNET)


def test_mainnet_flag_must_be_explicit(monkeypatch):
    monkeypatch.setenv("NEXA_VOUCHERS_ALLOW_MAINNET", "1")
    assert resolve_network(MAINNET) == MAINNET
    monkeypatch.setenv("NEXA_VOUCHERS_ALLOW_MAINNET", "no")
    with pytest.raises(MainnetNotCleared):
        resolve_network(MAINNET)


def test_testnet_prefix_is_not_mainnet():
    assert address_network("nexatest:nqtsq5g5example") == TESTNET
    assert address_network("nexa:nqtsq5g5example") == MAINNET
    with pytest.raises(NexaRpcError):
        assert_address_for_network("nexa:nqtsq5g5example", TESTNET)
    with pytest.raises(MainnetNotCleared):
        assert_address_for_network("nexa:nqtsq5g5example", MAINNET)


def test_tdpp_links_are_testnet_and_carry_a_callback():
    uri = build_tdpp(
        "tickets.example",
        "token-send",
        TESTNET,
        group="nexatest:subgroup",
        addr="nexatest:someone",
        qty="1",
        callback="https://tickets.example/nordic/vouchers/nexa/claim/abc/",
    )
    assert uri.startswith("tdpp://tickets.example/token-send?")
    assert "chain=nexatest" in uri
    assert "topic=nexa-voucher" in uri
    assert "group=nexatest" in uri
    carrier = build_wallet_carrier("voucher-present", TESTNET, group="nexatest:subgroup")
    assert carrier.startswith("https://w.nexa.org/tdpp/voucher-present?")


def test_client_refuses_mainnet_and_hides_credentials(monkeypatch):
    monkeypatch.delenv("NEXA_VOUCHERS_ALLOW_MAINNET", raising=False)
    with pytest.raises(MainnetNotCleared):
        NexaClient(
            network=MAINNET,
            rpc_url="http://127.0.0.1:9",
            rpc_user="rpc-user",
            rpc_password="super-secret-password",
            rostrum_url="",
        )
    client = NexaClient(
        network=TESTNET,
        rpc_url="http://rpc-user:super-secret-password@127.0.0.1:9/rpc",
        rpc_password="",
        rostrum_url="",
    )
    assert "super-secret-password" not in client.rpc_url
    with pytest.raises(NexaRpcError) as exc:
        client.rpc("token", ["info"])
    assert "super-secret-password" not in str(exc.value)
    assert "rpc-user" not in str(exc.value)


def test_marker_uses_public_rpc_methods():
    client = NexaClient(
        network=TESTNET,
        rpc_url="http://127.0.0.1:9",
        rpc_user="rpc-user",
        rpc_password="super-secret-password",
        rostrum_url="",
    )
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "createrawtransaction":
            return "00"
        if method == "signrawtransaction":
            return {"hex": "aa", "complete": True}
        if method == "sendrawtransaction":
            return "aabbcc"
        raise AssertionError(method)

    client.rpc = rpc
    txid = client.announce_marker("nexatest:subgroup", "11111111-1111-1111-1111-111111111111")
    assert txid == "aabbcc"
    assert [call[0] for call in calls] == ["createrawtransaction", "signrawtransaction", "sendrawtransaction"]
    data_hex = calls[0][1][1][0]["data"]
    assert bytes.fromhex(data_hex).startswith(b"NXVC:")
