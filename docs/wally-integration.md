# Wally integration

This plugin does not modify Wally or libnexakotlin. The queue and the claim page work in a browser today. Wally can already open an HTTPS link or show a QR code. The URIs below are the contract a future Wally build can implement so the organiser never has to copy a group id by hand.

Wally is the Nexa wallet (<https://gitlab.com/wallywallet/android>, libnexakotlin). Nordic Crypto maintains this pretix plugin. Neither this document nor the plugin claims to operate Nexa.

Use **testnet only** (`chain=nexatest`). The plugin refuses mainnet unless `NEXA_VOUCHERS_ALLOW_MAINNET` is set, and that flag is not cleared for production.

## What already works without a Wally change

- The organiser keeps voucher NFTs in the Wally wallet that corresponds to the event's issuer address, or to the list's organiser address.
- The pretix list page shows the subgroup id, the recipient address, and a QR code.
- The organiser sends quantity 1 of that subgroup with Wally's existing token-send screen.
- They paste the transaction id into pretix (or `POST` it, below). pretix asks Rostrum whether the recipient address now has an unspent output of that token. If it does, pretix rotates the voucher code.
- The recipient opens the claim HTTPS URL, signs the displayed message with the holding address (`verifymessage` on the organiser's testnet node), and pretix returns the current voucher code plus a shop URL `?voucher=`.

No private key, WIF, or mnemonic is stored in pretix. The only shared secret is the queue organiser token from the event's Nexa voucher settings.

## Organiser token

pretix shows a token on the Nexa voucher settings page. Wally should store it in the app's private settings for that event, not in a screenshot and not in a TDPP URI.

Send it on organiser calls as:

```http
Authorization: Bearer <token>
```

`X-Nexa-Queue-Token` is also accepted. Putting the token in a query string is not accepted, so it does not leak via `Referer`.

## HTTP API

All paths are under the event:

`https://<pretix>/<organizer>/<event>/nexa/…`

JSON clients should send `Accept: application/json` or `Content-Type: application/json`. HTML clients get the same pages rendered for a person.

| Method and path | Auth | Body | Result |
| --- | --- | --- | --- |
| `GET /nexa/lists/?format=json` | none |  | `{lists: [{slug, name, description}]}` |
| `GET /nexa/lists/<slug>/?format=json` | none |  | name, waiting count, `queue_join_uri`, `callback_url` |
| `POST /nexa/lists/<slug>/join` | none | `{display_name, nexa_address?}` | `201` `{public_id, position, status, status_url}` |
| `GET /nexa/lists/<slug>/entries/<public_id>?format=json` | none |  | position while waiting, assigned subgroup id, claim URL. No voucher code. |
| `POST /nexa/lists/<slug>/entries/<public_id>/address` | none (the id is unguessable) | `{nexa_address}` | updated entry, only while `waiting` |
| `POST /nexa/lists/<slug>/assign-next` | bearer | `{}` | next waiting entry that has an address, plus `token_send_uri` |
| `POST /nexa/lists/<slug>/confirm-transfer` | bearer | `{entry, txid}` | rotates the pretix code once Rostrum shows the token at `nexa_address` |
| `GET /nexa/claim/<public_id>?format=json` | none |  | message to sign, `voucher_present_uri`. No voucher code. |
| `POST /nexa/claim/<public_id>` | none | `{address, signature}` | `{voucher_code, redeem_url, subgroup_id}` after the signature and the UTXO check |

`assign-next` returns `409` when the queue is empty, when the next person has no address (they keep their place), or when no issued token is left. Tokens whose holder address equals the list's organiser address are preferred.

## TDPP-style URIs

These follow the shape of Nexa's delegated payment protocol (`tdpp://<host>/<action>?…`, and the `https://w.nexa.org/tdpp/…` carrier). They are **not signed**. Wally must show them to the user and must not auto-approve them. Signing with an entity key is future work; do not add a WIF to this repository to do it. If that work happens, the key has to come from the environment.

Common query keys: `chain=nexatest`, `topic=nexa-voucher`. Unknown keys must be ignored. `callback` is always an HTTPS URL on the pretix host.

### `queue-join`

```text
tdpp://<pretix-host>/queue-join?chain=nexatest&topic=nexa-voucher&list=<slug>&callback=<https list url>
```

Wally should open `callback` (or the in-app web view) so the person can join. It does not move coins.

### `token-send`

```text
tdpp://<pretix-host>/token-send?chain=nexatest&topic=nexa-voucher&group=<subgroup id>&addr=<nexatest:…>&qty=1&entry=<uuid>&list=<slug>&callback=<confirm url>
```

Requested Wally behaviour:

1. Resolve `group` as a Nexa group id the wallet holds, quantity exactly 1 (an NFT subgroup).
2. Build a token send of quantity 1 to `addr` on `chain`. Refuse if `chain` is anything but `nexatest` unless the user has a separate, explicit mainnet override. This plugin's own mainnet flag is not a reason for Wally to enable mainnet.
3. After broadcast, `POST callback` with `Authorization: Bearer <organiser token>`, body `{"entry": "<entry>", "txid": "<txid>"}`.
4. If the user cancels, do not call the callback.

`callback` is the confirm-transfer URL. The bearer token is the one stored for that pretix event, not a parameter of the URI.

Until this action exists, the organiser uses the subgroup id and address shown next to the QR and confirms the txid by hand.

### `voucher-present`

```text
tdpp://<pretix-host>/voucher-present?chain=nexatest&topic=nexa-voucher&group=<subgroup id>&msg=<message>&callback=<claim url>
```

The message is ASCII, four lines:

```text
Nordic Crypto pretix-nexa-vouchers
claim <public id>
hour <UTC yyyymmddHH>
network nexatest
```

pretix accepts a signature over the current UTC hour or the previous one.

Requested Wally behaviour:

1. Find the address in the wallet that currently holds `group`.
2. Sign `msg` with that address the same way `nexad` `verifymessage` expects.
3. `POST callback` with `{"address": "<nexatest:…>", "signature": "<sig>"}` and `Content-Type: application/json`.
4. Show `voucher_code` and offer to open `redeem_url`. Do not store the code in a public log.

The claim page already does this with a pasted signature, so recipients are not blocked on the Wally change.

## Suggested libnexakotlin / Wally changes

These are the gaps. They are not implemented here.

1. **Topic `nexa-voucher`.** Register the three actions above. Do not treat them as ordinary NEX `sendto` payments. `qty` is a token quantity, not satoshis.
2. **Token output builder.** `token-send` needs the same transaction the wallet already builds for a manual NFT send: one subgroup output, quantity 1, to the cashaddr in `addr`, plus the wallet's fee input. Reject fungible quantities other than 1 for this topic.
3. **Identity / message sign.** `voucher-present` needs the existing message-sign path aimed at the address that holds the group, then an HTTP POST of the result. The plugin verifies with the organiser's node, so Wally does not need a new signature algorithm.
4. **Organiser profile.** A small setting: pretix origin, event path, and the bearer token. Attach the header only to `assign-next` and `confirm-transfer` on that origin.
5. **Queue screen.** `GET` the list and the entry. Show position, status, and, once assigned, a button that runs `token-send` for the subgroup in the JSON.
6. **Testnet guard.** Default the feature to `nexatest`. A mainnet URI should require a user toggle that this spec does not define, because the pretix side is not cleared for mainnet either.
7. **QR.** Encode either the `tdpp:` URI or the HTTPS callback. pretix's own QR images encode the `tdpp:` URI for `token-send`, `voucher-present`, and `queue-join` (`/nexa/qr.svg?kind=send|tdpp|join`). The claim page also has an HTTPS QR (`kind=claim`) that opens the form.

`https://w.nexa.org/tdpp/<action>?<same query>` is included in JSON as `wallet_url` because the public Nexa spec defines that carrier. This plugin does not run that host. If Wally only intercepts `tdpp:`, the `wallet_url` field can be ignored.

## Rostrum checks the plugin already performs

- `token.address.listunspent(address, null, tokenId)` decides whether an address holds the subgroup.
- Confirm-transfer and claim both require a non-empty unspent list.
- Redemption, separately, treats an unspent output at the event's burn address as the burn, and otherwise melts or publishes the marker from the issuer wallet.

A Wally build does not need to call Rostrum for the queue if it performs the send and the signature itself. pretix repeats the check so a pasted txid cannot confirm a transfer that did not happen.
