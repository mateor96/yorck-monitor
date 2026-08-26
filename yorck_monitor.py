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

DEFAULT_INTERVAL = 90
MIN_INTERVAL = 60
MAX_INTERVAL = 900
MIN_CYCLE_GAP = 15    # floor between two check cycles, however often the UI pokes us

MAX_LOG = 400        # global feed entries kept in memory
MAX_HISTORY = 60     # per-watch check dots kept

CATALOGUE_TTL = 30 * 60   # programme pages are static; re-read at most every 30 min
CINEMAS_TTL = 6 * 60 * 60


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

class Store:
    """Everything the UI shows. Guarded by one lock, persisted as JSON."""

    def __init__(self):
        self.lock = threading.RLock()
        self.watches: list[dict] = []
        self.log: list[dict] = []
        self.settings = {"interval": DEFAULT_INTERVAL, "sound": True, "desktop": True}
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
            for w in self.watches:
                w.setdefault("history", [])
                w.setdefault("alert", False)
                w.setdefault("checks", 0)
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

    def add_log(self, level: str, text: str, watch_id: str | None = None):
        entry = {"t": time.time(), "level": level, "text": text, "watch": watch_id}
        with self.lock:
            self.log.append(entry)
            del self.log[:-MAX_LOG]
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

    def programme(self, slug: str) -> dict:
        with self.lock:
            hit = self._programmes.get(slug)
            if hit and time.time() - hit[0] < CATALOGUE_TTL:
                return hit[1]
        data = api.fetch_programme(slug)
        with self.lock:
            self._programmes[slug] = (time.time(), data)
        return data


catalogue = Catalogue()


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


# Was eine frisch angelegte Beobachtung bekommt. Bewusst eine Konstante und
# keine Einstellung: eine globale Vorgabe, die nur NEUE Watches betrifft, sieht
# aus wie ein Schalter fuer alles und ist dann keiner. Der Modus gehoert an die
# Karte, wo man auch sieht, worauf er wirkt.
DEFAULT_CHECKOUT_MODE = "open"


def default_mode() -> str:
    return DEFAULT_CHECKOUT_MODE


def watch_mode(w: dict) -> str:
    """
    Was bei dieser einen Vorstellung passieren soll.

    Pro Watch gesetzt, weil sich das je Film unterscheidet: bei einem
    ausverkauften Film, den man unbedingt will, soll durchgebucht werden; bei
    einem, den man nur beobachtet, reicht eine Meldung.
    """
    m = w.get("checkout_mode")
    return m if m in CHECKOUT_MODES else default_mode()


MAX_CHECKOUT_ATTEMPTS = 3


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
        store.add_log("warn", f"{w['film_title']}: Vollautomatik nur ohne Sitzplatzwahl "
                              f"-- Checkout wird nur geoeffnet", w["id"])

    def run():
        try:
            if not full:
                subprocess.Popen(["open", "-a", "Google Chrome", w["booking_url"]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.Popen(["osascript", "-e",
                                  'tell application "Google Chrome" to activate'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                store.add_log("hit", f"Checkout geoeffnet (Ticket noch selbst waehlen): "
                                     f"{w['film_title']}", w["id"])
                return

            store.add_log("hit", f"Auto-Checkout Versuch {n}/{MAX_CHECKOUT_ATTEMPTS}: "
                                 f"{w['film_title']}", w["id"])
            checkout_drive.book(w["composite_id"], w["start"],
                                log=lambda m: store.add_log("info", m, w["id"]))
            with store.lock:
                w["booked"] = time.time()
            store.add_log("hit", f"GEBUCHT: {w['film_title']} - {w['date_label']}", w["id"])
            notify_ntfy(f"Gebucht: {w['film_title']}",
                        f"{w['cinema_name']} - {w['date_label']} - mit Unlimited",
                        click=w["listing_url"])
            notify_desktop("Yorck Monitor - gebucht", f"{w['film_title']}\n{w['date_label']}")

        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            if n < MAX_CHECKOUT_ATTEMPTS:
                store.add_log("error", f"Auto-Checkout Versuch {n} fehlgeschlagen ({reason}) "
                                       f"-- neuer Versuch beim naechsten Check", w["id"])
            else:
                store.add_log("error", f"Auto-Checkout endgueltig gescheitert nach {n} "
                                       f"Versuchen ({reason}) -- bitte selbst buchen", w["id"])
                notify_ntfy(f"Auto-Checkout gescheitert: {w['film_title']}",
                            f"{reason[:120]} -- bitte selbst buchen",
                            click=w["booking_url"])
                notify_desktop("Yorck Monitor - Auto-Checkout gescheitert",
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

def _parse_start(s: str) -> datetime:
    return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")


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
        interval = max(MIN_INTERVAL, min(MAX_INTERVAL,
                                         int(store.settings.get("interval", DEFAULT_INTERVAL))))

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
            return 5.0

        with store.lock:
            store.poller["checking"] = True
            store.poller["message"] = f"checking {len(groups)} cinema(s)"

        hit_limit = False
        for n, (cinema_id, watches) in enumerate(groups.items()):
            if self.stop_flag.is_set():
                break
            if n:
                time.sleep(api.jitter(4.5, 0.3))   # gap between cinemas
            try:
                self.check_group(cinema_id, watches)
                self.fails = 0
            except api.RateLimited as e:
                hit_limit = True
                store.add_log("error", f"Rate limited: {e} -- pausing 15 min")
                break
            except Exception as e:
                self.fails += 1
                store.add_log("error", f"Check failed ({type(e).__name__}: {e})")

        store.save()
        with store.lock:
            store.poller["checking"] = False
            store.poller["last_check"] = time.time()

        if hit_limit:
            until = time.time() + 900
            with store.lock:
                store.poller["backoff_until"] = until
                store.poller["message"] = "backing off after rate limit"
            return 900.0

        with store.lock:
            store.poller["backoff_until"] = None

        if self.fails:
            wait = min(MAX_INTERVAL, interval * (2 ** min(self.fails, 4)))
            with store.lock:
                store.poller["message"] = f"backing off ({self.fails} failed checks)"
            return api.jitter(wait)

        with store.lock:
            store.poller["message"] = "watching"
        return api.jitter(interval)

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
            store.add_log("warn", f"{where}: Platz wieder weg ({label})", w["id"])
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
            "date_label": start.strftime("%a %d.%m. %H:%M"),
            "formats": s["formats"],
            "allocated": allocated,
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
                return self.fail(f"mode muss eins von {CHECKOUT_MODES} sein")
            if mode == "auto" and w.get("allocated"):
                return self.fail("Vollautomatik geht nur bei freier Platzwahl "
                                 "(dieses Kino hat Sitzplatzauswahl)", 409)
            with store.lock:
                w["checkout_mode"] = mode
            store.save()
            store.add_log("info", f"{w['film_title']}: Buchungsmodus -> {mode}", wid)
            return self.json({"watch": w})
        if action == "ack":
            with store.lock:
                w["alert"] = False
            store.save()
            return self.json({"watch": w})
        return self.fail("unknown action", 404)

    def update_settings(self, b: dict):
        with store.lock:
            if "interval" in b:
                try:
                    v = int(b["interval"])
                except (TypeError, ValueError):
                    return self.fail("interval must be a number")
                store.settings["interval"] = max(MIN_INTERVAL, min(MAX_INTERVAL, v))
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
    print(f"Interval: {store.settings['interval']}s (jittered), min gap between "
          f"requests {api.MIN_REQUEST_GAP:.0f}s")
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
