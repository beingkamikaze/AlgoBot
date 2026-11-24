"""Company news for NSE stocks (ported from the nse-news skill): NSE corporate
announcements (the filings), NSE board meetings (the next results date) and Google
News headlines. Cached for 10 minutes per stock. Headlines are leads, not facts."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from app.core.calendar import IST
from app.market.nse import NSE_HEADERS, equities

ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}&from_date={a}&to_date={b}"
BOARD = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol={sym}"
GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
IMPORTANT = re.compile(r"financial result|outcome of board|credit rating|acquisition|amalgamation|merger|resignation|"
                       r"appointment|fund raising|preferential|qip|buyback|dividend|bonus|split|pledge|insolvency|"
                       r"default|fraud|clarification|spurt in volume|price movement|search|raid|litigation|penalty|sebi",
                       re.I)
JUNK = re.compile(r"option chain|stock price, news, quote|share price today live|stock price live|stock quote", re.I)
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _get(url: str, headers: dict | None = None) -> bytes:
    r = httpx.get(url, headers=headers or NSE_HEADERS, timeout=20)
    r.raise_for_status()
    return r.content


def _nse_time(text: str) -> dt.datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).replace(tzinfo=IST)
        except (ValueError, AttributeError):
            continue
    return None


def company_name(symbol: str) -> str:
    try:
        for e in equities():
            if e["symbol"] == symbol:
                return re.sub(r"\s+(limited|ltd\.?)$", "", e["name"], flags=re.I)
    except Exception:  # noqa: BLE001 - fall back to the symbol
        pass
    return symbol


def for_symbol(symbol: str, days: int = 3, headlines: bool = True) -> dict:
    """{filings, next_board_meeting, headlines, errors} for one NSE stock."""
    key = f"{symbol}|{days}|{headlines}"
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    today = dt.datetime.now(IST).date()
    out: dict = {"symbol": symbol, "filings": [], "next_board_meeting": None, "headlines": [], "errors": []}
    q = urllib.parse.quote(symbol)
    try:
        rows = json.loads(_get(ANN.format(sym=q, a=(today - dt.timedelta(days=days)).strftime("%d-%m-%Y"),
                                          b=today.strftime("%d-%m-%Y"))))
        for r in rows if isinstance(rows, list) else []:
            subject = " ".join(str(r.get("desc") or "").split())
            text = " ".join(str(r.get("attchmntText") or "").split())
            t = _nse_time(r.get("an_dt") or "")
            out["filings"].append({"time": t.isoformat() if t else None, "subject": subject, "text": text[:300],
                                   "link": r.get("attchmntFile") or "", "important": bool(IMPORTANT.search(subject + " " + text[:200]))})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"NSE announcements: {e}")
    try:
        rows = json.loads(_get(BOARD.format(sym=q)))
        best = None
        for r in rows if isinstance(rows, list) else []:
            try:
                d = dt.datetime.strptime(str(r.get("bm_date")), "%d-%b-%Y").date()
            except ValueError:
                continue
            if d >= today and (best is None or d < best["date"]):
                purpose = " ".join(str(r.get("bm_purpose") or "").split())
                desc = " ".join(str(r.get("bm_desc") or "").split())
                best = {"date": d, "purpose": purpose, "results": bool(re.search(r"result", purpose + " " + desc, re.I))}
        if best:
            out["next_board_meeting"] = {**best, "date": best["date"].isoformat(), "days_away": (best["date"] - today).days}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"NSE board meetings: {e}")
    if headlines:
        name = company_name(symbol)
        keys = [k for k in {symbol, " ".join(name.split()[:2])} if len(k) >= 3]
        try:
            data = _get(GNEWS.format(q=urllib.parse.quote_plus(f'"{name}" when:{days}d')),
                        {"User-Agent": NSE_HEADERS["User-Agent"]})
            for it in ET.fromstring(data).findall("./channel/item"):
                title = " ".join((it.findtext("title") or "").split())
                src = (it.findtext("source") or "").strip()
                if src and title.endswith(" - " + src):
                    title = title[: -len(src) - 3]
                if not title or JUNK.search(title) or not any(re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", title, re.I) for k in keys):
                    continue
                try:
                    t = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(IST).isoformat()
                except (TypeError, ValueError):
                    t = None
                out["headlines"].append({"time": t, "title": title, "source": src})
            out["headlines"] = sorted(out["headlines"], key=lambda h: h["time"] or "", reverse=True)[:8]
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"headlines: {e}")
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def risks_for(symbol: str, side: str) -> list[str]:
    """News risks for a signal card, from facts only."""
    n = for_symbol(symbol, days=2, headlines=False)
    risks = []
    bm = n.get("next_board_meeting")
    if bm and bm["results"] and bm["days_away"] <= 1:
        when = "today" if bm["days_away"] == 0 else "tomorrow"
        risks.append(f"Results board meeting {when} ({bm['date']}): results can land during the session and move the price past any stop.")
    elif bm and bm["results"] and bm["days_away"] <= 7:
        risks.append(f"Results board meeting on {bm['date']} ({bm['days_away']} days away).")
    for f in n["filings"][:20]:
        if f["important"]:
            risks.append(f"Recent NSE filing ({(f['time'] or '')[:16].replace('T', ' ')}): {f['subject']}. Read it before trading.")
            break
    if n["errors"] and not n["filings"] and not bm:
        risks.append("News could not be checked right now (NSE did not answer).")
    return risks
