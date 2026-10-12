# BitsBots

**Current alpha:** `0.0.1-alpha`

## Download for Windows

**[Download the Windows installer](https://github.com/OBitsPlease/Bits-Bots/releases/download/v0.0.1-alpha/BitsBots-0.0.1-alpha-Windows-Setup.exe)**

[Download the portable ZIP](https://github.com/OBitsPlease/Bits-Bots/releases/download/v0.0.1-alpha/BitsBots-0.0.1-alpha-Windows-Portable.zip)
·
[View release notes and all downloads](https://github.com/OBitsPlease/Bits-Bots/releases/tag/v0.0.1-alpha)

The installer is the normal choice for most Windows users. The portable ZIP is
provided for testers who cannot or do not want to install the application.

> **Beta software — use at your own risk.** BitsBots does not guarantee
> financial success or profits. Automated trading can lose some or all of the
> funds assigned to it. Trade only with funds you can afford to lose. Research,
> signals, and AI-generated content in BitsBots are not financial advice.

BitsBots is a local NonKYC market scanner, paper-trading dashboard, and
optional live trader. It deeply ranks the most liquid USDT and USDC markets
using daily, 4-hour, 1-hour, and 15-minute candles and explains every score.
Live trading is **off by default** and stays off until you deliberately enable
it.

## Safety

- Do not paste exchange API keys/secrets into chat or any other AI tool.
  Enter them only in the app's own Settings → "NonKYC API key" form,
  where the secret is encrypted with a key file that stays on this machine
  (`data/local.key`) and is only ever sent to NonKYC's own API.
- Revoke any API credential that has been pasted into chat or another public
  location.
- A connected key is used read-only (balances, orders, trades) until you
  enable live trading on the Live Trading page. Changing or removing the key
  turns live trading off and halts live bots.
- For live trading, create a dedicated key with spot-trading permission,
  **withdrawals disabled**, and an IP allowlist containing only this PC's
  public IP. BitsBots never requests or uses withdrawal access.
- Signals are research output, not guarantees. Paper results include configurable
  fees and slippage but cannot perfectly reproduce live fills.

## Install and run

### Windows alpha installer

The GitHub prerelease for `v0.0.1-alpha` provides a per-user Windows installer
and a portable ZIP. The packaged app stores its database and locally generated
encryption key under `%LOCALAPPDATA%\BitsBots`; these files are never included
in release artifacts. This alpha is unsigned, so Windows SmartScreen may show
a warning.

Install Python 3.11 or newer, clone or download this repository, and run these
commands from the repository folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m bitsbots.main
```

Open <http://127.0.0.1:8765>. The scanner refreshes automatically. Browser
notifications can be enabled from the dashboard.

BitsBots is standalone and local-first. It binds only to `127.0.0.1`, so no
central server or VPS is required. Source checkouts create their own blank `data/` directory and database on first
launch. Packaged Windows installs use `%LOCALAPPDATA%\BitsBots` instead. The
first user registers a new local login, and any optional NonKYC credentials
must be supplied by that user. Local data, credentials, databases, virtual
environments, and environment files are excluded from Git.

`Start-BitsBots.ps1` is an optional Windows launcher. It resolves the app
directory from its own location, so the repository may be installed anywhere.

### Publishing a clean copy

Before publishing or uploading a release:

1. Start from a newly initialized Git repository in this folder.
2. Review `git status --short --ignored` and confirm that `data/`, `.venv/`,
   `.env*` (except a deliberately sanitized `.env.example`), caches, and
   `*.egg-info/` are ignored.
3. Never force-add an ignored credential or data file.
4. Run a secret scanner such as Gitleaks against the complete commit history
   before pushing.
5. Clone the resulting repository into a different empty folder and verify that
   it starts at the first-run registration screen with no users, account data,
   credentials, or trading history.

Git ignore rules protect files that have never been committed. If a credential
or personal data file is ever committed, removing it in a later commit does not
remove it from Git history; rotate the credential and clean the history before
publishing.

On first launch, create a local account and then create at least one paper bot.
Use **Create recommended paper bot** for a one-click conservative setup based on
the scanner's strongest unassigned candidate, or expand the custom settings to
choose the market, entry threshold, risk level, and maximum allocation manually.
Nothing starts until the creation button is pressed. Only running bots may open
new simulated positions; pausing or deleting a bot prevents new entries while
existing positions continue to be risk-managed.

Three paper-only strategies are available:

- **Momentum Bot** waits for aligned daily, 4-hour, 1-hour, and 15-minute signals.
- **AI Grid Bot** uses a fixed lower/upper range and fee-aware slices. A downward
  level crossing opens a slice and its target is the next grid level.
- **Adaptive Grid** uses the same slice rules, but may recenter its range only
  after price leaves the range and no grid slices remain open.

### Paper-only learning

After each successful market scan, an independent learner shadow-tests a
maximum of three bounded challengers for every user's paper strategy and
market. It reads only stored public candles and paper-bot settings; it never
reads exchange credentials or modifies live NonKYC bots. Decisions are
processed chronologically and a decision made from one candle cannot fill
until the next candle, preventing look-ahead.

Strategy/settings versions are immutable and scoped per strategy and market,
with a strategy-wide baseline (`*`) as fallback evidence. Results include
configured fees and slippage; the promotion objective is net return minus
half of maximum drawdown. A challenger cannot be promoted before both 15
completed trades and 7 elapsed days, and must materially exceed the champion
in both objective and net return. The safety policy never raises the bot's
risk level or maximum allocation and never loosens a grid safety stop.
Evaluations and promotions are kept in local SQLite and shown in the Paper
Bots page and `/api/state`.

Grid recommendations calculate the range, slice count, stop, and allocation
automatically. Advanced users can override them; the API rejects ranges that do
not contain the current price, stops that are not below the range, and spacing
too narrow to cover the configured fee and slippage model.
The automatic discovery cycle deeply scans the top 20 liquid pairs. Manual bot
creation searches every active NonKYC USDT and USDC pair, including low-volume pairs.
A selected market outside the top 20 is treated as a speculative play and
prioritized in the next deep scan before it is eligible to enter.

The top navigation opens dedicated Strategies, Paper Bots, Portfolio,
Backtesting, and Settings views. Select a bot card to return to the dashboard
and focus its market. On the chart, use the wheel to zoom, drag with the left
mouse button to pan, and double-click to reset the available history.

Interactive controls provide concise native hints when hovered and accessible
descriptions for assistive technology, without adding visual clutter. The bot
form's Auto-pick action ranks current unassigned scanner results, prefills
conservative settings, and explains the evidence; it never creates a bot
without confirmation.

The Market Assistant is a private local, data-grounded chat interface. It can
explain current signal evidence, referenced symbols, bot settings, risk, and the
paper portfolio. It does not send account data to a third-party language model.
Every response and the assistant page state that its output is a current-data
suggestion rather than financial advice.

### NonKYC account monitoring

Settings has a "NonKYC API key" card where you can connect your own NonKYC
API access key and secret. BitsBots validates the key against NonKYC, then
stores the secret encrypted with a local key file (`data/local.key`) that
never leaves this computer. Once connected, the Portfolio page shows your real
balances, open orders, recent trades, and native NonKYC grid bots, pulled
directly and only from NonKYC — clearly labeled apart from BitsBots' simulated
paper bots.

## Live Trading (beta)

The **Live Trading** page places real orders only after every safeguard below.
A risk notice is shown each time BitsBots starts.

### How it works

- **Modes.** *Dry run* rehearses everything with real NonKYC prices and
  simulated balances (1,000 USDT and 1,000 USDC); nothing leaves this PC.
  *Live* sends real orders. Paper bots are separate and never trade.
- **Markets.** Only `COIN/USDT` and `COIN/USDC` pairs. Limit orders only.
- **Enabling live trading** requires: a connected key, this PC's public IP
  (entered manually — BitsBots never uses an IP-lookup service), confirmation
  that the key has trading permission, withdrawals disabled, and the IP on
  its allowlist, a risk acknowledgement, the typed phrase
  `ENABLE LIVE TRADING`, and your BitsBots password. BitsBots cannot see your
  key's NonKYC settings, so these are recorded as *confirmed by you*.
- **Previews.** Every manual order is previewed first: rounded to the
  market's precision, with fees, balances, warnings, and every risk check.
  Previews expire after 60 seconds, risk checks run again at confirmation,
  and live confirmations require your password.
- **Risk limits** (each explained in the app): maximum order value, total
  exposure, per-bot allocation, reserve balance, daily loss limit (UTC day;
  protective sells still allowed), open orders, running bots, orders per day,
  market-data age, consecutive errors, and a manual price band.
- **Kill switch** blocks every new order and halts all bots; turning it off
  requires your password. **Emergency cancel** also cancels every open order
  *BitsBots created*. It never touches other orders on your account, such as
  native NonKYC grid bots.

### Grid bots hosted by BitsBots

NonKYC confirmed (October 2026) that it has no public API for native grid
bots or Trailing Up, and no test environment. BitsBots therefore runs grids
itself using NonKYC's documented order endpoints.

- **A grid bot only trades while BitsBots is running on this PC**, the PC is
  awake, and the internet is connected. Disable sleep while live bots run.
- Each slot buys at one grid line and sells one line higher. Grids whose
  spacing does not cover round-trip fees are refused. The grid starts by
  placing buys below the current price; it does not buy up front.
- New bots are created **armed** and do nothing until started. Starting a
  live bot requires your password.
- **Stop loss:** when price reaches it, the bot cancels its orders, sells its
  holdings at up to 1% below the best bid, and stops.
- **Trailing Up (optional):** when price stays above the grid for the
  confirmation time, unfilled buys are cancelled and the grid moves up.
  Holdings keep their sell targets, the stop only moves up, the bot never
  commits more than its allocation, and it pauses at the trailing ceiling or
  maximum number of moves.

### Order safety and recovery

- Every order is recorded with a unique client ID (`bb…`, no personal data)
  *before* it is sent.
- If a submission times out, BitsBots never resends it. The order is marked
  *submission unknown*, its bot halts, and BitsBots looks the order up on
  NonKYC. If nothing is found within five minutes it is treated as not
  placed. Conflicting evidence is marked *needs review*: check your NonKYC
  order history and use **Resolve** to link the order or confirm none exists.
- On restart, interrupted submissions are marked unknown before any new order
  is placed, and bots only place orders after every open order they own has
  been verified with NonKYC within the last two minutes.
- Writes are spaced at least one second apart and bots place at most three
  orders per cycle, because NonKYC does not publish rate limits.
- Any unexpected engine error halts only the affected bot.

### Recommended first steps

1. Rehearse with a **dry-run** grid bot for several days.
2. Create a dedicated NonKYC key (trading on, withdrawals off, IP allowlist).
3. Enable live trading, then place one minimum-size limit order far from the
   market, verify it on NonKYC, and cancel it.
4. Start a small live grid with conservative limits and watch it closely.

## Historical backfill

NonKYC's documented candle subscription returns a limited snapshot rather than
years of history. BitsBots saves live candles to `data/bitsbots.db`. You can
backfill vendor-exported OHLCV CSV data without sharing credentials:

```powershell
.\.venv\Scripts\bitsbots-import.exe BTC/USDT 60 .\history\btc_usdt_1h.csv
```

Required columns are `timestamp,open,high,low,close,volume`. Timestamps may be
Unix seconds, Unix milliseconds, or ISO-8601. Supported periods are 15, 60, 240,
and 1440 minutes.

## Current strategy

The scanner combines:

- daily and 4-hour trend/regime alignment;
- 1-hour momentum, pullback quality, and volume confirmation;
- optional 15-minute entry timing;
- volatility compression/breakout behavior;
- market liquidity and stale-market rejection.

Paper positions risk 0.5% of equity by default, cap allocation per position,
model fees/slippage, use ATR-based invalidation stops and targets, and trail
winning positions. All settings are in `src/bitsbots/config.py`.
