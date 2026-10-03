"""
Bhargav's free job-hunting bot.

Runs every hour on GitHub Actions (free). It reads official, public job feeds:
  - company careers pages hosted on Greenhouse, Lever, Ashby, Workable, SmartRecruiters
  - remote job boards with public APIs (Remotive, RemoteOK, Himalayas, Jobicy)
filters them (remote / India, fresher-to-mid level, no IT-services or staffing firms, no scams),
scores each job against Bhargav's resume, and saves the results to:
  - jobs.json   (machine-readable feed that the Claude apply routine reads)
  - JOBS.md     (easy-to-read list, opens nicely on GitHub)
  - status.md   (health report: which sources worked on the last run)

Only the Python standard library is used, so there is nothing to install.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
JOBS_FILE = ROOT / "jobs.json"
DISCOVERED_FILE = ROOT / "discovered_boards.json"
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "BhargavJobHunterBot/1.0 (personal job search; github actions)",
      "Accept": "application/json"}

status_lines = []


# ---------------------------------------------------------------- helpers
def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def to_dt(value):
    """Accepts ISO strings, epoch seconds or epoch milliseconds."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            v = float(value)
            if v > 1e12:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def phrase_re(word):
    w = word.strip().lower()
    # left word boundary always; right boundary only for short words (so "evaluat" matches "evaluator")
    right = r"(?![a-z])" if len(w) <= 4 else ""
    return re.compile(r"(?<![a-z])" + re.escape(w) + right)


def any_phrase(text, words):
    t = (text or "").lower()
    return any(phrase_re(w).search(t) for w in words)


def slug_variants(name):
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    joined = "".join(words)
    hyph = "-".join(words)
    out = [joined, hyph]
    for suffix in ("ai", "hq", "inc", "labs", "jobs", "careers", "technologies", "india"):
        if not joined.endswith(suffix):
            out.append(joined + suffix)
    if words and words[-1] in ("ai", "labs", "technologies", "games", "studios", "inc"):
        out.append("".join(words[:-1]))
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:5]


# ---------------------------------------------------------------- ATS fetchers
# Each returns a list of normalised jobs: dict(title, company, location, url, posted, description, salary, source)

def fetch_greenhouse(slug, company):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        out.append(dict(title=j.get("title", ""), company=j.get("company_name") or company,
                        location=(j.get("location") or {}).get("name", ""), url=j.get("absolute_url", ""),
                        posted=to_dt(j.get("first_published") or j.get("updated_at")),
                        description=strip_html(j.get("content", ""))[:6000], salary="",
                        source="Company careers (Greenhouse)"))
    return out


def fetch_lever(slug, company):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        if (j.get("workplaceType") or "").lower() == "remote" and "remote" not in loc.lower():
            loc = f"Remote, {loc}" if loc else "Remote"
        sal = j.get("salaryRange") or {}
        salary = f"{sal.get('currency','')} {sal.get('min','')}-{sal.get('max','')} {sal.get('interval','')}".strip() if sal else ""
        out.append(dict(title=j.get("text", ""), company=company, location=loc,
                        url=j.get("hostedUrl") or j.get("applyUrl", ""), posted=to_dt(j.get("createdAt")),
                        description=(j.get("descriptionPlain") or "")[:6000], salary=salary,
                        commitment=cat.get("commitment", ""), source="Company careers (Lever)"))
    return out


def fetch_ashby(slug, company):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        sec = [s.get("location", "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        if sec:
            loc = ", ".join([loc] + sec)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"Remote, {loc}"
        comp = j.get("compensation") or {}
        salary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or ""
        out.append(dict(title=j.get("title", ""), company=company, location=loc,
                        url=j.get("jobUrl") or j.get("applyUrl", ""), posted=to_dt(j.get("publishedAt")),
                        description=(j.get("descriptionPlain") or "")[:6000], salary=salary,
                        commitment=j.get("employmentType", ""), source="Company careers (Ashby)"))
    return out


def fetch_workable(slug, company):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city", ""), j.get("country", "")] if x)
        if j.get("telecommuting"):
            loc = f"Remote, {loc}" if loc else "Remote"
        out.append(dict(title=j.get("title", ""), company=data.get("name") or company, location=loc,
                        url=j.get("url") or j.get("application_url", ""),
                        posted=to_dt(j.get("published_on") or j.get("created_at")),
                        description="", salary="", commitment=j.get("employment_type", ""),
                        source="Company careers (Workable)"))
    return out


def fetch_smartrecruiters(slug, company):
    data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in data.get("content", []):
        l = j.get("location") or {}
        loc = ", ".join(x for x in [l.get("city", ""), l.get("country", "")] if x)
        if l.get("remote"):
            loc = f"Remote, {loc}" if loc else "Remote"
        out.append(dict(title=j.get("name", ""), company=(j.get("company") or {}).get("name") or company,
                        location=loc, url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}",
                        posted=to_dt(j.get("releasedDate")), description="", salary="",
                        commitment=(j.get("typeOfEmployment") or {}).get("label", ""),
                        source="Company careers (SmartRecruiters)"))
    return out


ATS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
       "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters}


def discover_board(company, known):
    """Find which ATS + slug a company uses. Cached in discovered_boards.json, re-checked weekly."""
    hit = known.get(company)
    if hit and hit.get("ats") and (NOW - to_dt(hit["checked"])).days < 7:
        return hit
    if hit and not hit.get("ats") and (NOW - to_dt(hit["checked"])).days < 14:
        return hit  # recently confirmed "not found"; don't hammer the APIs
    for slug in slug_variants(company):
        for ats, fn in ATS.items():
            try:
                jobs = fn(slug, company)
                if jobs or ats in ("greenhouse", "lever", "ashby"):
                    # greenhouse/lever/ashby return 404 for unknown boards, so success means the board exists
                    return {"ats": ats, "slug": slug, "checked": NOW.isoformat()}
            except Exception:
                continue
    return {"ats": None, "slug": None, "checked": NOW.isoformat()}


def fetch_company(company, known):
    info = discover_board(company, known)
    known[company] = info
    if not info.get("ats"):
        return company, None, []
    try:
        return company, info, ATS[info["ats"]](info["slug"], company)
    except Exception as e:
        return company, {**info, "error": str(e)[:80]}, []


# ---------------------------------------------------------------- remote job boards
def board_remotive():
    out = []
    for q in ["qa", "testing", "ai trainer", "llm", "support", "customer success", "game", "frontend", "full stack",
              "react", "sales engineer", "product analyst", "developer advocate", "technical writer", "implementation"]:
        try:
            data = get_json("https://remotive.com/api/remote-jobs?limit=100&search=" + urllib.parse.quote(q))
        except Exception:
            continue
        for j in data.get("jobs", []):
            out.append(dict(title=j.get("title", ""), company=j.get("company_name", ""),
                            location="Remote, " + (j.get("candidate_required_location") or ""),
                            url=j.get("url", ""), posted=to_dt(j.get("publication_date")),
                            description=strip_html(j.get("description", ""))[:6000],
                            salary=j.get("salary", ""), commitment=j.get("job_type", ""),
                            source="Remotive (find company link before applying)"))
        time.sleep(1)
    return out


def board_remoteok():
    data = get_json("https://remoteok.com/api")
    out = []
    for j in data[1:] if isinstance(data, list) else []:
        sal = ""
        if j.get("salary_min"):
            sal = f"USD {j.get('salary_min')}-{j.get('salary_max')} / year"
        out.append(dict(title=j.get("position", ""), company=j.get("company", ""),
                        location="Remote, " + (j.get("location") or "Worldwide"),
                        url=j.get("apply_url") or j.get("url", ""), posted=to_dt(j.get("epoch") or j.get("date")),
                        description=strip_html(j.get("description", ""))[:6000], salary=sal,
                        salary_min_usd=j.get("salary_min") or 0,
                        source="RemoteOK (find company link before applying)"))
    return out


def board_himalayas():
    out = []
    for offset in range(0, 1000, 20):
        try:
            data = get_json(f"https://himalayas.app/jobs/api?limit=20&offset={offset}")
        except Exception:
            break
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            locs = j.get("locationRestrictions") or []
            loc = "Remote, " + (", ".join(locs) if locs else "Worldwide")
            sal = ""
            if j.get("minSalary"):
                sal = f"{j.get('currency') or 'USD'} {j.get('minSalary')}-{j.get('maxSalary')} / year"
            out.append(dict(title=j.get("title", ""), company=j.get("companyName", ""), location=loc,
                            url=j.get("applicationLink") or j.get("guid", ""), posted=to_dt(j.get("pubDate")),
                            description=strip_html(j.get("description") or j.get("excerpt", ""))[:6000],
                            salary=sal, salary_min_usd=j.get("minSalary") if (j.get("currency") or "USD") == "USD" else 0,
                            commitment=j.get("employmentType", ""), source="Himalayas"))
        time.sleep(0.5)
    return out


def board_jobicy():
    data = get_json("https://jobicy.com/api/v2/remote-jobs?count=100")
    out = []
    for j in data.get("jobs", []):
        sal = ""
        if j.get("annualSalaryMin"):
            sal = f"{j.get('salaryCurrency','USD')} {j.get('annualSalaryMin')}-{j.get('annualSalaryMax')} / year"
        out.append(dict(title=html.unescape(j.get("jobTitle", "")), company=j.get("companyName", ""),
                        location="Remote, " + (j.get("jobGeo") or "Anywhere"), url=j.get("url", ""),
                        posted=to_dt(j.get("pubDate")),
                        description=strip_html(j.get("jobDescription") or j.get("jobExcerpt", ""))[:6000],
                        salary=sal, commitment=", ".join(j.get("jobType") or []) if isinstance(j.get("jobType"), list) else (j.get("jobType") or ""),
                        source="Jobicy (find company link before applying)"))
    return out


def board_workingnomads():
    data = get_json("https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for j in data if isinstance(data, list) else []:
        out.append(dict(title=j.get("title", ""), company=j.get("company_name", ""),
                        location="Remote, " + (j.get("location") or "Anywhere"), url=j.get("url", ""),
                        posted=to_dt(j.get("pub_date")), description=strip_html(j.get("description", ""))[:6000],
                        salary="", source="Working Nomads (find company link before applying)"))
    return out


def board_weworkremotely():
    import xml.etree.ElementTree as ET
    out = []
    for feed in ["remote-customer-support-jobs", "remote-full-stack-programming-jobs", "remote-front-end-programming-jobs",
                 "remote-product-jobs", "remote-sales-and-marketing-jobs", "all-other-remote-jobs"]:
        try:
            req = urllib.request.Request(f"https://weworkremotely.com/categories/{feed}.rss", headers=UA)
            root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
        except Exception:
            continue
        for it in root.iter("item"):
            raw_title = it.findtext("title") or ""
            company, _, title = raw_title.partition(":")
            out.append(dict(title=title.strip() or raw_title, company=company.strip(),
                            location="Remote, " + (it.findtext("region") or "Anywhere"), url=it.findtext("link") or "",
                            posted=to_dt_rss(it.findtext("pubDate")),
                            description=strip_html(it.findtext("description") or "")[:6000], salary="",
                            source="We Work Remotely (find company link before applying)"))
    return out


def to_dt_rss(s):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None


BOARDS = {"remotive": board_remotive, "remoteok": board_remoteok, "himalayas": board_himalayas, "jobicy": board_jobicy,
          "workingnomads": board_workingnomads, "weworkremotely": board_weworkremotely}


# ---------------------------------------------------------------- filtering + scoring
GAMING_COMPANIES = {c.lower() for c in CONFIG.get("gaming_companies", [])}


def is_gaming(job):
    """A gaming job: at a game company, a game-related title, or a description clearly about games."""
    comp = (job.get("company") or "").lower()
    if comp in GAMING_COMPANIES or any(g in comp for g in ("games", "gaming", "studios", "esports")):
        return True
    if any_phrase(job.get("title", ""), ["game", "games", "gaming", "player", "esports", "playtest", "lqa"]):
        return True
    d = (job.get("description") or "").lower()
    return len(re.findall(r"(?<![a-z])(?:game|games|gaming|gamers?|players?)(?![a-z])", d)) >= 6


def classify(title):
    best = None
    for name, cat in CONFIG["categories"].items():
        if any_phrase(title, cat["keywords"]):
            if best is None or cat["weight"] > best[1]:
                best = (name, cat["weight"])
    return best


INR_RE = re.compile(r"(?:₹|inr|rs\.?)\s*([\d,\.]+)\s*(lpa|lakh|lac|l|k)?", re.I)


def salary_too_low(job):
    """True only when the listing clearly pays below the floor. Unknown salary is kept."""
    floor = CONFIG["min_salary_inr_per_year"]
    usd = job.get("salary_min_usd") or 0
    try:
        usd = float(usd)
    except Exception:
        usd = 0
    if usd and usd * 83 < floor:  # rough USD->INR
        return True
    m = INR_RE.search(job.get("salary") or "")
    if m:
        try:
            n = float(m.group(1).replace(",", ""))
            unit = (m.group(2) or "").lower()
            if unit in ("lpa", "lakh", "lac", "l"):
                n *= 100000
            elif unit == "k":
                n *= 1000
            if 1000 < n < floor:
                return True
        except Exception:
            pass
    return False


REMOTE_WORDS = ["remote", "anywhere", "worldwide", "work from home", "wfh", "distributed"]


INDIA_PLACES = ["india", "bengaluru", "bangalore", "hyderabad", "secunderabad", "chennai", "pune", "mumbai", "navi mumbai",
                "gurugram", "gurgaon", "noida", "delhi", "new delhi", "ncr", "kolkata", "visakhapatnam", "vizag",
                "vijayawada", "ahmedabad", "kochi", "cochin", "coimbatore", "jaipur", "chandigarh", "indore",
                "thiruvananthapuram", "trivandrum", "mysuru", "mysore", "bhubaneswar", "nagpur", "lucknow", "goa",
                "mangalore", "mangaluru", "surat", "vadodara", "guntur", "srikakulam"]
FOREIGN_HINTS = ["us", "usa", "u.s", "uk", "eu", "europe", "emea", "americas", "canada", "singapore", "dubai", "uae",
                 "germany", "london", "new york", "san francisco", "australia", "japan", "philippines", "indonesia",
                 "malaysia", "vietnam", "brazil", "mexico", "poland", "estonia", "ireland", "netherlands", "france"]


def work_mode(loc, description=""):
    """Returns 'Remote', 'Hybrid' or 'On-site' when the job suits Bhargav, else None.
    Remote: must be open to India (India / APAC / Asia / worldwide / plain 'Remote').
    Hybrid / On-site: only in India."""
    l = (loc or "").lower()
    if not l.strip():
        return None
    in_india = any_phrase(l, INDIA_PLACES)
    hybrid = any_phrase(l, ["hybrid"]) or (in_india and any_phrase((description or "")[:4000], ["hybrid"]))
    onsite_word = any_phrase(l, ["on-site", "onsite", "in office", "in-office", "office"])
    is_remote = any_phrase(l, REMOTE_WORDS) and not hybrid and not onsite_word
    if not is_remote and in_india and not hybrid:
        # listed under an Indian city, but the description may say the role is remote
        if any_phrase((description or "")[:4000], ["fully remote", "remote-first", "remote first",
                                                   "work from home", "work from anywhere", "100% remote"]):
            is_remote = True
    if is_remote:
        if any_phrase(l, ["india", "apac", "asia", "asia pacific", "anywhere", "worldwide", "global"]) or in_india:
            return "Remote"
        rest = l
        for w in REMOTE_WORDS + ["fully", "100%", "first", "only", "position", "role", "job"]:
            rest = re.sub(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", " ", rest)
        return "Remote" if re.sub(r"[^a-z]+", "", rest) == "" else None
    if in_india and not (any_phrase(l, FOREIGN_HINTS) and "india" not in l):
        return "Hybrid" if hybrid else "On-site"
    return None


SENIOR_WORDS = ["senior", "sr", "sr.", "staff", "principal", "lead", "director", "head of", "vp", "vice president", "chief"]


def title_ok(title):
    t = title.lower()
    if any(k in t for k in CONFIG["keep_even_if_excluded"]) and not any_phrase(t, SENIOR_WORDS):
        return True
    return not any_phrase(t, CONFIG["exclude_title_words"])


def score(job, cat_weight):
    text = (job["title"] + " " + job.get("description", "")).lower()
    hits = [k for k in CONFIG["resume_keywords"] if phrase_re(k).search(text)]
    s = cat_weight + min(len(hits) * 3, 30)
    loc = job["location"].lower()
    mode = job.get("mode", "Remote")
    if mode == "Remote":
        s += 10 if any_phrase(loc, INDIA_PLACES) else 7 if any_phrase(loc, ["anywhere", "worldwide", "global", "apac"]) else 4
    else:
        # office roles: closer to Srikakulam is better
        s += 8 if any_phrase(loc, ["visakhapatnam", "vizag", "vijayawada", "srikakulam", "guntur"]) \
            else 5 if any_phrase(loc, ["hyderabad", "secunderabad", "bhubaneswar"]) \
            else 3 if any_phrase(loc, ["bengaluru", "bangalore", "chennai"]) else 0
    if job.get("posted"):
        age = (NOW - job["posted"]).days
        s += 10 if age <= 3 else 6 if age <= 7 else 2
    if "company careers" in job["source"].lower():
        s += 5  # official company page preferred
    return min(s, 100), hits[:8]


YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years|yrs)", re.I)
PRODUCT_COMPANIES = {c.lower() for c in CONFIG["companies"]}


def is_coding_role(title):
    t = title.lower()
    if any(k in t for k in CONFIG.get("keep_even_if_coding", [])):
        return False
    return any_phrase(t, CONFIG.get("coding_title_words", []))


GENERIC_RELIGION_WORDS = {"religion", "religious", "spiritual", "faith"}  # appear in equal-opportunity statements


def is_religious(job):
    """Skip religion-related companies/products. Company + title use every word; the description only uses
    specific words, because almost every job post says 'without regard to ... religion' in its EEO statement."""
    words = CONFIG.get("religious_words", [])
    if any_phrase(" ".join([job.get("company") or "", job.get("title") or ""]), words):
        return True
    strong = [w for w in words if w not in GENERIC_RELIGION_WORDS]
    return any_phrase((job.get("description") or "")[:3000], strong)


def years_required(description):
    """Smallest 'N years' mentioned near experience wording; None if not stated."""
    d = (description or "").lower()
    mins = []
    for m in YEARS_RE.finditer(d):
        window = d[max(0, m.start() - 60): m.end() + 60]
        if "experience" in window or "exp" in window:
            n = int(m.group(1))
            if 0 < n < 20:
                mins.append(n)
    return min(mins) if mins else None


def evaluate(job):
    title = job.get("title") or ""
    if not title or not job.get("url"):
        return None
    company_l = (job.get("company") or "").lower()
    if any(b in company_l for b in CONFIG["blocked_companies"]):
        return None
    if not title_ok(title) or is_religious(job):
        return None
    mode = work_mode(job.get("location"), job.get("description", ""))
    if not mode:
        return None
    # region locks hidden in the title, e.g. "Customer Success Associate (Denver, CO)" or "US Remote Support Advisor"
    if (any_phrase(title, FOREIGN_HINTS + ["denver", "austin", "seattle", "chicago", "boston", "toronto", "berlin", "paris"])
            and not any_phrase(title, INDIA_PLACES)) or re.search(r"\(([^)]*,\s*)?[A-Z]{2}\)", title):
        return None
    if any_phrase(title, CONFIG.get("services_words", [])):
        return None
    job["mode"] = mode
    # Remote: coding and non-coding roles are both fine. Hybrid / on-site: non-coding roles only.
    if mode != "Remote" and is_coding_role(title):
        return None
    if any_phrase(job.get("description", ""), CONFIG.get("services_words", [])):
        return None
    yrs = years_required(job.get("description"))
    if yrs is not None and yrs > CONFIG.get("max_years_experience_required", 3):
        return None
    c = (job.get("commitment") or "").lower()
    if any(w in c for w in ("part", "intern", "temporary")):
        return None
    if any(w in (job.get("description") or "").lower() for w in CONFIG["scam_words"]):
        return None
    if salary_too_low(job):
        return None
    if any_phrase(title + " " + (job.get("description") or "")[:3000], CONFIG.get("ghost_words", [])):
        return None  # talent-pool / evergreen posts are not real openings
    if not job.get("posted") and "company careers" not in job["source"].lower():
        return None  # board listings with no date can't be checked for freshness
    if job.get("posted") and (NOW - job["posted"]).days > CONFIG["max_age_days"]:
        return None
    cat = classify(title)
    gaming = is_gaming(job)
    if gaming:
        # gaming roles must not need coding (QA tester, player support, community, live ops, producer, design, localization...)
        t = title.lower()
        if any_phrase(t, CONFIG.get("gaming_coding_words", [])) and not any(k in t for k in CONFIG.get("keep_gaming_even_if_engineer", [])):
            return None
        if cat is None or cat[0].startswith("AI-assisted developer") or cat[0] == "Trainee / graduate program" or \
                any_phrase(t, CONFIG["categories"]["Gaming"]["keywords"]):
            cat = ("Gaming", CONFIG["categories"]["Gaming"]["weight"])
        else:
            cat = (f"Gaming · {cat[0]}", max(cat[1], CONFIG["categories"]["Gaming"]["weight"]))
    if not cat:
        return None
    if mode != "Remote" and cat[0].startswith("AI-assisted developer"):
        return None
    sc, hits = score(job, cat[1])
    if gaming:
        sc += CONFIG.get("gaming_bonus", 0)
    if company_l in PRODUCT_COMPANIES:
        sc += CONFIG.get("product_company_bonus", 0)
    if any_phrase(title, ["junior", "associate", "entry level", "entry-level", "trainee", "graduate", "fresher", "new grad"]) \
            or (yrs is not None and yrs <= 1):
        sc += CONFIG.get("easy_level_bonus", 0)
    if cat[0].startswith("AI-assisted developer") and any_phrase(job.get("description", ""), CONFIG.get("ai_tools_words", [])):
        sc += CONFIG.get("ai_tools_bonus", 0)  # dev roles that use AI tools suit him best
    sc = min(sc, 100)
    if sc < CONFIG["min_score_to_list"]:
        return None
    return dict(
        id=re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{title}".lower()).strip("-")[:90],
        title=title.strip(), company=(job.get("company") or "").strip(), category=cat[0], score=sc,
        location=job["location"].strip(", "), mode=mode, salary=job.get("salary") or "Not listed",
        url=job["url"], source=job["source"],
        posted=job["posted"].date().isoformat() if job.get("posted") else "",
        matched_skills=hits, contract=("contract" in c or "contract" in title.lower()),
    )


# ---------------------------------------------------------------- ghost-job checks
def link_is_live(url):
    """Opens the job link. Dead links and 'no longer accepting applications' pages mean the job is closed."""
    try:
        req = urllib.request.Request(url, headers={**UA, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 400:
                return False
            page = r.read(300000).decode("utf-8", errors="ignore").lower()
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 429)  # blocked bots, not closed: keep, the daily check will open it properly
    except Exception:
        return True  # network hiccup: don't drop the job for that
    text = re.sub(r"<[^>]+>", " ", page)[:200000]
    return not any(w in text for w in CONFIG.get("closed_page_words", []) if w != "404")


# ---------------------------------------------------------------- telegram alerts (optional)
def send_telegram(new_jobs):
    """Sends new matches to Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set as GitHub secrets."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat or not new_jobs:
        return "skipped" if not token or not chat else "no new jobs"
    min_score = CONFIG.get("telegram_min_score", 60)
    picks = [j for j in new_jobs if j["score"] >= min_score][:10]
    if not picks:
        return "no new jobs above telegram_min_score"
    esc = lambda s: html.escape(str(s or ""))
    lines = [f"<b>🔎 {len(picks)} new job match{'es' if len(picks) > 1 else ''}</b>"]
    for j in picks:
        lines.append(f"\n<b>{j['score']}</b> · <a href=\"{esc(j['url'])}\">{esc(j['title'])}</a>\n"
                     f"{esc(j['company'])} · {esc(j.get('mode', 'Remote'))} · {esc(j['location'][:40])}\n💰 {esc(j['salary'][:40])}")
    lines.append("\nThe daily Claude task applies to the best ones every morning around 11 AM IST.")
    body = urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body),
                               timeout=20).read()
        return f"sent {len(picks)}"
    except Exception as e:
        return f"error: {str(e)[:80]}"


# ---------------------------------------------------------------- main
def main():
    known = json.loads(DISCOVERED_FILE.read_text()) if DISCOVERED_FILE.exists() else {}
    raw = []

    with ThreadPoolExecutor(max_workers=16) as pool:
        for company, info, jobs in pool.map(lambda c: fetch_company(c, known), CONFIG["companies"]):
            raw.extend(jobs)
            if info and info.get("ats"):
                status_lines.append(f"| {company} | {info['ats']} | {len(jobs)} | {info.get('error','ok')} |")
            else:
                status_lines.append(f"| {company} | not found | 0 | add careers link manually if needed |")

    board_counts = {}
    for name in CONFIG["remote_boards"]:
        try:
            jobs = BOARDS[name]()
            raw.extend(jobs)
            board_counts[name] = len(jobs)
        except Exception as e:
            board_counts[name] = f"error: {str(e)[:60]}"

    found = {}
    for job in raw:
        ev = evaluate(job)
        if ev and (ev["id"] not in found or ev["score"] > found[ev["id"]]["score"]):
            found[ev["id"]] = ev

    old = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else {"jobs": []}
    old_by_id = {j["id"]: j for j in old.get("jobs", [])}
    jobs, ghosts = [], 0
    to_check = []
    for jid, j in found.items():
        j["first_seen"] = old_by_id.get(jid, {}).get("first_seen") or NOW.isoformat(timespec="minutes")
        j["new"] = jid not in old_by_id
        listed_days = (NOW - to_dt(j["first_seen"])).days
        if listed_days > CONFIG.get("ghost_max_days_listed", 45):
            ghosts += 1  # open for too long (or reposted again and again): likely a ghost job
            continue
        if "company careers" in j["source"].lower():
            j["verified"] = "Live on company careers page"
            jobs.append(j)
        else:
            to_check.append(j)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for j, live in zip(to_check, pool.map(lambda x: link_is_live(x["url"]), to_check)):
            if live:
                j["verified"] = "Link checked, still open"
                jobs.append(j)
            else:
                ghosts += 1
    jobs.sort(key=lambda j: (-j["score"], j["first_seen"]))

    JOBS_FILE.write_text(json.dumps({"updated": NOW.isoformat(timespec="minutes"), "count": len(jobs),
                                     "new_this_run": sum(j["new"] for j in jobs), "jobs": jobs},
                                    indent=1, ensure_ascii=False), encoding="utf-8")
    DISCOVERED_FILE.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")

    md = [f"# Job matches for Bhargav\n", f"Updated {NOW.strftime('%d %b %Y, %H:%M UTC')} · {len(jobs)} open matches\n",
          "| Score | Role | Company | Mode | Location | Salary | Posted |", "|---|---|---|---|---|---|---|"]
    for j in jobs[:150]:
        flag = " 🆕" if j["new"] else ""
        md.append(f"| {j['score']} | [{j['title']}]({j['url']}){flag} | {j['company']} | {j.get('mode','Remote')} | {j['location'][:40]} | "
                  f"{j['salary'][:30]} | {j['posted']} |")
    (ROOT / "JOBS.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    st = [f"# Bot health\n", f"Last run {NOW.isoformat(timespec='minutes')} · raw jobs read: {len(raw)} · matches: {len(jobs)} · closed or ghost jobs removed: {ghosts}\n",
          "## Remote boards", *[f"- {k}: {v}" for k, v in board_counts.items()],
          "\n## Company careers feeds", "| Company | System | Jobs read | Note |", "|---|---|---|---|", *sorted(status_lines)]
    (ROOT / "status.md").write_text("\n".join(st) + "\n", encoding="utf-8")
    tg = send_telegram([j for j in jobs if j["new"]])
    with open(ROOT / "status.md", "a", encoding="utf-8") as f:
        f.write(f"\nTelegram: {tg}\n")
    print(f"telegram={tg} raw={len(raw)} matches={len(jobs)} new={sum(j['new'] for j in jobs)} boards={board_counts}")


if __name__ == "__main__":
    sys.exit(main())
