#!/usr/bin/env python3
"""
Yorck Monitor -- a small local dashboard that watches sold-out screenings at the
Yorck cinemas and tells you the moment a seat frees up.

    python3 yorck_monitor.py            # http://localhost:4000
    python3 yorck_monitor.py --app      # ... and open it in a small Chrome window
    python3 yorck_monitor.py --port 4100

Pick a cinema, a day and a film in the browser; the watcher then checks that
screening every ~90 seconds and reports back after every single check.
It never books anything -- it only shows you when to.

Design notes on not getting blocked:
  * availability comes from one lightweight JSON endpoint (~4 KB), the same one
    the yorck.de frontend calls;
  * one request covers a whole cinema, so N films at one cinema still cost one
    request per cycle;
  * a process-wide throttle keeps >= 4 s between any two requests;
  * the interval is jittered +-15 % so the traffic never looks mechanical;
  * errors trigger exponential backoff, and a 429/403 parks the watcher for
    15 minutes and says so in the UI;
  * watching stops by itself once a screening has started;
  * nothing in the booking flow is ever touched -- no orders, no seat holds.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import checkout_drive
import yorck_api as api

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser("~/.yorck_monitor")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "log.jsonl")
FILMS_FILE = os.path.join(STATE_DIR, "catalogue.json")
LOG_KEEP_SECONDS = 24 * 3600

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 5
INTERVAL_CHOICES = (5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 300, 600)
MAX_INTERVAL = 900
MIN_CYCLE_GAP = 3     # floor between two check cycles, however often the UI pokes us

MAX_LOG = 400        # global feed entries kept in memory
MAX_HISTORY = 60     # per-watch check dots kept

CATALOGUE_TTL = 30 * 60   # programme pages are static; re-read at most every 30 min
CINEMAS_TTL = 6 * 60 * 60
FILMS_TTL = 30 * 60       # how long a full crawl of every cinema stays good
NEW_FOR = 7 * 24 * 3600   # how long a film that just appeared keeps its badge
FORGET_AFTER = 90 * 24 * 3600   # ...and how long we remember one that left


# ---------------------------------------------------------------------------
# kleine Helfer -- weit oben, weil Store.load() sie beim Import schon braucht
# ---------------------------------------------------------------------------

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def date_label(start: datetime) -> str:
    """"Thu 27.08. 20:45" -- spelled out rather than strftime('%a'), which
    follows the system locale and silently changes language on you."""
    return f"{WEEKDAYS[start.weekday()]} {start:%d.%m. %H:%M}"


def _parse_start(s: str) -> datetime:
    return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

class Store:
    """Everything the UI shows. Guarded by one lock, persisted as JSON."""

    def __init__(self):
        self.lock = threading.RLock()
        self.watches: list[dict] = []
        self.log: list[dict] = []
        self.settings = {"sound": True, "desktop": True}
        self.poller = {
            "running": False,
            "next_check": None,
            "last_check": None,
            "checking": False,
            "backoff_until": None,
            "message": "idle",
        }
        self._seq = 0
        self.load()
        self.load_log()

    # -- persistence -------------------------------------------------------

    def load(self):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
        except Exception:
            return
        with self.lock:
            self.watches = data.get("watches", [])
            self.settings.update(data.get("settings", {}))
            # Reste frueherer Fassungen: der Modus sitzt jetzt an der Watch.
            self.settings.pop("auto_open", None)
            self.settings.pop("checkout_mode", None)
            legacy_iv = self.settings.pop("interval", None)
            if legacy_iv:                      # global -> an jede Watch, einmalig
                for x in self.watches:
                    x.setdefault("interval", max(MIN_INTERVAL, min(MAX_INTERVAL, int(legacy_iv))))
            for w in self.watches:
                w.setdefault("history", [])
                w.setdefault("alert", False)
                w.setdefault("checks", 0)
                try:                       # Wochentag ggf. auf Deutsch nachziehen
                    w["date_label"] = date_label(_parse_start(w["start"]))
                except (ValueError, KeyError):
                    pass               # nur kaputte Datumsfelder, nichts anderes
                if not w.get("last_free_at"):
                    # Nachtragen fuer Beobachtungen von vor dieser Fassung.
                    # Die Historie haelt nur 60 Eintraege (bei 60 s also eine
                    # Stunde), also faellt sie auf freed_at zurueck -- den
                    # Zeitpunkt des letzten Umschlags auf "frei".
                    free = [h for h in w["history"] if h.get("s") == "available"]
                    if free:
                        w["last_free_at"] = free[-1]["t"]
                        w["last_free_seats"] = free[-1].get("n")
                    elif w.get("freed_at"):
                        w["last_free_at"] = w["freed_at"]
                        w["last_free_seats"] = None

    def save(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        with self.lock:
            data = {"watches": self.watches, "settings": self.settings}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, STATE_FILE)

    # -- helpers -----------------------------------------------------------

    def next_id(self) -> str:
        with self.lock:
            self._seq += 1
            return f"w{int(time.time())}{self._seq}"

    def load_log(self):
        """
        Bring back the last 24 h of log lines and drop everything older.

        Kept as JSON Lines and rewritten on start: it makes "how long was that
        seat actually available" answerable after the fact, which it was not
        while the log lived only in memory and died with every restart.
        """
        cutoff = time.time() - LOG_KEEP_SECONDS
        kept = []
        try:
            with open(LOG_FILE) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("t", 0) >= cutoff:
                        kept.append(e)
        except FileNotFoundError:
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            for e in kept:
                f.write(json.dumps(e) + "\n")
        os.replace(tmp, LOG_FILE)
        with self.lock:
            self.log = kept[-MAX_LOG:]

    def add_log(self, level: str, text: str, watch_id: str | None = None):
        entry = {"t": time.time(), "level": level, "text": text, "watch": watch_id}
        with self.lock:
            self.log.append(entry)
            del self.log[:-MAX_LOG]
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass          # Logging darf den Watcher nie stoppen
        stamp = datetime.now().strftime("%H:%M:%S")
        mark = {"hit": ">>>", "warn": " ! ", "error": " ! "}.get(level, "   ")
        print(f"[{stamp}]{mark}{text}", flush=True)

    def find(self, wid: str) -> dict | None:
        with self.lock:
            return next((w for w in self.watches if w["id"] == wid), None)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "watches": json.loads(json.dumps(self.watches)),
                "log": self.log[-120:],
                "settings": dict(self.settings),
                "poller": dict(self.poller),
                "net": api.stats(),
                "push": {
                    "ntfy": bool(self.settings.get("ntfy_topic") or os.getenv("NTFY_TOPIC")),
                    "telegram": bool(os.getenv("TG_TOKEN") and os.getenv("TG_CHAT")),
                },
                "now": time.time(),
            }


store = Store()


# ---------------------------------------------------------------------------
# catalogue cache (cinema list + programme pages)
# ---------------------------------------------------------------------------

class Catalogue:
    def __init__(self):
        self.lock = threading.Lock()
        self._cinemas: tuple[float, list] | None = None
        self._programmes: dict[str, tuple[float, dict]] = {}

    def cinemas(self) -> list[dict]:
        with self.lock:
            if self._cinemas and time.time() - self._cinemas[0] < CINEMAS_TTL:
                return self._cinemas[1]
        data = api.fetch_cinemas()
        with self.lock:
            self._cinemas = (time.time(), data)
        return data

    def programme(self, slug: str, force: bool = False) -> dict:
        """`force` skips the cache -- otherwise a refresh inside the TTL would
        re-read seat counts but never notice a film that was just added."""
        with self.lock:
            hit = self._programmes.get(slug)
            if hit and not force and time.time() - hit[0] < CATALOGUE_TTL:
                return hit[1]
        data = api.fetch_programme(slug)
        with self.lock:
            self._programmes[slug] = (time.time(), data)
        return data


catalogue = Catalogue()


# ---------------------------------------------------------------------------
# film index -- the whole programme, seen film-first instead of cinema-first
# ---------------------------------------------------------------------------

def _publish(films: dict) -> list[dict]:
    """A private copy of the index, safe to hand to the HTTP threads.

    The crawl keeps appending to the film dicts while a request may be
    serialising them, so every snapshot gets fresh outer objects. The screening
    dicts themselves are never touched again after they are created, so those
    can be shared.
    """
    out = []
    for f in films.values():
        g = dict(f)
        g["screenings"] = sorted(f["screenings"], key=lambda s: s["start"])
        out.append(g)
    out.sort(key=lambda f: f["title"].lower())
    return out


def _mark_new(out: list[dict], seen: dict, seeding: bool,
              now: float, prev_built: float | None) -> None:
    """Flag the films that were not in the programme the last time we looked.

    The badge is anchored to a stored timestamp, not to a diff against the
    previous crawl -- a diff would clear itself on the very next refresh, and
    "what came in this week" is the question worth answering.

    Absence is measured against the previous crawl, never against the clock.
    Measuring against the clock conflated "this film was away" with "nobody
    opened the tab for a fortnight", so a gap in *looking* lit up the entire
    programme as new.

    On the very first crawl nothing is new: there is no baseline to be new
    against, and flagging all 123 films would say nothing at all.
    """
    for f in out:
        rec = seen.get(f["key"])
        if seeding:
            first = 0.0                      # "was here before we started counting"
        elif rec is None:
            first = now                      # never seen before
        elif prev_built and rec.get("last", 0.0) < prev_built - 0.5:
            first = now                      # missing from the last crawl, back now
        else:
            first = rec.get("first", 0.0)
        f["first_seen"] = first
        f["new"] = bool(first) and (now - first) < NEW_FOR


class FilmIndex:
    """
    Every film Yorck currently has on sale anywhere, with all of its dates.

    The programme pages are cinema-first: to find the one 35 mm repertory
    screening three weeks out you would have to open fifteen cinemas and click
    through every day. This turns the same data inside out -- one crawl of all
    cinemas, merged by film slug (stable across houses; vistaId is not, it is
    empty for series and festival entries).

    Cost is one programme page plus one seat lookup per cinema, so about thirty
    small requests for the lot, and a single seat lookup covers a cinema's
    entire horizon -- months of presale in one response. That is too much to
    put on a timer next to a running watch, so it only ever runs when someone
    actually opens the tab, and then holds for FILMS_TTL.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.films: list[dict] = []
        self.built: float | None = None    # end of the last complete crawl
        self.building = False
        self.done = 0
        self.total = 0
        self.error: str | None = None
        self.seen: dict[str, dict] = {}    # film key -> {first, last} seen
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        """Last crawl from disk, so a restart is not a cold minute of waiting."""
        try:
            with open(FILMS_FILE) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("films"), list):
                self.films = d["films"]
                self.built = d.get("built")
                self.seen = d.get("seen") or {}
                if not self.seen and self.built:
                    # An index from before this was tracked is still a perfectly
                    # good baseline -- read it as "all of these were already
                    # here", so the very next crawl can say what has arrived
                    # since, instead of throwing that away and starting blank.
                    self.seen = {f["key"]: {"first": 0.0, "last": self.built}
                                 for f in self.films if f.get("key")}
        except Exception:
            pass

    def _persist(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = FILMS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"built": self.built, "films": self.films,
                           "seen": self.seen}, f)
            os.replace(tmp, FILMS_FILE)
        except Exception as e:
            store.add_log("warn", f"could not save the film index: {e}")

    # -- reading -----------------------------------------------------------

    def snapshot(self, force: bool = False) -> dict:
        """What the UI gets: whatever we have, plus whether it is being redone.

        Stale data is served immediately and refreshed behind it -- a minute of
        blank screen would be a worse answer than a programme from an hour ago.
        """
        with self.lock:
            stale = self.built is None or time.time() - self.built > FILMS_TTL
            out = {
                "films": self.films,
                "built": self.built,
                "building": self.building,
                "done": self.done,
                "total": self.total,
                "stale": stale,
                "error": self.error,
            }
        if force or stale:
            if self.start(force=force):
                out["building"] = True
        with store.lock:
            out["watched"] = sorted({w["session_id"] for w in store.watches})
        return out

    # -- crawling ----------------------------------------------------------

    def start(self, force: bool = False) -> bool:
        with self.lock:
            if self.building:
                return False
            self.building = True
            self.done, self.total, self.error = 0, 0, None
        threading.Thread(target=self._build, args=(force,),
                         name="film-index", daemon=True).start()
        return True

    def _build(self, force: bool = False):
        started = time.time()
        seeding = not self.seen          # first ever crawl: nothing to be new against
        prev_built = self.built
        complete = True
        try:
            cinemas = catalogue.cinemas()
            with self.lock:
                self.total = len(cinemas)
            films: dict[str, dict] = {}
            for c in cinemas:
                try:
                    self._one_cinema(c, films, force)
                except api.RateLimited:
                    raise
                except Exception as e:
                    complete = False
                    store.add_log("warn", f"programme: {c['slug']} skipped ({e})")
                with self.lock:
                    self.done += 1
                    # On a cold start let the list fill cinema by cinema --
                    # something to read beats a spinner. On a refresh there is
                    # already a full list on screen, and replacing it with a
                    # growing stub would wipe it for a minute; keep it and swap
                    # once the new one is complete.
                    if not self.built:
                        out = _publish(films)
                        _mark_new(out, self.seen, seeding, started, prev_built)
                        self.films = out

            now = time.time()
            out = _publish(films)
            _mark_new(out, self.seen, seeding, now, prev_built)
            self._remember(out, now, complete)
            with self.lock:
                self.built = now
                self.films = out
            self._persist()
            shows = sum(len(f["screenings"]) for f in films.values())
            fresh = sum(1 for f in out if f["new"])
            store.add_log("info", f"programme: {len(films)} films, {shows} screenings, "
                                  f"{len(cinemas)} cinemas, {fresh} new "
                                  f"({time.time() - started:.0f}s)")
        except api.RateLimited as e:
            with self.lock:
                self.error = f"yorck.de is rate limiting us: {e}"
            store.add_log("warn", f"programme crawl stopped: {e}")
        except Exception as e:
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
            store.add_log("error", f"programme crawl failed: {e}")
        finally:
            with self.lock:
                self.building = False

    def _remember(self, out: list[dict], now: float, complete: bool) -> None:
        """Move the baseline: everything present now, plus what recently left.

        `complete` is false when a cinema was skipped -- rate limited, or a bad
        response. Its films are missing from this crawl through no fault of
        their own, so nothing counts as departed and the whole programme would
        otherwise come back flagged as new on the next run.
        """
        seen = {f["key"]: {"first": f["first_seen"], "last": now} for f in out}
        for key, rec in self.seen.items():
            if key in seen:
                continue
            if not complete:
                seen[key] = {"first": rec.get("first", 0.0), "last": now}
            elif now - rec.get("last", 0.0) < FORGET_AFTER:
                seen[key] = rec
        self.seen = seen

    def _one_cinema(self, cinema: dict, films: dict, force: bool = False):
        prog = catalogue.programme(cinema["slug"], force=force)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")

        # Contentful lists some screenings twice -- once plainly and once as a
        # "Preview: ..." special. Same rule the picker uses: one row per session
        # id, the plainest title wins.
        pick: dict[str, tuple[dict, dict]] = {}
        for film in prog["films"]:
            for s in film["sessions"]:
                if s["start"] < now:
                    continue
                old = pick.get(s["session_id"])
                if old and len(old[0]["title"]) <= len(film["title"]):
                    continue
                pick[s["session_id"]] = (film, s)
        if not pick:
            return

        # One lookup for the cinema's whole horizon -- months of presale come
        # back in a single response, so a longer range costs nothing extra.
        dates = sorted({s["date"] for _, s in pick.values()})
        end = (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            avail = api.fetch_availability(cinema["vista_id"], dates[0], end)
        except api.RateLimited:
            raise
        except Exception as e:
            store.add_log("warn", f"seat lookup failed for {cinema['slug']}: {e}")
            avail = {}

        for sid, (film, s) in pick.items():
            info = avail.get(sid)
            status, seats = _classify(info)
            key = film["slug"] or film["title"].strip().lower()
            f = films.get(key)
            if f is None:
                f = films[key] = {
                    "key": key,
                    "slug": film["slug"],
                    "title": film["title"].strip(),
                    "image": film.get("image") or "",
                    "genre": film["label"] or "",
                    "runtime": film["runtime"],
                    "fsk": film["fsk"],
                    "screenings": [],
                }
            else:
                if len(film["title"].strip()) < len(f["title"]):
                    f["title"] = film["title"].strip()
                # A film runs at several cinemas; take a picture from whichever
                # entry actually carries one.
                if not f["image"] and film.get("image"):
                    f["image"] = film["image"]
            f["screenings"].append({
                "cinema_slug": cinema["slug"],
                "cinema_name": cinema["name"],
                "session_id": sid,
                "composite_id": s["id"],
                "date": s["date"],
                "time": s["time"],
                "start": s["start"],
                "formats": s["formats"],
                "status": status,
                "seats": seats,
                "allocated": bool(info.get("allocated")) if info else False,
            })


film_index = FilmIndex()


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------

def notify_desktop(title: str, text: str):
    if not store.settings.get("desktop"):
        return
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return

    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (f'display notification "{esc(text)}" with title "{esc(title)}" '
              f'sound name "Glass"')
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def ntfy_topic() -> str | None:
    """
    The push topic, from NTFY_TOPIC or -- once it has been seen there -- from
    the saved settings, so a plain restart keeps pushing to the phone instead
    of silently going quiet.
    """
    env = os.getenv("NTFY_TOPIC")
    if env:
        with store.lock:
            if store.settings.get("ntfy_topic") != env:
                store.settings["ntfy_topic"] = env
                store.settings["ntfy_server"] = os.getenv("NTFY_SERVER") or None
                store.save()
        return env
    return store.settings.get("ntfy_topic")


CHECKOUT_MODES = ("off", "open", "auto")

# Which row the auto-checkout clicks. Matched against the visible label on the
# page, so these must read exactly as the checkout prints them. UNLIMITED is
# good for one seat per screening -- a second ticket (for someone else) needs
# one of the paid rows.
TICKET_CHOICES = (
    "Yorck Unlimited",                 # 0,00 EUR, once per screening
    "Mitglieder Ticket",               # members' online discount, 1 EUR off
    "Begleitticket (Unlimited 10%)",   # for companions of an Unlimited holder
    "Normal (Online)",                 # full price, no membership needed
)
DEFAULT_TICKET = "Yorck Unlimited"


# Was eine frisch angelegte Beobachtung bekommt. Bewusst eine Konstante und
# keine Einstellung: eine globale Vorgabe, die nur NEUE Watches betrifft, sieht
# aus wie ein Schalter fuer alles und ist dann keiner. Der Modus gehoert an die
# Karte, wo man auch sieht, worauf er wirkt.
DEFAULT_CHECKOUT_MODE = "open"


def default_mode() -> str:
    return DEFAULT_CHECKOUT_MODE


def watch_interval(w: dict) -> int:
    """Wie oft diese eine Vorstellung geprueft wird, in Sekunden."""
    try:
        v = int(w.get("interval") or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        v = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


def watch_ticket(w: dict) -> str:
    t = w.get("ticket_type")
    return t if t in TICKET_CHOICES else DEFAULT_TICKET


def watch_mode(w: dict) -> str:
    """
    Was bei dieser einen Vorstellung passieren soll.

    Pro Watch gesetzt, weil sich das je Film unterscheidet: bei einem
    ausverkauften Film, den man unbedingt will, soll durchgebucht werden; bei
    einem, den man nur beobachtet, reicht eine Meldung.
    """
    m = w.get("checkout_mode")
    return m if m in CHECKOUT_MODES else default_mode()


# One attempt per freed seat. Retrying reloaded the checkout three times in
# half a minute, and the failures that actually occur -- a ticket type the site
# refuses -- do not get better by trying again. The counter resets when the
# seat disappears, so a genuinely new opportunity still gets a fresh attempt.
MAX_CHECKOUT_ATTEMPTS = 1


def grab_seat(w: dict) -> None:
    """
    React to a seat being available, according to this watch's own mode.

      off   do nothing
      open  open the checkout in the user's Chrome -- once; the tab stays put,
            so reopening it every cycle would only spam tabs
      auto  additionally pick the UNLIMITED ticket and place the order, and
            RETRY on failure while the seat is still there

    The retry is the point: an attempt can fail for reasons that pass (signed
    out, Chrome busy, a timeout), and a single failure must not mean the ticket
    is missed. Called on every check while the seat is available, so the guards
    below -- not the caller -- decide whether anything happens.

    Hard stops: never after a successful booking, never more than
    MAX_CHECKOUT_ATTEMPTS orders, never two runs at once.

    "auto" is refused for allocated-seating cinemas: those insert a seat-picking
    step this flow does not know, and guessing there would book the wrong thing.
    """
    mode = watch_mode(w)
    if mode == "off" or sys.platform != "darwin":
        return

    full = mode == "auto" and not w.get("allocated")

    with store.lock:
        if w.get("booked") or w.get("checkout_running"):
            return
        attempts = w.get("checkout_attempts", 0)
        if not full:
            # "open" and the allocated-seating fallback: one tab, then done.
            if w.get("grabbed"):
                return
            w["grabbed"] = time.time()
        else:
            if attempts >= MAX_CHECKOUT_ATTEMPTS:
                return
            w["checkout_attempts"] = attempts + 1
            w["grabbed"] = time.time()
            w["checkout_running"] = True
        n = w.get("checkout_attempts", 0)

    if mode == "auto" and w.get("allocated"):
        store.add_log("warn", f"{w['film_title']}: auto-booking needs free seating "
                              f"-- only opening the checkout", w["id"])

    def run():
        try:
            if not full:
                subprocess.Popen(["open", "-a", "Google Chrome", w["booking_url"]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.Popen(["osascript", "-e",
                                  'tell application "Google Chrome" to activate'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                store.add_log("hit", f"Checkout opened (pick the ticket yourself): "
                                     f"{w['film_title']}", w["id"])
                return

            store.add_log("hit", f"Auto-checkout attempt {n}/{MAX_CHECKOUT_ATTEMPTS}: "
                                 f"{w['film_title']} ({watch_ticket(w)})", w["id"])
            res = checkout_drive.book(w["composite_id"], w["start"],
                                      ticket=watch_ticket(w),
                                      log=lambda m: store.add_log("info", m, w["id"]))

            if res.get("reserved"):
                # Signed out: the ticket sits in the basket and the seat is held
                # for the order session. Not booked -- the human has to finish.
                with store.lock:
                    w["reserved_at"] = time.time()
                store.add_log("hit", f"IN BASKET (signed out): {w['film_title']} - "
                                     f"{res.get('ticket')} -- sign in and finish, "
                                     f"the checkout tab is open", w["id"])
                notify_ntfy(f"Seat held: {w['film_title']}",
                            f"You are signed out. {res.get('ticket')} is in the basket "
                            f"-- finish within ~10 min.", click=w["booking_url"])
                notify_desktop("Yorck Monitor - seat in basket",
                               f"{w['film_title']}\nSigned out -- finish the checkout")
                return

            with store.lock:
                w["booked"] = time.time()
            store.add_log("hit", f"BOOKED: {w['film_title']} - {w['date_label']}", w["id"])
            notify_ntfy(f"Booked: {w['film_title']}",
                        f"{w['cinema_name']} - {w['date_label']} - {watch_ticket(w)}",
                        click=w["listing_url"])
            notify_desktop("Yorck Monitor - booked", f"{w['film_title']}\n{w['date_label']}")

        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            if n < MAX_CHECKOUT_ATTEMPTS:
                store.add_log("error", f"Auto-checkout attempt {n} failed ({reason}) "
                                       f"-- retrying on the next check", w["id"])
            else:
                store.add_log("error", f"Auto-checkout gave up after {n} attempts "
                                       f"({reason}) -- book it yourself", w["id"])
                notify_ntfy(f"Auto-checkout failed: {w['film_title']}",
                            f"{reason[:120]} -- book it yourself",
                            click=w["booking_url"])
                notify_desktop("Yorck Monitor - auto-checkout failed",
                               f"{w['film_title']}\n{reason[:100]}")
        finally:
            with store.lock:
                w["checkout_running"] = False
            store.save()

    threading.Thread(target=run, name=f"checkout-{w['id']}", daemon=True).start()


def notify_ntfy(title: str, text: str, click: str | None = None):
    """
    Push to the phone via ntfy.sh -- no account, no API key.

    Set NTFY_TOPIC to an unguessable name and subscribe to it in the ntfy app.
    NTFY_SERVER lets you point at your own instance instead.

    The JSON publish API is used rather than the header-based one because film
    titles carry umlauts and en-dashes, and HTTP headers are ASCII-only.
    """
    topic = ntfy_topic()
    if not topic:
        return
    server = (os.getenv("NTFY_SERVER")
              or store.settings.get("ntfy_server")
              or "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": topic,
        "title": title,
        "message": text,
        "priority": 5,          # urgent: bypasses the phone's quiet modes
        "tags": ["ticket"],
    }
    if click:
        payload["click"] = click        # tapping opens the booking page
    req = urllib.request.Request(
        server, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        store.add_log("warn", f"ntfy failed: {e}")


def notify_telegram(text: str):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15)
    except Exception as e:
        store.add_log("warn", f"Telegram failed: {e}")


# ---------------------------------------------------------------------------
# the watcher
# ---------------------------------------------------------------------------

def _classify(info: dict | None) -> tuple[str, int | None]:
    """Map a raw availability record onto a status the UI can show."""
    if info is None:
        return "gone", None
    seats = info.get("seats")
    if not info.get("sales_open"):
        return "closed", seats
    if seats is None:
        return "unknown", None
    if seats <= 0:
        return "sold_out", 0
    return "available", seats


class Watcher(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="watcher")
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self.fails = 0
        self.due: dict[str, float] = {}   # cinema_id -> naechster Faelligkeitszeitpunkt
        self.capped = None                # zuletzt gemeldete Drossel-Warnung

    # -- main loop ---------------------------------------------------------

    def run(self):
        store.poller["running"] = True
        while not self.stop_flag.is_set():
            delay = self.cycle()
            with store.lock:
                store.poller["next_check"] = time.time() + delay
            self.wake.wait(delay)
            self.wake.clear()
        store.poller["running"] = False

    def cycle(self) -> float:
        """
        Check the cinemas that are due, then sleep until the next one is.

        Each watch carries its own interval, and a cinema is polled at the
        shortest interval any of its watches asks for -- one request covers all
        of them anyway, so a slow watch costs nothing extra when it shares a
        cinema with a fast one.
        """
        # Adding a watch or changing a setting wakes the loop early. Keep a
        # floor between cycles so a burst of UI clicks cannot turn into a burst
        # of requests.
        last = store.poller.get("last_check")
        if last:
            cooling = MIN_CYCLE_GAP - (time.time() - last)
            if cooling > 0:
                self.wake.clear()
                if self.stop_flag.wait(cooling):
                    return 0.1

        self.retire_past_screenings()
        groups = self.active_groups()

        if not groups:
            with store.lock:
                store.poller["message"] = "no active watches"
                store.poller["checking"] = False
            self.due.clear()
            return 5.0

        now = time.time()
        rates = {cid: min(watch_interval(w) for w in ws) for cid, ws in groups.items()}
        for cid in list(self.due):
            if cid not in groups:
                del self.due[cid]                    # Kino nicht mehr beobachtet
        # Ein schon gesetzter Termin darf nie weiter weg liegen als das aktuell
        # gewuenschte Intervall. Sonst bleibt ein Kino, das eben noch auf 10
        # Minuten stand, nach dem Umschalten auf 5 Sekunden minutenlang geparkt
        # -- also genau dann, wenn man die Frequenz gerade hochgedreht hat.
        for cid, rate in rates.items():
            if cid in self.due:
                self.due[cid] = min(self.due[cid], now + rate)

        due_now = [cid for cid in groups if now >= self.due.get(cid, 0)]

        if not due_now:
            with store.lock:
                store.poller["message"] = "watching"
                store.poller["checking"] = False
            return max(1.0, min(self.due[c] for c in groups) - now)

        span = min(rates.values())
        need = len(groups) * api.MIN_REQUEST_GAP
        if need > span and self.capped != (len(groups), span):
            self.capped = (len(groups), span)
            store.add_log("warn", f"{span}s interval cannot be met with {len(groups)} cinemas "
                                  f"-- the {api.MIN_REQUEST_GAP:.0f}s request throttle caps it "
                                  f"at about {need:.0f}s")
        with store.lock:
            store.poller["checking"] = True
            store.poller["message"] = f"checking {len(due_now)} cinema(s)"

        hit_limit = False
        for n, cinema_id in enumerate(due_now):
            if self.stop_flag.is_set():
                break
            if n:
                # Abstand zwischen zwei Kinos: normalerweise ~4,5 s, bei sehr
                # kurzen Intervallen entsprechend weniger -- sonst waere ein
                # 5-Sekunden-Takt bei zwei Kinos gar nicht erreichbar. Die
                # harte Untergrenze bleibt api.MIN_REQUEST_GAP.
                span = min(rates.values())
                time.sleep(api.jitter(max(api.MIN_REQUEST_GAP, min(4.5, span / 3)), 0.3))
            try:
                self.check_group(cinema_id, groups[cinema_id])
                self.fails = 0
                self.due[cinema_id] = time.time() + api.jitter(rates[cinema_id])
            except api.RateLimited as e:
                hit_limit = True
                store.add_log("error", f"Rate limited: {e} -- pausing 15 min")
                break
            except Exception as e:
                self.fails += 1
                self.due[cinema_id] = time.time() + api.jitter(rates[cinema_id])
                store.add_log("error", f"Check failed ({type(e).__name__}: {e})")

        store.save()
        with store.lock:
            store.poller["checking"] = False
            store.poller["last_check"] = time.time()

        if hit_limit:
            until = time.time() + 900
            for cid in groups:
                self.due[cid] = time.time() + 900
            with store.lock:
                store.poller["backoff_until"] = until
                store.poller["message"] = "backing off after rate limit"
            return 900.0

        with store.lock:
            store.poller["backoff_until"] = None

        if self.fails:
            wait = min(MAX_INTERVAL, min(rates.values()) * (2 ** min(self.fails, 4)))
            for cid in due_now:
                self.due[cid] = time.time() + wait
            with store.lock:
                store.poller["message"] = f"backing off ({self.fails} failed checks)"
            return api.jitter(wait)

        with store.lock:
            store.poller["message"] = "watching"
        return max(1.0, min(self.due.values()) - time.time())

    # -- one cinema per request -------------------------------------------

    def active_groups(self) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        with store.lock:
            for w in store.watches:
                if w.get("active") and w.get("status") != "past":
                    groups.setdefault(w["cinema_id"], []).append(w)
        return groups

    def check_group(self, cinema_id: str, watches: list[dict]):
        dates = sorted({w["start"][:10] for w in watches})
        date_from = dates[0]
        date_to = (datetime.strptime(dates[-1], "%Y-%m-%d")
                   + timedelta(days=1)).strftime("%Y-%m-%d")

        avail = api.fetch_availability(cinema_id, date_from, date_to)

        for w in watches:
            info = avail.get(str(w["session_id"]))
            status, seats = _classify(info)
            self.apply(w, status, seats)

    def apply(self, w: dict, status: str, seats: int | None):
        prev = w.get("status")
        now = time.time()

        with store.lock:
            w["status"] = status
            w["seats"] = seats
            w["last_checked"] = now
            if status == "available":
                # Wann zuletzt ueberhaupt ein Platz online war -- die Zahl, an
                # der man ablesen kann, ob sich das Warten lohnt.
                w["last_free_at"] = now
                w["last_free_seats"] = seats
            w["checks"] = w.get("checks", 0) + 1
            w["history"].append({"t": now, "s": status, "n": seats})
            del w["history"][:-MAX_HISTORY]

        label = {
            "available": f"{seats} seat{'s' if seats != 1 else ''} free",
            "sold_out": "sold out",
            "closed": "sales closed",
            "gone": "screening no longer listed",
            "unknown": "no seat data",
        }.get(status, status)

        where = f"{w['film_title']} - {w['cinema_name']} {w['date_label']}"

        if status == "available" and prev != "available":
            with store.lock:
                w["alert"] = True
                w["freed_at"] = now
            msg = f"TICKETS FREE: {where} - {label}"
            store.add_log("hit", msg, w["id"])
            grab_seat(w)          # zuerst den Platz sichern, dann Bescheid geben
            notify_desktop("Yorck Monitor - tickets free", f"{where}\n{label}")
            notify_ntfy(f"Tickets free: {w['film_title']}",
                        f"{w['cinema_name']} - {w['date_label']} - {label}",
                        click=w["booking_url"])
            notify_telegram(f"{msg}\n{w['booking_url']}")
            print("\a", end="", flush=True)
        elif status == "available":
            store.add_log("ok", f"still free: {where} - {label}", w["id"])
            grab_seat(w)      # erneut antreten, falls ein Versuch scheiterte
        elif prev == "available":
            # Der Platz ist wieder weg. Alarm loeschen, sonst blinkt die Karte
            # gruen weiter, waehrend sie AUSVERKAUFT anzeigt.
            with store.lock:
                w["alert"] = False
                # Platz weg: Zaehler zuruecksetzen, damit ein spaeter erneut
                # freier Platz wieder volle Versuche bekommt. Gebuchtes bleibt.
                if not w.get("booked"):
                    w["checkout_attempts"] = 0
                    w["grabbed"] = None
            store.add_log("warn", f"{where}: seat gone again ({label})", w["id"])
        elif status != prev and prev is not None:
            store.add_log("warn", f"{where}: {prev} -> {label}", w["id"])
        else:
            store.add_log("info", f"{where}: {label}", w["id"])

    # -- housekeeping ------------------------------------------------------

    def retire_past_screenings(self):
        now = datetime.now()
        with store.lock:
            for w in store.watches:
                if w.get("status") == "past":
                    continue
                try:
                    start = _parse_start(w["start"])
                except Exception:
                    continue
                if now > start + timedelta(minutes=15):
                    w["status"] = "past"
                    w["active"] = False
                    store.add_log("info", f"{w['film_title']}: screening started, watch stopped",
                                  w["id"])


watcher = Watcher()


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "YorckMonitor/1.0"

    def log_message(self, *a):
        pass  # the watcher does the talking

    # -- plumbing ----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def fail(self, msg: str, code: int = 400):
        self.json({"error": msg}, code)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self.json(store.snapshot())
            if u.path == "/api/cinemas":
                return self.json({"cinemas": catalogue.cinemas()})
            if u.path == "/api/programme":
                slug = (q.get("cinema") or [""])[0]
                if not slug:
                    return self.fail("cinema missing")
                return self.json(catalogue.programme(slug))
            if u.path == "/api/showtimes":
                return self.showtimes(q)
            if u.path == "/api/films":
                return self.json(film_index.snapshot(
                    force=(q.get("refresh") or [""])[0] == "1"))
        except api.RateLimited as e:
            return self.fail(f"yorck.de is rate limiting us: {e}", 503)
        except Exception as e:
            return self.fail(f"{type(e).__name__}: {e}", 500)
        self.fail("not found", 404)

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [p for p in u.path.split("/") if p]
        try:
            if u.path == "/api/watch":
                return self.add_watch(self.body())
            if u.path == "/api/settings":
                return self.update_settings(self.body())
            if u.path == "/api/check-now":
                watcher.wake.set()
                return self.json({"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "watch"]:
                return self.watch_action(parts[2], parts[3], self.body())
        except Exception as e:
            return self.fail(f"{type(e).__name__}: {e}", 500)
        self.fail("not found", 404)

    # -- picker ------------------------------------------------------------

    def showtimes(self, q):
        """
        Every screening at one cinema on one day, with live seat counts, so the
        picker itself already shows what is sold out. Two upstream requests at
        most, both cached.
        """
        slug = (q.get("cinema") or [""])[0]
        date = (q.get("date") or [""])[0]
        if not slug or not date:
            return self.fail("cinema and date required")

        cinema = next((c for c in catalogue.cinemas() if c["slug"] == slug), None)
        if not cinema:
            return self.fail(f"unknown cinema {slug!r}", 404)

        prog = catalogue.programme(slug)
        rows: dict[str, dict] = {}
        for film in prog["films"]:
            for s in film["sessions"]:
                if s["date"] != date:
                    continue
                # Contentful sometimes lists one screening twice (e.g. as a
                # "Preview: ..." special). Keep the plainest title.
                old = rows.get(s["session_id"])
                if old and len(old["film_title"]) <= len(film["title"]):
                    continue
                rows[s["session_id"]] = {
                    "session_id": s["session_id"],
                    "composite_id": s["id"],
                    "film_title": film["title"],
                    "film_slug": film["slug"],
                    "runtime": film["runtime"],
                    "fsk": film["fsk"],
                    "label": film["label"],
                    "time": s["time"],
                    "start": s["start"],
                    "formats": s["formats"],
                    "seats": None,
                    "status": "unknown",
                    "allocated": None,
                }

        try:
            nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            avail = api.fetch_availability(cinema["vista_id"], date, nxt)
        except api.RateLimited:
            raise
        except Exception as e:
            store.add_log("warn", f"seat lookup failed for {slug} {date}: {e}")
            avail = {}

        for sid, row in rows.items():
            info = avail.get(sid)
            status, seats = _classify(info)
            row["status"] = status
            row["seats"] = seats
            row["allocated"] = bool(info.get("allocated")) if info else None

        with store.lock:
            watched = {w["session_id"] for w in store.watches if w.get("active")}
        for row in rows.values():
            row["watched"] = row["session_id"] in watched

        out = sorted(rows.values(), key=lambda r: (r["time"], r["film_title"]))
        return self.json({"cinema": cinema, "date": date, "dates": prog["dates"],
                          "showtimes": out})

    # -- mutations ---------------------------------------------------------

    def add_watch(self, b: dict):
        slug = b.get("cinema")
        sid = str(b.get("session_id") or "")
        if not slug or not sid:
            return self.fail("cinema and session_id required")

        cinema = next((c for c in catalogue.cinemas() if c["slug"] == slug), None)
        if not cinema:
            return self.fail(f"unknown cinema {slug!r}", 404)

        with store.lock:
            if any(w["session_id"] == sid and w["cinema_id"] == cinema["vista_id"]
                   for w in store.watches):
                return self.fail("already watching that screening", 409)

        prog = catalogue.programme(slug)
        found = None
        for film in prog["films"]:
            for s in film["sessions"]:
                if s["session_id"] == sid:
                    if found and len(found[0]["title"]) <= len(film["title"]):
                        continue
                    found = (film, s)
        if not found:
            return self.fail("screening not in the current programme", 404)
        film, s = found

        start = _parse_start(s["start"])
        allocated = bool(b.get("allocated"))
        w = {
            "id": store.next_id(),
            "cinema_id": cinema["vista_id"],
            "cinema_slug": cinema["slug"],
            "cinema_name": cinema["name"],
            "session_id": sid,
            "composite_id": s["id"],
            "film_title": film["title"],
            "film_slug": film["slug"],
            "start": s["start"],
            "date": s["date"],
            "time": s["time"],
            "date_label": date_label(start),
            "formats": s["formats"],
            "allocated": allocated,
            "interval": DEFAULT_INTERVAL,
            "ticket_type": DEFAULT_TICKET,
            "checkout_mode": (b.get("checkout_mode")
                              if b.get("checkout_mode") in CHECKOUT_MODES
                              else default_mode()),
            "booking_url": api.booking_url(s["id"], allocated),
            "listing_url": api.listing_url(cinema["slug"], s["date"], film["slug"]),
            "active": True,
            "alert": False,
            "status": None,
            "seats": None,
            "checks": 0,
            "last_checked": None,
            "history": [],
            "created": time.time(),
        }
        with store.lock:
            store.watches.append(w)
        store.save()
        store.add_log("info", f"watching {w['film_title']} - {w['cinema_name']} {w['date_label']}",
                      w["id"])
        watcher.wake.set()
        return self.json({"watch": w})

    def watch_action(self, wid: str, action: str, body: dict | None = None):
        w = store.find(wid)
        if not w:
            return self.fail("no such watch", 404)
        if action == "remove":
            with store.lock:
                store.watches = [x for x in store.watches if x["id"] != wid]
            store.save()
            store.add_log("info", f"stopped watching {w['film_title']}")
            return self.json({"ok": True})
        if action in ("pause", "resume"):
            with store.lock:
                w["active"] = (action == "resume")
                if w["active"] and w.get("status") == "past":
                    w["status"] = None
            store.save()
            store.add_log("info", f"{action}d {w['film_title']}", wid)
            if w["active"]:
                watcher.wake.set()
            return self.json({"watch": w})
        if action == "mode":
            mode = (body or {}).get("mode")
            if mode not in CHECKOUT_MODES:
                return self.fail(f"mode must be one of {CHECKOUT_MODES}")
            if mode == "auto" and w.get("allocated"):
                return self.fail("Auto-booking only works with free seating "
                                 "(this cinema has assigned seats)", 409)
            with store.lock:
                w["checkout_mode"] = mode
            store.save()
            store.add_log("info", f"{w['film_title']}: mode -> {mode}", wid)
            return self.json({"watch": w})
        if action == "interval":
            try:
                v = int((body or {}).get("interval"))
            except (TypeError, ValueError):
                return self.fail("interval must be a number")
            if v not in INTERVAL_CHOICES:
                return self.fail(f"interval must be one of {INTERVAL_CHOICES}")
            with store.lock:
                w["interval"] = v
            store.save()
            store.add_log("info", f"{w['film_title']}: interval -> {v}s", wid)
            watcher.wake.set()
            return self.json({"watch": w})
        if action == "ticket":
            t = (body or {}).get("ticket")
            if t not in TICKET_CHOICES:
                return self.fail(f"ticket must be one of {TICKET_CHOICES}")
            with store.lock:
                w["ticket_type"] = t
            store.save()
            store.add_log("info", f"{w['film_title']}: ticket -> {t}", wid)
            return self.json({"watch": w})
        if action == "ack":
            with store.lock:
                w["alert"] = False
            store.save()
            return self.json({"watch": w})
        return self.fail("unknown action", 404)

    def update_settings(self, b: dict):
        with store.lock:
            for key in ("sound", "desktop"):
                if key in b:
                    store.settings[key] = bool(b[key])
        store.save()
        watcher.wake.set()
        return self.json({"settings": store.settings})


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def open_window(url: str, app_mode: bool):
    chrome = "/Applications/Google Chrome.app"
    if app_mode and sys.platform == "darwin" and os.path.exists(chrome):
        subprocess.Popen(["open", "-na", "Google Chrome", "--args",
                          f"--app={url}", "--window-size=460,940",
                          "--window-position=1460,40"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        opener = {"darwin": "open"}.get(sys.platform, "xdg-open")
        if shutil.which(opener):
            subprocess.Popen([opener, url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description="Local ticket watcher for Yorck cinemas")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open the UI in your browser")
    ap.add_argument("--app", action="store_true",
                    help="open a small chrome-less window (macOS + Chrome)")
    args = ap.parse_args()

    url = f"http://localhost:{args.port}/"
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"Cannot bind {args.host}:{args.port} -- {e}")
        print("Something else is using the port; try --port 4001.")
        return 1

    # Resolve (and thereby persist) the push topic before anything else, so a
    # later restart without the env var still reaches the phone.
    topic = ntfy_topic()
    watcher.start()
    print(f"Yorck Monitor on {url}")
    channels = ["desktop"]
    if topic:
        channels.append(f"ntfy ({topic})")
    if os.getenv("TG_TOKEN") and os.getenv("TG_CHAT"):
        channels.append("telegram")
    print(f"Alerts: {', '.join(channels)}")
    print(f"State: {STATE_FILE}")
    print(f"Interval: per screening ({DEFAULT_INTERVAL}s default, jittered), "
          f"min gap between requests {api.MIN_REQUEST_GAP:.0f}s")
    print("Ctrl+C to stop.\n")

    if args.open or args.app:
        threading.Timer(0.6, open_window, (url, args.app)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        watcher.stop_flag.set()
        watcher.wake.set()
        store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
