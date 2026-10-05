#!/usr/bin/env python3
"""Spend ledger, price table and request fingerprints for generate2dmedia.

Every paid or quota call is reserved in <project>/.forge/ledger.jsonl before it
is sent, then committed as done, failed, unknown or not_sent. The file is
append-only; readers fold it per reservation and the last line wins. An unknown
outcome keeps its reservation counted against the caps until someone settles it
with the settle command. Stdlib only; never reads credentials.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import uuid

LEDGER_API_VERSION = "1"
STATUSES = ("reserved", "done", "failed", "unknown", "not_sent")
FINAL_STATUSES = STATUSES[1:]
ROUTES = ("rest", "codex-cli", "grok-cli", "grok-acp")
QUOTA_ROUTES = ROUTES[1:]
MAX_PAID_ENV = "FORGE_MAX_PAID_REQUESTS"
PRICES_PATH = Path(__file__).resolve().parent.parent / "references" / "prices.json"
# Plan keys that identify a request; output paths, timestamps and secrets never do.
FINGERPRINT_KEYS = ("provider", "kind", "route", "endpoint", "requestedModel", "model", "options", "promptSha256")
# Price-row qualifiers: a row that names one only matches requests with that value.
QUALIFIERS = ("resolution", "quality", "size")
ENTRY_TEXT = ("jobDir", "fingerprint", "provider", "model", "kind", "route")
ENTRY_KEYS = (*ENTRY_TEXT, "reservedUsd", "quotaCall")
# ledger_line_v1 (plan Appendix B) plus reservationId, in file order.
LINE_KEYS = ("ts", "reservationId", "status", *ENTRY_TEXT, "reservedUsd", "actualUsd", "quotaCall")
SHA256 = re.compile(r"[0-9a-f]{64}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class LedgerError(Exception):
    """Invalid ledger use, unreadable configuration or a lock that cannot be taken."""


class CapExceeded(LedgerError):
    """Sending one more call would break a cap; nothing may be sent."""


class DuplicateRequest(LedgerError):
    """An identical request already succeeded, is still open, or has an unknown outcome."""

    def __init__(self, entry):
        self.entry = entry
        state = {"done": "already succeeded", "reserved": "is still in flight or pending",
                 "unknown": "has an unknown outcome (check the provider's usage history)"}[entry["status"]]
        super().__init__(f"An identical request {state}: job {entry.get('jobDir')}, reserved {entry.get('reservedAt')}")


def utc_timestamp(moment=None):
    """ISO 8601 UTC with milliseconds, e.g. 2026-10-05T12:00:00.000Z."""
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _usd(value, name="usd"):
    """None or a finite, non-negative amount rounded to 1e-6 USD."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite amount >= 0")
    return round(float(value), 6)


def _amount(value):
    """A stored USD amount, or None when absent or not a usable number (hand-edited lines)."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return value if ok else None


def load_prices(path=None) -> dict:
    """Read a prices_v1 table; every row needs provider, model, unit, usd, source, verifiedAt."""
    path = PRICES_PATH if path is None else Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"Cannot read price table {path.name}: {type(exc).__name__}") from None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise LedgerError(f"Price table {path.name} needs a rows list")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in
                                                  ("provider", "model", "unit", "source", "verifiedAt")):
            raise LedgerError(f"Price row {index} needs provider, model, unit, source and verifiedAt")
        if not DATE.fullmatch(row["verifiedAt"]):
            raise LedgerError(f"Price row {index}: verifiedAt must be YYYY-MM-DD")
        try:
            if _usd(row.get("usd")) is None:
                raise ValueError
        except ValueError:
            raise LedgerError(f"Price row {index}: usd must be a finite amount >= 0") from None
    return data


def prices_version(prices: dict) -> str:
    """The table's declared version, else a short hash of its rows."""
    version = prices.get("version")
    if isinstance(version, str) and version:
        return version
    rows = json.dumps(prices.get("rows", []), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rows.encode("utf-8")).hexdigest()[:16]


def _find_row(rows, provider, model, unit, wanted):
    """The most specific row whose qualifiers all equal the requested values."""
    best = None
    for row in rows:
        if (row["provider"], row["model"], row["unit"]) != (provider, model, unit):
            continue
        named = [q for q in QUALIFIERS if q in row]
        if all(str(row[q]) == str(wanted.get(q)) for q in named) and (best is None or len(named) > best[0]):
            best = (len(named), row)
    return best[1] if best else None


def _describe(unit, qualifiers):
    named = ",".join(f"{k}={v}" for k, v in qualifiers.items() if v is not None)
    return unit + (f"({named})" if named else "")


def estimate(plan: dict, prices=None) -> dict:
    """Estimate a plan's list-price cost: {usd, currency, basis, pricesVersion, items}.

    usd is None when a needed row is missing; basis then names the missing rows.
    Subscription (quota) routes cost 0 USD here and are limited by call caps.
    """
    prices = load_prices() if prices is None else prices
    version = prices_version(prices)
    if plan.get("route") in QUOTA_ROUTES or plan.get("quotaCall") is True:
        return {"usd": 0.0, "currency": "USD", "pricesVersion": version, "items": [],
                "basis": "subscription quota call: no per-call USD price; counted by call caps"}
    provider = plan.get("provider")
    model = plan.get("requestedModel") or plan.get("model")
    options = plan.get("options") or {}
    inputs = len(plan.get("references") or []) + (plan.get("lastFrame") is not None)
    calls = int(plan.get("paidRequests", 1))
    if plan.get("kind") == "video":
        wanted = [("output_second", options.get("duration"), {"resolution": options.get("resolution")})]
    else:
        wanted = [("output_image", options.get("n", 1), {q: options.get(q) for q in QUALIFIERS})]
    wanted.append(("input_image", inputs, {}))
    items, missing = [], []
    for unit, quantity, qualifiers in wanted:
        if not quantity:
            continue
        row = _find_row(prices.get("rows", []), provider, model, unit, qualifiers)
        if row is None:
            missing.append(_describe(unit, qualifiers))
            continue
        items.append({"unit": unit, "quantity": quantity, "usdEach": row["usd"],
                      "usd": round(quantity * row["usd"] * calls, 6), "source": row["source"],
                      "verifiedAt": row["verifiedAt"], **{q: row[q] for q in QUALIFIERS if q in row}})
    if missing:
        known = sorted({_describe(r["unit"], {q: r.get(q) for q in QUALIFIERS}) + f" {r['usd']}"
                        for r in prices.get("rows", []) if (r["provider"], r["model"]) == (provider, model)})
        basis = (f"unpriced: no verified price row for {provider}/{model} " + ", ".join(missing)
                 + ("; known rows: " + "; ".join(known) if known else ""))
        return {"usd": None, "currency": "USD", "basis": basis, "pricesVersion": version, "items": items}
    usd = round(sum(item["usd"] for item in items), 6)
    terms = " + ".join(f"{i['quantity']} {i['unit']} x {i['usdEach']}" for i in items)
    dates = ", ".join(sorted({i["verifiedAt"] for i in items}))
    times = f" x {calls} calls" if calls != 1 else ""
    basis = f"{terms}{times} = {usd} USD list price (verified {dates}); excludes tax and rejected attempts"
    return {"usd": usd, "currency": "USD", "basis": basis, "pricesVersion": version, "items": items}


def fingerprint(plan: dict, ref_hashes) -> str:
    """sha256 of the request identity: provider, kind, route, endpoint, model,
    options, prompt hash and the ordered reference hashes."""
    identity = {k: plan[k] for k in FINGERPRINT_KEYS if plan.get(k) is not None}
    identity["references"] = list(ref_hashes)
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def max_paid_requests():
    """FORGE_MAX_PAID_REQUESTS as an int, or None when unset."""
    raw = os.environ.get(MAX_PAID_ENV, "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise LedgerError(f"{MAX_PAID_ENV} must be a whole number >= 0")
    return int(raw)


if os.name == "nt":
    import msvcrt

    def _lock_file(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # retries for about 10 s

    def _unlock_file(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock_file(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ledger:
    """Append-only spend ledger at <project_dir>/.forge/ledger.jsonl.

    Reading never creates files. reserve() checks caps and duplicates and
    appends under an exclusive lock (threads and processes), so concurrent
    callers cannot both pass a cap that only one of them fits under.
    """

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / ".forge" / "ledger.jsonl"
        self._lock_path = self.path.with_name("ledger.lock")

    def lines(self) -> list:
        """Parsed ledger lines; torn or foreign lines are skipped."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        parsed = []
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and isinstance(line.get("reservationId"), str) and line.get("status") in STATUSES:
                parsed.append(line)
        return parsed

    def entries(self) -> dict:
        """Current state per reservation id, in reservation order (last line wins).
        reservedAt is the timestamp of the reservation's first line."""
        states = {}
        for line in self.lines():
            previous = states.get(line["reservationId"], {"reservedAt": line.get("ts")})
            states[line["reservationId"]] = {**previous, **line}
        return states

    def totals(self, states=None) -> dict:
        """Call counts and USD: committed (settled) plus held (reserved or unknown)."""
        states = self.entries() if states is None else states
        totals = {"calls": 0, "paidCalls": 0, "quotaCalls": 0, "open": 0, "unknown": 0,
                  "unpricedCalls": 0, "committedUsd": 0.0, "heldUsd": 0.0}
        for state in states.values():
            status = state["status"]
            if status == "not_sent":
                continue
            quota = bool(state.get("quotaCall"))
            totals["calls"] += 1
            totals["quotaCalls" if quota else "paidCalls"] += 1
            reserved, actual = _amount(state.get("reservedUsd")), _amount(state.get("actualUsd"))
            if status in ("reserved", "unknown"):
                totals["open" if status == "reserved" else "unknown"] += 1
                held = actual if actual is not None else reserved
                totals["unpricedCalls"] += held is None and not quota
                totals["heldUsd"] += held or 0.0
            elif status == "done":
                spent = actual if actual is not None else reserved
                totals["unpricedCalls"] += spent is None and not quota
                totals["committedUsd"] += spent or 0.0
            else:  # failed: provider refused or failed; only an explicit actual amount counts
                totals["committedUsd"] += actual or 0.0
        totals["committedUsd"] = round(totals["committedUsd"], 6)
        totals["heldUsd"] = round(totals["heldUsd"], 6)
        totals["usd"] = round(totals["committedUsd"] + totals["heldUsd"], 6)
        return totals

    def find(self, fingerprint, statuses=("done",), *, states=None):
        """Latest reservation with this fingerprint whose status is in statuses."""
        for state in reversed(list((self.entries() if states is None else states).values())):
            if state.get("fingerprint") == fingerprint and state["status"] in statuses:
                return state
        return None

    def find_success(self, fingerprint) -> dict | None:
        return self.find(fingerprint, ("done",))

    def check_caps(self, budget_usd=None, max_calls=None, *, next_usd=0.0, quota_call=False, states=None) -> dict:
        """Raise CapExceeded unless one more call costing next_usd fits every cap.

        max_calls counts paid and quota calls; FORGE_MAX_PAID_REQUESTS counts paid
        calls only; budget_usd compares committed + held USD + next_usd. A priced
        budget cannot be enforced for an unpriced (next_usd None) paid call.
        Caps are cumulative over this project's ledger. Returns the totals.
        """
        budget_usd = _usd(budget_usd, "budget_usd")
        if max_calls is not None and (isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 0):
            raise ValueError("max_calls must be a whole number >= 0")
        totals = self.totals(states)
        paid_cap = max_paid_requests()
        where = self.path.as_posix()
        if not quota_call and paid_cap is not None and totals["paidCalls"] + 1 > paid_cap:
            raise CapExceeded(f"{MAX_PAID_ENV}={paid_cap} reached: {totals['paidCalls']} paid calls recorded in {where}")
        if max_calls is not None and totals["calls"] + 1 > max_calls:
            raise CapExceeded(f"call cap {max_calls} reached: {totals['calls']} calls recorded in {where}")
        if budget_usd is not None and not quota_call:
            if next_usd is None:
                raise CapExceeded("no verified price for this request, so the USD budget cannot be enforced; "
                                  "use a call cap instead or supply a verified price row")
            if totals["usd"] + next_usd > budget_usd + 1e-9:
                raise CapExceeded(f"budget {budget_usd} USD would be exceeded: {totals['usd']} USD recorded or held "
                                  f"in {where} + {next_usd} USD for this request")
        return totals

    @contextlib.contextmanager
    def _locked(self):
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(str(self._lock_path.absolute()), threading.Lock())
        with thread_lock:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+b") as handle:
                try:
                    _lock_file(handle)
                except OSError:
                    raise LedgerError(f"Ledger {self.path.as_posix()} is locked by another process") from None
                try:
                    yield
                finally:
                    _unlock_file(handle)

    def _append(self, line):
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(self.path, "a+b") as handle:
            # A crash mid-append can leave a torn last line; start on a fresh one.
            if handle.seek(0, os.SEEK_END) > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def reserve(self, entry, *, budget_usd=None, max_calls=None, refuse_duplicate=False) -> str:
        """Append a reserved line and return its reservation id.

        entry: jobDir, fingerprint, provider, model, kind, route, reservedUsd
        (None when unpriced) and optional quotaCall (default: route is a CLI
        route). Caps and FORGE_MAX_PAID_REQUESTS are checked under the lock.
        """
        unknown = set(entry) - set(ENTRY_KEYS)
        if unknown:
            raise ValueError("Unknown ledger entry fields: " + ", ".join(sorted(unknown)))
        for name in ENTRY_TEXT:
            if not isinstance(entry.get(name), str) or not entry[name]:
                raise ValueError(f"Ledger entry needs a non-empty {name}")
        if entry["route"] not in ROUTES:
            raise ValueError("route must be one of " + ", ".join(ROUTES))
        if not SHA256.fullmatch(entry["fingerprint"]):
            raise ValueError("fingerprint must be a sha256 hex digest")
        quota = entry.get("quotaCall", entry["route"] in QUOTA_ROUTES)
        if not isinstance(quota, bool):
            raise ValueError("quotaCall must be true or false")
        reserved = _usd(entry.get("reservedUsd"), "reservedUsd")
        rid = uuid.uuid4().hex
        line = {"ts": utc_timestamp(), "reservationId": rid, "status": "reserved",
                **{k: entry[k] for k in ENTRY_TEXT}, "reservedUsd": reserved, "quotaCall": quota}
        with self._locked():
            states = self.entries()
            prior = self.find(line["fingerprint"], ("done", "reserved", "unknown"), states=states)
            if refuse_duplicate and prior is not None:
                raise DuplicateRequest(prior)
            self.check_caps(budget_usd, max_calls, next_usd=reserved, quota_call=quota, states=states)
            self._append(line)
        return rid

    def commit(self, reservation_id, *, actual_usd=None, status) -> dict:
        """Append the outcome of a reservation (done, failed, unknown or not_sent)."""
        if status not in FINAL_STATUSES:
            raise ValueError("status must be one of " + ", ".join(FINAL_STATUSES))
        actual = _usd(actual_usd, "actual_usd")
        with self._locked():
            state = self.entries().get(reservation_id)
            if state is None:
                raise LedgerError(f"Unknown reservation {reservation_id} in {self.path.as_posix()}")
            # An earlier commit's actualUsd is not carried over: each outcome states its own.
            line = {k: state[k] for k in LINE_KEYS if k in state and k != "actualUsd"}
            line.update(ts=utc_timestamp(), status=status)
            if actual is not None:
                line["actualUsd"] = actual
            self._append(line)
        return line

    def summary(self) -> dict:
        """Totals plus the reservations that still hold budget (reserved or unknown)."""
        states = self.entries()
        unsettled = [{k: s.get(k) for k in ("reservationId", "status", "reservedAt", "jobDir", "provider", "model", "reservedUsd")}
                     for s in states.values() if s["status"] in ("reserved", "unknown")]
        return {"ledger": self.path.as_posix(), **self.totals(states), "unsettled": unsettled}


def _local_utf8_stdio():
    """Console-safe output on legacy code pages (twin of forge_core.utf8_stdio)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def parser():
    p = argparse.ArgumentParser(description="Inspect or settle the generate2dmedia spend ledger (<project>/.forge/ledger.jsonl).")
    commands = p.add_subparsers(dest="command", required=True)
    s = commands.add_parser("summary", help="Print call counts, USD totals and unsettled reservations")
    s.add_argument("--project-dir", default=".", help="Project root that holds .forge/ (default: current directory)")
    t = commands.add_parser("settle", help="Record the real outcome of a reservation after checking the provider's history")
    t.add_argument("reservation", help="Reservation id from summary or job.json")
    t.add_argument("--status", required=True, choices=("done", "failed", "not_sent"),
                   help="done: the provider charged it; failed: it did not; not_sent: it never left this machine")
    t.add_argument("--actual-usd", type=float, help="Charged amount when known")
    t.add_argument("--project-dir", default=".", help="Project root that holds .forge/ (default: current directory)")
    return p


def main(argv=None):
    _local_utf8_stdio()
    args = parser().parse_args(argv)
    ledger = Ledger(args.project_dir)
    try:
        if args.command == "summary":
            result = ledger.summary()
        else:
            result = ledger.commit(args.reservation, actual_usd=args.actual_usd, status=args.status)
    except (LedgerError, ValueError, OSError) as exc:
        print("error: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
