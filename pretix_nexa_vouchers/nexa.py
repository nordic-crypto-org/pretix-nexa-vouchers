# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexa testnet client.

Speaks the public nexad ``token`` JSON-RPC and Rostrum JSON-RPC. This module is a
clean-room client for those documented interfaces. It does not implement wallet
custody: keys stay in the operator's node or in Wally.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TESTNET = "nexatest"
MAINNET = "nexa"
TOPIC = "nexa-voucher"
OPERATOR = "Nordic Crypto"

_SECRET_RE = re.compile(r"(?i)(password|secret|wif|mnemonic|authorization)([\"']?\s*[:=]\s*)(\S+)")
_USERINFO_RE = re.compile(r"//[^/\s@]+@")


class MainnetNotCleared(Exception):
    """Raised when a caller asks for Nexa mainnet while the flag is off.

    Mainnet support is not cleared. The default is testnet only.
    """


class NexaRpcError(Exception):
    """A node or Rostrum call failed. The message must not contain credentials."""


def mainnet_allowed() -> bool:
    raw = os.environ.get("NEXA_VOUCHERS_ALLOW_MAINNET", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_network(requested: str | None) -> str:
    network = (requested or TESTNET).strip().lower() or TESTNET
    if network not in {TESTNET, MAINNET}:
        raise NexaRpcError(f"Unknown Nexa network '{network}'. Use '{TESTNET}'.")
    if network == MAINNET and not mainnet_allowed():
        raise MainnetNotCleared(
            "Nexa mainnet is not enabled. This plugin is testnet-only until mainnet "
            "is explicitly cleared. NEXA_VOUCHERS_ALLOW_MAINNET defaults off."
        )
    return network


def address_network(address: str) -> str | None:
    if not address:
        return None
    # Check the testnet prefix first. "nexatest:" also starts with "nexa".
    if address.startswith("nexatest:"):
        return TESTNET
    if address.startswith("nexa:"):
        return MAINNET
    return None


def assert_address_for_network(address: str, network: str, *, allow_empty: bool = False) -> str:
    address = (address or "").strip()
    network = resolve_network(network)
    if not address:
        if allow_empty:
            return ""
        raise NexaRpcError("A Nexa address is required.")
    seen = address_network(address)
    if seen != network:
        expected = "nexatest:" if network == TESTNET else "nexa:"
        raise NexaRpcError(f"Address must use the {network} prefix ({expected}).")
    return address


def public_error(exc: BaseException) -> str:
    text = _USERINFO_RE.sub("//", str(exc))
    text = _SECRET_RE.sub(r"\1\2[redacted]", text)
    return text[:500]


def build_tdpp(host: str, action: str, chain: str, **params: str) -> str:
    query = {"chain": chain, "topic": TOPIC}
    for key, value in params.items():
        if value is not None and value != "":
            query[key] = value
    return f"tdpp://{host}/{action}?{urllib.parse.urlencode(query)}"


def build_wallet_carrier(action: str, chain: str, **params: str) -> str:
    """HTTPS carrier described by the public Nexa delegated-payment spec.

    ``w.nexa.org`` is part of the Nexa project documentation. This plugin does not
    operate that host.
    """
    query = {"chain": chain, "topic": TOPIC}
    for key, value in params.items():
        if value is not None and value != "":
            query[key] = value
    return f"https://w.nexa.org/tdpp/{action}?{urllib.parse.urlencode(query)}"


def _strip_userinfo(url: str) -> tuple[str, str, str]:
    parts = urllib.parse.urlsplit(url)
    username = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    return clean, username, password


class NexaClient:
    """Small JSON-RPC client. Credentials are kept off exception strings."""

    def __init__(
        self,
        *,
        network: str,
        rpc_url: str,
        rpc_user: str = "",
        rpc_password: str = "",
        rostrum_url: str = "",
        timeout: float = 20,
    ):
        self.network = resolve_network(network)
        clean_url, embedded_user, embedded_password = _strip_userinfo(rpc_url or "")
        self.rpc_url = clean_url
        self.rpc_user = rpc_user or embedded_user
        self.rpc_password = rpc_password or embedded_password
        clean_rostrum, _, _ = _strip_userinfo(rostrum_url or "")
        self.rostrum_url = clean_rostrum
        self.timeout = timeout

    @classmethod
    def from_env(cls, network: str) -> NexaClient:
        return cls(
            network=network,
            rpc_url=os.environ.get("NEXA_VOUCHERS_RPC_URL", ""),
            rpc_user=os.environ.get("NEXA_VOUCHERS_RPC_USER", ""),
            rpc_password=os.environ.get("NEXA_VOUCHERS_RPC_PASSWORD", ""),
            rostrum_url=os.environ.get("NEXA_VOUCHERS_ROSTRUM_URL", ""),
        )

    def rpc(self, method: str, params: list):
        return self._post(self.rpc_url, method, params, auth=True)

    def rostrum(self, method: str, params: list):
        if not self.rostrum_url:
            raise NexaRpcError("NEXA_VOUCHERS_ROSTRUM_URL is not set.")
        return self._post(self.rostrum_url, method, params, auth=False)

    def _post(self, url: str, method: str, params: list, *, auth: bool):
        if not url:
            raise NexaRpcError("NEXA_VOUCHERS_RPC_URL is not set.")
        payload = json.dumps(
            {"jsonrpc": "1.0", "id": "pretix-nexa-vouchers", "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        if auth and (self.rpc_user or self.rpc_password):
            token = base64.b64encode(f"{self.rpc_user}:{self.rpc_password}".encode()).decode()
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise NexaRpcError(f"Nexa RPC {method} failed with HTTP {exc.code}.") from None
        except urllib.error.URLError as exc:
            raise NexaRpcError(public_error(NexaRpcError(f"Nexa RPC {method} could not connect: {exc.reason}"))) from None
        except NexaRpcError:
            raise
        except Exception:
            raise NexaRpcError(f"Nexa RPC {method} failed.") from None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise NexaRpcError(f"Nexa RPC {method} returned a non-JSON body.") from None
        if parsed.get("error"):
            error = parsed["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise NexaRpcError(public_error(NexaRpcError(f"Nexa RPC {method} error: {message}")))
        return parsed.get("result")

    def create_group(self, *, address: str, ticker: str, name: str, document_url: str, document_hash: str):
        result = self.rpc(
            "token",
            ["new", address, ticker, name, document_url, document_hash, 0],
        )
        return _as_group(result), _as_txid(result)

    def subgroup(self, parent_group_id: str, data_hex: str) -> str:
        data = data_hex if data_hex.startswith("0x") else f"0x{data_hex}"
        result = self.rpc("token", ["subgroup", parent_group_id, data])
        return _as_group(result)

    def mint(self, group_id: str, address: str, quantity: int = 1) -> str:
        result = self.rpc("token", ["mint", group_id, address, int(quantity)])
        return _as_txid(result)

    def melt(self, group_id: str, quantity: int = 1) -> str:
        result = self.rpc("token", ["melt", group_id, int(quantity)])
        return _as_txid(result)

    def announce_marker(self, subgroup_id: str, public_id: str) -> str:
        """Publish an issuer OP_RETURN marker when the NFT is no longer in the issuer wallet.

        Nexa cannot melt a token the issuer wallet does not hold. The marker is the
        on-chain record pretix checks. A holder-signed send to the burn address is
        preferred when Wally can do it; see docs/wally-integration.md.
        """
        raw = f"NXVC:{public_id}:{subgroup_id}".encode()
        unsigned = self.rpc("createrawtransaction", [[], [{"data": raw.hex()}]])
        signed = self.rpc("signrawtransaction", [unsigned])
        if isinstance(signed, dict):
            if signed.get("complete") is False:
                raise NexaRpcError("The issuer wallet could not sign the redemption marker.")
            hex_tx = signed.get("hex") or ""
        else:
            hex_tx = str(signed)
        if not hex_tx:
            raise NexaRpcError("The issuer wallet returned an empty signed transaction.")
        result = self.rpc("sendrawtransaction", [hex_tx])
        return _as_txid(result)

    def verify_message(self, address: str, signature: str, message: str) -> bool:
        result = self.rpc("verifymessage", [address, signature, message])
        return bool(result)

    def token_unspent(self, address: str, token_id: str) -> list:
        if not address or not token_id:
            return []
        result = self.rostrum("token.address.listunspent", [address, None, token_id])
        if isinstance(result, dict):
            rows = result.get("unspent") or result.get("utxos") or result.get("result") or []
        else:
            rows = result or []
        return list(rows)

    def holds_token(self, address: str, token_id: str) -> bool:
        return len(self.token_unspent(address, token_id)) > 0


def _as_group(result) -> str:
    if isinstance(result, dict):
        for key in ("groupIdentifier", "groupId", "group_id", "subgroup", "group", "result"):
            if result.get(key):
                return str(result[key])
        raise NexaRpcError("The token RPC did not return a group id.")
    if result is None or str(result).strip() == "":
        raise NexaRpcError("The token RPC did not return a group id.")
    return str(result).strip()


def _as_txid(result) -> str:
    if isinstance(result, dict):
        for key in ("txid", "transaction", "txidem", "tx_hash"):
            if result.get(key):
                return str(result[key])
        return ""
    return "" if result is None else str(result)


def get_client(event):
    """Return the client for an event.

    Tests inject ``settings.NEXA_VOUCHERS_CLIENT_FACTORY``. Production reads the
    network from the event and the node URL from the environment.
    """
    from django.conf import settings

    requested = event.settings.get("nexa_vouchers_network", default=TESTNET) or TESTNET
    network = resolve_network(str(requested))
    factory = getattr(settings, "NEXA_VOUCHERS_CLIENT_FACTORY", None)
    if factory is not None:
        return factory(network)
    return NexaClient.from_env(network)
