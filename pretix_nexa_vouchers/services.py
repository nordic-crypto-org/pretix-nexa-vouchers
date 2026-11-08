# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Voucher
from pretix.base.models.vouchers import generate_code
from pretix.base.services.orders import OrderError
from pretix.multidomain.urlreverse import eventreverse_absolute

from pretix_nexa_vouchers.models import IssuedVoucher, QueueEntry, TokenSeries, VoucherList, VoucherProduct
from pretix_nexa_vouchers.nexa import (
    OPERATOR,
    MainnetNotCleared,
    NexaRpcError,
    assert_address_for_network,
    build_tdpp,
    build_wallet_carrier,
    get_client,
    public_error,
    resolve_network,
)

logger = logging.getLogger("pretix_nexa_vouchers")

MAX_VOUCHERS = 1000


class PluginError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def network_for_event(event) -> str:
    requested = event.settings.get("nexa_vouchers_network", default="nexatest") or "nexatest"
    return resolve_network(str(requested))


def issuer_address(event) -> str:
    return (event.settings.get("nexa_vouchers_issuer_address", default="") or "").strip()


def burn_address(event) -> str:
    return (event.settings.get("nexa_vouchers_burn_address", default="") or "").strip()


def source_url(event) -> str:
    return (event.settings.get("nexa_vouchers_source_url", default="") or "").strip()


def queue_token(event, *, rotate: bool = False) -> str:
    current = event.settings.get("nexa_vouchers_queue_token", default="") or ""
    if rotate or not current:
        current = secrets.token_urlsafe(32)
        event.settings.nexa_vouchers_queue_token = current
    return current


def token_matches(event, presented: str) -> bool:
    expected = queue_token(event)
    if not presented or not expected:
        return False
    return secrets.compare_digest(presented, expected)


def issue_for_order(order) -> None:
    with scope(organizer=order.event.organizer):
        _issue_for_order(order)


def mark_paid_order(order) -> None:
    with scope(organizer=order.event.organizer):
        _mark_paid_order(order)


def retry_pending(limit: int = 50) -> None:
    with scopes_disabled():
        rows = list(
            IssuedVoucher.objects.filter(
                status__in=[
                    IssuedVoucher.STATUS_CHAIN_PENDING,
                    IssuedVoucher.STATUS_BLOCKED,
                    IssuedVoucher.STATUS_REDEEM_PENDING,
                ]
            ).values_list("id", "event__organizer_id")[:limit]
        )
    for pk, organizer_id in rows:
        with scope(organizer=organizer_id):
            issued = (
                IssuedVoucher.objects.select_related("product", "product__item", "order", "event", "voucher", "series")
                .filter(pk=pk)
                .first()
            )
            if issued is None:
                continue
            try:
                if issued.status == IssuedVoucher.STATUS_REDEEM_PENDING:
                    _finish_redeem(issued)
                else:
                    _finish_chain(issued)
            except Exception:
                logger.exception("Retry failed for issued voucher %s", issued.public_id)


def assert_voucher_redeemable(event, positions) -> None:
    """Block checkout when a linked token is already marked or not on chain yet."""
    if not positions:
        return
    for position in positions:
        voucher = getattr(position, "voucher", None)
        if voucher is None:
            continue
        issued = IssuedVoucher.objects.filter(event=event, voucher=voucher).first()
        if issued is None:
            continue
        if issued.status in {
            IssuedVoucher.STATUS_REDEEMED,
            IssuedVoucher.STATUS_REDEEM_PENDING,
            IssuedVoucher.STATUS_BLOCKED,
            IssuedVoucher.STATUS_FAILED,
        } or issued.redeem_txid:
            raise OrderError("This voucher's Nexa token is not available to redeem.")
        if not issued.subgroup_id or issued.status == IssuedVoucher.STATUS_CHAIN_PENDING:
            raise OrderError("This voucher's Nexa token is not issued on the network yet.")
        if not _position_allowed(issued, position):
            raise OrderError("This voucher cannot be used for the selected product.")


def join_queue(voucher_list: VoucherList, display_name: str, nexa_address: str = "") -> QueueEntry:
    if not voucher_list.active:
        raise PluginError("This list is closed.", status=409)
    name = (display_name or "").strip()
    if not name or len(name) > 80:
        raise PluginError("Enter a display name of at most 80 characters.")
    network = network_for_event(voucher_list.event)
    address = (nexa_address or "").strip()
    if address:
        try:
            address = assert_address_for_network(address, network)
        except (NexaRpcError, MainnetNotCleared) as exc:
            raise PluginError(str(exc)) from exc
    return QueueEntry.objects.create(
        voucher_list=voucher_list,
        display_name=name,
        nexa_address=address,
    )


def set_entry_address(entry: QueueEntry, nexa_address: str) -> QueueEntry:
    if entry.status != QueueEntry.STATUS_WAITING:
        raise PluginError("Only a waiting entry can change its address.", status=409)
    network = network_for_event(entry.voucher_list.event)
    try:
        address = assert_address_for_network(nexa_address, network)
    except (NexaRpcError, MainnetNotCleared) as exc:
        raise PluginError(str(exc)) from exc
    entry.nexa_address = address
    entry.save(update_fields=["nexa_address"])
    return entry


def queue_position(entry: QueueEntry) -> int | None:
    if entry.status != QueueEntry.STATUS_WAITING:
        return None
    ahead = QueueEntry.objects.filter(voucher_list_id=entry.voucher_list_id, status=QueueEntry.STATUS_WAITING).filter(
        Q(created__lt=entry.created) | Q(created=entry.created, pk__lt=entry.pk)
    ).count()
    return ahead + 1


def assign_next(voucher_list: VoucherList) -> QueueEntry:
    with transaction.atomic():
        entry = (
            QueueEntry.objects.select_for_update()
            .filter(voucher_list=voucher_list, status=QueueEntry.STATUS_WAITING)
            .order_by("created", "id")
            .first()
        )
        if entry is None:
            raise PluginError("The queue is empty.", status=409)
        if not entry.nexa_address:
            raise PluginError(
                "The next person has no Nexa address yet, so they keep their place.",
                status=409,
            )
        candidates = IssuedVoucher.objects.select_for_update().filter(
            event=voucher_list.event,
            status=IssuedVoucher.STATUS_ISSUED,
            queue_entry__isnull=True,
        ).exclude(subgroup_id="")
        if voucher_list.organiser_address:
            held = candidates.filter(holder_address=voucher_list.organiser_address)
            if held.exists():
                candidates = held
        issued = candidates.order_by("id").first()
        if issued is None:
            raise PluginError("No issued token is waiting to be handed out.", status=409)
        entry.issued = issued
        entry.status = QueueEntry.STATUS_ASSIGNED
        entry.assigned_at = now()
        entry.save(update_fields=["issued", "status", "assigned_at"])
        issued.status = IssuedVoucher.STATUS_ASSIGNED
        issued.save(update_fields=["status"])
        return entry


def confirm_transfer(entry: QueueEntry, txid: str, client=None) -> QueueEntry:
    if entry.status != QueueEntry.STATUS_ASSIGNED or entry.issued_id is None:
        raise PluginError("This entry has no assigned voucher.", status=409)
    if not entry.nexa_address:
        raise PluginError("This entry has no Nexa address.", status=409)
    txid = (txid or "").strip()
    if not txid:
        raise PluginError("A transaction id is required.")
    issued = entry.issued
    client = client or get_client(entry.voucher_list.event)
    if not client.holds_token(entry.nexa_address, issued.subgroup_id):
        raise PluginError("The token is not at the recipient address yet.", status=409)
    _rotate_code(issued, entry.nexa_address, txid)
    entry.status = QueueEntry.STATUS_CLAIMED
    entry.save(update_fields=["status"])
    return entry


def claim_messages(issued: IssuedVoucher, moment: datetime | None = None) -> list[str]:
    moment = moment or datetime.now(timezone.utc)
    hours = [moment, moment - timedelta(hours=1)]
    network = network_for_event(issued.event)
    return [_claim_message(issued, hour.strftime("%Y%m%d%H"), network) for hour in hours]


def perform_claim(issued: IssuedVoucher, address: str, signature: str, client=None) -> str:
    if issued.status in {IssuedVoucher.STATUS_REDEEMED, IssuedVoucher.STATUS_REDEEM_PENDING} or issued.redeem_txid:
        raise PluginError("This voucher was already redeemed.", status=409)
    if not issued.subgroup_id or not issued.voucher_id:
        raise PluginError("This token is not ready.", status=409)
    network = network_for_event(issued.event)
    try:
        address = assert_address_for_network(address, network)
    except (NexaRpcError, MainnetNotCleared) as exc:
        raise PluginError(str(exc)) from exc
    client = client or get_client(issued.event)
    signed = False
    for message in claim_messages(issued):
        if client.verify_message(address, signature, message):
            signed = True
            break
    if not signed:
        raise PluginError("The wallet signature was not accepted.", status=403)
    if not client.holds_token(address, issued.subgroup_id):
        raise PluginError("This address does not hold the voucher token.", status=403)
    if issued.holder_address != address and issued.voucher.redeemed == 0:
        _rotate_code(issued, address, issued.transfer_txid)
        if issued.status in {IssuedVoucher.STATUS_ISSUED, IssuedVoucher.STATUS_ASSIGNED}:
            issued.status = IssuedVoucher.STATUS_TRANSFERRED
            issued.save(update_fields=["status"])
    issued.refresh_from_db()
    return issued.voucher.code


def entry_payload(entry: QueueEntry, *, include_handover: bool = False) -> dict:
    issued = entry.issued if entry.issued_id else None
    assigned = None
    if issued is not None:
        assigned = {
            "public_id": str(issued.public_id),
            "subgroup_id": issued.subgroup_id,
            "status": issued.status,
            "claim_url": absolute_url(issued.event, "plugins:pretix_nexa_vouchers:claim", public_id=issued.public_id),
        }
        if include_handover and entry.nexa_address and issued.subgroup_id:
            assigned.update(handover_links(entry))
    return {
        "public_id": str(entry.public_id),
        "display_name": entry.display_name,
        "status": entry.status,
        "position": queue_position(entry),
        "nexa_address": entry.nexa_address,
        "list": entry.voucher_list.slug,
        "assigned_voucher": assigned,
    }


def handover_links(entry: QueueEntry) -> dict:
    issued = entry.issued
    event = entry.voucher_list.event
    network = network_for_event(event)
    host = _host(event)
    callback = absolute_url(event, "plugins:pretix_nexa_vouchers:api.confirm", slug=entry.voucher_list.slug)
    params = {
        "group": issued.subgroup_id,
        "addr": entry.nexa_address,
        "qty": "1",
        "entry": str(entry.public_id),
        "list": entry.voucher_list.slug,
        "callback": callback,
    }
    return {
        "token_send_uri": build_tdpp(host, "token-send", network, **params),
        "wallet_url": build_wallet_carrier("token-send", network, **params),
        "callback_url": callback,
    }


def present_links(issued: IssuedVoucher) -> dict:
    event = issued.event
    network = network_for_event(event)
    host = _host(event)
    callback = absolute_url(event, "plugins:pretix_nexa_vouchers:claim", public_id=issued.public_id)
    message = claim_messages(issued)[0]
    params = {
        "group": issued.subgroup_id,
        "callback": callback,
        "msg": message,
    }
    return {
        "voucher_present_uri": build_tdpp(host, "voucher-present", network, **params),
        "wallet_url": build_wallet_carrier("voucher-present", network, **params),
        "callback_url": callback,
        "message": message,
        "messages": claim_messages(issued),
    }


def join_links(voucher_list: VoucherList) -> dict:
    event = voucher_list.event
    network = network_for_event(event)
    host = _host(event)
    callback = absolute_url(event, "plugins:pretix_nexa_vouchers:public.list", slug=voucher_list.slug)
    params = {"list": voucher_list.slug, "callback": callback}
    return {
        "queue_join_uri": build_tdpp(host, "queue-join", network, **params),
        "wallet_url": build_wallet_carrier("queue-join", network, **params),
        "callback_url": callback,
    }


def redeem_url(event, code: str) -> str:
    base = eventreverse_absolute(event, "presale:event.index")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'voucher': code})}"


def absolute_url(event, name: str, **kwargs) -> str:
    return eventreverse_absolute(event, name, kwargs=kwargs)


def canonical_document(series: TokenSeries) -> bytes:
    payload = {
        "event": series.event.slug,
        "item": series.product.item_id,
        "name": series.token_name,
        "network": "see-event-setting",
        "operator": OPERATOR,
        "series": str(series.public_id),
        "standard": "pretix-nexa-vouchers/1",
        "ticker": series.ticker,
    }
    # The network is stored beside the body so a retry hashes the same bytes.
    payload["network"] = network_for_event(series.event)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _issue_for_order(order) -> None:
    try:
        network = network_for_event(order.event)
    except MainnetNotCleared as exc:
        _block_order(order, str(exc))
        return
    positions = order.positions.filter(canceled=False).select_related("item")
    processed = 0
    for position in positions:
        config = VoucherProduct.objects.filter(item_id=position.item_id, enabled=True).first()
        if config is None:
            continue
        count = min(config.voucher_count or 0, MAX_VOUCHERS)
        for index in range(count):
            try:
                issued = _shell_for(order, position, config, index, network)
            except (NexaRpcError, MainnetNotCleared) as exc:
                IssuedVoucher.objects.get_or_create(
                    order_position=position,
                    index=index,
                    defaults={
                        "event": order.event,
                        "order": order,
                        "product": config,
                        "status": IssuedVoucher.STATUS_CHAIN_PENDING,
                        "error": public_error(exc),
                        "allowed_item_ids": [],
                        "allowed_quota_ids": [],
                    },
                )
                processed += 1
                continue
            processed += 1
            if issued.status in {
                IssuedVoucher.STATUS_ISSUED,
                IssuedVoucher.STATUS_ASSIGNED,
                IssuedVoucher.STATUS_TRANSFERRED,
                IssuedVoucher.STATUS_REDEEMED,
                IssuedVoucher.STATUS_BLOCKED,
            }:
                continue
            _finish_chain(issued)
    if processed:
        order.log_action(
            "pretix_nexa_vouchers.order.processed",
            data={"order": order.code, "rows": processed},
        )


def _block_order(order, message: str) -> None:
    for position in order.positions.filter(canceled=False):
        config = VoucherProduct.objects.filter(item_id=position.item_id, enabled=True).first()
        if config is None:
            continue
        count = min(config.voucher_count or 0, MAX_VOUCHERS)
        for index in range(count):
            IssuedVoucher.objects.get_or_create(
                order_position=position,
                index=index,
                defaults={
                    "event": order.event,
                    "order": order,
                    "product": config,
                    "status": IssuedVoucher.STATUS_BLOCKED,
                    "error": message,
                    "allowed_item_ids": [],
                    "allowed_quota_ids": [],
                },
            )


def _shell_for(order, position, config, index: int, network: str) -> IssuedVoucher:
    existing = IssuedVoucher.objects.filter(order_position=position, index=index).first()
    if existing:
        return existing
    item_ids = list(config.target_items.values_list("pk", flat=True))
    quota_ids = list(config.target_quotas.values_list("pk", flat=True))
    holder, issuer = _holder_for(position, config, order.event, network)
    with transaction.atomic():
        voucher = _create_pretix_voucher(order, config, item_ids, quota_ids)
        return IssuedVoucher.objects.create(
            event=order.event,
            order=order,
            order_position=position,
            product=config,
            voucher=voucher,
            index=index,
            holder_address=holder,
            issuer_address=issuer,
            status=IssuedVoucher.STATUS_CHAIN_PENDING,
            allowed_item_ids=item_ids,
            allowed_quota_ids=quota_ids,
        )


def _create_pretix_voucher(order, config, item_ids, quota_ids) -> Voucher:
    kwargs = {
        "event": order.event,
        "code": generate_code(prefix="NX"),
        "max_usages": config.max_usages or 1,
        "tag": config.tag or "nexa",
        "comment": f"Issued by pretix-nexa-vouchers for order {order.code}.",
        "price_mode": config.price_mode or "none",
        "value": None if config.price_mode in (None, "", "none") else config.value,
        "valid_until": _deadline(config),
        "allow_ignore_quota": config.allow_ignore_quota,
        "block_quota": False,
        "show_hidden_items": True,
    }
    if len(item_ids) == 1 and not quota_ids:
        kwargs["item_id"] = item_ids[0]
        kwargs["block_quota"] = config.block_quota
    elif len(quota_ids) == 1 and not item_ids:
        kwargs["quota_id"] = quota_ids[0]
        kwargs["block_quota"] = config.block_quota
    return Voucher.objects.create(**kwargs)


def _deadline(config):
    moment = now()
    absolute = config.valid_until
    dynamic = moment + timedelta(days=config.validity_days) if config.validity_days else None
    if absolute and dynamic:
        return min(absolute, dynamic)
    return absolute or dynamic


def _holder_for(position, config, event, network: str) -> tuple[str, str]:
    issuer = issuer_address(event)
    holder = issuer
    if config.holder_mode == VoucherProduct.HOLDER_BUYER and config.address_question_id:
        answer = position.answers.filter(question_id=config.address_question_id).values_list("answer", flat=True).first()
        if answer and str(answer).strip():
            holder = str(answer).strip()
    if holder:
        holder = assert_address_for_network(holder, network)
    if issuer:
        issuer = assert_address_for_network(issuer, network)
    return holder, issuer


def _finish_chain(issued: IssuedVoucher) -> None:
    if issued.subgroup_id and issued.issuance_txid and issued.status == IssuedVoucher.STATUS_CHAIN_PENDING:
        issued.status = IssuedVoucher.STATUS_ISSUED
        issued.error = ""
        issued.save(update_fields=["status", "error"])
        return
    try:
        network = network_for_event(issued.event)
        if not issued.issuer_address:
            raise NexaRpcError("Set the issuer address in the Nexa voucher settings.")
        assert_address_for_network(issued.issuer_address, network)
        if not issued.holder_address:
            raise NexaRpcError("No holder address is available for this voucher.")
        assert_address_for_network(issued.holder_address, network)
        client = get_client(issued.event)
        series = _ensure_series(client, issued.product, issued.event)
        issued.series = series
        issued.parent_group_id = series.parent_group_id
        if not issued.commitment:
            issued.commitment = hashlib.sha256(str(issued.public_id).encode()).hexdigest()
        if not issued.subgroup_id:
            issued.subgroup_id = client.subgroup(series.parent_group_id, issued.commitment)
        issued.save()
        if not issued.issuance_txid:
            issued.issuance_txid = client.mint(issued.subgroup_id, issued.holder_address, 1)
        issued.status = IssuedVoucher.STATUS_ISSUED
        issued.error = ""
        issued.save()
    except MainnetNotCleared as exc:
        issued.status = IssuedVoucher.STATUS_BLOCKED
        issued.error = public_error(exc)
        issued.save(update_fields=["status", "error"])
    except Exception as exc:
        logger.exception("Nexa issuance failed for %s", issued.public_id)
        issued.status = IssuedVoucher.STATUS_CHAIN_PENDING
        issued.error = public_error(exc)
        issued.save(update_fields=["status", "error", "series", "parent_group_id", "commitment", "subgroup_id"])


def _ensure_series(client, config: VoucherProduct, event) -> TokenSeries:
    with transaction.atomic():
        locked = VoucherProduct.objects.select_for_update().get(pk=config.pk)
        series = TokenSeries.objects.filter(product=locked).first()
        if series and series.parent_group_id:
            return series
        if series is None:
            series = TokenSeries.objects.create(
                event=event,
                product=locked,
                ticker=_ticker(locked.item_id),
                token_name=str(locked.item.name)[:64] or "Voucher",
            )
        body = canonical_document(series)
        series.document_body = body.decode()
        series.document_hash = hashlib.sha256(body).hexdigest()
        series.document_url = absolute_url(
            event,
            "plugins:pretix_nexa_vouchers:token.document",
            public_id=series.public_id,
        )
        group_id, txid = client.create_group(
            address=issuer_address(event),
            ticker=series.ticker,
            name=series.token_name,
            document_url=series.document_url,
            document_hash=series.document_hash,
        )
        series.parent_group_id = group_id
        series.genesis_txid = txid
        series.save()
        return series


def _ticker(item_id: int) -> str:
    if item_id <= 9_999_999:
        return f"V{item_id:07d}"
    return "V" + format(item_id, "x")[-7:]


def _mark_paid_order(order) -> None:
    voucher_ids = [position.voucher_id for position in order.positions.all() if position.voucher_id]
    if not voucher_ids:
        return
    issued_rows = list(IssuedVoucher.objects.filter(event=order.event, voucher_id__in=voucher_ids))
    for issued in issued_rows:
        if issued.status == IssuedVoucher.STATUS_REDEEMED:
            continue
        issued.status = IssuedVoucher.STATUS_REDEEM_PENDING
        issued.save(update_fields=["status"])
        _finish_redeem(issued)


def _finish_redeem(issued: IssuedVoucher) -> None:
    if issued.status == IssuedVoucher.STATUS_REDEEMED and issued.redeem_txid:
        return
    try:
        client = get_client(issued.event)
        txid, kind = _mark_token(client, issued, burn_address(issued.event))
    except Exception as exc:
        logger.exception("Nexa redemption marker failed for %s", issued.public_id)
        issued.status = IssuedVoucher.STATUS_REDEEM_PENDING
        issued.error = public_error(exc)
        issued.save(update_fields=["status", "error"])
        return
    issued.redeem_txid = txid
    issued.redeem_kind = kind
    issued.status = IssuedVoucher.STATUS_REDEEMED
    issued.redeemed_at = now()
    issued.error = ""
    issued.save(update_fields=["redeem_txid", "redeem_kind", "status", "redeemed_at", "error"])


def _mark_token(client, issued: IssuedVoucher, burn: str) -> tuple[str, str]:
    if burn and issued.subgroup_id and client.holds_token(burn, issued.subgroup_id):
        utxos = client.token_unspent(burn, issued.subgroup_id)
        txid = ""
        if utxos and isinstance(utxos[0], dict):
            txid = str(utxos[0].get("tx_hash") or utxos[0].get("txid") or "")
        return txid or "burn-observed", "burn-address"
    if (
        issued.issuer_address
        and issued.holder_address == issued.issuer_address
        and issued.subgroup_id
        and client.holds_token(issued.issuer_address, issued.subgroup_id)
    ):
        return client.melt(issued.subgroup_id, 1), "melt"
    return client.announce_marker(issued.subgroup_id, str(issued.public_id)), "marker"


def _rotate_code(issued: IssuedVoucher, new_holder: str, txid: str) -> None:
    if issued.voucher_id and issued.voucher.redeemed == 0 and issued.holder_address != new_holder:
        issued.voucher.code = generate_code(prefix="NX")
        issued.voucher.save(update_fields=["code"])
    issued.holder_address = new_holder
    if txid:
        issued.transfer_txid = txid
    if issued.status in {IssuedVoucher.STATUS_ISSUED, IssuedVoucher.STATUS_ASSIGNED}:
        issued.status = IssuedVoucher.STATUS_TRANSFERRED
    issued.save(update_fields=["holder_address", "transfer_txid", "status"])


def _position_allowed(issued: IssuedVoucher, position) -> bool:
    item_ids = [int(value) for value in (issued.allowed_item_ids or [])]
    quota_ids = [int(value) for value in (issued.allowed_quota_ids or [])]
    item_ok = position.item_id in item_ids if item_ids else True
    if quota_ids:
        have = set(position.item.quotas.values_list("pk", flat=True))
        quota_ok = bool(have.intersection(quota_ids))
    else:
        quota_ok = True
    if item_ids and quota_ids:
        return item_ok or quota_ok
    return item_ok and quota_ok


def _claim_message(issued: IssuedVoucher, hour: str, network: str) -> str:
    return "\n".join(
        [
            f"{OPERATOR} pretix-nexa-vouchers",
            f"claim {issued.public_id}",
            f"hour {hour}",
            f"network {network}",
        ]
    )


def _host(event) -> str:
    sample = absolute_url(event, "plugins:pretix_nexa_vouchers:public.lists")
    from urllib.parse import urlsplit

    return urlsplit(sample).netloc


def copy_product_config(source, target) -> None:
    config = VoucherProduct.objects.filter(item=source).first()
    if config is None or VoucherProduct.objects.filter(item=target).exists():
        return
    question = None
    if config.address_question_id and config.address_question.event_id == target.event_id:
        question = config.address_question
    clone = VoucherProduct.objects.create(
        item=target,
        enabled=config.enabled,
        voucher_count=config.voucher_count,
        tag=config.tag,
        validity_days=config.validity_days,
        valid_until=config.valid_until,
        block_quota=config.block_quota,
        allow_ignore_quota=config.allow_ignore_quota,
        price_mode=config.price_mode,
        value=config.value,
        max_usages=config.max_usages,
        holder_mode=config.holder_mode,
        address_question=question,
    )
    if target.event_id == source.event_id:
        clone.target_items.set(config.target_items.all())
        clone.target_quotas.set(config.target_quotas.all())

