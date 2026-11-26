<p align="center">
  <img src="logo.png" width="168" alt="Blockwatch logo">
</p>

# Blockwatch

A read-only watch list for the Omarchy bar. Blockwatch watches BTC, LTC,
DOGE, ETH, SOL and XMR addresses, a whole Bitcoin wallet by its xpub, and a
Blink Lightning wallet, and tells you when value is credited or debited. It is
not a wallet: it holds no spend key, cannot sign, and never sends a
transaction.

<p align="center">
  <img src="preview.png" width="420" alt="Blockwatch panel with four fictional watches and USD estimates">
</p>
<p align="center"><sub>Fictional demo data, captured with <code>demo/run</code>; fiat estimates use a fixed fixture rate.</sub></p>

## What it does

- Adds a small eye to the bar. It glows while there are unseen events.
- Checks the watch list on a timer (default every 120 seconds) and on network
  reconnect.
- Sends a desktop notification for new credit or debit activity.
- Watches a whole Bitcoin wallet by its **xpub or output descriptor**, so a
  spend that moves change to a new address does not make the balance drop to 0.
- Watches a Blink BTC wallet over its read-only API, so Lightning receipts show
  up as sats even though they never touch the on-chain address history.
- Shows each watched address, its native balance (or, for Monero, the amount
  received), a selectable fiat estimate, the last event and any provider error
  in a panel.
- Converts balances to USD, EUR, GBP, JPY, CAD, AUD, CHF, CNY or INR with
  cached CoinGecko rates; a rate outage never stops address monitoring.
- Edits a live watch in place: click its row to change the label, address,
  Monero server or view key without deleting and re-adding it.
- Remembers custom XMR light-wallet servers in a dropdown beside the built-in
  public LWS and local `monero-wallet-rpc` options.
- Keeps its own state so a restart does not re-notify old history. The first
  successful check of a new address is a silent baseline.
- Runs one headless service for the whole shell, so a bar on four monitors
  still makes one provider call per interval and one notification per event.

## Chains and providers

| Chain | Provider | Notes |
| --- | --- | --- |
| BTC | [mempool.space](https://mempool.space) | Public transactions; no key needed. |
| BTC wallet | [Blockbook](https://github.com/trezor/blockbook) | One xpub or descriptor covers every address, including change. See below. |
| LTC | [litecoinspace.org](https://litecoinspace.org) | mempool.space-compatible API. |
| DOGE | [BlockCypher](https://www.blockcypher.com) | Free tier; an API token can be configured. |
| ETH | [Blockscout](https://eth.blockscout.com) | Native ETH transfers only; tokens are not followed. |
| SOL | Solana JSON-RPC | Public signatures and balance; no key needed. |
| XMR | [monero-lws](https://github.com/vtnerd/monero-lws) or `monero-wallet-rpc` | Needs the private **view** key. See below. |
| Lightning | [Blink](https://blink.sv) account API | Needs a read-only API key. Custodial: Blink holds the sats. See below. |
| Fiat rates | [CoinGecko](https://www.coingecko.com/en/api) | One cached request per five minutes; sends coin symbols and the selected currency, never addresses. |

The endpoint for one address can be overridden in the config file, so a
different Blockscout instance (Base, Optimism, Gnosis, …), a private Solana
RPC, or a self-hosted light wallet server can be used without code changes.

## Install

```bash
omarchy plugin add https://github.com/ekollof/blockwatch.git
omarchy plugin enable io.github.ekollof.blockwatch
omarchy bar put io.github.ekollof.blockwatch --section right
```

Saving a file in the installed plugin reloads it. `omarchy plugin validate`
checks the manifest; `omarchy agent look plugin` renders the bar icon.

## Add an address

Click the eye, choose **Watch an address**, pick the chain and paste the
public address. A label is optional. For **BTC wallet**, paste the account
xpub or output descriptor instead of an address; for **sats**, paste your
`name@blink.sv` and a read-only Blink API key. **Click a row to edit its
label, address, server, Monero mode, Blink key or xpub**; the trash button
stops watching it.

The same operations are available without the UI:

```bash
BW=~/.config/omarchy/plugins/io.github.ekollof.blockwatch/bin/blockwatch

$BW add --chain btc --address bc1q... --label "Cold storage"
$BW add --chain btcwallet --address xpub6... --label "Main wallet"
$BW add --chain eth --address 0x... --label "Treasury"
$BW add --chain doge --address D...   # set BLOCKWATCH_API_KEY first
$BW add --chain lightning --address name@blink.sv \
    --label "Phone sats"             # set BLOCKWATCH_API_KEY first
$BW list
$BW update btc:bc1q... --label "Deep cold storage"
$BW update btc:bc1q... --address bc1q... --label "Replaced device"
$BW remove btc:bc1q...
$BW poll | jq .
```

An update keeps a stored Monero view key unless you pass a new `--view-key`,
so changing only the label or server does not make you paste the key again.
Changing the address or chain drops the old key and asks for the one that
belongs to the new target.

For scripts and the panel, secrets are better passed through the environment
than through argv, where `ps` and shell history can see them:

```bash
read -rsp "View key: " BLOCKWATCH_VIEW_KEY; echo; export BLOCKWATCH_VIEW_KEY
read -rsp "API key: " BLOCKWATCH_API_KEY; echo; export BLOCKWATCH_API_KEY
read -rsp "RPC user: " BLOCKWATCH_RPC_USER; echo; export BLOCKWATCH_RPC_USER
read -rsp "RPC password: " BLOCKWATCH_RPC_PASSWORD; echo; export BLOCKWATCH_RPC_PASSWORD
```

`read -s` keeps the value out of the terminal and shell history; a secret
manager's `lookup` command is another good source. The `--view-key`,
`--api-key`, `--rpc-user` and `--rpc-password` flags remain for one-off
commands, but they are visible in `ps` and shell history. The panel passes the
view key to its helper through `BLOCKWATCH_VIEW_KEY`, never as an argument.

Environment API/RPC credentials apply when **adding** a watch. An `update` does
not reapply a global `BLOCKWATCH_API_KEY`/`BLOCKWATCH_RPC_*` to a changed
endpoint: pass the new provider's credential explicitly with `--api-key` /
`--rpc-user` / `--rpc-password` (or edit the `0600` config), so a token chosen
for one provider cannot follow an endpoint change to another. The panel rotates
a Blink key by exporting it as `BLOCKWATCH_API_KEY` and passing
`--api-key-from-env`, an explicit "use this new key" signal; the key itself
never appears in argv.

Custom XMR servers are remembered automatically when a watch using one is
saved. They can also be managed directly:

```bash
$BW servers
$BW server add --label "Home LWS" --endpoint https://lws.example.org
$BW server remove https://lws.example.org
```

The helper stores its watch list in `~/.config/blockwatch/config.json`
(`$XDG_CONFIG_HOME/blockwatch/config.json`) and its state in
`~/.local/state/blockwatch/state.json`
(`$XDG_STATE_HOME/blockwatch/state.json`). Both are created with mode `0600`,
and the directories with `0700`, because a Monero view key or Blink API key is
a secret.

## Bitcoin: one address or the whole wallet

A watch on a single Bitcoin address shows that address only. An HD wallet uses
a new address for every payment and sends change to another new address, so a
single-address watch reads 0 as soon as the wallet spends — even though the
wallet still holds funds.

The **BTC wallet** watch takes an account **xpub** (or a public output
descriptor) instead of an address, and asks a Blockbook indexer about the
whole account: every receive address, every change address, one balance and
one transaction list. That is the row that behaves like the wallet.

- Export the **account-level** xpub from your wallet: `xpub…` (BIP44),
  `ypub…` (BIP49) or `zpub…` (BIP84). Testnet `tpub…`, `upub…` and `vpub…`
  work against a testnet endpoint.
- Taproot and explicit script types work through a descriptor:
  `wpkh(xpub…/0/*)`, `tr(xpub…)`, `sh(wpkh(xpub…))` or `pkh(xpub…)`, with
  optional `[fingerprint/path]` origin and `<0;1>` change selector. Blockbook
  derives the addresses; Blockwatch sends the descriptor URL-encoded.
- **Never paste an xprv, yprv, zprv, tprv, uprv or vprv.** Blockwatch refuses
  these by name. A private key can spend; an xpub cannot.
- An xpub reveals every address of the wallet to the Blockbook server. The
  default is Trezor's public instance, with Zelcore's as a fallback when the
  first is rate-limited; set `endpoint` to your own Blockbook to keep the
  xpub private. This is a privacy cost, not a spending risk.

The balance includes unconfirmed transactions, like the address watches, and
the row note says so while a spend or receipt is pending. The BIP scheme is
inferred from the xpub prefix, so use a `zpub` for a native SegWit account and
a `wpkh(…)`/`tr(…)` descriptor when the prefix alone is not enough.

```bash
$BW add --chain btcwallet --address xpub6... --label "Main wallet"
$BW add --chain btcwallet --address 'tr(xpub6...)' --label "Taproot"
```

## Lightning: reading sats from Blink

**A Bitcoin address cannot see Lightning payments.** Lightning receipts settle
off-chain, so they never appear in mempool.space or in the address history of
a wallet. The authoritative record is the wallet that received the invoice.

Blockwatch reads that record from a **Blink** account with a read-only API
key. It shows the BTC wallet balance in sats, lists settled Lightning,
on-chain and Blink-to-Blink receipts into that wallet, and notifies once per
new transaction. It never creates an invoice, pays, or changes channels.

Blink is **custodial**: Blink holds the keys and the sats. The API key is
read-only (the `Read` scope), so it can see history and balances but cannot
send. If you would rather not trust a custodian, run your own node and use a
provider with a read-only node credential instead; Blockwatch does not ship
one yet.

Set it up once:

1. Sign in at [dashboard.blink.sv](https://dashboard.blink.sv) with the same
   account as the Blink app on your phone. Claim a username; your Lightning
   address is `username@blink.sv`.
2. Open **API keys**, create a key with **Read** only, and copy it.
3. In the panel choose **sats**, enter `username@blink.sv` and paste the key.
   Or from a shell:

```bash
read -rsp "Blink read-only key: " BLOCKWATCH_API_KEY; echo; export BLOCKWATCH_API_KEY
$BW add --chain lightning --address name@blink.sv --label "Phone sats"
```

The watch stores the key in the `0600` config and sends it only to
`https://api.blink.sv/graphql` as `X-API-KEY`. The username in the address is
checked against the account the key belongs to, so a key pasted for the wrong
account is refused instead of silently watching someone else's balance.

Only **settled** transactions notify; a pending invoice is counted in the row
note instead, so a payment that fails does not produce a false receipt. The
BTC wallet balance includes on-chain receipts into Blink as well; each event's
note says whether it arrived by Lightning, on-chain or from another Blink
user. Fiat estimates price sats at the BTC rate.

## Monero: what actually works

**An XMR address cannot be watched on its own.** Monero replaces the recipient
address with a one-time stealth address and hides amounts with RingCT, so no
block explorer can list "the transactions of address 4…". Public `monerod`
nodes — including the lists at monerodevs.org and Cake Wallet's public nodes —
serve blocks and transactions, not an address index.

There are two honest ways to watch an XMR address, and Blockwatch supports
both:

### 1. A light wallet server (default)

A light wallet server scans the chain with your **private view key** and
returns your incoming history. It never gets a spend key. Blockwatch uses the
MyMonero/monero-lws REST API:

- Default server: `https://lws.monero.fail` (community-run; review its
  operator before trusting it with a view key).
- The add form offers a **server dropdown**: the community LWS, a local
  `monero-wallet-rpc`, or **Custom server…**. A custom URL is remembered in
  the config's `servers` list and appears next time.
- The server may require the wallet to be registered at
  `https://lws.monero.fail/register` (email + public address + view key). If
  the server does not know the wallet, Blockwatch shows a clear error instead
  of pretending the address is quiet.
- Better: run your own `monero-lws` instance and add its URL as a custom
  server.

Get the private view key from the wallet that owns the address (`viewkey` in
`monero-wallet-cli`). Use the wallet's **main address**, which starts with
`4`, not a subaddress that starts with `8`. A view key alone cannot prove
outgoing spends, so the panel labels the number **received** instead of
calling it a balance, and the true balance stays in the spending wallet.

### 2. A local `monero-wallet-rpc`

For the most correct balance and without handing a view key to a third
party, run a view-only wallet locally:

```bash
monero-wallet-cli --generate-from-view-key watch
# enter the main address and private view key when prompted

monero-wallet-rpc \
  --wallet-file watch \
  --daemon-address https://xmr-node.cakewallet.com:18081 \
  --rpc-bind-port 18083 \
  --disable-rpc-login \
  --prompt-for-password
```

Then add the address with:

```bash
$BW add --chain xmr --address 4... --mode wallet-rpc \
  --endpoint http://127.0.0.1:18083/json_rpc
```

The `--daemon-address` can be any trusted public `monerod`. Two that answered
at the time of writing are:

- `https://xmr-node.cakewallet.com:18081`
- `https://node.monerodevs.org:18089`

Those are **daemons**, not watch endpoints: they supply chain data to your
wallet, while the scanning and view key stay on your machine. They are not
valid choices in the XMR server dropdown. A view-only wallet still cannot see
outgoing spends unless key images have been imported, so its balance can be too
high. Blockwatch says so in the row.

### What about an address starting with 8?

Addresses starting with `8` are subaddresses. They belong to the same wallet
account as its main `4…` address. Register the main address and its view key;
the light wallet server scans the account's subaddresses too, and Blockwatch
reports the account-level activity.

## Configuration

The panel covers the common case. The config file exposes the rest:

```json
{
  "version": 1,
  "addresses": [
    {
      "id": "btc:bc1qexample",
      "chain": "btc",
      "address": "bc1qexample",
      "label": "Cold storage",
      "endpoint": "https://mempool.space"
    },
    {
      "id": "btcwallet:xpub6example",
      "chain": "btcwallet",
      "address": "xpub6example",
      "label": "Main wallet",
      "endpoint": "https://btc1.trezor.io"
    },
    {
      "id": "xmr:4example",
      "chain": "xmr",
      "address": "4example",
      "label": "Donations",
      "mode": "lws",
      "endpoint": "https://lws.example.org",
      "viewKey": "64-hex-private-view-key"
    },
    {
      "id": "xmr:4example2",
      "chain": "xmr",
      "address": "4example2",
      "mode": "wallet-rpc",
      "endpoint": "http://127.0.0.1:18083/json_rpc",
      "authUser": "optional",
      "authPass": "optional"
    },
    {
      "id": "lightning:name@blink.sv",
      "chain": "lightning",
      "address": "name@blink.sv",
      "label": "Phone sats",
      "endpoint": "https://api.blink.sv/graphql",
      "apiKey": "blink-read-only-key"
    }
  ],
  "servers": [
    {
      "id": "https://lws.example.org",
      "label": "Home LWS",
      "endpoint": "https://lws.example.org",
      "mode": "lws",
      "custom": true
    }
  ]
}
```

The built-in server list is `lws.monero.fail` and the local
`monero-wallet-rpc`; `servers` holds only the custom endpoints Blockwatch
remembered. `id` is the endpoint for a remembered custom server.

Per-address keys:

| Key | Meaning |
| --- | --- |
| `chain` | One of `btc`, `btcwallet`, `ltc`, `doge`, `eth`, `sol`, `xmr`, `lightning`. |
| `address` | The public address, an xpub/descriptor for `btcwallet`, or the Blink Lightning address (`name@blink.sv`). |
| `label` | Optional display name, at most 80 characters. |
| `endpoint` | Optional provider override. HTTPS required, except loopback for wallet RPC. |
| `viewKey` | Monero private view key (64 hex). Required for `lws`, not for `wallet-rpc`. |
| `mode` | Monero only: `lws` or `wallet-rpc`. |
| `apiKey` | BlockCypher token, a bearer token for a Solana RPC, or a Blink read-only API key. |
| `authUser`, `authPass` | Optional basic auth for `monero-wallet-rpc`. |

Keep `id` consistent with `chain:address`; the helper recomputes it when
loading. Quotes and control characters in labels are displayed as plain text
and are never interpreted as markup.

Widget settings live in the normal Omarchy entry (`omarchy bar set`):

| Setting | Default | Range |
| --- | --- | --- |
| `refreshIntervalSec` | 120 | 30–3600 |
| `notify` | `true` | boolean |
| `fiatCurrency` | `USD` | USD, EUR, GBP, JPY, CAD, AUD, CHF, CNY, INR |

The panel’s currency dropdown changes `fiatCurrency` and refreshes immediately.
`blockwatch --fiat EUR poll` and `--fiat EUR status` do the same from a script.
Rates are cached for five minutes; stale rates are shown with a small warning,
and native balances continue to update if CoinGecko is unavailable.

## Notifications and state

- The first check of an address stores a baseline and does not notify.
- A transaction id that was not in the state produces one event. A change in
  balance with no new transaction (an internal transfer, or a chain whose
  detail is not public) produces one balanced-change event.
- Monero light wallet servers report *received* totals; Blockwatch does not
  turn that into a spendable balance.
- A Lightning watch reports settled Blink BTC-wallet transactions and the
  wallet balance in sats; pending invoices are only counted in the note, so a
  failed payment never becomes a false receipt.
- Solana exposes signatures but not recipient amounts through the cheap RPC
  call, so a single new signature is shown with the **net balance delta**; a
  burst is shown as activity.
- Blockwatch caps a report at 25 events per address and notifies for at most
  three plus a summary, so a very busy exchange address cannot flood the
  desktop.

## Privacy and security

- **Read-only by construction.** No spend key, no seed, no signing, no
  broadcast, no send/receive UI. Monero uses the private *view* key, which
  can only reveal incoming payments. A Blink key is created with the `Read`
  scope only; it can list balances and history but cannot pay.
- No telemetry, analytics or crash reporting. Outbound requests go only to the
  provider or server shown for each watched address.
- The fiat-rate request goes to CoinGecko and contains only the fixed coin
  symbols and the selected currency code; no addresses, labels or keys.
- Transparent chains are queried by address at the provider listed above.
  That reveals which addresses you care about to that provider. Use your own
  endpoint if that matters to you.
- An xpub is not a spending key, but it links every address of the wallet.
  Treat it as private data: the Blockbook server can see your whole wallet
  history, and the default public instances are third parties. Blockwatch
  refuses xprv/seed material before it reaches the config.
- A Monero view key is sent only to the configured light wallet server (or
  stays local with `wallet-rpc`). Treat a third-party LWS as you would any
  other service that can see your incoming history.
- A Blink API key is sent only to `api.blink.sv` and stored in the `0600`
  config. A key with the `Read` scope can see your whole Blink history, so
  treat it like a password and do not reuse an admin or `Write` key. Blink is
  custodial: using it means trusting Blink with the sats, which is a different
  trade-off from watching a self-custody address.
- Custom XMR server URLs are stored in the config and shown in the form. Do
  not put credentials or tokens in those URLs; `wallet-rpc` has separate
  `authUser`/`authPass` fields for Basic auth.
- HTTPS only; loopback HTTP is allowed only for `monero-wallet-rpc`. Redirects
  cannot change host or scheme.
- Every request has a timeout and a response-size cap; every list and string
  is bounded before display or storage.
- Stopping or editing a watch cancels any in-flight poll before the config
  write. CLI mutations use the same config lock as each address check, so a
  CLI removal also cannot be followed by a request from an older poll.
- Processes are started with argv arrays through omakit's `Run` block, never a
  shell string. QML displays provider and user text with
  `Text.PlainText`.
- Config and state are written atomically with mode `0600`.

## Development

```bash
./tests/run          # stdlib unittest suite, no network
omarchy plugin validate .
mise exec node@26.13.0 -- omakit inspect .
```

The helper is Python 3 standard library only. `bin/blockwatch` is the CLI;
`lib/blockwatch/` holds the provider adapters, validation, state and poll
logic. `BarWidget.qml` is the bar surface and form; `service.qml` is the
headless service that owns the timer, unread count and notifications for all
bar instances. Provider parsers can be tested without network by monkeypatching
`blockwatch.watch.fetch` or the private parser functions.

The README screenshot is generated from committed fictional data, not a live
wallet:

```bash
./demo/run          # temporary fixture helper, restored on exit
```

`demo/fixtures/report.json` is the only data shown; `demo/run` backs up the
installed helper, uses an empty monitor, captures the panel and restores the
real helper/config on exit.

## Limitations

- Native coin only. ERC-20/SPL tokens, NFTs and contract events are not
  followed.
- Polling, not push. The minimum interval is 30 seconds; public APIs may rate
  limit larger watch lists.
- Solana amounts are approximate net balance moves for the single-signature
  case and unknown for bursts.
- The BlockCypher free tier is rate limited; a token is recommended for more
  than a couple of Dogecoin addresses.
- Fiat values are estimates from a cached CoinGecko spot rate, not accounting
  or tax figures; the native balance is authoritative.
- `BTC wallet` needs a Blockbook indexer; public instances rate-limit, and the
  default has one fallback. Set `endpoint` to your own Blockbook for a private
  and predictable source. Descriptors are limited to `pkh`, `sh(wpkh)`, `wpkh`
  and `tr` forms.
- An LWS that has no history for a wallet begins where the server registered
  it. Watching from now on is the normal case; historical backfill is the
  server's business.
- Blockwatch speaks HTTPS directly and does not ship a Tor transport, so the
  onion-only LWS endpoints cannot be selected; use a clearnet LWS you trust or
  a local `monero-lws`.
- Lightning watches need a Blink account with a username and a `Read` API key.
  The watch sees the Blink BTC wallet, so an on-chain receipt into Blink is
  listed too. Removing the key in Blink stops the watch at its next poll.
  Cake Wallet's built-in Lightning (Spark) has no account API, so it cannot be
  read this way; a wallet whose history lives only on the phone is invisible
  to any desktop watcher.

## Uninstall

```bash
omarchy plugin remove io.github.ekollof.blockwatch --yes
rm -rf ~/.config/blockwatch ~/.local/state/blockwatch
```

## License

MIT. See [LICENSE](LICENSE).
