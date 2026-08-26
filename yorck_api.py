#!/usr/bin/env python3
"""
Thin, polite client for the public data endpoints behind yorck.de.

Two sources are used, both of them the exact same ones the yorck.de frontend
itself talks to:

1. www.yorck.de/en/cinemas[/<slug>]  ->  the Next.js __NEXT_DATA__ blob.
   Gives the cinema list and the full programme (film titles, slugs, showtimes,
   session ids). Static, cached for a long time -- this is the *catalogue*.

2. <api-gateway>/vista/OData.svc/Sessions  ->  Vista ticketing OData.
   Gives `SeatsAvailable` per session. This is the *availability* signal and
   the only thing that gets polled repeatedly.

Nothing here ever touches the booking flow: no orders are created, no seats are
held. Everything is a plain GET.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = "https://www.yorck.de"
VISTA = "https://uq8lgoj7z2.execute-api.eu-central-1.amazonaws.com/production/api/vista"

# The frontend sends a normal browser UA; matching it keeps us indistinguishable
# from a single ordinary visitor rather than looking like an unknown bot.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Hard floor between *any* two outbound requests, enforced process-wide.
MIN_REQUEST_GAP = 4.0


class RateLimited(Exception):
    """Server pushed back (429/403/503). Caller must back off hard."""


class _Throttle:
    """One global gate: at most one request in flight, >= MIN_REQUEST_GAP apart."""

    def __init__(self, gap: float = MIN_REQUEST_GAP):
        self._gap = gap
        self._lock = threading.Lock()
        self._last = 0.0

    def __enter__(self):
        self._lock.acquire()
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        self._last = time.monotonic()
        self._lock.release()
        return False


_throttle = _Throttle()

_stats = {"requests": 0, "bytes": 0, "errors": 0, "last_request": None}


def stats() -> dict:
    return dict(_stats)


def _get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": SITE + "/",
    }
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with _throttle:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            _stats["errors"] += 1
            if e.code in (403, 429, 503):
                raise RateLimited(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}") from e
            raise
        except Exception:
            _stats["errors"] += 1
            raise
        _stats["requests"] += 1
        _stats["bytes"] += len(body)
        _stats["last_request"] = time.time()
        return body


# --------------------------------------------------------------------------
# 1. Catalogue: cinemas and programme, from the Next.js page payload
# --------------------------------------------------------------------------

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _next_data(path: str) -> dict:
    html = _get(SITE + path, headers={"Accept": "text/html"}).decode("utf-8", "replace")
    m = _NEXT_DATA.search(html)
    if not m:
        raise ValueError(f"no __NEXT_DATA__ on {path} (site layout changed?)")
    return json.loads(m.group(1))


def fetch_cinemas() -> list[dict]:
    """All Yorck cinemas, newest data straight off the cinema listing page."""
    pp = _next_data("/en/cinemas")["props"]["pageProps"]
    out = []
    for c in pp.get("cinemas", []):
        f = c.get("fields", {})
        if not f.get("vistaId") or not f.get("slug"):
            continue
        out.append({
            "vista_id": str(f["vistaId"]),
            "slug": f["slug"],
            "name": (f.get("name") or f["slug"]).strip(),
            "district": f.get("district") or "",
        })
    out.sort(key=lambda c: c["name"].lower())
    return out


def fetch_programme(slug: str) -> dict:
    """
    Full programme of one cinema: every film with every upcoming showtime.

    Returns {"films": [...], "dates": ["YYYY-MM-DD", ...]} where each film is
      {title, slug, vista_id, runtime, fsk, label,
       sessions: [{id, session_id, date, time, start, formats}]}
    `id` is the composite "<cinemaVistaId>-<sessionId>" the site uses in URLs.
    """
    pp = _next_data(f"/en/cinemas/{urllib.parse.quote(slug)}")["props"]["pageProps"]
    films, dates = [], set()

    for entry in (pp.get("filmsSpecials") or []) + (pp.get("presaleFilms") or []):
        f = entry.get("fields") or {}
        title = f.get("title")
        if not title:
            continue
        sessions = []
        for s in f.get("sessions") or []:
            sid = (s.get("sys") or {}).get("id")            # "1015-700"
            start = ((s.get("fields") or {}).get("startTime") or "")[:16]  # naive local
            if not sid or len(start) < 16:
                continue
            sessions.append({
                "id": sid,
                "session_id": sid.split("-", 1)[-1],
                "date": start[:10],
                "time": start[11:16],
                "start": start,
                "formats": (s.get("fields") or {}).get("formats") or [],
            })
            dates.add(start[:10])
        if not sessions:
            continue
        sessions.sort(key=lambda s: s["start"])
        films.append({
            "title": title.strip(),
            "slug": f.get("slug") or "",
            "vista_id": f.get("vistaId") or "",
            "runtime": f.get("runtime"),
            "fsk": f.get("fsk"),
            "label": f.get("mainLabel") or "",
            "sessions": sessions,
        })

    films.sort(key=lambda f: f["title"].lower())
    return {"films": films, "dates": sorted(dates)}


# --------------------------------------------------------------------------
# 2. Availability: Vista OData -- the only thing we poll
# --------------------------------------------------------------------------

_ODATA_FIELDS = ("ID,SessionId,ScheduledFilmId,Showtime,SeatsAvailable,"
                 "IsAllocatedSeating,AllowTicketSales,SoldoutStatus")


def fetch_availability(cinema_vista_id: str, date_from: str, date_to: str) -> dict:
    """
    Seat counts for every session of one cinema in [date_from, date_to).

    Dates are "YYYY-MM-DD" strings, interpreted as local cinema time (that is
    how Vista stores Showtime). Returns {session_id: {...}}.

    One request covers an entire cinema and date range -- watching ten films at
    the same cinema still costs exactly one request per cycle.
    """
    flt = (f"CinemaId eq '{cinema_vista_id}' "
           f"and Showtime ge DATETIME'{date_from}T00:00:00' "
           f"and Showtime lt DATETIME'{date_to}T00:00:00'")
    qs = urllib.parse.urlencode({"$filter": flt, "$select": _ODATA_FIELDS})
    raw = _get(f"{VISTA}/OData.svc/Sessions?{qs}",
               # The gateway rejects the request without this content type,
               # even for GET. Quirk of their API gateway, not a workaround.
               headers={"Content-Type": "application/json"})
    payload = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(payload, dict) or "value" not in payload:
        raise ValueError(f"unexpected OData payload: {str(payload)[:200]}")

    out = {}
    for s in payload["value"]:
        sid = str(s.get("SessionId"))
        seats = s.get("SeatsAvailable")
        out[sid] = {
            "session_id": sid,
            "composite_id": s.get("ID"),
            "film_id": s.get("ScheduledFilmId"),
            "showtime": (s.get("Showtime") or "")[:16],
            "seats": int(seats) if isinstance(seats, int) else None,
            "allocated": bool(s.get("IsAllocatedSeating")),
            "sales_open": bool(s.get("AllowTicketSales")),
        }
    return out


def booking_url(composite_id: str, allocated: bool, locale: str = "en") -> str:
    """Deep link into the checkout for one session, same route the site uses."""
    page = "seats" if allocated else "tickets"
    return f"{SITE}/{locale}/checkout/{page}?sessionid={urllib.parse.quote(composite_id)}"


def listing_url(cinema_slug: str, date: str, film_slug: str = "", locale: str = "en") -> str:
    """The public programme page, scrolled to the film. Always works."""
    url = f"{SITE}/{locale}/cinemas/{cinema_slug}?date={date}"
    return f"{url}#{film_slug}" if film_slug else url


def jitter(seconds: float, spread: float = 0.15) -> float:
    """Interval +/- spread, so the polling never looks like a metronome."""
    return seconds * (1.0 + random.uniform(-spread, spread))
