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

## Running it

```
python collect.py                      # defaults: jobs.db, companies.yaml, verticals.yaml
python peek.py --tier 1 --us           # the shortlist
python peek.py --counts                # per-board totals, and which boards returned nothing
python peek.py --rejected              # scored but did not match — tune terms here
```

`peek.py` is standard library only. `collect.py` needs `requests` and `pyyaml`.

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

**Phenom's `keywords` re-ranks, it does not filter.** The board still returns
everything, just in relevance order. Setting it and paging to a limit means each
run reads a different slice of an unchanged board: two runs pulled 210 then 263
postings and retired 33 rows that had never disappeared. Leave it unset.

**An empty board is not always a broken config.** MITRE's Workday tenant answers
`200` with `{"total":0,"jobPostings":[]}` and every other site path on it 404s —
the tenant is simply abandoned. Check `peek.py --counts`, which flags `0` and
`-1` separately: empty answer versus fetcher raised.

## A warning about keys

`USAJOBS_KEY` and `USAJOBS_EMAIL` are constants at the top of `collect.py`, and
**this repository is public**. If you paste a real key there, it goes out with
your next commit. Keep it out of any commit you push.
