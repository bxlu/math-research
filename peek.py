#!/usr/bin/env python3
"""peek.py -- read jobs.db for the RESEARCH search.

Standard library only; no venv needed, though running it with the venv python
is fine too.

    python peek.py                 # the target set: internship AND research
    python peek.py --new 7         # only postings first seen in the last 7 days
    python peek.py --tier 1        # PhD-explicit Summer 2027 research only
    python peek.py --us            # drop postings with no US location
    python peek.py --company imc
    python peek.py --rejected      # scored but did not match -- tune terms here
    python peek.py --counts        # per-vertical and per-company totals

The default view is the intersection this search was built around: a posting
that matched `internship` AND at least one of math_research / quant_research /
ml_research. Matching either one alone is not interesting -- a full-time quant
role and a marketing internship both fail the same test.
"""

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone

RESEARCH = ("math_research", "quant_research", "ml_research")

US = re.compile(r"\b(united states|usa|new york|chicago|boston|austin|miami|"
                r"seattle|san francisco|houston|dallas|atlanta|philadelphia|"
                r"washington|los angeles|denver|remote - us|, [A-Z]{2}\b)", re.I)
NON_PHD = re.compile(r"\b(undergrad|bachelor|bs/ms|bsc|ug/ms|m1/m2)\b", re.I)
PHD = re.compile(r"\bph\.?d\b", re.I)
RESEARCH_TITLE = re.compile(r"research", re.I)
INTERNISH = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)


def tier(title, term_2027):
    """Same tiering as the shortlist CSV, recomputed from the title.

    The internship test is separate from the PhD test on purpose: "Graduate
    Quantitative Researcher, PhD (2027 Start)" is a graduating-class full-time
    role, not an internship, and belongs in tier 3 however good it looks.
    """
    nonphd = bool(NON_PHD.search(title))
    intern = bool(INTERNISH.search(title))
    research = bool(RESEARCH_TITLE.search(title))
    if intern and research and PHD.search(title) and not nonphd and term_2027:
        return 1
    if intern and research and not nonphd:
        return 2
    if research and not intern:
        return 3
    if nonphd:
        return 5
    return 4


def connect(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def targets(c, args):
    q = """
    select distinct p.key, p.company, p.title, p.location, p.url,
           p.posted_at, p.first_seen,
           (select group_concat(m2.vertical) from matches m2
             where m2.key = p.key and m2.matched = 1) as verticals
    from postings p
    join matches i on i.key = p.key and i.vertical = 'internship' and i.matched = 1
    join matches r on r.key = p.key and r.matched = 1
                  and r.vertical in ('math_research','quant_research','ml_research')
    where p.is_live = 1
    """
    rows = [dict(r) for r in c.execute(q)]
    if args.company:
        rows = [r for r in rows if args.company.lower() in r["company"].lower()]
    if args.new:
        cut = (datetime.now(timezone.utc) - timedelta(days=args.new)).isoformat()
        rows = [r for r in rows if (r["first_seen"] or "") >= cut]
    if args.us:
        rows = [r for r in rows if US.search(r["location"] or "")]
    for r in rows:
        r["tier"] = tier(r["title"], "2027" in r["title"])
    if args.tier:
        rows = [r for r in rows if r["tier"] == args.tier]
    rows.sort(key=lambda r: (r["tier"], r["company"], r["title"]))
    return rows


def show(rows):
    if not rows:
        print("nothing matched")
        return
    cur = None
    for r in rows:
        if r["tier"] != cur:
            cur = r["tier"]
            print(f"\n--- tier {cur} ---")
        vs = ",".join(sorted(v.split("_")[0] for v in (r["verticals"] or "").split(",")
                             if v != "internship"))
        print(f"  {r['company'][:24]:26} {(r['location'] or '')[:24]:26} "
              f"{r['title'].strip()[:52]:54} [{vs}]")
        print(f"      {r['url']}")
    print(f"\n{len(rows)} postings")


def rejected(c, limit):
    """Scored on a research vertical but did not match, highest score first.

    This is the tuning view: if real roles are sitting here, min_score is too
    high or the terms miss the vocabulary that board actually uses.
    """
    q = """
    select p.company, p.title, m.vertical, m.score
    from postings p join matches m on m.key = p.key
    where m.matched = 0 and m.score > 0 and p.is_live = 1
      and m.vertical in ('math_research','quant_research','ml_research')
    order by m.score desc limit ?
    """
    for r in c.execute(q, (limit,)):
        print(f"  {r['score']:3}  {r['vertical'][:14]:16} {r['company'][:22]:24} "
              f"{r['title'].strip()[:56]}")


def counts(c):
    print("-- postings --")
    print("  live:", c.execute("select count(*) from postings where is_live=1").fetchone()[0])
    print("\n-- matched per vertical --")
    for r in c.execute("select vertical, sum(matched) m, count(*) n "
                       "from matches group by 1 order by 2 desc"):
        print(f"  {r['vertical']:16} matched={r['m']:5}  scored={r['n']}")
    print("\n-- postings per company --")
    for r in c.execute("select company, count(*) n from postings where is_live=1 "
                       "group by 1 order by 2 desc"):
        print(f"  {r['company'][:34]:36} {r['n']}")
    # fetches columns are (ts, source, token, returned, ok, note) -- `returned`
    # is -1 when the fetcher raised, 0 when the board answered but was empty.
    # Those two cases are the whole reason to look at this table, so they are
    # called out rather than left as a quiet number in a list.
    print("\n-- last fetch per token --")
    rows = c.execute(
        "select source, token, returned, note from fetches f "
        "where token not like '%:detail' "
        "  and ts = (select max(ts) from fetches f2 where f2.token = f.token) "
        "order by returned desc").fetchall()
    for r in rows:
        n = r["returned"]
        flag = "   <-- ERROR" if n == -1 else ("   <-- ZERO" if n == 0 else "")
        print(f"  {r['source'][:11]:13} {r['token'][:32]:34} {n:6}{flag}")
        if n == -1 and r["note"]:
            print(f"        {r['note'][:150]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="jobs.db")
    p.add_argument("--new", type=int, metavar="DAYS",
                   help="only postings first seen in the last N days")
    p.add_argument("--tier", type=int, choices=[1, 2, 3, 4, 5],
                   help="1=PhD Summer 2027 research intern, 2=research intern "
                        "PhD-eligible, 3=full-time research, 4=other, 5=non-PhD")
    p.add_argument("--us", action="store_true", help="drop non-US locations")
    p.add_argument("--company")
    p.add_argument("--rejected", action="store_true")
    p.add_argument("--counts", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    a = p.parse_args()

    conn = connect(a.db)
    if a.counts:
        counts(conn)
    elif a.rejected:
        rejected(conn, a.limit)
    else:
        show(targets(conn, a))
