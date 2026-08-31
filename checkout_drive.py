#!/usr/bin/env python3
"""
Drive the Yorck checkout in the user's own, already logged-in Chrome.

No credentials are handled anywhere: this talks to a browser that is already
signed in, the same way you would click the page yourself. Name and email on
the payment step come prefilled from the account.

    python3 checkout_drive.py inspect 1015-700
    python3 checkout_drive.py book    1015-700 2026-08-27T20:45 [--dry-run]

Requires Chrome: View > Developer > Allow JavaScript from Apple Events.

Everything is located by visible text rather than by CSS selector, because the
page ships generated MUI class names (css-1x1xqjw) that change on every build.
The one exception is the quantity stepper: its minus and plus icons carry the
same aria-label="quantity" and can only be told apart by x position.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time

CHECKOUT = "https://www.yorck.de/de/checkout/tickets?sessionid={}"
UNLIMITED = "Yorck Unlimited"
# The only row a signed-out visitor is offered. Member tariffs appear after
# logging in, so the guest fallback cannot use the configured ticket type.
GUEST_TICKET = "Normal (Online)"

# German month abbreviations: the checkout page prints them, so they stay
# German regardless of what language this tool speaks.
MONTHS_DE = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
             "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


class ChromeError(RuntimeError):
    pass


class VerifyError(RuntimeError):
    """The page on screen is not the screening we meant to book."""


# --------------------------------------------------------------------------
# talking to Chrome
# --------------------------------------------------------------------------

def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode:
        err = (r.stderr or "").strip()
        if "turned off" in err:
            raise ChromeError("Chrome blocks scripting. Enable it under "
                              "View > Developer > Allow JavaScript from Apple Events.")
        if "not running" in err or "-600" in err:
            raise ChromeError("Chrome is not running.")
        raise ChromeError(err or "osascript failed")
    return r.stdout.strip()


def js(code: str):
    """
    Run JS in the front window's active tab and parse the JSON it returns.

    The code travels via a temp file, not an AppleScript string literal: the
    snippets contain newlines, // comments and regex backslashes, none of which
    survive being quoted into osascript.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(code)
        path = tmp.name
    raw = osa(f'''
      set jsCode to (read POSIX file "{path}" as «class utf8»)
      tell application "Google Chrome"
        return execute (active tab of front window) javascript jsCode
      end tell
    ''')
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "raw": raw}


def open_checkout(session_id: str, settle: float = 6.0) -> None:
    """
    Open a FRESH tab for exactly this session.

    Reusing "any tab whose URL contains /checkout/" is how you add a ticket to a
    stale order for a different screening -- once the site creates the order it
    can rewrite the URL, and the session id is no longer in it to match on. So
    old checkout tabs are closed first and a new one is opened; callers must
    still verify() before clicking anything.
    """
    osa('''
      tell application "Google Chrome"
        repeat with w in windows
          -- backwards: closing shifts the indices of everything after it
          repeat with i from (count of tabs of w) to 1 by -1
            try
              if URL of tab i of w contains "/checkout/" then close tab i of w
            end try
          end repeat
        end repeat
      end tell
    ''')
    subprocess.run(["open", "-a", "Google Chrome", CHECKOUT.format(session_id)],
                   check=False)
    time.sleep(settle)
    osa('tell application "Google Chrome" to activate')


# --------------------------------------------------------------------------
# page probes
# --------------------------------------------------------------------------

_INSPECT = r"""
(() => {
  const t = document.body.innerText.replace(/\s+/g, ' ').trim();
  // Read every ticket row the page actually shows, instead of looking up a
  // hardcoded list of names. A ticket type that exists on the page but not in
  // that list reported "quantity None" and looked like a rejection.
  const txt = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
  const rows = {};
  document.querySelectorAll('div,li,tr').forEach(el => {
    const s = txt(el);
    const q = s.match(/Anzahl\s*(\d+)/);
    if (!q || s.length > 420) return;
    // Label = everything before the description or the price
    const m = s.match(/^(.{3,60}?)\s(?:Online Rabatt|Unsere Online|Du erhältst|Nutze deine|\d+,\d{2}\s*€)/);
    const label = (m ? m[1] : s.slice(0, 40)).trim();
    if (!(label in rows) || s.length < rows[label][1]) rows[label] = [Number(q[1]), s.length];
  });
  Object.keys(rows).forEach(k => { rows[k] = rows[k][0]; });
  return JSON.stringify({
    ok: true,
    url: location.href,
    signedIn: !/Buchung als Gast|Book as guest/i.test(t),
    countdown: (t.match(/^(\d{1,2}:\d{2})/) || [])[1] || null,
    total: (t.match(/Gesamt\s*([\d,]+\s*€)/) || [])[1] || null,
    rows: rows,
    // The screening's date/time sits below the ticket rows, well past any
    // sensible text slice, so pull it out explicitly. The page prints it as
    // "Datum Fr 28 Aug Uhrzeit 20:45".
    when: (() => {
      const m = t.match(/Datum\s+(.{3,24}?)\s+Uhrzeit\s+(\d{1,2}:\d{2})/);
      return m ? (m[1] + ' ' + m[2]) : null;
    })(),
    // The site reports refusals inline, e.g. after picking an UNLIMITED ticket
    // for a screening the member already holds one for. Without this the only
    // symptom is a quantity that stubbornly stays at 0.
    // The site reports refusals in several shapes, and only one of them starts
    // with "Fehler:". Missing the others made a hard refusal look like a
    // timeout ("payment page not reached"), which sent the search in the wrong
    // direction entirely.
    error: (() => {
      const pats = [
        /Fehler:\s*([^.]{3,140})/,
        /(Leider[^.]{3,140}nicht mehr[^.]{0,80})/i,
        /(nicht mehr gen(ü|ue)gend Sitze[^.]{0,80})/i,
        /(Es gibt keine Tickets mehr[^.]{0,40})/i,
      ];
      for (const p of pats) { const m = t.match(p); if (m) return (m[1] || m[0]).trim(); }
      return null;
    })(),
    soldOut: /keine Tickets mehr|out of tickets/i.test(t),
    buttons: [...document.querySelectorAll('button,[role=button]')]
      .map(b => (b.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean),
    text: t.slice(0, 400)
  });
})()
"""

_ADD = r"""
(() => {
  const NEEDLE = %s;
  const txt = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
  let row = null;
  document.querySelectorAll('div,li,tr').forEach(el => {
    const s = txt(el);
    if (!s.includes(NEEDLE) || !/Anzahl/.test(s)) return;
    if (!row || s.length < txt(row).length) row = el;
  });
  if (!row) return JSON.stringify({ ok: false, why: 'ticket row not found' });

  // minus and plus share aria-label="quantity"; only x position separates them
  const ctrls = [...row.querySelectorAll('img')]
    .map(e => ({ e, x: e.getBoundingClientRect().left }))
    .filter(o => o.x > 0).sort((a, b) => a.x - b.x);
  if (ctrls.length < 2) return JSON.stringify({ ok: false, why: 'quantity stepper not found' });

  const plus = ctrls[ctrls.length - 1].e;
  (plus.closest('button,[role=button]') || plus).click();
  return JSON.stringify({ ok: true, plusAtX: Math.round(ctrls[ctrls.length - 1].x) });
})()
"""

_CLICK = r"""
(() => {
  const RE = new RegExp(%s, 'i');
  const b = [...document.querySelectorAll('button,[role=button]')]
    .find(x => RE.test((x.innerText || '').replace(/\s+/g, ' ')));
  if (!b) return JSON.stringify({ ok: false, why: 'button not found',
    buttons: [...document.querySelectorAll('button')]
      .map(x => (x.innerText || '').trim()).filter(Boolean) });
  if (b.disabled) return JSON.stringify({ ok: false, why: 'button is disabled' });
  b.click();
  return JSON.stringify({ ok: true, clicked: (b.innerText || '').trim() });
})()
"""


def inspect() -> dict:
    return js(_INSPECT)


def wait_for(cond, timeout: float = 15.0, step: float = 0.4) -> dict:
    """
    Poll the page until `cond` holds, then return that reading.

    Every fixed sleep in this flow has produced a false failure at some point:
    the page simply had not finished re-rendering when the deadline passed, and
    a generous guess is both slower on a good day and still too short on a bad
    one. Waiting for the actual condition fixes both ends.
    """
    end = time.monotonic() + timeout
    seen: dict = {}
    while time.monotonic() < end:
        time.sleep(step)
        seen = inspect()
        if cond(seen):
            return seen
    return seen


def _quantity(rows: dict | None, ticket: str):
    """Row labels are read off the page, so match loosely rather than exactly."""
    if not rows:
        return None
    if ticket in rows:
        return rows[ticket]
    key = ticket.lower()
    for label, qty in rows.items():
        if key in label.lower() or label.lower() in key:
            return qty
    return None


def expected_tokens(start: str) -> list[str]:
    """['26 Aug', '20:45'] from '2026-08-26T20:45' -- what the page prints."""
    month = int(start[5:7])
    day = str(int(start[8:10]))
    return [f"{day} {MONTHS_DE[month - 1]}", start[11:16]]


def verify(session_id: str, expect: list[str]) -> dict:
    """
    Refuse to click unless this really is the intended screening.

    Two independent signals must agree: the session id still present in the URL,
    and every expected token (day, month, time) found in the page text. Plus the
    session must be signed in, or UNLIMITED would not be on offer at all.
    """
    d = wait_for(lambda x: bool(x.get("rows")) or x.get("soldOut"), 12)
    if not d.get("ok"):
        raise VerifyError(f"page not readable: {str(d)[:160]}")
    if session_id not in d.get("url", ""):
        raise VerifyError(f"wrong screening in the tab: {d.get('url')}")
    when = d.get("when")
    if not when:
        raise VerifyError("no date/time found on the page -- refusing to click")
    missing = [tok for tok in expect if tok not in when]
    if missing:
        raise VerifyError(f"screening does not match: page shows {when!r}, expected {expect}")
    # Readiness gate. Without it every judgement below is made on a page that
    # may not have rendered -- and "signed in" is inferred from the ABSENCE of
    # a guest banner, so a blank page reads as signed in. That failed open.
    if not d.get("rows") and not d.get("soldOut"):
        raise VerifyError("checkout has not rendered yet -- no ticket rows, no sold-out notice")
    if d.get("soldOut"):
        raise RuntimeError("checkout says there are no tickets left "
                           "(the seat counter disagrees -- phantom seat)")
    return d


def add_ticket(needle: str = UNLIMITED) -> dict:
    return js(_ADD % json.dumps(needle))


def click_button(pattern: str) -> dict:
    return js(_CLICK % json.dumps(pattern))


# --------------------------------------------------------------------------
# the whole flow
# --------------------------------------------------------------------------

def book(session_id: str, start: str, ticket: str = UNLIMITED,
         guest_ticket: str = GUEST_TICKET, log=print, dry_run: bool = False) -> dict:
    """
    tickets -> pick one ticket -> payment -> order. Returns a step-by-step trace.

    Verified before the first click and again on the payment page, so a tab that
    drifted to another screening cannot be ordered by accident. dry_run stops
    right before the irreversible button.
    """
    trace: list[dict] = []

    def step(name, result):
        trace.append({"step": name, "result": result})
        log(f"checkout[{session_id}] {name}: {result}")
        return result

    open_checkout(session_id)
    d = verify(session_id, expected_tokens(start))
    step("verify", {k: d.get(k) for k in ("url", "when", "signedIn", "countdown")})

    # Signed out, the member tariffs are not on offer. Rather than give up,
    # put the cheapest guest ticket in the basket and stop: that holds the seat
    # against other buyers for the length of the order session, which is worth
    # far more than a clean abort while the seat disappears.
    signed_in = bool(d.get("signedIn"))
    ticket = ticket if signed_in else guest_ticket

    r = step("add_ticket", add_ticket(ticket))
    if not r.get("ok"):
        raise RuntimeError(f"could not select the ticket: {r}")

    # Wait for an outcome instead of a fixed pause. With a single attempt the
    # reported reason is all you get, and a 2.5 s guess was routinely too short:
    # the row had not re-rendered, so the failure read "quantity is 0" instead
    # of the site's own "Unlimited-Karte konnte nicht erkannt werden".
    got, why, after = None, None, {}
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.3)
        after = inspect()
        got = _quantity(after.get("rows"), ticket)
        why = after.get("error")
        if got == 1 or why:
            break
    if got != 1:
        seen = list((after.get("rows") or {}).keys())
        raise RuntimeError(f"{ticket} was not accepted: "
                           f"{why or f'quantity stayed {got!r} after 8s'} "
                           f"| rows on the page: {seen}")
    step("quantity", {ticket: got})

    if not signed_in:
        step("guest_hold", f"{ticket} in the basket, stopping -- sign in and finish")
        return {"ok": True, "reserved": True, "signed_in": False,
                "ticket": ticket, "trace": trace}

    r = step("proceed", click_button("prüfen und zahlen|pruefen und zahlen"))
    if not r.get("ok"):
        raise RuntimeError(f"could not proceed to payment: {r}")
    pay = wait_for(lambda x: "payment" in (x.get("url") or "")
                             or bool(x.get("error")), 20)
    if "payment" not in pay.get("url", ""):
        raise RuntimeError(f"payment page not reached: {pay.get('url')}")
    if session_id not in pay.get("url", ""):
        raise VerifyError(f"payment page belongs to a different screening: {pay.get('url')}")
    step("payment_page", {"url": pay.get("url"), "total": pay.get("total")})

    if dry_run:
        step("dry_run", "stopped before ordering")
        return {"ok": True, "dry_run": True, "trace": trace}

    r = step("order", click_button("zahlungspflichtig bestellen"))
    if not r.get("ok"):
        raise RuntimeError(f"placing the order failed: {r}")
    done = wait_for(lambda x: "success" in (x.get("url") or "")
                              or bool(x.get("error")), 30)
    booked = "success" in done.get("url", "")
    step("result", {"url": done.get("url"), "booked": booked})
    if not booked:
        raise RuntimeError(f"no confirmation page: {done.get('url')} / {done.get('text', '')[:110]}")
    return {"ok": True, "reserved": False, "signed_in": True,
            "ticket": ticket, "trace": trace}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, sid = sys.argv[1], sys.argv[2]
    try:
        if cmd == "inspect":
            open_checkout(sid)
            d = inspect()
            for k in ("url", "signedIn", "countdown", "total", "rows", "buttons"):
                print(f"  {k:<10}: {d.get(k)}")
            return 0
        if cmd == "book":
            if len(sys.argv) < 4:
                print("  missing start time, e.g. 2026-08-27T20:45")
                return 2
            print(book(sid, sys.argv[3], dry_run="--dry-run" in sys.argv))
            return 0
        print(f"unknown command {cmd!r}")
        return 2
    except (ChromeError, VerifyError, RuntimeError) as e:
        print(f"  ABORTED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
