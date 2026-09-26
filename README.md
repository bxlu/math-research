# math-research

A daily job collector aimed at one narrow question: **which math-PhD research
internships and quantitative research roles are open right now?**

It snapshots a set of job boards into SQLite, classifies every posting against
term lists held in config, and keeps history so that postings appearing and
disappearing is itself a signal. It does not apply to anything, rank employers,
or score candidates.

## Design

Three layers, and only the middle one changes often:

| Layer | File | Role |
|---|---|---|
| Collectors | `collect.py` | Fetch boards, write postings, build history |
| Verticals | `verticals.yaml` | What counts as a match — config, not code |
| Employers | `companies.yaml` | Which boards to read |

Two axes of extension, neither of which touches the matching logic: a new job
area is a block in `verticals.yaml`; a new source is a fetcher returning the
standard dict, registered in `FETCHERS`.

## The intersection

`internship` is a vertical of its own rather than a keyword inside the research
blocks, because "Internship" in a title would otherwise pull *Marketing
Internship* into `quant_research`. A target posting matches **two** verticals:

```sql
SELECT p.title, p.company, p.url FROM postings p
JOIN matches i ON i.key = p.key AND i.vertical = 'internship'     AND i.matched = 1
JOIN matches r ON r.key = p.key AND r.vertical = 'quant_research' AND r.matched = 1
WHERE p.is_live = 1;
```

`peek.py` wraps that and a few other views.

## Sources

| Fetcher | Notes |
|---|---|
| `greenhouse`, `lever`, `ashby` | Public JSON APIs, one call per board |
| `smartrecruiters`, `recruitee` | Public APIs, second call per posting for the body |
| `workday` | `cxs` API; POST, paged, second call per posting |
| `mathjobs` | AMS public JSON feed — per-employer or whole-board, carries real application deadlines |
| `phenom` | Phenom People career sites; results are server-rendered into the page, so the JSON is lifted out of the HTML |
| `usajobs` | Federal listings; needs a free API key |

## Getting started

Developed and run on Python 3.14. Nothing in the code uses version-specific
syntax, so 3.9 or newer should be fine.

**1. Get the code**

```
git clone https://github.com/bxlu/math-research.git
cd math-research
```

**2. Create a virtual environment**

Windows:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux:

```
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Two dependencies, `requests` and `PyYAML`. `peek.py` is standard library only,
so you can read results with a bare `python peek.py` even outside the venv.

**3. First run**

```
.venv\Scripts\python.exe collect.py          # Windows
.venv/bin/python collect.py                  # macOS / Linux
```

This creates `jobs.db` in the same folder and prints a line per board, e.g.
`greenhouse/jumptrading: 58 matched of 110`. Expect roughly 2,000-2,500
postings across the configured boards. The first run takes a while — the
Workday and Phenom boards need one extra request per posting to get the body —
and later runs are no faster, since the whole corpus is re-read each time to
detect edits and disappearances.

Two lines in the output are normal on a fresh checkout:

```
! usajobs/usajobs-math: USAJOBS_KEY/USAJOBS_EMAIL not set
```

That source is skipped until you paste a free key into the constants at the top
of `collect.py`. Everything else runs regardless.

**4. Read the results**

```
python peek.py --tier 1 --us     # PhD-track research internships, US locations
python peek.py --counts          # per-board totals; flags boards that returned nothing
python peek.py --rejected        # scored but did not match — tune terms here
python peek.py --new 7           # only postings first seen in the last week
```

**5. Make it daily (optional)**

History is the point — a posting that appears and vanishes is a signal you only
see across runs. Schedule it once a day.

Windows, using Task Scheduler:

```
schtasks /create /tn "Research job collector" /sc daily /st 19:00 ^
  /tr "cmd /c cd /d C:\path\to\math-research && .venv\Scripts\python.exe collect.py"
```

macOS / Linux, in `crontab -e`:

```
0 19 * * *  cd /path/to/math-research && .venv/bin/python collect.py
```

## Making it yours

The two config files are the whole interface; `collect.py` should not need
editing to retarget this.

- **Different employers** — edit `companies.yaml`. Each block is one fetcher.
  A greenhouse entry is just the token from a board URL
  (`job-boards.greenhouse.io/<token>`); verify it by opening
  `https://boards-api.greenhouse.io/v1/boards/<token>/jobs` in a browser before
  adding it, since a wrong token returns 404 and a run logs it as an error.
- **Different field** — edit `verticals.yaml`. Each block is a label, a
  `min_score`, and a term list. A posting matches a vertical if its title
  contains a term, or its body contains terms `min_score` times. Delete the
  blocks you do not want and write your own; `collect.py` reads whatever is
  there.
- **Check your work** — `peek.py --rejected` shows postings that scored but
  fell short. If real roles are sitting in that list, your `min_score` is too
  high or your terms miss the vocabulary that board actually uses.

## Tuning notes

Kept because they were expensive to learn and are not obvious from the code.

**Body scoring drowns in employer boilerplate.** At a quant firm every posting's
body talks about trading and research, and some firms use one shared blurb
across the whole board — which pulled hardware and software roles into the
research verticals. `min_score` for `math_research` and `quant_research` is set
high enough to reject boilerplate. Explicit titles are unaffected: a title hit
short-circuits the score.

**Subfield vocabulary misses research institutes.** A defense/FFRDC posting names
the field and the working style, not subfields. The IDA/CCR Princeton listing
scored 4 on subfield terms alone — under threshold, silently dropped. Terms like
`research staff`, `signals analysis` and `applied mathematical` lift it to 11
while adding almost nothing to quant postings.

**Page a re-ranking board by its reported total, not by exhaustion.** Phenom
re-ranks between requests, so a page part-way through a walk can come back
entirely made of postings already seen. Reading that as the end of the list ends
the walk at a different point every night: six consecutive runs collected
261-284 of MITRE's 326 postings and retired the remainder as "gone", which is
the one signal a daily collector exists to produce. Every page reports
`totalHits` — drive the loop off that, tolerate a few barren pages, and
re-request the offsets that produced them, since advancing past a re-ranked page
skips whatever it should have shown.

Phenom's `keywords` is part of the same trap: it re-ranks rather than filtering,
so it does not reduce the work and it makes the order less stable. Leave it
unset.

**An empty board is not always a broken config.** MITRE's Workday tenant answers
`200` with `{"total":0,"jobPostings":[]}` and every other site path on it 404s —
the tenant is simply abandoned. Check `peek.py --counts`, which flags `0` and
`-1` separately: empty answer versus fetcher raised.

## A warning about keys

`USAJOBS_KEY` and `USAJOBS_EMAIL` are constants at the top of `collect.py`, and
**this repository is public**. If you paste a real key there, it goes out with
your next commit. Keep it out of any commit you push.
