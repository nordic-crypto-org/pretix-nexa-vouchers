# Copyright (C) 2026 Nordic Crypto
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from functools import wraps

import segno
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from pretix.control.permissions import EventPermissionRequiredMixin

from pretix_nexa_vouchers.forms import EventNexaSettingsForm, VoucherListForm
from pretix_nexa_vouchers.models import IssuedVoucher, QueueEntry, TokenSeries, VoucherList
from pretix_nexa_vouchers.nexa import mainnet_allowed
from pretix_nexa_vouchers.services import (
    PluginError,
    absolute_url,
    assign_next,
    claim_messages,
    confirm_transfer,
    entry_payload,
    handover_links,
    join_links,
    join_queue,
    perform_claim,
    present_links,
    queue_position,
    queue_token,
    redeem_url,
    set_entry_address,
    source_url,
    token_matches,
)


def _wants_json(request) -> bool:
    if request.GET.get("format") == "json":
        return True
    if "application/json" in request.headers.get("Accept", ""):
        return True
    return bool(request.content_type and "application/json" in request.content_type)


def _body(request) -> dict:
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise PluginError("The request body is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise PluginError("The JSON body must be an object.")
        return data
    return request.POST


def _presented_token(request, data) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    header_token = request.headers.get("X-Nexa-Queue-Token", "")
    if header_token:
        return header_token.strip()
    if isinstance(data, dict) and data.get("token"):
        return str(data.get("token"))
    return ""


def _require_organiser(request, data):
    if not token_matches(request.event, _presented_token(request, data)):
        raise PluginError("The queue organiser token was not accepted.", status=403)


def api_view(func):
    @csrf_exempt
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except PluginError as exc:
            if _wants_json(request):
                return JsonResponse({"error": str(exc)}, status=exc.status)
            messages.error(request, str(exc))
            referer = request.META.get("HTTP_REFERER")
            if referer:
                return redirect(referer)
            return HttpResponse(str(exc), status=exc.status)

    return wrapper


def _list_or_404(request, slug) -> VoucherList:
    return get_object_or_404(VoucherList, event=request.event, slug=slug)


def _issued_or_404(request, public_id) -> IssuedVoucher:
    try:
        return IssuedVoucher.objects.get(event=request.event, public_id=public_id)
    except (IssuedVoucher.DoesNotExist, ValueError, ValidationError):
        raise Http404("Voucher token not found.") from None


def _entry_or_404(voucher_list, public_id) -> QueueEntry:
    try:
        return voucher_list.entries.get(public_id=public_id)
    except (QueueEntry.DoesNotExist, ValueError, ValidationError):
        raise Http404("Queue entry not found.") from None


class IndexView(EventPermissionRequiredMixin, View):
    permission = "event.vouchers:read"

    def get(self, request, organizer, event):
        lists = VoucherList.objects.filter(event=request.event)
        issued = IssuedVoucher.objects.filter(event=request.event).select_related("voucher", "order")[:100]
        return render(
            request,
            "pretix_nexa_vouchers/control/index.html",
            {
                "lists": lists,
                "issued": issued,
                "mainnet_allowed": mainnet_allowed(),
                "network": request.event.settings.nexa_vouchers_network or "nexatest",
            },
        )


class SettingsView(EventPermissionRequiredMixin, View):
    permission = "event.settings.general:write"

    def get(self, request, organizer, event):
        return self._render(request, EventNexaSettingsForm(request.event))

    def post(self, request, organizer, event):
        form = EventNexaSettingsForm(request.event, data=request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("Nexa voucher settings saved."))
            return redirect(
                reverse(
                    "plugins:pretix_nexa_vouchers:settings",
                    kwargs={"organizer": request.organizer.slug, "event": request.event.slug},
                )
            )
        return self._render(request, form)

    def _render(self, request, form):
        return render(
            request,
            "pretix_nexa_vouchers/control/settings.html",
            {
                "form": form,
                "queue_token": queue_token(request.event),
                "mainnet_allowed": mainnet_allowed(),
            },
        )


class ListCreateView(EventPermissionRequiredMixin, View):
    permission = "event.vouchers:write"

    def get(self, request, organizer, event):
        return render(
            request,
            "pretix_nexa_vouchers/control/list_form.html",
            {"form": VoucherListForm(request.event)},
        )

    def post(self, request, organizer, event):
        form = VoucherListForm(request.event, data=request.POST)
        if form.is_valid():
            voucher_list = form.save()
            return redirect(
                reverse(
                    "plugins:pretix_nexa_vouchers:list.detail",
                    kwargs={
                        "organizer": request.organizer.slug,
                        "event": request.event.slug,
                        "slug": voucher_list.slug,
                    },
                )
            )
        return render(request, "pretix_nexa_vouchers/control/list_form.html", {"form": form})


class ListDetailView(EventPermissionRequiredMixin, View):
    permission = "event.vouchers:read"

    def get(self, request, organizer, event, slug):
        voucher_list = _list_or_404(request, slug)
        entries = []
        for entry in voucher_list.entries.select_related("issued"):
            payload = entry_payload(entry, include_handover=True)
            payload["position"] = queue_position(entry)
            entries.append(payload)
        return render(
            request,
            "pretix_nexa_vouchers/control/list_detail.html",
            {
                "voucher_list": voucher_list,
                "entries": entries,
                "join": join_links(voucher_list),
                "can_assign": request.user.has_event_permission(
                    request.organizer, request.event, "event.vouchers:write", request=request
                ),
            },
        )


class AssignNextView(EventPermissionRequiredMixin, View):
    permission = "event.vouchers:write"

    def post(self, request, organizer, event, slug):
        voucher_list = _list_or_404(request, slug)
        try:
            entry = assign_next(voucher_list)
        except PluginError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Assigned the next voucher."))
            if _wants_json(request):
                return JsonResponse(entry_payload(entry, include_handover=True))
        return redirect(
            reverse(
                "plugins:pretix_nexa_vouchers:list.detail",
                kwargs={"organizer": request.organizer.slug, "event": request.event.slug, "slug": slug},
            )
        )


def public_lists(request, **kwargs):
    lists = VoucherList.objects.filter(event=request.event, active=True)
    if _wants_json(request):
        return JsonResponse(
            {
                "lists": [
                    {"slug": row.slug, "name": row.name, "description": row.description}
                    for row in lists
                ]
            }
        )
    return render(
        request,
        "pretix_nexa_vouchers/presale/lists.html",
        {"lists": lists, "source_url": source_url(request.event)},
    )


def public_list(request, slug, **kwargs):
    voucher_list = _list_or_404(request, slug)
    waiting = voucher_list.entries.filter(status=QueueEntry.STATUS_WAITING).count()
    links = join_links(voucher_list)
    if _wants_json(request):
        return JsonResponse(
            {
                "slug": voucher_list.slug,
                "name": voucher_list.name,
                "description": voucher_list.description,
                "waiting": waiting,
                "active": voucher_list.active,
                **links,
            }
        )
    return render(
        request,
        "pretix_nexa_vouchers/presale/list.html",
        {
            "voucher_list": voucher_list,
            "waiting": waiting,
            "links": links,
            "source_url": source_url(request.event),
        },
    )


@api_view
def queue_join(request, slug, **kwargs):
    if request.method != "POST":
        return HttpResponseBadRequest("POST a display name to join.")
    voucher_list = _list_or_404(request, slug)
    data = _body(request)
    entry = join_queue(voucher_list, data.get("display_name") or request.POST.get("display_name", ""), data.get("nexa_address", ""))
    payload = entry_payload(entry)
    payload["status_url"] = absolute_url(
        request.event,
        "plugins:pretix_nexa_vouchers:queue.entry",
        slug=voucher_list.slug,
        public_id=entry.public_id,
    )
    if _wants_json(request):
        return JsonResponse(payload, status=201)
    return redirect(payload["status_url"])


def queue_entry(request, slug, public_id, **kwargs):
    voucher_list = _list_or_404(request, slug)
    entry = _entry_or_404(voucher_list, public_id)
    payload = entry_payload(entry, include_handover=False)
    if _wants_json(request):
        return JsonResponse(payload)
    claim = None
    if entry.issued_id and entry.issued.subgroup_id:
        claim = present_links(entry.issued)
    return render(
        request,
        "pretix_nexa_vouchers/presale/entry.html",
        {
            "voucher_list": voucher_list,
            "entry": entry,
            "payload": payload,
            "claim": claim,
            "source_url": source_url(request.event),
        },
    )


@api_view
def queue_entry_address(request, slug, public_id, **kwargs):
    if request.method != "POST":
        return HttpResponseBadRequest("POST a nexa_address.")
    voucher_list = _list_or_404(request, slug)
    entry = _entry_or_404(voucher_list, public_id)
    data = _body(request)
    entry = set_entry_address(entry, data.get("nexa_address", ""))
    return JsonResponse(entry_payload(entry))


@api_view
def api_assign_next(request, slug, **kwargs):
    if request.method != "POST":
        return HttpResponseBadRequest("POST to assign the next voucher.")
    data = _body(request)
    _require_organiser(request, data)
    voucher_list = _list_or_404(request, slug)
    entry = assign_next(voucher_list)
    return JsonResponse(entry_payload(entry, include_handover=True))


@api_view
def api_confirm_transfer(request, slug, **kwargs):
    if request.method != "POST":
        return HttpResponseBadRequest("POST the transfer txid.")
    data = _body(request)
    _require_organiser(request, data)
    voucher_list = _list_or_404(request, slug)
    public_id = data.get("entry") or data.get("public_id")
    if not public_id:
        raise PluginError("entry is required.")
    entry = _entry_or_404(voucher_list, public_id)
    entry = confirm_transfer(entry, data.get("txid", ""))
    return JsonResponse(entry_payload(entry, include_handover=True))


@api_view
def claim(request, public_id, **kwargs):
    issued = _issued_or_404(request, public_id)
    links = present_links(issued)
    if request.method == "GET":
        payload = {
            "public_id": str(issued.public_id),
            "subgroup_id": issued.subgroup_id,
            "status": issued.status,
            "network": request.event.settings.nexa_vouchers_network or "nexatest",
            **links,
            "messages": claim_messages(issued),
        }
        if _wants_json(request):
            return JsonResponse(payload)
        return render(
            request,
            "pretix_nexa_vouchers/presale/claim.html",
            {"issued": issued, "links": links, "source_url": source_url(request.event), "result": None},
        )
    if request.method != "POST":
        return HttpResponseBadRequest("POST address and signature.")
    data = _body(request)
    code = perform_claim(issued, data.get("address", ""), data.get("signature", ""))
    result = {
        "voucher_code": code,
        "redeem_url": redeem_url(request.event, code),
        "subgroup_id": issued.subgroup_id,
    }
    if _wants_json(request):
        return JsonResponse(result)
    return render(
        request,
        "pretix_nexa_vouchers/presale/claim.html",
        {"issued": issued, "links": links, "source_url": source_url(request.event), "result": result},
    )


def token_document(request, public_id, **kwargs):
    try:
        series = TokenSeries.objects.get(event=request.event, public_id=public_id)
    except (TokenSeries.DoesNotExist, ValueError, ValidationError):
        raise Http404("Token document not found.") from None
    if not series.document_body:
        raise Http404("Token document not found.")
    return HttpResponse(series.document_body, content_type="application/json")


def qr_svg(request, **kwargs):
    kind = request.GET.get("kind", "")
    target = ""
    if kind == "claim":
        issued = _issued_or_404(request, request.GET.get("id", ""))
        target = absolute_url(request.event, "plugins:pretix_nexa_vouchers:claim", public_id=issued.public_id)
    elif kind == "tdpp":
        issued = _issued_or_404(request, request.GET.get("id", ""))
        target = present_links(issued)["voucher_present_uri"]
    elif kind == "send":
        voucher_list = _list_or_404(request, request.GET.get("list", ""))
        entry = _entry_or_404(voucher_list, request.GET.get("entry", ""))
        if not entry.issued_id:
            raise Http404("Nothing to send.")
        target = handover_links(entry)["token_send_uri"]
    elif kind == "join":
        voucher_list = _list_or_404(request, request.GET.get("list", ""))
        target = join_links(voucher_list)["queue_join_uri"]
    else:
        return HttpResponseBadRequest("Unknown QR kind.")
    svg = segno.make(target, error="m").svg_inline(scale=4)
    return HttpResponse(svg, content_type="image/svg+xml")
