#!/usr/bin/env python3
"""
collect.py -- daily snapshot of job postings into SQLite.

Layer 1 of a three-layer design:
  1. collectors  (this file)  -- boring, deterministic, builds history
  2. verticals   (verticals.yaml) -- config, not code
  3. agent       (later)      -- reads this DB, searches live, wanders

Two axes of extension, neither of which requires editing logic here:
  * new job area   -> add a block to verticals.yaml
  * new source     -> write a fetcher returning the standard dict, register
                      it in FETCHERS

Usage:
    python collect.py --db jobs.db --companies companies.yaml
    python collect.py --db jobs.db --verticals verticals.yaml
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

UA = {"User-Agent": "personal-job-tracker/1.0"}
TIMEOUT = 20

# USAJOBS needs a free API key: request one at https://developer.usajobs.gov/apirequest/
# Paste the key here and the email you registered with. Until both are set,
# the usajobs source errors out and logs it; every other source is unaffected.
#
# THIS FILE IS IN A PUBLIC REPOSITORY. A real key pasted below goes public with
# the next commit -- keep it out of anything you push.
USAJOBS_KEY = ""
USAJOBS_EMAIL = ""

# If this fraction of a company's known postings change body in one run, treat
# it as a template edit rather than N independent edits.
BULK_EDIT_RATIO = 0.30
BULK_MIN_POSTINGS = 5

# Universal metadata, recorded on every posting but NOT used to filter.
# Filtering on these belongs at query time, where you can change your mind
# without losing history.
REMOTE_TERMS = [
    "remote", "work from home", "wfh", "distributed team", "anywhere",
    "home-based", "hybrid",
]

INTENSITY_TERMS = [
    "fast-paced", "wear many hats", "move fast", "hustle", "scrappy",
    "0 to 1", "zero to one", "on-call", "oncall", "high-growth",
    "ambiguity", "self-starter", "roll up your sleeves", "24/7",
    "startup environment", "unlimited pto",
]

SENIORITY_TERMS = [
    "staff", "principal", "senior", "lead", "director", "head of",
    "manager", "vp", "junior", "entry level", "intern",
]


# --- sources ---------------------------------------------------------------
# Every fetcher returns a list of dicts with the same keys. That uniformity is
# what makes adding a source cheap -- job boards, RSS feeds, and scraped
# aggregators all land in the same shape.

def _std(ats, company, external_id, title, location, url, body, posted_at=""):
    return {"ats": ats, "company": company, "external_id": str(external_id),
            "title": title or "", "location": location or "", "url": url or "",
            "body": body or "", "posted_at": posted_at or ""}


def fetch_greenhouse(token):
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
        headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [_std("greenhouse", token, j.get("id"), j.get("title"),
                 (j.get("location") or {}).get("name"), j.get("absolute_url"),
                 j.get("content"), j.get("updated_at"))
            for j in r.json().get("jobs", [])]


def fetch_lever(token):
    r = requests.get(f"https://api.lever.co/v0/postings/{token}?mode=json",
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        posted = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                  if ts else "")
        out.append(_std("lever", token, j.get("id"), j.get("text"),
                        cat.get("location"), j.get("hostedUrl"),
                        j.get("descriptionPlain"), posted))
    return out


def fetch_ashby(token):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                     headers=UA, params={"includeCompensation": "true"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return [_std("ashby", token, j.get("id"), j.get("title"),
                 j.get("location"), j.get("jobUrl"),
                 j.get("descriptionPlain") or j.get("descriptionHtml"),
                 j.get("publishedAt"))
            for j in r.json().get("jobs", [])]




# --- broader-market sources -------------------------------------------------
# Greenhouse/Lever/Ashby are startup-tier ATSs. Non-tech-native employers --
# insurers, hospital systems, manufacturers, logistics, government contractors
# -- mostly run Workday, SmartRecruiters or Recruitee. These three fetchers are
# what reach that market.
#
# SmartRecruiters and Workday need a second call per posting to get the body,
# so they are slower than the others. DETAIL_CAP bounds that per company.

DETAIL_CAP = 2000
DETAIL_PAUSE = 0.25          # starting pace; backs off automatically on 429
DETAIL_MAX_PAUSE = 4.0
DETAIL_RETRIES = 3


def new_detail_stats():
    return {"attempted": 0, "ok": 0, "failed": 0, "throttled": 0,
            "pause": DETAIL_PAUSE, "errors": {}, "last_error": ""}


def detail_get(url, stats, as_text=False):
    """Fetch one posting body, with backoff, and never fail silently.

    Thousands of detail calls against one tenant will get rate-limited. The
    previous version swallowed the exception, so a throttled run looked
    identical to a run where every posting genuinely had no description.
    """
    stats["attempted"] += 1
    for attempt in range(DETAIL_RETRIES + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code in (429, 503):
                stats["throttled"] += 1
                wait = r.headers.get("Retry-After")
                wait = float(wait) if wait and wait.isdigit() else stats["pause"] * 4
                # Slow the whole company down, not just this call.
                stats["pause"] = min(stats["pause"] * 1.5, DETAIL_MAX_PAUSE)
                time.sleep(min(wait, 30))
                continue
            r.raise_for_status()
            stats["ok"] += 1
            # mathjobs serves HTML, every other detail source serves JSON.
            return r.text if as_text else r.json()
        except Exception as e:
            name = type(e).__name__
            stats["errors"][name] = stats["errors"].get(name, 0) + 1
            stats["last_error"] = f"{name}: {e}"[:180]
            time.sleep(stats["pause"] * (attempt + 1))
    stats["failed"] += 1
    return None


def detail_summary(stats):
    if not stats["attempted"]:
        return ""
    bits = [f"bodies {stats['ok']}/{stats['attempted']}"]
    if stats["throttled"]:
        bits.append(f"throttled {stats['throttled']}x")
    if stats["failed"]:
        top = ", ".join(f"{k}:{v}" for k, v in
                        sorted(stats["errors"].items(), key=lambda x: -x[1])[:2])
        bits.append(f"failed {stats['failed']} ({top})")
    if stats["pause"] > DETAIL_PAUSE:
        bits.append(f"paced to {stats['pause']:.1f}s")
    return "  [" + "; ".join(bits) + "]"


DETAIL_STATS = {}


def _strip_html(s):
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", " ",
                                 re.sub(r"\s+", " ", s or ""))).strip()


def fetch_smartrecruiters(token):
    """https://api.smartrecruiters.com/v1/companies/<token>/postings"""
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    out, offset, total_hi = [], 0, 0
    while True:
        r = requests.get(base, headers=UA, timeout=TIMEOUT,
                         params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        if not items:
            break
        for j in items:
            loc = j.get("location") or {}
            out.append(_std("smartrecruiters", token, j.get("id"),
                            j.get("name"),
                            ", ".join(x for x in (loc.get("city"),
                                                  loc.get("region"),
                                                  loc.get("country")) if x),
                            j.get("ref") or "", "", j.get("releasedDate")))
        offset += len(items)
        # Track the high-water mark. Some ATS tenants report an honest total
        # on page 1 and 0 on every page after, which truncates the fetch if
        # each page's fresh value is trusted. (Confirmed on Workday.)
        total_hi = max(total_hi, data.get("totalFound") or 0)
        if offset >= total_hi or offset > 5000:
            break
        time.sleep(0.3)

    stats = new_detail_stats()
    for job in out[:DETAIL_CAP]:
        d = detail_get(f"{base}/{job['external_id']}", stats)
        if d:
            parts = []
            for sec in (d.get("jobAd", {}).get("sections") or {}).values():
                if isinstance(sec, dict) and sec.get("text"):
                    parts.append(_strip_html(sec["text"]))
            job["body"] = " ".join(parts)
            job["url"] = d.get("applyUrl") or job["url"]
        time.sleep(stats["pause"])
    DETAIL_STATS[token] = stats
    return out


def fetch_recruitee(token):
    """https://<token>.recruitee.com/api/offers/ -- body included, one call."""
    r = requests.get(f"https://{token}.recruitee.com/api/offers/",
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [_std("recruitee", token, j.get("id"), j.get("title"),
                 j.get("location") or j.get("city") or "",
                 j.get("careers_url") or j.get("url"),
                 _strip_html(j.get("description", "")
                             + " " + (j.get("requirements") or "")),
                 j.get("published_at"))
            for j in r.json().get("offers", [])]


def fetch_workday(cfg):
    """Workday cxs API. Needs a dict, not a plain token:

    workday:
      - token: acmecorp                       # name used in the DB
        host: acmecorp.wd5.myworkdayjobs.com
        site: External                        # the career-site path segment

    Find host and site by opening the company's Workday careers page and
    looking at the URL: https://<host>/en-US/<site>/...
    """
    token, host, site = cfg["token"], cfg["host"], cfg["site"]
    base = f"https://{host}/wday/cxs/{cfg.get('tenant', token)}/{site}"
    out, offset, total_hi = [], 0, 0
    while True:
        r = requests.post(f"{base}/jobs", headers={**UA,
                          "Content-Type": "application/json"},
                          timeout=TIMEOUT,
                          json={"limit": 20, "offset": offset,
                                "appliedFacets": {}, "searchText": ""})
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            out.append(_std("workday", token,
                            j.get("bulletFields", [j.get("externalPath")])[0],
                            j.get("title"), j.get("locationsText"),
                            f"https://{host}/en-US/{site}"
                            f"{j.get('externalPath', '')}",
                            "", j.get("postedOn")))
            out[-1]["_path"] = j.get("externalPath")
        offset += len(posts)
        # Workday reports an honest total on page 1 and 0 on every page after,
        # for some tenants. Trusting each page's fresh value truncates the
        # fetch at ~40 postings. Track the high-water mark instead.
        total_hi = max(total_hi, data.get("total") or 0)
        if offset >= total_hi or offset > 20000:
            break
        time.sleep(0.3)

    stats = new_detail_stats()
    for job in out[:DETAIL_CAP]:
        path = job.pop("_path", None)
        if not path:
            continue
        d = detail_get(f"{base}{path}", stats)
        if d:
            info = d.get("jobPostingInfo", {})
            job["body"] = _strip_html(info.get("jobDescription", ""))
            job["posted_at"] = info.get("startDate") or job["posted_at"]
        time.sleep(stats["pause"])
    DETAIL_STATS[token] = stats
    for job in out:
        job.pop("_path", None)
    return out


# --- research-market sources ------------------------------------------------
# The two below reach employers no ATS fetcher can: mathjobs.org is where math
# departments and the defense research institutes advertise, and USAJOBS is the
# federal route (NSA's internship programs post there).

MATHJOBS = "https://www.mathjobs.org"

# Consecutive all-duplicate pages tolerated before a paged walk gives up.
# Above 1 so a mid-walk re-rank does not end the walk; low enough that a
# site ignoring the offset parameter is still caught quickly.
BLANK_PAGE_TOLERANCE = 3


def fetch_mathjobs(cfg):
    """mathjobs.org (AMS) public JSON feed -- NOT scraped HTML.

    The feed is linked off every employer page as "position listing in
    reusable JSON format" and needs no key:

        https://www.mathjobs.org/jobs/<tenant_id>/public_job_boards   (one employer)
        https://www.mathjobs.org/jobs/public_job_boards               (whole board)

    Both accept limit, page, search, all_postings, date_from, date_to,
    unit_name, tenant_id, position_id. Two config shapes:

    mathjobs:
      - token: ida                 # one employer, by numeric tenant id
        tenant: 1857
      - token: mathjobs-research   # whole board, keyword-filtered
        search: summer

    The feed carries description, qualifications and a real application
    deadline, so there is no per-posting second call. The deadline is prefixed
    onto the body because postings has no column for it -- grep the body for
    "Application deadline:".

    Employer name goes in `location`, not `title`: location is never scored by
    the verticals, so an employer called "Institute for Mathematical Research"
    cannot manufacture a title hit.
    """
    token = cfg if isinstance(cfg, str) else cfg["token"]
    tenant = None if isinstance(cfg, str) else cfg.get("tenant")
    path = f"/jobs/{tenant}/public_job_boards" if tenant else "/jobs/public_job_boards"
    params = {"limit": 200, "page": 1}
    if not isinstance(cfg, str):
        for k in ("search", "all_postings", "date_from", "date_to", "unit_name"):
            if cfg.get(k) is not None:
                params[k] = cfg[k]

    out = []
    while True:
        r = requests.get(MATHJOBS + path, headers=UA, params=params,
                         timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            break
        for j in results:
            dl = _text(j.get("deadline_raw")).strip()
            dl = "" if dl.startswith("0000") else dl
            body = " ".join(filter(None, [
                f"Application deadline: {dl}." if dl else "",
                f"Position type: {_text(j.get('type'))}." if j.get("type") else "",
                f"Subject area: {_text(j.get('subject'))}." if j.get("subject") else "",
                _strip_html(_text(j.get("description", ""))),
                _strip_html(_text(j.get("qualifications", ""))),
            ]))
            where = j.get("location") or ", ".join(
                filter(None, [_text(j.get("city")), _text(j.get("state")),
                              _text(j.get("country"))]))
            employer = j.get("univ") or j.get("unit_name") or ""
            out.append(_std("mathjobs", token,
                            j.get("id") or j.get("legacy_position_id"),
                            _strip_html(_text(j.get("name", ""))),
                            " · ".join(filter(None, [_text(employer),
                                                     _text(where)])),
                            _text(j.get("url", "")), body,
                            _text(j.get("open_date_raw"))[:10]))
        if len(results) < params["limit"]:
            break
        params["page"] += 1
        if params["page"] > 25:
            break
        time.sleep(0.5)
    return out


def fetch_usajobs(cfg):
    """USAJOBS search API -- the federal route, including NSA.

    usajobs:
      - token: nsa-math
        keyword: mathematics
        organization: DJ        # optional agency code; omit to search all
        results: 250            # optional, max 500

    Needs USAJOBS_KEY and USAJOBS_EMAIL set at the top of this file. The API
    returns the full announcement text in the search response, so unlike
    Workday there is no per-posting second call.
    """
    if not USAJOBS_KEY or not USAJOBS_EMAIL:
        raise RuntimeError(
            "USAJOBS_KEY/USAJOBS_EMAIL not set -- request a free key at "
            "https://developer.usajobs.gov/apirequest/ and paste it at the "
            "top of collect.py")
    token = cfg["token"]
    headers = {"Host": "data.usajobs.gov",
               "User-Agent": USAJOBS_EMAIL,
               "Authorization-Key": USAJOBS_KEY}
    params = {"ResultsPerPage": min(int(cfg.get("results", 250)), 500), "Page": 1}
    if cfg.get("keyword"):
        params["Keyword"] = cfg["keyword"]
    if cfg.get("organization"):
        params["Organization"] = cfg["organization"]
    if cfg.get("position_title"):
        params["PositionTitle"] = cfg["position_title"]

    out = []
    while True:
        r = requests.get("https://data.usajobs.gov/api/Search",
                         headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        res = r.json().get("SearchResult", {})
        items = res.get("SearchResultItems", [])
        if not items:
            break
        for it in items:
            d = it.get("MatchedObjectDescriptor", {})
            ud = d.get("UserArea", {}).get("Details", {})
            body = " ".join(filter(None, [
                d.get("QualificationSummary", ""),
                ud.get("JobSummary", ""),
                ud.get("MajorDuties") if isinstance(ud.get("MajorDuties"), str)
                else " ".join(ud.get("MajorDuties") or []),
                ud.get("Requirements", ""),
                ud.get("Education", ""),
            ]))
            locs = ", ".join(l.get("LocationName", "") for l in
                             (d.get("PositionLocation") or [])[:4])
            out.append(_std("usajobs", token,
                            it.get("MatchedObjectId") or d.get("PositionID"),
                            d.get("PositionTitle"), locs,
                            d.get("PositionURI"), _strip_html(body),
                            d.get("PublicationStartDate", "")))
        if len(out) >= int(res.get("SearchResultCountAll") or 0):
            break
        params["Page"] += 1
        if params["Page"] > 40:
            break
        time.sleep(0.5)
    return out


def _text(v):
    """Flatten a Phenom field to a string.

    Phenom switches a field between string and list depending on the posting:
    a single-location job has multi_location "Aberdeen, Maryland, United
    States of America", a multi-location one has a list. sqlite3 refuses to
    bind a list, so the first multi-location posting aborted the whole run
    inside upsert -- after several companies had already been committed.
    """
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(_text(x) for x in v if x not in (None, ""))
    return v if isinstance(v, str) else str(v)


def _embedded_json(text, key, want=dict):
    """Pull one JSON value out of a server-rendered page by its key.

    Phenom renders results into the page instead of exposing a search API --
    its /api/apply/v2/jobs endpoint answers "Tenant not identified" from
    outside. The data sits in window.phApp.ddo.<key>. raw_decode parses
    exactly one value from the offset, so nesting and braces inside strings
    are handled properly; a regex for the closing brace is not.

    Every occurrence is tried, not just the first: on a MITRE job page the
    first "jobDetail" is an analytics mapping whose value is the string
    "data.job", and the real object comes later. `want` is what makes the
    difference between finding the job and calling .get() on a string.
    """
    marker = f'"{key}"'
    dec = json.JSONDecoder()
    start = 0
    while True:
        i = text.find(marker, start)
        if i < 0:
            return None
        start = i + len(marker)
        j = text.find(":", start)
        if j < 0:
            return None
        try:
            value, _ = dec.raw_decode(text, j + 1)
        except ValueError:
            continue
        if isinstance(value, want):
            return value


def fetch_phenom(cfg):
    """Phenom People career sites (careers.<company>.com).

    phenom:
      - token: mitre
        host: careers.mitre.org
        keywords: intern          # optional, filters server-side
        path: /us/en/search-results   # optional, this is the default

    Ten postings per page, paged with ?from=N&s=1. The list carries only a
    teaser, so each posting's own page is fetched for the full description.

    Paging is driven by `totalHits`, which every page reports, NOT by running
    until a page brings nothing new. The board re-ranks between requests, so a
    page part-way through a walk can come back entirely made of postings
    already seen; treating that as the end of the list stopped runs early at a
    different point each night. MITRE has 326 postings and six consecutive
    runs collected 261-284 of them, retiring the remainder as "gone". Pages
    that add nothing are now tolerated (up to BLANK_PAGE_TOLERANCE in a row)
    and the walk continues until `totalHits` is reached.

    `keywords` RE-RANKS rather than filtering -- the board still returns
    everything, just in a different order -- so it does not reduce the work
    and it makes the ordering less stable. Leave it unset.
    """
    token = cfg["token"]
    host = cfg["host"]
    path = cfg.get("path", "/us/en/search-results")
    base = f"https://{host}{path}"
    params = {}
    if cfg.get("keywords"):
        params["keywords"] = cfg["keywords"]

    out, seen = [], set()
    state = {"total": None}

    def read_page(offset):
        """Fetch one page; return (postings_on_page, newly_added)."""
        q = dict(params)
        if offset:
            q.update({"from": offset, "s": 1})
        r = requests.get(base, headers=UA, params=q, timeout=TIMEOUT)
        r.raise_for_status()
        blob = _embedded_json(r.text, "eagerLoadRefineSearch") or {}
        if state["total"] is None and isinstance(blob.get("totalHits"), int):
            state["total"] = blob["totalHits"]
        jobs = (blob.get("data") or {}).get("jobs") or []
        added = 0
        for j in jobs:
            jid = j.get("jobSeqNo") or j.get("reqId") or j.get("jobId")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            added += 1
            out.append(_std("phenom", token, jid, _text(j.get("title")),
                            _text(j.get("multi_location")
                                  or j.get("cityStateCountry")
                                  or j.get("location", "")),
                            _text(j.get("jobUrl") or j.get("applyUrl", "")),
                            _strip_html(_text(j.get("descriptionTeaser", ""))),
                            _text(j.get("postedDate"))[:10]))
        return len(jobs), added

    frm, blanks, barren = 0, 0, []
    for _ in range(int(cfg.get("max_pages", 80))):
        got, added = read_page(frm)
        if not got:
            break
        # A page of nothing but already-seen postings means the board re-ranked
        # mid-walk, not that the list ended. Keep going; give up only if it
        # keeps happening, which is what a site ignoring `from` looks like.
        if added:
            blanks = 0
        else:
            blanks += 1
            barren.append(frm)
            if blanks >= BLANK_PAGE_TOLERANCE:
                break
        frm += got
        if state["total"] is not None and len(seen) >= state["total"]:
            break
        time.sleep(0.2)

    # Advancing past a re-ranked page skips whatever it should have shown, so
    # the walk can finish short without ever stopping early. Re-request just
    # those offsets once -- a transient re-rank yields them the second time.
    total = state["total"]
    if barren and total is not None and len(seen) < total:
        for offset in barren:
            time.sleep(0.2)
            read_page(offset)
            if len(seen) >= total:
                break

    if total is not None and len(seen) < total:
        print(f"  ! phenom/{token}: collected {len(seen)} of {total} reported "
              f"postings -- disappearance data for this board is unreliable "
              f"this run", file=sys.stderr)

    stats = new_detail_stats()
    for job in out[:DETAIL_CAP]:
        if not job["url"]:
            continue
        page = detail_get(job["url"], stats, as_text=True)
        if not page:
            continue
        detail = _embedded_json(page, "jobDetail") or {}
        desc = ((detail.get("data") or {}).get("job") or {}).get("description")
        if not desc:
            desc = _embedded_json(page, "description", want=str)
        if isinstance(desc, str) and len(desc) > len(job["body"]):
            job["body"] = _strip_html(desc)[:40000]
        time.sleep(stats["pause"])
    DETAIL_STATS[token] = stats
    return out


FETCHERS = {
    "phenom": fetch_phenom,
    "mathjobs": fetch_mathjobs,
    "usajobs": fetch_usajobs,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
}


# --- matching --------------------------------------------------------------

_TERM_RE = {}


def term_re(term):
    """Word-boundary pattern for a term.

    Substring matching is a trap: "rag" matches storage, average, leverage,
    fragment; "llm" and "mcp" are safer but "ai" would match chair, claim,
    email. Boundaries are applied only where the term's own edge is a word
    character, so terms like "ci/cd" and "0 to 1" still match.
    """
    p = _TERM_RE.get(term)
    if p is None:
        t = term.strip().lower()
        left = r"\b" if t[:1].isalnum() else ""
        right = r"\b" if t[-1:].isalnum() else ""
        p = re.compile(left + re.escape(t) + right)
        _TERM_RE[term] = p
    return p


def hits(text, terms):
    low = " " + (text or "").lower().replace("\n", " ") + " "
    return sorted({t for t in terms if term_re(t).search(low)})


def score_vertical(job, spec):
    """Return (matched, score, title_hit) for one vertical.

    Frequency beats presence: a company blurb mentions 'AI' once, a real
    posting mentions its domain repeatedly. A title hit short-circuits,
    since a title is never boilerplate.
    """
    terms = [t.lower() for t in spec["terms"]]
    body = " " + (job["body"] or "").lower().replace("\n", " ") + " "
    title_hit = bool(hits(job["title"], terms))
    score = sum(len(term_re(t).findall(body)) for t in terms)
    matched = title_hit or score >= spec.get("min_score", 3)
    return matched, score, title_hit


def excluded_by_title(job, excludes):
    """Titles that disqualify a posting regardless of body score.

    Pre-sales and GTM roles legitimately discuss the technology they sell --
    a Sales Engineer's description really is about Kubernetes. Score cannot
    separate "builds the platform" from "demos the platform", so the title
    has to. This also closes the title-hit bypass: without it, any title
    containing a vertical term (e.g. "Chief Compliance Officer") matched
    without ever being scored.
    """
    return hits(job["title"], excludes) if excludes else []


def classify(job, verticals, excludes=()):
    blob = f"{job['title']} {job['location']} {job['body']}"
    hard_excluded = excluded_by_title(job, excludes)
    matches = {}
    would_match = False
    for vid, spec in verticals.items():
        matched, score, title_hit = score_vertical(job, spec)
        if matched:
            would_match = True
        # A vertical may add its own exclusions on top of the global list.
        if matched and (hard_excluded or
                        excluded_by_title(job, spec.get("exclude_titles") or [])):
            matched = False
        if matched or score:
            matches[vid] = {"matched": matched, "score": score,
                            "title_hit": title_hit}
    return {
        "matches": matches,
        "excluded_by_title": hard_excluded,
        # True only when the title exclusion actually cost us a match. A
        # recruiter or tax manager was never in scope, so counting those as
        # "excluded" inflates the number several-fold and hides the real one.
        "suppressed": would_match and not any(
            m["matched"] for m in matches.values()),
        "any_match": any(m["matched"] for m in matches.values()),
        "remote_hits": hits(blob, REMOTE_TERMS),
        "intensity_hits": hits(blob, INTENSITY_TERMS),
        "seniority_hits": hits(job["title"], SENIORITY_TERMS),
    }


def content_hash(job):
    return hashlib.sha256((job["body"] or "").encode()).hexdigest()[:16]


# --- storage ---------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    key             TEXT PRIMARY KEY,
    ats             TEXT, company TEXT, external_id TEXT,
    title           TEXT, location TEXT, url TEXT, posted_at TEXT,
    first_seen      TEXT, last_seen TEXT,
    content_hash    TEXT,
    edit_count      INTEGER DEFAULT 0,
    disappear_count INTEGER DEFAULT 0,
    is_live         INTEGER DEFAULT 1,
    matched         INTEGER DEFAULT 0,
    remote_hits     TEXT, intensity_hits TEXT, seniority_hits TEXT,
    body            TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    key       TEXT,
    vertical  TEXT,
    score     INTEGER,
    title_hit INTEGER,
    matched   INTEGER,
    PRIMARY KEY (key, vertical)
);
CREATE TABLE IF NOT EXISTS events (key TEXT, ts TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS fetches (
    ts TEXT, source TEXT, token TEXT, returned INTEGER, ok INTEGER, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetches_tok ON fetches(token, ts);
CREATE INDEX IF NOT EXISTS idx_events_key ON events(key);
CREATE INDEX IF NOT EXISTS idx_matches_v ON matches(vertical, matched);
"""


def migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    for name, decl in (("seniority_hits", "TEXT"), ("matched", "INTEGER DEFAULT 0")):
        if name not in cols:
            conn.execute(f"ALTER TABLE postings ADD COLUMN {name} {decl}")


def log(conn, key, kind, detail=""):
    conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                 (key, datetime.now(timezone.utc).isoformat(), kind, detail))


def bulk_edit_ratio(conn, jobs):
    """Fraction of a company's existing postings whose body changed.

    A company that rewrites shared boilerplate -- EEO text, benefits blurb,
    "about us" -- changes every posting's hash at once. Counting that as an
    edit on each req inflates edit_count until it stops meaning anything,
    which matters because edit_count is a ghost-detection signal.
    """
    known = changed = 0
    for j in jobs:
        key = f"{j['ats']}:{j['company']}:{j['external_id']}"
        row = conn.execute("SELECT content_hash FROM postings WHERE key=?",
                           (key,)).fetchone()
        if row is None:
            continue
        known += 1
        if row[0] != content_hash(j):
            changed += 1
    return changed, known, (changed / known if known else 0.0)


def upsert(conn, job, verticals, now, bulk=False, excludes=()):
    key = f"{job['ats']}:{job['company']}:{job['external_id']}"
    c = classify(job, verticals, excludes)
    matched = 1 if c["any_match"] else 0
    body = job["body"] if matched else ""
    h = content_hash(job)

    conn.execute("DELETE FROM matches WHERE key=?", (key,))
    for vid, m in c["matches"].items():
        conn.execute("INSERT INTO matches VALUES (?,?,?,?,?)",
                     (key, vid, m["score"], int(m["title_hit"]),
                      int(m["matched"])))

    meta = (json.dumps(c["remote_hits"]), json.dumps(c["intensity_hits"]),
            json.dumps(c["seniority_hits"]))
    row = conn.execute("SELECT content_hash, title, is_live FROM postings "
                       "WHERE key=?", (key,)).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO postings (key,ats,company,external_id,title,location,"
            "url,posted_at,first_seen,last_seen,content_hash,matched,"
            "remote_hits,intensity_hits,seniority_hits,body) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, job["ats"], job["company"], job["external_id"], job["title"],
             job["location"], job["url"], job["posted_at"], now, now, h,
             matched, *meta, body))
        log(conn, key, "first_seen", job["title"])
        return "new"

    old_hash, old_title, is_live = row
    conn.execute("UPDATE postings SET last_seen=?, is_live=1, title=?, "
                 "location=?, url=?, body=?, content_hash=?, matched=?, "
                 "remote_hits=?, intensity_hits=?, seniority_hits=? WHERE key=?",
                 (now, job["title"], job["location"], job["url"], body, h,
                  matched, *meta, key))

    if not is_live:
        conn.execute("UPDATE postings SET disappear_count=disappear_count+1 "
                     "WHERE key=?", (key,))
        log(conn, key, "reappeared")
        return "reappeared"
    if old_title != job["title"]:
        # Log this independently: previously a retitle bundled with any body
        # edit was recorded only as "edited", hiding title changes entirely.
        log(conn, key, "retitled", f"{old_title} -> {job['title']}")
    if old_hash != h:
        if bulk:
            # Company-wide template change: record it, but do not charge the
            # individual req for something it did not do.
            log(conn, key, "bulk_edited", f"{old_hash} -> {h}")
            return "bulk_edited"
        conn.execute("UPDATE postings SET edit_count=edit_count+1 WHERE key=?",
                     (key,))
        log(conn, key, "edited", f"{old_hash} -> {h}")
        return "edited"
    if old_title != job["title"]:
        return "retitled"
    return "unchanged"


# A board that returns materially fewer postings than last time is more
# likely a partial fetch than a mass delisting. Skip the disappearance pass
# for that company rather than writing events we cannot trust.
SHRINK_GUARD = 0.90


def recently_fetched(conn, token, hours):
    """Was this board successfully fetched within the last N hours?

    Lets an interrupted overnight run be resumed without refetching tens of
    thousands of postings that already landed.
    """
    r = conn.execute(
        "SELECT ts FROM fetches WHERE token=? AND ok=1 "
        "AND ts > datetime('now', ?) ORDER BY ts DESC LIMIT 1",
        (token, f"-{int(hours)} hours")).fetchone()
    return bool(r)


def last_good_count(conn, token):
    r = conn.execute("SELECT returned FROM fetches WHERE token=? AND ok=1 "
                     "ORDER BY ts DESC LIMIT 1", (token,)).fetchone()
    return r[0] if r else None


def mark_gone(conn, seen, polled, now, skip=()):
    gone = 0
    for comp in polled:
        if comp in skip:
            continue
        for (key,) in conn.execute("SELECT key FROM postings WHERE company=? "
                                   "AND is_live=1", (comp,)).fetchall():
            if key not in seen:
                conn.execute("UPDATE postings SET is_live=0 WHERE key=?", (key,))
                log(conn, key, "disappeared")
                gone += 1
    return gone


def retire_unlisted(conn, listed, now, apply=False):
    """Companies dropped from companies.yaml keep is_live=1 forever, because
    mark_gone only visits boards it polled. Retire them explicitly."""
    rows = conn.execute("SELECT company, COUNT(*) FROM postings "
                        "WHERE is_live=1 GROUP BY company").fetchall()
    stale = [(c, n) for c, n in rows if c not in listed]
    if not stale:
        return []
    if apply:
        for comp, _ in stale:
            for (key,) in conn.execute("SELECT key FROM postings WHERE "
                                       "company=? AND is_live=1",
                                       (comp,)).fetchall():
                conn.execute("UPDATE postings SET is_live=0 WHERE key=?", (key,))
                log(conn, key, "retired", "company removed from companies.yaml")
        conn.commit()
    return stale


# --- main ------------------------------------------------------------------

def retire_only(db_path, companies_path):
    """Retire companies removed from companies.yaml, without fetching anything.

    Retirement is a database update -- there is no reason to sit through a
    multi-hour collection run to apply it.
    """
    cfg = yaml.safe_load(open(companies_path, encoding="utf-8")) or {}
    listed = {t if isinstance(t, str) else (t.get("token") or t.get("tenant"))
              for toks in cfg.values() for t in (toks or [])}
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    now = datetime.now(timezone.utc).isoformat()
    stale = retire_unlisted(conn, listed, now, apply=True)
    total = sum(n for _, n in stale)
    if stale:
        print(f"retired {total} live postings across {len(stale)} company(ies): "
              f"{', '.join(c for c, _ in stale)}")
    else:
        print("nothing to retire -- every live company is in companies.yaml")
    conn.close()


def run(db_path, companies_path, verticals_path, retire=False, skip_fresh=0):
    cfg = yaml.safe_load(open(companies_path, encoding="utf-8"))
    raw = yaml.safe_load(open(verticals_path, encoding="utf-8")) or {}
    # Keys starting with "_" are settings, not verticals.
    excludes = [t.lower() for t in (raw.pop("_exclude_titles", None) or [])]
    verticals = {k: v for k, v in raw.items() if not k.startswith("_")}
    print(f"verticals: {', '.join(verticals)}")
    if excludes:
        print(f"excluded titles: {len(excludes)} terms\n")
    else:
        print()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    migrate(conn)
    now = datetime.now(timezone.utc).isoformat()
    seen, polled, counts, suspect, fresh = set(), [], {}, set(), []

    for source, tokens in cfg.items():
        fetch = FETCHERS.get(source)
        if not fetch:
            print(f"  ! unknown source '{source}', skipping", file=sys.stderr)
            continue
        for entry in tokens or []:
            token = entry if isinstance(entry, str) else entry.get("token")
            if skip_fresh and recently_fetched(conn, token, skip_fresh):
                fresh.append(token)
                polled.append(token)   # keep it out of the disappearance pass
                seen |= {k for (k,) in conn.execute(
                    "SELECT key FROM postings WHERE company=? AND is_live=1",
                    (token,))}
                continue
            try:
                jobs = fetch(entry)
            except Exception as e:
                conn.execute("INSERT INTO fetches VALUES (?,?,?,?,?,?)",
                             (now, source, token, -1, 0, str(e)[:200]))
                print(f"  ! {source}/{token}: {e}", file=sys.stderr)
                continue
            prev = last_good_count(conn, token)
            conn.execute("INSERT INTO fetches VALUES (?,?,?,?,?,?)",
                         (now, source, token, len(jobs), 1, ""))
            if prev and len(jobs) < prev * SHRINK_GUARD:
                suspect.add(token)
                shrink_note = (f"  [SHRANK {len(jobs)} vs {prev} last run -- "
                               f"disappearance pass skipped]")
            else:
                shrink_note = ""
            polled.append(token)
            changed, known, ratio = bulk_edit_ratio(conn, jobs)
            bulk = known >= BULK_MIN_POSTINGS and ratio >= BULK_EDIT_RATIO
            if bulk:
                log(conn, f"company:{source}:{token}", "template_change",
                    f"{changed}/{known} bodies changed ({ratio:.0%})")
            kept = dropped = 0
            for j in jobs:
                seen.add(f"{j['ats']}:{j['company']}:{j['external_id']}")
                r = upsert(conn, j, verticals, now, bulk=bulk,
                           excludes=excludes)
                counts[r] = counts.get(r, 0) + 1
                c = classify(j, verticals, excludes)
                if c["any_match"]:
                    kept += 1
                elif c["suppressed"]:
                    dropped += 1
            conn.commit()   # partial credit: an interrupted run keeps
                            # everything up to the last completed company
            dstats = DETAIL_STATS.pop(token, None)
            if dstats:
                conn.execute(
                    "INSERT INTO fetches VALUES (?,?,?,?,?,?)",
                    (now, source, token + ":detail", dstats["ok"],
                     1 if not dstats["failed"] else 0,
                     f"attempted={dstats['attempted']} failed={dstats['failed']} "
                     f"throttled={dstats['throttled']} "
                     f"errors={dstats['errors']} last={dstats['last_error']}"))
            note = f"  [template change: {changed}/{known} bodies]" if bulk else ""
            note += detail_summary(dstats) if dstats else ""
            if dropped:
                note += f"  [{dropped} would-have-matched, title-excluded]"
            print(f"  {source}/{token}: {kept} matched of {len(jobs)}"
                  f"{note}{shrink_note}")
            time.sleep(1)

    listed = {t if isinstance(t, str) else (t.get("token") or t.get("tenant"))
              for toks in cfg.values() for t in (toks or [])}
    stale = retire_unlisted(conn, listed, now, apply=retire)
    if stale:
        total = sum(n for _, n in stale)
        verb = "Retired" if retire else "UNLISTED (run with --retire to retire)"
        print(f"\n{verb}: {total} live postings across "
              f"{len(stale)} company(ies) no longer in companies.yaml: "
              f"{', '.join(c for c, _ in stale)}", file=sys.stderr)

    if fresh:
        print(f"\nskipped {len(fresh)} board(s) fetched within the last "
              f"{skip_fresh}h: {', '.join(fresh[:12])}"
              f"{'...' if len(fresh) > 12 else ''}", file=sys.stderr)

    gone = mark_gone(conn, seen, polled, now, skip=suspect)
    if suspect:
        print(f"\n!! {len(suspect)} board(s) returned fewer postings than last "
              f"run; disappearance pass skipped for: "
              f"{', '.join(sorted(suspect))}", file=sys.stderr)
    conn.commit()

    print("\nby vertical (live):")
    for vid, n in conn.execute(
            "SELECT m.vertical, COUNT(*) FROM matches m JOIN postings p "
            "ON p.key=m.key WHERE m.matched=1 AND p.is_live=1 "
            "GROUP BY m.vertical ORDER BY 2 DESC"):
        print(f"  {vid:<12} {n}")

    total = conn.execute("SELECT COUNT(*) FROM postings WHERE matched=1 "
                         "AND is_live=1").fetchone()[0]
    print(f"\n{now}  seen={len(seen)}  matched={total}  gone={gone}  {counts}")
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="jobs.db")
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--verticals", default="verticals.yaml")
    p.add_argument("--skip-fresh", type=int, default=0, metavar="HOURS",
                   help="skip boards already fetched successfully within this "
                        "many hours -- use to resume an interrupted run")
    p.add_argument("--retire-only", action="store_true",
                   help="retire companies removed from companies.yaml and "
                        "exit -- no fetching, takes seconds")
    p.add_argument("--retire", action="store_true",
                   help="mark postings dead for companies removed from "
                        "companies.yaml (otherwise only warns)")
    a = p.parse_args()
    if a.retire_only:
        retire_only(a.db, a.companies)
    else:
        run(a.db, a.companies, a.verticals, retire=a.retire,
            skip_fresh=a.skip_fresh)
