#!/usr/bin/env python3
"""
HZZ Zadar Job Bot
=================
Prati Burzu rada HZZ-a (HTML pretraga + RSS dopuna) za Zadarsku županiju
i šalje Telegram obavijest za svaki NOVI oglas čije je mjesto rada Zadar.

HTML: https://burzarada.hzz.hr/Posloprimac_RadnaMjesta.aspx
RSS:  https://burzarada.hzz.hr/rss/rsszup20.xml
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv

# =============================================================================
# KONFIGURACIJA
# -----------------------------------------------------------------------------
# 1) Kopiraj .env.example u .env i popuni token + chat_id  (preporučeno)
# 2) ILI upiši vrijednosti u DEFAULT_* polja ispod.
# Varijable okoline / .env imaju prednost nad DEFAULT_* vrijednostima.
# =============================================================================

DEFAULT_TELEGRAM_BOT_TOKEN = ""  # npr. 123456789:AA-xxxx  (od @BotFather)
DEFAULT_TELEGRAM_CHAT_ID = ""  # npr. 123456789  (tvoj chat s botom)

RSS_URL = "https://burzarada.hzz.hr/rss/rsszup20.xml"
SEARCH_URL = "https://burzarada.hzz.hr/Posloprimac_RadnaMjesta.aspx"
JOB_URL_TEMPLATE = "https://burzarada.hzz.hr/RadnoMjesto_Ispis.aspx?WebSifra={job_id}"
# Napredna pretraga: Zadarska županija (isti ID kao rsszup20.xml).
ZUPANIJA_ZADARSKA = "20"
LISTING_PAGE_SIZE = "75"

# Lokalno vrijeme (Hrvatska). Bot se budi u 00:00, 08:00 i 16:00 (svakih 8 sati).
TIMEZONE_NAME = "Europe/Zagreb"
CHECK_HOURS = (0, 8, 16)

# Filtriraj SAMO ovaj grad (ne ostala mjesta u županiji: Petrčane, Bibinje, …).
WORKPLACE_CITY = "ZADAR"

REQUEST_TIMEOUT = 30
RSS_RETRIES = 3
DETAIL_RETRIES = 2
TELEGRAM_RETRIES = 3
DETAIL_DELAY_SEC = 0.35
TELEGRAM_DELAY_SEC = 0.8

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36 HZZZadarJobBot/1.0"
)

# =============================================================================
# Putanje
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SEEN_FILE = BASE_DIR / "seen_jobs.json"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or DEFAULT_TELEGRAM_BOT_TOKEN.strip()
)
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID", "").strip() or DEFAULT_TELEGRAM_CHAT_ID.strip()
)
TIMEZONE_NAME = os.getenv("TIMEZONE", TIMEZONE_NAME).strip() or "Europe/Zagreb"


def telegram_chat_ids() -> list[str]:
    """Jedan ili više chat ID-ova, odvojeni zarezom ili razmakom."""
    raw = TELEGRAM_CHAT_ID.replace(";", ",")
    ids = [part.strip() for part in raw.replace(" ", ",").split(",") if part.strip()]
    return ids

# =============================================================================
# Logging
# =============================================================================


def setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger("hzz_zadar_bot")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


log = logging.getLogger("hzz_zadar_bot")

# =============================================================================
# Model
# =============================================================================


@dataclass
class Job:
    job_id: str
    url: str
    title: str = ""
    workplace: str = ""
    employer: str = ""
    deadline: str = ""
    category: str = ""
    description: str = ""
    published: str = ""
    from_detail: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.category:
            return self.category
        return f"Oglas {self.job_id}"


# =============================================================================
# seen_jobs.json
# =============================================================================


def load_seen() -> dict[str, Any]:
    if not SEEN_FILE.exists():
        return {"seen_ids": [], "last_check": None}
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ne mogu pročitati %s (%s) — krećem s praznom listom.", SEEN_FILE.name, exc)
        return {"seen_ids": [], "last_check": None}

    if isinstance(data, list):
        return {"seen_ids": [str(x) for x in data], "last_check": None}
    if isinstance(data, dict):
        ids = data.get("seen_ids") or data.get("ids") or []
        return {
            "seen_ids": [str(x) for x in ids],
            "last_check": data.get("last_check"),
        }
    return {"seen_ids": [], "last_check": None}


def save_seen(state: dict[str, Any]) -> None:
    payload = {
        "seen_ids": sorted(set(str(x) for x in state.get("seen_ids", [])), key=int_or_str),
        "last_check": state.get("last_check"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(set(str(x) for x in state.get("seen_ids", []))),
    }
    tmp = SEEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SEEN_FILE)


def int_or_str(value: str) -> tuple:
    return (0, int(value)) if str(value).isdigit() else (1, str(value))


def mark_seen(state: dict[str, Any], job_id: str) -> None:
    ids = state.setdefault("seen_ids", [])
    if job_id not in ids:
        ids.append(job_id)


# =============================================================================
# Encoding / tekst
# =============================================================================


def decode_rss_bytes(raw: bytes) -> str:
    """RSS s Burze rada je često iso-8859-2, a HTTP header laže da je UTF-8."""
    declared = "iso-8859-2"
    match = re.search(br"""encoding\s*=\s*["']([\w\-]+)["']""", raw[:240], re.I)
    if match:
        declared = match.group(1).decode("ascii", "ignore") or declared

    candidates: list[str] = []
    for enc in (declared, "iso-8859-2", "windows-1250", "cp1250", "utf-8"):
        if enc and enc not in candidates:
            candidates.append(enc)

    best_text = None
    best_score = -1
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
        croatian = len(re.findall(r"[čćžšđČĆŽŠĐ]", text[:8000]))
        replacement = text.count("\ufffd")
        score = croatian * 10 - replacement * 50
        if score > best_score:
            best_score = score
            best_text = text
            chosen = enc
    if best_text is None:
        log.warning("Dekodiranje RSS-a nije uspjelo čisto, koristim iso-8859-2 s replace.")
        return raw.decode("iso-8859-2", errors="replace")
    log.debug("RSS dekodiran kao %s (score=%s)", chosen, best_score)
    return best_text


def clean_text(value: str) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"&nbsp;?", " ", text, flags=re.I)
    text = html.unescape(text)
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ").replace("\r", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \t\n,;")


def html_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


# =============================================================================
# Filter mjesta rada
# =============================================================================

# Grad iz WORKPLACE_CITY, eventualno s dodatkom u zagradi / iza crtice.
# NE prolazi: Petrčane, Bibinje, Poličnik, Zadarska (kao županija), itd.
CITY_RE = re.compile(
    rf"^(?:GRAD\s+)?{re.escape(WORKPLACE_CITY)}(?:\s*[-–/,(].*)?$",
    re.IGNORECASE,
)


def is_zadar_workplace(mjesto: str) -> bool:
    if not mjesto:
        return False
    text = clean_text(mjesto).split("\n", 1)[0]
    if not text:
        return False
    first = re.split(r"\s*,\s*", text, maxsplit=1)[0].strip()
    first = re.sub(r"\s+", " ", first)
    return bool(CITY_RE.match(first))


def workplace_looks_valid(mjesto: str) -> bool:
    """RSS ponekad odreže XML pa u 'Mjesto rada' upadne cijeli opis posla."""
    if not mjesto:
        return False
    text = clean_text(mjesto)
    if not text or len(text) > 80 or "\n" in mjesto.strip()[:120]:
        return False
    if re.search(r"opis posla|odgovornost|početak rada|tražimo", text, re.I):
        return False
    return True


# =============================================================================
# HTTP
# =============================================================================


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "hr,en;q=0.8",
        }
    )
    return session


def request_with_retry(
    session: requests.Session,
    url: str,
    *,
    attempts: int,
    what: str,
) -> requests.Response:
    last_error: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            if not response.content:
                raise requests.RequestException("Prazan odgovor")
            return response
        except requests.RequestException as exc:
            last_error = exc
            log.warning("%s nije uspio (%s/%s): %s", what, i, attempts, exc)
            if i < attempts:
                time.sleep(2 * i)
    raise last_error or RuntimeError(f"{what} nije uspio")


# =============================================================================
# RSS
# =============================================================================

RSS_FIELD_RE = re.compile(
    r"(Opis posla|Kategorija|Rok za prijavu|Mjesto rada|Općina|Opcina|Županija|Zupanija)\s*:\s*",
    re.IGNORECASE,
)
WEBSIFRA_RE = re.compile(r"WebSifra=(\d+)", re.IGNORECASE)


def parse_rss_description(description: str) -> dict[str, str]:
    text = clean_text(description)
    if not text:
        return {}
    parts = RSS_FIELD_RE.split(text)
    fields: dict[str, str] = {}
    if parts and parts[0].strip() and not fields:
        leftover = parts[0].strip()
        if leftover:
            fields["opis"] = leftover
    # split() daje [prefix, label, value, label, value, ...]
    i = 1
    while i + 1 < len(parts):
        label = parts[i].strip().lower()
        value = parts[i + 1].strip(" \t\n,;")
        key = {
            "opis posla": "opis",
            "kategorija": "kategorija",
            "rok za prijavu": "rok",
            "mjesto rada": "mjesto",
            "općina": "opcina",
            "opcina": "opcina",
            "županija": "zupanija",
            "zupanija": "zupanija",
        }.get(label)
        if key:
            fields[key] = clean_text(value)
        i += 2
    # Ako regex nije rascijepio sljedeće labele (encoding / odrezan RSS),
    # ostavi samo prvi red i dio do sljedeće labele.
    mjesto = fields.get("mjesto") or ""
    mjesto = mjesto.split("\n", 1)[0]
    mjesto = re.split(
        r",\s*(?:Općina|Opcina|Županija|Zupanija)\s*:",
        mjesto,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    if mjesto:
        fields["mjesto"] = mjesto
    return fields


def extract_job_id(entry: Any) -> str:
    for candidate in (
        getattr(entry, "link", ""),
        getattr(entry, "id", ""),
        getattr(entry, "guid", ""),
    ):
        match = WEBSIFRA_RE.search(str(candidate) or "")
        if match:
            return match.group(1)
    return ""


def fetch_jobs_from_rss(session: requests.Session) -> list[Job]:
    log.info("Dohvaćam RSS: %s", RSS_URL)
    response = request_with_retry(session, RSS_URL, attempts=RSS_RETRIES, what="RSS")
    xml_text = decode_rss_bytes(response.content)
    # feedparser, ako vidi encoding="iso-8859-2" u zaglavlju, ponovo dekodira
    # već dekodirani UTF-8 tekst i dobije mojibake (ÄŒ umjesto Č).
    xml_for_parser = re.sub(
        r'encoding\s*=\s*["\'][^"\']+["\']',
        'encoding="utf-8"',
        xml_text,
        count=1,
        flags=re.I,
    )
    feed = feedparser.parse(xml_for_parser.encode("utf-8"))

    if getattr(feed, "bozo", False) and not feed.entries:
        bozo_exc = getattr(feed, "bozo_exception", None)
        raise RuntimeError(f"RSS nije valjan: {bozo_exc or 'nepoznata greška'}")

    jobs: list[Job] = []
    seen_in_feed: set[str] = set()
    for entry in feed.entries:
        job_id = extract_job_id(entry)
        if not job_id or job_id in seen_in_feed:
            continue
        seen_in_feed.add(job_id)
        raw_desc = entry.get("description") or entry.get("summary") or ""
        fields = parse_rss_description(raw_desc)
        title = clean_text(entry.get("title") or "")
        jobs.append(
            Job(
                job_id=job_id,
                url=JOB_URL_TEMPLATE.format(job_id=job_id),
                title=title,
                workplace=fields.get("mjesto", ""),
                deadline=fields.get("rok", ""),
                category=fields.get("kategorija", ""),
                description=fields.get("opis", ""),
                published=clean_text(entry.get("published") or entry.get("pubDate") or ""),
            )
        )
    log.info("RSS: %s oglasa u feedu Zadarske županije.", len(jobs))
    return jobs


# =============================================================================
# HTML pretraga (isti izvor kao stranica „Pronađeno oglasa“)
# RSS često odreže opis pa fali „Mjesto rada“ — zato je broj 126 bio prenizak.
# Grid na Burzi rada prikaže najviše ~300 redova; ostatak se dopuni iz RSS-a.
# =============================================================================

LISTING_JOB_RE = re.compile(
    r"class=\"TitleLink\"\s+href='RadnoMjesto_Ispis\.aspx\?WebSifra=(\d+)'"
    r"[\s\S]*?>([^<]+)</a>"
    r"[\s\S]*?MjeNazivLabel[^>]*>([^<]+)</span>"
    r"[\s\S]*?PosNazivLabel[^>]*>([^<]*)</span>"
    r"[\s\S]*?RadMjeRokPrijaveLabel[^>]*>([^<]*)</span>",
    re.IGNORECASE,
)
PAGER_RE = re.compile(
    r'title="Idi na stranicu (\d+)" href="javascript:__doPostBack\(&#39;([^&]+)&#39;,&#39;([^&]*)&#39;\)"',
    re.IGNORECASE,
)


def _form_fields(page: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for match in re.finditer(r"<input\b([^>]*)>", page, flags=re.I):
        tag = match.group(1)
        name_m = re.search(r'\bname="([^"]*)"', tag)
        if not name_m:
            continue
        name = html.unescape(name_m.group(1))
        type_m = re.search(r'\btype="([^"]*)"', tag, flags=re.I)
        field_type = (type_m.group(1).lower() if type_m else "text")
        if field_type in {"submit", "button", "image"}:
            continue
        value_m = re.search(r'\bvalue="([^"]*)"', tag)
        value = html.unescape(value_m.group(1)) if value_m else ""
        if field_type == "radio":
            if "checked" in tag.lower():
                data[name] = value
            continue
        if field_type == "checkbox":
            if "checked" in tag.lower():
                data[name] = value or "on"
            continue
        data[name] = value
    for match in re.finditer(r"<select\b([^>]*)>(.*?)</select>", page, flags=re.I | re.S):
        name_m = re.search(r'\bname="([^"]*)"', match.group(1))
        if not name_m:
            continue
        name = html.unescape(name_m.group(1))
        selected = re.search(
            r'<option[^>]*selected[^>]*value="([^"]*)"', match.group(2), flags=re.I
        ) or re.search(r'<option[^>]*value="([^"]*)"', match.group(2), flags=re.I)
        data[name] = html.unescape(selected.group(1)) if selected else ""
    return data


def _asp_post(
    session: requests.Session,
    page: str,
    event_target: str,
    extra: dict[str, str] | None = None,
    event_arg: str = "",
) -> str:
    data = _form_fields(page)
    data["__EVENTTARGET"] = event_target
    data["__EVENTARGUMENT"] = event_arg
    if extra:
        data.update(extra)
    response = session.post(
        SEARCH_URL,
        data=data,
        timeout=REQUEST_TIMEOUT,
        headers={"Origin": "https://burzarada.hzz.hr", "Referer": SEARCH_URL},
        allow_redirects=True,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _parse_listing_jobs(html: str) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()
    for match in LISTING_JOB_RE.finditer(html):
        job_id = match.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append(
            Job(
                job_id=job_id,
                url=JOB_URL_TEMPLATE.format(job_id=job_id),
                title=clean_text(match.group(2)),
                workplace=clean_text(match.group(3)),
                employer=clean_text(match.group(4)),
                deadline=clean_text(match.group(5)),
            )
        )
    return jobs


def fetch_jobs_from_website(session: requests.Session) -> tuple[list[Job], int | None]:
    """Vrati oglase Zadarske županije s HTML tražilice i službeni broj 'Pronađeno oglasa'."""
    log.info("Dohvaćam HTML pretragu Zadarske županije...")
    last_error: Exception | None = None
    for attempt in range(1, RSS_RETRIES + 1):
        try:
            landing = session.get(SEARCH_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            landing.raise_for_status()
            landing.encoding = landing.apparent_encoding or "utf-8"
            page = landing.text
            page = _asp_post(session, page, "ctl00$MainContent$lnkNapredno")
            page = _asp_post(
                session,
                page,
                "ctl00$MainContent$btnPretrazivanje",
                {"ctl00$MainContent$ddlZupanija": ZUPANIJA_ZADARSKA},
            )
            if "lblResults" not in page:
                raise requests.RequestException("Nema rezultata pretrage (lblResults).")
            page = _asp_post(
                session,
                page,
                "ctl00$MainContent$ddlPageSize",
                {"ctl00$MainContent$ddlPageSize": LISTING_PAGE_SIZE},
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            log.warning("HTML pretraga nije uspjela (%s/%s): %s", attempt, RSS_RETRIES, exc)
            if attempt < RSS_RETRIES:
                time.sleep(2 * attempt)
    else:
        raise last_error or RuntimeError("HTML pretraga nije uspjela")

    label_m = re.search(r'id="ctl00_MainContent_lblResults"[^>]*>([^<]+)', page)
    label = clean_text(label_m.group(1)) if label_m else ""
    reported = None
    num_m = re.search(r"(\d+)", label.replace(".", "").replace(",", ""))
    if num_m:
        reported = int(num_m.group(1))
    log.info("Burza rada: %s", label or "(nema lblResults)")

    all_jobs: dict[str, Job] = {}
    visited_pages: set[int] = set()
    for _ in range(20):
        active_m = re.search(r'<li class=active><a title="Idi na stranicu (\d+)"', page)
        current = int(active_m.group(1)) if active_m else 1
        for job in _parse_listing_jobs(page):
            all_jobs[job.job_id] = job
        visited_pages.add(current)
        pages = [
            (int(num), html.unescape(target), html.unescape(arg))
            for num, target, arg in PAGER_RE.findall(page)
        ]
        nxt = next(((n, t, a) for n, t, a in pages if n not in visited_pages), None)
        log.debug("HTML stranica %s: ukupno %s oglasa", current, len(all_jobs))
        if not nxt:
            break
        page = _asp_post(session, page, nxt[1], event_arg=nxt[2])

    log.info(
        "HTML lista: %s oglasa (službeni broj %s). Grid često prikaže najviše 300.",
        len(all_jobs),
        reported if reported is not None else "?",
    )
    return list(all_jobs.values()), reported


def merge_job(primary: Job, extra: Job) -> Job:
    if not primary.title and extra.title:
        primary.title = extra.title
    if not primary.workplace and extra.workplace:
        primary.workplace = extra.workplace
    if not primary.employer and extra.employer:
        primary.employer = extra.employer
    if not primary.deadline and extra.deadline:
        primary.deadline = extra.deadline
    if not primary.category and extra.category:
        primary.category = extra.category
    if not primary.description and extra.description:
        primary.description = extra.description
    return primary


def fetch_all_jobs(session: requests.Session) -> list[Job]:
    """HTML pretraga (naslov/poslodavac/rok) + RSS ID-ovi koji nisu u gridu (limit 300)."""
    web_jobs: list[Job] = []
    reported = None
    try:
        web_jobs, reported = fetch_jobs_from_website(session)
    except Exception:
        log.exception("HTML pretraga nije uspjela, nastavljam sa RSS-om.")

    rss_jobs: list[Job] = []
    try:
        rss_jobs = fetch_jobs_from_rss(session)
    except Exception:
        if not web_jobs:
            raise
        log.exception("RSS nije uspio; koristim samo HTML listu.")

    by_id: dict[str, Job] = {job.job_id: job for job in web_jobs}
    extra_rss = 0
    for job in rss_jobs:
        if job.job_id in by_id:
            by_id[job.job_id] = merge_job(by_id[job.job_id], job)
        else:
            by_id[job.job_id] = job
            extra_rss += 1
    if extra_rss:
        log.info("RSS dopunio %s oglasa kojih nema u HTML gridu.", extra_rss)
    if reported is not None:
        log.info("Zadarska županija ukupno (službeno): %s, spoji HTML+RSS: %s", reported, len(by_id))
    return list(by_id.values())


# =============================================================================
# Detalji oglasa (naslov + poslodavac; RSS često nema ni jedno ni drugo)
# =============================================================================

SPAN_IDS = {
    "workplace": "ctl00_MainContent_lblMjestoRada",
    "employer": "ctl00_MainContent_lblNazivPoslodavca",
    "deadline": "ctl00_MainContent_lblVrijediDo",
    "description": "ctl00_MainContent_lblOpisPosla",
}


def extract_span(page_html: str, span_id: str) -> str:
    match = re.search(
        rf'<span[^>]*\bid=["\']{re.escape(span_id)}["\'][^>]*>(.*?)</span>',
        page_html,
        flags=re.I | re.S,
    )
    if not match:
        return ""
    return clean_text(match.group(1))


def extract_job_title(page_html: str) -> str:
    match = re.search(
        r'<div[^>]*id=["\']ctl00_MainContent_pnlAjaxBlock["\'][\s\S]*?<h3[^>]*>(.*?)</h3>',
        page_html,
        flags=re.I,
    )
    if match:
        title = clean_text(match.group(1))
        if title:
            return title
    match = re.search(r"<h3[^>]*>(.*?)</h3>", page_html, flags=re.I | re.S)
    if match:
        title = clean_text(match.group(1))
        if title and "burza" not in title.lower():
            return title
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.I | re.S)
    if match:
        title = clean_text(match.group(1))
        title = re.sub(r"\s*[-–|]\s*Burza rada.*$", "", title, flags=re.I)
        return title.strip()
    return ""


def enrich_job_from_page(session: requests.Session, job: Job) -> Job:
    try:
        response = request_with_retry(
            session,
            job.url,
            attempts=DETAIL_RETRIES,
            what=f"Oglas {job.job_id}",
        )
    except requests.RequestException as exc:
        log.warning("Detalji oglasa %s nisu dohvaćeni: %s", job.job_id, exc)
        return job

    page = response.content.decode(response.apparent_encoding or "utf-8", errors="replace")
    if "ctl00_MainContent" not in page:
        log.debug("Oglas %s: stranica nema očekivana polja.", job.job_id)
        return job

    title = extract_job_title(page)
    workplace = extract_span(page, SPAN_IDS["workplace"])
    employer = extract_span(page, SPAN_IDS["employer"])
    deadline = extract_span(page, SPAN_IDS["deadline"])
    description = extract_span(page, SPAN_IDS["description"])

    if title:
        job.title = title
    if workplace:
        job.workplace = workplace
    if employer:
        job.employer = employer
    if deadline:
        job.deadline = deadline
    if description and (not job.description or len(description) > len(job.description)):
        job.description = description
    job.from_detail = True
    return job


def needs_detail_page(job: Job) -> bool:
    """Dohvati stranicu ako ne znamo je li Zadar, ili ako fali naslov/poslodavac/rok."""
    if not workplace_looks_valid(job.workplace):
        return True
    if not is_zadar_workplace(job.workplace):
        return False
    return not (job.title and job.employer and job.deadline)


# =============================================================================
# Telegram
# =============================================================================


def require_telegram_config() -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("123456"):
        raise SystemExit(
            "Nedostaje TELEGRAM_BOT_TOKEN.\n"
            "Kopiraj .env.example u .env i upiši token od @BotFather.\n"
            "Upute su u README.md."
        )
    ids = telegram_chat_ids()
    if not ids or ids == ["123456789"] or ids == ["tvoj_chat_id"]:
        raise SystemExit(
            "Nedostaje TELEGRAM_CHAT_ID.\n"
            "Upute kako ga dobiti su u README.md.\n"
            "Više osoba: TELEGRAM_CHAT_ID=111,222"
        )


def format_job_message(job: Job) -> str:
    lines = [
        "🔔 <b>Novi oglas za posao u Zadru</b>",
        "",
        f"📌 <b>Naslov:</b> {html_escape(job.display_title)}",
        f"📍 <b>Mjesto rada:</b> {html_escape(job.workplace or 'Zadar')}",
    ]
    if job.employer:
        lines.append(f"🏢 <b>Poslodavac:</b> {html_escape(job.employer)}")
    if job.deadline:
        lines.append(f"📅 <b>Rok za prijavu:</b> {html_escape(job.deadline)}")
    if job.category:
        lines.append(f"🗂 <b>Kategorija:</b> {html_escape(job.category)}")
    lines += [
        "",
        f'🔗 <a href="{html_escape(job.url)}">Otvori oglas na Burzi rada</a>',
    ]
    return "\n".join(lines)


def _send_telegram_one(
    session: requests.Session, chat_id: str, text: str
) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    last_error: Exception | None = None
    for i in range(1, TELEGRAM_RETRIES + 1):
        try:
            response = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            data = {}
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 400:
                log.error("Telegram 400 (chat %s): %s", chat_id, data or response.text)
                return False
            if response.status_code == 429:
                retry_after = int((data.get("parameters") or {}).get("retry_after") or 5)
                log.warning("Telegram rate limit, čekam %ss...", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            if not data.get("ok", True):
                raise requests.RequestException(data.get("description") or "Telegram ok=false")
            return True
        except requests.RequestException as exc:
            last_error = exc
            log.warning(
                "Telegram slanje na %s nije uspjelo (%s/%s): %s",
                chat_id,
                i,
                TELEGRAM_RETRIES,
                exc,
            )
            if i < TELEGRAM_RETRIES:
                time.sleep(2 * i)
    log.error("Telegram poruka nije poslana na %s: %s", chat_id, last_error)
    return False


def send_telegram(session: requests.Session, text: str, *, dry_run: bool = False) -> bool:
    if dry_run:
        log.info("[dry-run] Telegram poruka:\n%s", text)
        return True

    require_telegram_config()
    ok_all = True
    for chat_id in telegram_chat_ids():
        if not _send_telegram_one(session, chat_id, text):
            ok_all = False
    return ok_all


# =============================================================================
# Glavna provjera
# =============================================================================


def run_check(
    session: requests.Session,
    *,
    send_existing: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    stats = {
        "feed": 0,
        "new": 0,
        "zadar_new": 0,
        "skipped_other_city": 0,
        "sent": 0,
        "failed": 0,
        "seeded": 0,
    }
    state = load_seen()
    seen: set[str] = set(state.get("seen_ids") or [])
    first_run = len(seen) == 0
    if first_run:
        log.info(
            "Prvo pokretanje: postojeći oglasi se %s.",
            "ŠALJU na Telegram" if send_existing else "samo spremaju (bez obavijesti)",
        )

    try:
        jobs = fetch_all_jobs(session)
    except Exception:
        log.exception("Ne mogu dohvatiti oglase (HTML/RSS). Provjera prekinuta.")
        return stats

    stats["feed"] = len(jobs)
    if not jobs:
        log.warning("Nema oglasa za obradu.")
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        if not dry_run:
            save_seen(state)
        return stats

    seed_only = first_run and not send_existing
    sent_this_run = 0

    for job in jobs:
        if job.job_id in seen:
            continue
        stats["new"] += 1

        if seed_only:
            mark_seen(state, job.job_id)
            seen.add(job.job_id)
            stats["seeded"] += 1
            if not dry_run and stats["seeded"] % 50 == 0:
                save_seen(state)
            continue

        if limit is not None and sent_this_run >= limit:
            log.info("Dosegnut --limit %s, ostatak novih oglasa čeka iduću provjeru.", limit)
            break

        known_not_zadar = workplace_looks_valid(job.workplace) and not is_zadar_workplace(
            job.workplace
        )
        if known_not_zadar:
            mark_seen(state, job.job_id)
            seen.add(job.job_id)
            stats["skipped_other_city"] += 1
            log.debug("Preskačem %s (mjesto: %s)", job.job_id, job.workplace)
            continue

        if needs_detail_page(job):
            time.sleep(DETAIL_DELAY_SEC)
            job = enrich_job_from_page(session, job)

        if not is_zadar_workplace(job.workplace):
            if job.workplace:
                mark_seen(state, job.job_id)
                seen.add(job.job_id)
                stats["skipped_other_city"] += 1
                log.info("Oglas %s nije Zadar (%s) — preskačem.", job.job_id, job.workplace)
            elif job.from_detail:
                mark_seen(state, job.job_id)
                seen.add(job.job_id)
                stats["skipped_other_city"] += 1
                log.info("Oglas %s: stranica nema mjesto rada — preskačem.", job.job_id)
            else:
                log.warning(
                    "Oglas %s: mjesto rada nije utvrđeno (mreža/RSS), ostavljam za iduću provjeru.",
                    job.job_id,
                )
            continue

        stats["zadar_new"] += 1
        log.info("Novi oglas za Zadar: %s — %s", job.job_id, job.display_title)

        ok = send_telegram(session, format_job_message(job), dry_run=dry_run)
        if ok:
            mark_seen(state, job.job_id)
            seen.add(job.job_id)
            stats["sent"] += 1
            sent_this_run += 1
            if not dry_run:
                save_seen(state)
            time.sleep(TELEGRAM_DELAY_SEC)
        else:
            stats["failed"] += 1
            log.error("Oglas %s NIJE označen kao viđen jer slanje nije uspjelo.", job.job_id)

    if seed_only:
        zadar_in_feed = sum(1 for j in jobs if is_zadar_workplace(j.workplace))
        unknown = sum(1 for j in jobs if not j.workplace)
        log.info(
            "Inicijalno spremljeno %s ID-ova. Grad Zadar=%s, ostala mjesta županije=%s, bez mjesta=%s.",
            stats["seeded"],
            zadar_in_feed,
            stats["seeded"] - zadar_in_feed - unknown,
            unknown,
        )
        where = (
            "GitHub Actions (tri puta dnevno, ~00:00, ~08:00 i ~16:00)"
            if os.getenv("GITHUB_ACTIONS") == "true"
            else "sljedeće provjere (00:00, 08:00 i 16:00)"
        )
        msg = (
            "✅ <b>HZZ Zadar bot je pokrenut</b>\n\n"
            f"Pratim nove oglase za mjesto rada <b>Zadar</b>.\n"
            f"U Zadarskoj županiji: {stats['feed']} oglasa, "
            f"od toga <b>{zadar_in_feed}</b> s mjestom rada Zadar "
            f"(Petrčane, Bibinje, Silba… se ne šalju).\n\n"
            f"Postojeći oglasi nisu poslani. Od {where} "
            "dobivat ćeš samo NOVE oglase."
        )
        send_telegram(session, msg, dry_run=dry_run)

    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    if dry_run:
        log.info("dry-run: seen_jobs.json NIJE spremljen.")
    else:
        save_seen(state)
    log.info(
        "Gotovo. feed=%s novi=%s zadar_novi=%s poslano=%s nije_zadar=%s fail=%s seeded=%s",
        stats["feed"],
        stats["new"],
        stats["zadar_new"],
        stats["sent"],
        stats["skipped_other_city"],
        stats["failed"],
        stats["seeded"],
    )
    return stats


# =============================================================================
# Raspored 00:00 / 08:00 / 16:00
# =============================================================================


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except Exception:
        log.warning("Nepoznata zona %s, koristim Europe/Zagreb.", TIMEZONE_NAME)
        return ZoneInfo("Europe/Zagreb")


def next_scheduled_time(now: datetime | None = None) -> datetime:
    now = now or datetime.now(tz())
    hours = sorted(CHECK_HOURS)
    for hour in hours:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now + timedelta(seconds=15):
            return candidate
    tomorrow = (now + timedelta(days=1)).replace(
        hour=hours[0], minute=0, second=0, microsecond=0
    )
    return tomorrow


def sleep_until(target: datetime) -> None:
    """Spava do ciljanog vremena, u kratkim intervalima (preživi sleep laptopa)."""
    while True:
        now = datetime.now(tz())
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        log.info("Sljedeća provjera u %s (za %s).", target.strftime("%Y-%m-%d %H:%M"), _fmt_delta(remaining))
        time.sleep(min(remaining, 60))


def _fmt_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}min"
    if m:
        return f"{m}min {s}s"
    return f"{s}s"


def run_scheduler(session: requests.Session, args: argparse.Namespace) -> None:
    log.info(
        "Bot radi u petlji. Provjera u %s sati, zona %s. Ctrl+C za prekid.",
        " i ".join(f"{h:02d}:00" for h in CHECK_HOURS),
        TIMEZONE_NAME,
    )
    log.info("Prva provjera odmah...")
    run_check(
        session,
        send_existing=args.send_existing,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    # send_existing vrijedi samo za prvi prolaz
    args.send_existing = False
    while True:
        sleep_until(next_scheduled_time())
        log.info("Zakazana provjera...")
        run_check(session, send_existing=False, dry_run=args.dry_run, limit=args.limit)


# =============================================================================
# CLI
# =============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Telegram bot za nove HZZ oglase u Zadru (Burza rada)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Jedna provjera pa izlaz (za cron / Task Scheduler / launchd).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Pošalji testnu poruku na Telegram i izađi.",
    )
    parser.add_argument(
        "--send-existing",
        action="store_true",
        help="Pri prvom pokretanju pošalji i trenutno aktivne oglase za Zadar.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ne šalji na Telegram, samo ispiši što bi bilo poslano.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maksimalan broj novih oglasa za slanje u ovoj provjeri.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Detaljniji log.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global log
    log = setup_logging(verbose=args.verbose)

    if os.getenv("GITHUB_ACTIONS") == "true" and not args.once and not args.test:
        args.once = True
        log.info("GitHub Actions detektiran — radim jednu provjeru (--once).")

    log.info("=== HZZ Zadar Job Bot ===")
    log.info("Mapa: %s", BASE_DIR)
    log.info("HTML: %s", SEARCH_URL)
    log.info("RSS:  %s", RSS_URL)

    session = make_session()

    if args.test:
        ok = send_telegram(
            session,
            "✅ Test: HZZ Zadar bot radi.\nAko vidiš ovu poruku, token i chat_id su ispravni.",
            dry_run=args.dry_run,
        )
        return 0 if ok else 1

    if not args.dry_run:
        try:
            require_telegram_config()
        except SystemExit as exc:
            log.error("%s", exc)
            return 2

    try:
        if args.once:
            run_check(
                session,
                send_existing=args.send_existing,
                dry_run=args.dry_run,
                limit=args.limit,
            )
        else:
            run_scheduler(session, args)
    except KeyboardInterrupt:
        log.info("Prekinuto (Ctrl+C). Bot je ugašen.")
        return 0
    except Exception:
        log.exception("Neočekivana greška.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
