"""NSE's daily F&O bhavcopy: real end-of-day prices of every Nifty option and future, from 2021-01-01.

Kite has no history for expired contracts, so option backtests use NSE's own end-of-day files. Two formats
(both checked by hand on 2026-11-13):

  old    archives.nseindia.com/content/historical/DERIVATIVES/2024/JUL/fo05JUL2024bhav.csv.zip
         last file 2024-07-05 (2024-07-08 answers 404)
  UDiFF  nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv.zip
         from 2024-07-05 at the latest (2021 files answer 404)

Days before SWITCH use the old format first, later days the new one; each falls back to the other on a 404.
Only NIFTY index options and futures are kept, normalised to one table and saved per day as gzipped CSV in
backend/data/bhavcopy/nifty/YYYY/YYYY-MM-DD.csv.gz (git-ignored). The manifest records days NSE has no file
for (holidays), so a rerun resumes without asking again.

Units: `volume` is in CONTRACTS (lots) and `oi` in UNITS (shares) in both formats; `close` is the last trade
price on the old format's CLOSE and the UDiFF ClsPric (for contracts that did not trade it is a carried or
theoretical value, so research should check `volume`); `settle` is NSE's settlement price; `underlying` is the
index close (UDiFF only, empty before July 2024).

    .venv/bin/python -m app.fno_research.bhavcopy download --since 2021-01-01
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import logging
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("fno.bhavcopy")

ROOT = Path(__file__).resolve().parents[2] / "data" / "bhavcopy"
OLD_URL = "https://archives.nseindia.com/content/historical/DERIVATIVES/{y}/{mon}/fo{d:%d}{mon}{y}bhav.csv.zip"
NEW_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
SWITCH = dt.date(2024, 7, 8)             # first day the old archive no longer has
FIRST_DAY = dt.date(2021, 1, 1)
# A generic browser identity: nothing about the user goes into requests.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36",
           "Accept": "application/zip, */*", "Accept-Language": "en-IN,en;q=0.9"}
COLUMNS = ("date", "symbol", "instrument", "expiry", "strike", "kind", "open", "high", "low", "close", "settle",
           "oi", "volume", "underlying")
SYMBOL = "NIFTY"
PUBLISHED_BY = dt.time(20, 0)            # IST: today's file is not asked for before this


@dataclass(frozen=True)
class Row:
    date: dt.date
    symbol: str
    instrument: str        # "OPT" or "FUT"
    expiry: dt.date
    strike: float          # 0 for futures
    kind: str              # "CE", "PE" or "FUT"
    open: float
    high: float
    low: float
    close: float
    settle: float
    oi: int                # units
    volume: int            # contracts
    underlying: float | None = None


# ---------------------------------------------------------------- parsing

def _f(x: str) -> float:
    x = (x or "").strip()
    return float(x) if x else 0.0


def _i(x: str) -> int:
    return int(round(_f(x)))


def _old_date(x: str) -> dt.date:
    return dt.datetime.strptime(x.strip().title(), "%d-%b-%Y").date()


def parse_old(text: str, symbol: str = SYMBOL) -> list[Row]:
    """The pre-July-2024 file: INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,...,TIMESTAMP."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        inst = r.get("INSTRUMENT", "")
        if r.get("SYMBOL") != symbol or inst not in ("OPTIDX", "FUTIDX"):
            continue
        is_opt = inst == "OPTIDX"
        out.append(Row(date=_old_date(r["TIMESTAMP"]), symbol=symbol, instrument="OPT" if is_opt else "FUT",
                       expiry=_old_date(r["EXPIRY_DT"]), strike=_f(r["STRIKE_PR"]) if is_opt else 0.0,
                       kind=r["OPTION_TYP"] if is_opt else "FUT", open=_f(r["OPEN"]), high=_f(r["HIGH"]),
                       low=_f(r["LOW"]), close=_f(r["CLOSE"]), settle=_f(r["SETTLE_PR"]), oi=_i(r["OPEN_INT"]),
                       volume=_i(r["CONTRACTS"])))
    return out


def parse_new(text: str, symbol: str = SYMBOL) -> list[Row]:
    """The UDiFF file (July 2024 on): TradDt,...,FinInstrmTp (IDO/IDF),TckrSymb,XpryDt,FininstrmActlXpryDt,..."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        tp = r.get("FinInstrmTp", "")
        if r.get("TckrSymb") != symbol or tp not in ("IDO", "IDF"):
            continue
        is_opt = tp == "IDO"
        exp = r.get("FininstrmActlXpryDt") or r["XpryDt"]          # the actual expiry, after any holiday shift
        u = _f(r.get("UndrlygPric", ""))
        out.append(Row(date=dt.date.fromisoformat(r["TradDt"]), symbol=symbol, instrument="OPT" if is_opt else "FUT",
                       expiry=dt.date.fromisoformat(exp), strike=_f(r["StrkPric"]) if is_opt else 0.0,
                       kind=r["OptnTp"] if is_opt else "FUT", open=_f(r["OpnPric"]), high=_f(r["HghPric"]),
                       low=_f(r["LwPric"]), close=_f(r["ClsPric"]), settle=_f(r["SttlmPric"]), oi=_i(r["OpnIntrst"]),
                       volume=_i(r["TtlTradgVol"]), underlying=u or None))
    return out


def parse_zip(blob: bytes, new_format: bool, symbol: str = SYMBOL) -> list[Row]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("utf-8", errors="replace")
    return (parse_new if new_format else parse_old)(text, symbol)


# ---------------------------------------------------------------- storage

def day_path(d: dt.date, root: Path | None = None) -> Path:
    return (root or ROOT) / SYMBOL.lower() / f"{d.year}" / f"{d.isoformat()}.csv.gz"


def _num(x: float) -> str:
    """Exact to 4 decimals without trailing zeros (':g' would cut 24320.55 to 24320.5)."""
    return f"{x:.4f}".rstrip("0").rstrip(".")


def write_day(d: dt.date, rows: list[Row], root: Path | None = None) -> Path:
    p = day_path(d, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r.date.isoformat(), r.symbol, r.instrument, r.expiry.isoformat(), _num(r.strike), r.kind,
                        _num(r.open), _num(r.high), _num(r.low), _num(r.close), _num(r.settle), r.oi, r.volume,
                        "" if r.underlying is None else _num(r.underlying)])
    tmp.replace(p)
    return p


def read_day(d: dt.date, root: Path | None = None) -> list[Row] | None:
    """The stored day, or None when it was never downloaded (or NSE has no file for it)."""
    p = day_path(d, root)
    if not p.exists():
        return None
    out = []
    with gzip.open(p, "rt", newline="") as fh:
        for r in csv.DictReader(fh):
            out.append(Row(date=dt.date.fromisoformat(r["date"]), symbol=r["symbol"], instrument=r["instrument"],
                           expiry=dt.date.fromisoformat(r["expiry"]), strike=float(r["strike"]), kind=r["kind"],
                           open=float(r["open"]), high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
                           settle=float(r["settle"]), oi=int(r["oi"]), volume=int(r["volume"]),
                           underlying=float(r["underlying"]) if r["underlying"] else None))
    return out


def stored_days(root: Path | None = None) -> list[dt.date]:
    base = (root or ROOT) / SYMBOL.lower()
    if not base.exists():
        return []
    return sorted(dt.date.fromisoformat(p.name[:10]) for p in base.glob("*/*.csv.gz"))


def _manifest_path(root: Path | None) -> Path:
    return (root or ROOT) / "manifest.json"


def load_manifest(root: Path | None = None) -> dict[str, str]:
    p = _manifest_path(root)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_manifest(m: dict[str, str], root: Path | None = None) -> None:
    p = _manifest_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(m.items())), indent=0))
    tmp.replace(p)


# ---------------------------------------------------------------- download

def urls_for(d: dt.date) -> list[tuple[str, bool]]:
    """(url, is_new_format), the likely one first."""
    old = (OLD_URL.format(y=d.year, mon=d.strftime("%b").upper(), d=d), False)
    new = (NEW_URL.format(d=d), True)
    return [old, new] if d < SWITCH else [new, old]


class NotPublished(Exception):
    pass


class Throttle:
    """At most one request per `gap` seconds."""

    def __init__(self, gap: float = 1.0, sleep=time.sleep, clock=time.monotonic):
        self.gap, self.sleep, self.clock, self.last = gap, sleep, clock, -1e9

    def wait(self) -> None:
        dt_ = self.clock() - self.last
        if dt_ < self.gap:
            self.sleep(self.gap - dt_)
        self.last = self.clock()


def fetch_day(d: dt.date, client: httpx.Client, throttle: Throttle, retries: int = 4, sleep=time.sleep) -> list[Row] | None:
    """The day's NIFTY rows; None when NSE has no file in either format (a holiday). Raises on repeated failures."""
    for url, new_format in urls_for(d):
        for attempt in range(retries):
            throttle.wait()
            try:
                r = client.get(url, timeout=30)
            except httpx.HTTPError as e:
                log.info("bhavcopy %s: %s (try %d)", d, e, attempt + 1)
                sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 404:
                break
            if r.status_code in (403, 429) or r.status_code >= 500:
                log.info("bhavcopy %s: HTTP %s, backing off (try %d)", d, r.status_code, attempt + 1)
                sleep(5 * 2 ** attempt)
                continue
            if r.status_code != 200 or r.content[:2] != b"PK":
                log.info("bhavcopy %s: HTTP %s, not a zip", d, r.status_code)
                sleep(2 ** (attempt + 1))
                continue
            return parse_zip(r.content, new_format)
        else:
            raise NotPublished(f"{d}: NSE kept failing at {url}")
    return None


def pending(since: dt.date, until: dt.date, manifest: dict[str, str], root: Path | None = None) -> list[dt.date]:
    have = set(stored_days(root))
    out, d = [], since
    while d <= until:
        if d.weekday() < 5 and d not in have and manifest.get(d.isoformat()) != "none":
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def last_publishable(now: dt.datetime) -> dt.date:
    from app.core.calendar import IST
    now = now.astimezone(IST)
    return now.date() if now.time() >= PUBLISHED_BY else now.date() - dt.timedelta(days=1)


def download(since: dt.date = FIRST_DAY, until: dt.date | None = None, root: Path | None = None,
             stop: threading.Event | None = None, client: httpx.Client | None = None, throttle: Throttle | None = None,
             say=print) -> dict:
    from app.core.calendar import now_ist
    until = min(until or last_publishable(now_ist()), last_publishable(now_ist()))
    manifest = load_manifest(root)
    todo = pending(since, until, manifest, root)
    say(f"{len(todo)} weekdays to fetch between {since} and {until}")
    own = client is None
    client = client or httpx.Client(headers=HEADERS, follow_redirects=True)
    throttle = throttle or Throttle(1.0)
    got = none = failed = 0
    try:
        for i, d in enumerate(todo):
            if stop is not None and stop.is_set():
                break
            try:
                rows = fetch_day(d, client, throttle)
            except NotPublished as e:
                failed += 1
                say(f"skipped for now: {e}")
                continue
            if rows is None or not rows:
                manifest[d.isoformat()] = "none"
                none += 1
            else:
                write_day(d, rows, root)
                manifest[d.isoformat()] = "ok"
                got += 1
            if (i + 1) % 20 == 0 or i + 1 == len(todo):
                save_manifest(manifest, root)
                say(f"{i + 1}/{len(todo)} done ({got} days stored, {none} holidays, {failed} failed); at {d}")
    finally:
        save_manifest(manifest, root)
        if own:
            client.close()
    return {"stored": got, "holidays": none, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("download", help="fetch missing days (resumable)")
    a1.add_argument("--since", default=FIRST_DAY.isoformat())
    a1.add_argument("--until", default="")
    sub.add_parser("status", help="what is stored")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if a.cmd == "download":
        r = download(dt.date.fromisoformat(a.since), dt.date.fromisoformat(a.until) if a.until else None)
        print(r)
        return 0 if not r["failed"] else 2
    days = stored_days()
    print(f"{len(days)} days stored" + (f", {days[0]} .. {days[-1]}" if days else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
