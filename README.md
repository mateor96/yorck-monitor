# Yorck Monitor

A small local dashboard that watches sold-out screenings at the
[Yorck cinemas](https://www.yorck.de) in Berlin and tells you when a seat frees
up.

## Why

The films I actually want to see sell out fast, and the only way to get in is to
catch a cancellation — someone gives their ticket back and it quietly reappears
on the site. Refreshing the page all evening is not a plan. So this watches the
screening for me and says something when a seat comes back.

## Run it

```bash
python3 yorck_monitor.py --app     # small window for the corner of a monitor
python3 yorck_monitor.py           # or just http://localhost:4000
```

Python 3.9+, no dependencies. `Ctrl+C` to stop — watches are saved and resume.

Two tabs at the top. **Monitor** is the watcher; **Programme** is the catalogue.

Pick a cinema and a day, click a screening, done. Every ~90 s it re-checks and
tells you the seat count. When something frees up the card turns green, a sound
plays and a **Book now** button appears that links straight into the checkout.

## Finding the films nobody announces

The yorck.de programme is sorted by cinema. That is fine for the film playing
five times a day, and useless for the single 35 mm repertory screening three
weeks out — to find that one you would have to open fifteen cinemas and click
through every day.

The **Programme** tab turns the same data inside out: one crawl of all fifteen
cinemas, merged by film, so the film is the row and its dates are the detail.
Sorted **rarest first** by default, which is the whole point — right now 64 of
the 123 films on sale run exactly once in the entire upcoming programme, and
those float to the top instead of drowning under the one that runs 41 times.
Search, filter by cinema or by how far ahead, or flip on *rare only*.

Every screening shows its live seat count. If something is free you get a
**book** link straight into the checkout; if it is sold out you get **watch**,
which hands it to the Monitor tab in one click. Watching is only offered for
what you cannot buy right now — the monitor reports seats *freeing up*, so
watching an available screening would just fire immediately.

**refresh** re-reads the programme pages themselves, not just the seat counts,
and marks what has arrived since the last look as **new** — for a week, so the
badge survives the next three refreshes instead of clearing itself the moment
you look again. *new only* narrows the list to those. Absence is measured
against the previous crawl, never against the clock: not opening the tab for a
fortnight is not the same as a film having been away, and a cinema that fails
mid-crawl does not make its whole programme look new next time.

Each row carries the film's still, and the one you open gets it as a banner.
These are the 16:9 images from the cinema pages, which come along with the
crawl for free — the portrait poster exists too, but only on each film's own
page, which would mean one extra request per film. Contentful resizes them on
request, so a row thumbnail is ~4 KB instead of the 286 KB original, and they
load lazily as you scroll.

The horizon is however far Yorck has loaded: presale reaches into next February
at some houses.

### When a seat frees up

Each watched screening carries its own setting, on its card — because it
differs per film. Something you badly want should go through on its own;
something you are merely curious about should just say hello.

| | |
|---|---|
| **melden** | sound, desktop notification, phone push. Nothing else. |
| **öffnen** | additionally opens the checkout in your own Chrome. You pick the ticket and confirm. *(default)* |
| **auto** | additionally picks *Yorck Unlimited* and places the order. |

**auto** drives your own logged-in Chrome via AppleScript — no credentials are
handled anywhere, name and email come prefilled from your account. It needs
Chrome's *View → Developer → Allow JavaScript from Apple Events* switched on,
and it only works at cinemas without seat selection (the open-air Sommerkino);
elsewhere it falls back to opening the tab, because the seat-picking step is
not implemented and guessing there would book the wrong thing.

It verifies before clicking — the session id in the URL *and* the date and time
printed on the page must both match — and again on the payment page. A failed
attempt is retried on the next check while the seat is still there, up to three
times, then it gives up with a push telling you why. After a successful booking
it never runs again for that screening.

Worth knowing: nothing before the final confirmation reserves anything. Opening
the checkout starts an order session, and picking a ticket fills a basket, but
the availability count does not move until the booking completes — I measured
all three. Only a finished booking holds the seat.

## Notifications on your phone

Via [ntfy](https://ntfy.sh) — no account, no API key:

```bash
NTFY_TOPIC=some-name-only-you-know python3 yorck_monitor.py --app
```

Subscribe to the same topic in the ntfy app. The topic name *is* the secret on
the public server, so don't pick something guessable. It's remembered after the
first start. Telegram works too (`TG_TOKEN` + `TG_CHAT`).

## How it finds out

The programme page contains no availability information at all — every showtime
is rendered as `<a href="#">` whether tickets exist or not, and the state is
filled in client-side. Scraping the HTML can't work.

The real signal is the Vista ticketing endpoint the site's own frontend calls,
where `SeatsAvailable` per session is the truth. Two things cost me time:

- The API gateway **requires `Content-Type: application/json`, even on GET**.
  Without it everything comes back `500 Unexpected end of JSON input`.
- The `SoldoutStatus` field in the same response is **always `0`**, at every
  cinema. It is not maintained. Use `SeatsAvailable`.

Film titles and session ids come from the `__NEXT_DATA__` blob on the cinema
pages, cached for 30 minutes.

## Being polite about it

One request per cinema per cycle (~400 bytes), a 60 s floor on the interval with
±15 % jitter, a 4 s gap between any two requests, backoff on errors, and the polling itself
never touches the booking flow — no orders, no seat holds. That works out
to roughly 40 tiny requests an hour, less than leaving the page open in a tab.

The Programme tab costs one programme page plus one seat lookup per cinema —
about thirty small requests for the lot, and a single seat lookup covers a
cinema's whole horizon, months of presale in one response. That is too much to
put on a timer next to a running watch, so it only ever runs when you actually
open the tab, then holds for 30 minutes and is served from
`~/.yorck_monitor/catalogue.json` in the meantime. A restart does not re-crawl.

The exception is the checkout: opening it creates a real order session, and
**auto** completes a booking. Both only ever fire for a seat you asked to be
watched, at most three attempts, exactly as clicking the page yourself would.

**The throttling is deliberate. Please leave it in.**

## Files

`yorck_monitor.py` — server, watcher, notifications ·
`yorck_api.py` — the yorck.de client ·
`checkout_drive.py` — drives the checkout in your Chrome (also usable on its own) ·
`ui.html` — the dashboard, plain HTML/CSS/JS ·
`~/.yorck_monitor/state.json` — your watches ·
`~/.yorck_monitor/catalogue.json` — the last programme crawl

## License

MIT
