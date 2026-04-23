"""
sitemap_scout.py

Discovers a website's sitemap and categorizes pages by type (home, service,
location, bio, blog, faq, contact, legal, other). Powers the site map
configurator that feeds into onboarding for CORE clients and standalone
website builds.

Strategy:
  1. Try /sitemap.xml -> /sitemap_index.xml -> robots.txt for sitemap discovery
  2. Recursively walk sitemap indexes (capped) to collect all <loc> URLs
  3. Bounded crawl fallback if no sitemap (max 200 URLs, depth 3)
  4. Categorize each URL via path-segment regex first (cheap, deterministic)
  5. Parent-inheritance second pass: reclassify unknown URLs that live under
     a known index path (e.g. /selfenergyblog/<slug> -> blog_post if
     /selfenergyblog was detected as blog_index)
  6. Batch-classify still-ambiguous URLs in one Anthropic API call (LLM cleanup)
  7. Collapse any category > COLLAPSE_THRESHOLD into a sample + full-list marker

Tier 1 (no browser, no lock). Pure HTTP + at most one Anthropic API call.
Duration: 5-30 seconds typical. Cost: ~$0.01 per scout (LLM only on ambiguous).
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

import httpx

logger = logging.getLogger("moonraker.sitemap_scout")

# ── Tunables ────────────────────────────────────────────────────────────────
MAX_TOTAL_URLS = 2000          # hard ceiling on URLs we'll consider
MAX_CRAWL_PAGES = 200          # bounded fallback crawl
MAX_CRAWL_DEPTH = 3
MAX_SUBSITEMAPS = 25           # don't recurse forever into a sitemap index
COLLAPSE_THRESHOLD = 8         # categories larger than this get collapsed
HTTP_TIMEOUT = 12              # per-request timeout
# Realistic UA — Cloudflare + sitecore WAFs block the obvious bot string.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Categorization regexes ──────────────────────────────────────────────────
# Three-pass design:
#   Pass 1: Match each URL against CATEGORY_PATTERNS (single-segment paths and
#           well-known multi-segment patterns). Track which paths are blog/bio
#           index pages so pass 2 can identify their children.
#   Pass 2: For URLs left as 'unknown', check if they live under a known index
#           parent (e.g. /selfenergyblog/<slug> if /selfenergyblog was matched
#           as blog_index). Reclassify as <parent>_post.
#   Pass 3: Anything still unknown -> LLM cleanup batch.
#
# Patterns are matched against the URL path (lowercased, slashes preserved).
# Order matters: first match wins. More specific patterns come first.
CATEGORY_PATTERNS = [
    # Legal first — narrow matches that could otherwise be miscategorized
    ("legal_privacy", re.compile(r"/privacy(-policy)?/?$|/privacy-notice/?$")),
    ("legal_terms",   re.compile(r"/terms(-of-(service|use))?/?$|/tos/?$|/disclaimer/?$")),
    ("legal_other",   re.compile(r"/cookie-policy/?$|/accessibility/?$|/gdpr/?$")),
    # Contact / book
    ("contact",       re.compile(r"/contact(-us)?/?$|/get-in-touch/?$|/book(-(a-)?(call|appointment|consult|consultation))?/?$|/schedule/?$")),
    # FAQ
    ("faq",           re.compile(r"/faqs?(-\d+)?(-\d+)?/?$|/frequently-asked-questions/?$|/questions/?$")),
    # Bio / team — patterns vary widely. /team/, /staff/, /our-people/, /meet-<n>/
    ("bio",           re.compile(
        r"/(meet-|about-)?(our-)?(team|staff|therapists|clinicians|providers|psychologists|counselors|counsellors)(/[^/]+)?/?$"
        r"|/our-people/?$"
        r"|/meet-(?!our-)[a-z]+(-[a-z]+)*/?$"
        r"|/team/[^/]+/?$"
        r"|/staff/[^/]+/?$"
        r"|/providers/[^/]+/?$"
        r"|/about/[^/]+/?$"
    )),
    # Locations
    ("location",      re.compile(
        r"/locations?/?$"
        r"|/locations?/[^/]+/?$"
        r"|/[a-z]+-(nm|ny|ca|tx|fl|wa|or|az|co|ma|il|pa|oh|ga|nc|sc|va|mi|mn|wi|in|tn|mo|md|nj|ct|ut|ia|ks|ne|nv|nh|me|ri|de|mt|sd|nd|wy|ak|hi|wv|ar|ms|al|ky|la|id|ok)/?$"
        r"|/(albuquerque|atlanta|austin|baltimore|boston|charlotte|chicago|cleveland|columbus|dallas|denver|detroit|houston|indianapolis|jacksonville|kansas-city|las-vegas|los-angeles|memphis|miami|milwaukee|minneapolis|nashville|new-york|nyc|oklahoma-city|omaha|orlando|philadelphia|phoenix|pittsburgh|portland|raleigh|sacramento|salt-lake-city|san-antonio|san-diego|san-francisco|san-jose|seattle|tampa|toronto|tucson|tulsa|vancouver|virginia-beach|washington-dc|washington)(-[a-z]+)*/?$"
    )),
    # Blog index — catches standard names + custom "*blog" names (e.g. /selfenergyblog)
    # plus SQSP-style duplicates (/blog-1, /blog-2)
    ("blog_index",    re.compile(
        r"^/blog(-\d+)?/?$"
        r"|^/[a-z0-9][a-z0-9-]*?blog(-\d+)?/?$"
        r"|/blogs/?$"
        r"|/posts?/?$"
        r"|/articles?/?$"
        r"|/news/?$"
        r"|/insights/?$"
        r"|/journal/?$"
        r"|/in-the-media/?$"
        r"|/press/?$"
        r"|/media/?$"
    )),
    # Blog post — standard parent segments. Custom parents handled by pass 2.
    ("blog_post",     re.compile(
        r"/blogs?/[^/]+/?$"
        r"|/posts?/[^/]+/?$"
        r"|/articles?/[^/]+/?$"
        r"|/news/[^/]+/?$"
        r"|/insights/[^/]+/?$"
        r"|/journal/[^/]+/?$"
        r"|/in-the-media/[^/]+/?$"
        r"|/press/[^/]+/?$"
        r"|/media/[^/]+/?$"
        r"|/\d{4}/\d{2}/[^/]+/?$"
    )),
    # Service pages — therapy modalities + presenting issues.
    # Three patterns:
    #   1. Suffix-based: <anything>-therapy / -counseling / -counselling / -treatment / -recovery / -coaching
    #   2. (therapy|counseling|counselling)-for-<anything>  +  online-(therapy|counseling)(-in-<location>)?
    #   3. Single-segment path matching a known modality/issue keyword (e.g. /emdr, /brainspotting)
    ("service",       re.compile(
        r"^/[a-z0-9-]+-(therapy|counseling|counselling|treatment|recovery|coaching)/?$"
        r"|^/(therapy|counseling|counselling|coaching)-for-[a-z0-9-]+/?$"
        r"|^/[a-z0-9-]+-coaching-for-[a-z0-9-]+/?$"
        r"|^/online-(therapy|counseling|counselling)(-in-[a-z-]+)?/?$"
        r"|^/(anxiety|depression|trauma|ptsd|emdr|cbt|dbt|grief|couples|family|teen|adolescent|child|individual|group|sex|intimacy|relationship|marriage|premarital|discernment|infidelity|affair|addiction|substance|adhd|autism|bipolar|ocd|eating-disorder|anorexia|bulimia|binge|body-image|self-esteem|stress|burnout|career|life-transition|lgbtq|gay|lesbian|trans|queer|bipoc|men|women|maternal|postpartum|perinatal|infertility|miscarriage|pregnancy|parenting|narcissistic-abuse|codependency|attachment|complex-trauma|brainspotting|ifs|internal-family-systems|parts-work|somatic|gottman|emotionally-focused|psychodynamic|solution-focused|mindfulness|christian-counseling|faith-based|holistic|intensive|intensives|retreat|retreats)/?$"
    )),
    # Home indicators (incl. SQSP duplicates like /home-2)
    ("home",          re.compile(r"^/?$|^/home(-\d+)?/?$|^/index(\.html?)?/?$|^/welcome/?$")),
    # Common page patterns — about, services index, testimonials, fees, careers, store
    ("services_index", re.compile(r"/services?/?$|/what-we-treat/?$|/specialties/?$|/our-approach/?$")),
    ("about",         re.compile(r"/about/?$|/about-us/?$|/our-story/?$|/our-mission/?$|/philosophy/?$")),
    ("testimonials",  re.compile(r"/testimonials/?$|/reviews(-\d+)?/?$|/endorsements/?$")),
    ("fees",          re.compile(r"/fees/?$|/rates/?$|/pricing/?$|/insurance/?$|/payment/?$|/investment/?$")),
    ("careers",       re.compile(r"/careers/?$|/jobs/?$|/join-us/?$|/we-re-hiring/?$|/.+-careers/?$")),
    ("store",         re.compile(r"/shop/?$|/store/?$|/products?/?$|/cart/?$|/checkout/?$|/digital-products(-\d+)?/?$")),
    ("thank_you",     re.compile(r"/thank-you/?$|/thanks/?$|/success/?$|/confirmation/?$")),
]

# Categories that should NOT be part of the configurator (admin/system pages,
# duplicates, paginated listings). Stripped from output entirely.
EXCLUDE_PATTERNS = [
    re.compile(r"/wp-admin/|/wp-login|/wp-content/|/wp-includes/"),
    re.compile(r"/feed/?$|/feed/atom/?$|/rss/?$"),
    re.compile(r"\?.*page=|/page/\d+/?$"),            # paginated archives
    re.compile(r"/category/|/tag/|/author/"),          # WP/Yoast taxonomy archives
    re.compile(r"\.(jpg|jpeg|png|gif|pdf|zip|webp|svg|ico|css|js)(\?.*)?$", re.I),
    re.compile(r"/cdn-cgi/|/_next/|/static/"),
    re.compile(r"/sitemap.*\.xml|robots\.txt"),
    # SQSP template-duplicate scaffolding: /home-testimonials, /optin-testimonials,
    # /coaching-testimonials, /home-services, /home-faq, and their
    # /blog-post-title-{one,two,...}-<slug> children. The live site links only
    # to /, but SQSP leaves the template blocks addressable from the sitemap.
    re.compile(r"^/[a-z0-9-]+-testimonials(/.*)?$", re.I),
    re.compile(r"^/home-(services|faq|about|contact|fees|hero|cta|footer|header|nav)(/.*)?$", re.I),
    re.compile(r"/blog-post-title-(one|two|three|four|five|six|seven|eight|nine|ten)(-[a-z0-9]+)*/?$", re.I),
]


def _is_excluded(path: str) -> bool:
    for pat in EXCLUDE_PATTERNS:
        if pat.search(path):
            return True
    return False


def _categorize_url(url: str) -> str:
    """Return category name or 'unknown' for LLM cleanup pass."""
    parsed = urlparse(url)
    path = (parsed.path or "/").lower()
    # Strip duplicate slashes
    path = re.sub(r"/+", "/", path)
    if _is_excluded(path):
        return "_excluded"
    for name, pat in CATEGORY_PATTERNS:
        if pat.search(path):
            return name
    return "unknown"


# Maps an "index" category to the category its children should inherit.
# E.g. if /selfenergyblog is detected as blog_index, then /selfenergyblog/<slug>
# becomes blog_post. Same logic for bio (team index -> bio children) and
# location (locations index -> location children).
INDEX_CHILD_MAP = {
    "blog_index": "blog_post",
    "bio": "bio",            # /team -> /team/<slug>
    "location": "location",  # /locations -> /locations/<slug>
}


def _apply_parent_inheritance(uniq_urls, categorized, unknowns):
    """
    Reclassify unknown URLs that live under a known index path.

    Pass 1 may detect /selfenergyblog as blog_index but leave /selfenergyblog/<slug>
    as unknown because the slug doesn't match any standard blog post regex
    (which expects /blog/, /posts/, etc. as the parent segment). This pass walks
    the unknowns and reclassifies them based on their parent path.

    Returns updated (categorized, unknowns).
    """
    # Build dict of (lowercased, no-trailing-slash) parent paths for each
    # category that supports inheritance.
    inheritance_parents = {}  # parent_path_str -> child_category
    for parent_cat, child_cat in INDEX_CHILD_MAP.items():
        for parent_url in categorized.get(parent_cat, []):
            parent_path = urlparse(parent_url).path.rstrip("/").lower()
            if parent_path:
                inheritance_parents[parent_path] = child_cat

    if not inheritance_parents:
        return categorized, unknowns

    still_unknown = []
    for u in unknowns:
        path = urlparse(u).path.rstrip("/").lower()
        matched_child_cat = None
        segments = path.split("/")
        # Iterate from longest possible parent down to shortest — prefer more
        # specific matches (e.g. /team/chicago/<slug> over /team/<slug>).
        for i in range(len(segments) - 1, 0, -1):
            candidate_parent = "/".join(segments[:i])
            if candidate_parent in inheritance_parents:
                matched_child_cat = inheritance_parents[candidate_parent]
                break
        if matched_child_cat:
            categorized.setdefault(matched_child_cat, []).append(u)
        else:
            still_unknown.append(u)

    return categorized, still_unknown


# ── Sitemap discovery ───────────────────────────────────────────────────────

async def _fetch_text(client, url):
    """Fetch URL, return (status, text). Returns (0, '') on network error."""
    try:
        r = await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        logger.debug(f"_fetch_text {url}: {e}")
        return 0, ""


def _extract_locs(xml_text):
    """Pull <loc>...</loc> values out of sitemap XML. Tolerant of namespacing."""
    if not xml_text:
        return []
    # Cheap regex extraction — more robust than full XML parse for messy sitemaps
    locs = re.findall(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", xml_text)
    return [u.strip() for u in locs if u.strip()]


def _looks_like_sitemap_index(xml_text):
    return "<sitemapindex" in xml_text or "<sitemap>" in xml_text


async def _discover_sitemap_urls(client, base_url):
    """
    Find sitemap URL(s) for the site. Tries common locations + robots.txt.
    Returns (sitemap_urls, source) where source ∈ {'sitemap.xml','sitemap_index.xml','robots.txt',''}.
    """
    base = base_url.rstrip("/")

    # 1. /sitemap.xml
    status, body = await _fetch_text(client, f"{base}/sitemap.xml")
    if status == 200 and body and ("<urlset" in body or "<sitemapindex" in body or "<loc>" in body):
        return [f"{base}/sitemap.xml"], "sitemap.xml"

    # 2. /sitemap_index.xml
    status, body = await _fetch_text(client, f"{base}/sitemap_index.xml")
    if status == 200 and body and ("<urlset" in body or "<sitemapindex" in body or "<loc>" in body):
        return [f"{base}/sitemap_index.xml"], "sitemap_index.xml"

    # 3. robots.txt -> Sitemap: lines
    status, body = await _fetch_text(client, f"{base}/robots.txt")
    if status == 200 and body:
        urls = []
        for line in body.splitlines():
            m = re.match(r"^\s*sitemap\s*:\s*(.+?)\s*$", line, re.I)
            if m:
                urls.append(m.group(1))
        if urls:
            return urls, "robots.txt"

    return [], ""


async def _walk_sitemaps(client, sitemap_urls):
    """
    Recursively walk sitemap indexes to collect all page URLs. Capped at
    MAX_SUBSITEMAPS sitemap fetches and MAX_TOTAL_URLS final URLs.
    """
    all_urls = []
    seen_sitemaps = set()
    queue = list(sitemap_urls)

    while queue and len(seen_sitemaps) < MAX_SUBSITEMAPS:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        status, body = await _fetch_text(client, sm_url)
        if status != 200 or not body:
            logger.debug(f"sitemap {sm_url} -> status {status}")
            continue

        locs = _extract_locs(body)
        if _looks_like_sitemap_index(body):
            # Sub-sitemaps to walk
            for loc in locs:
                if loc not in seen_sitemaps:
                    queue.append(loc)
        else:
            # Page URLs to collect
            for loc in locs:
                if len(all_urls) >= MAX_TOTAL_URLS:
                    break
                all_urls.append(loc)

        if len(all_urls) >= MAX_TOTAL_URLS:
            break

    return all_urls


# ── Bounded crawl fallback ──────────────────────────────────────────────────

LINK_RE = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\']', re.I)


async def _bounded_crawl(client, start_url):
    """
    BFS crawl from start_url, same-origin only, capped at MAX_CRAWL_PAGES
    and MAX_CRAWL_DEPTH. Used when no sitemap is discoverable.
    """
    parsed = urlparse(start_url)

    seen = set()
    found = []
    queue = [(start_url, 0)]

    while queue and len(seen) < MAX_CRAWL_PAGES:
        url, depth = queue.pop(0)
        url, _ = urldefrag(url)
        if url in seen:
            continue
        seen.add(url)

        # Skip non-HTML by extension
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|webp|svg|zip|css|js)(\?|$)", url, re.I):
            continue

        try:
            r = await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            ct = r.headers.get("content-type", "")
            if "html" not in ct.lower():
                continue
            found.append(url)
        except Exception:
            continue

        if depth >= MAX_CRAWL_DEPTH:
            continue

        for href in LINK_RE.findall(r.text):
            absu = urljoin(url, href)
            absu, _ = urldefrag(absu)
            ap = urlparse(absu)
            if ap.netloc != parsed.netloc:
                continue
            if not ap.scheme.startswith("http"):
                continue
            if absu not in seen:
                queue.append((absu, depth + 1))

        # Polite delay
        await asyncio.sleep(0.2)

    return found


# ── Anthropic cleanup pass ──────────────────────────────────────────────────

async def _llm_classify_unknowns(unknowns, anthropic_key):
    """
    Send a batch of unknown URLs to Claude for categorization. Returns a dict
    of {url: category}. Category must be one of the valid category names.
    Falls back to 'other' on any failure — never raises.
    """
    if not unknowns or not anthropic_key:
        return {u: "other" for u in unknowns}

    # Cap how many we send. Categorizer is here to clean up unusual URLs, not
    # classify thousands. If something's miscategorized at scale, the regex
    # patterns above need updating.
    sample = unknowns[:50]

    categories = ", ".join([
        "home", "service", "location", "bio", "blog_index", "blog_post",
        "faq", "contact", "about", "testimonials", "fees", "careers", "store",
        "thank_you", "services_index", "legal_privacy", "legal_terms",
        "legal_other", "other"
    ])

    prompt = f"""Classify each URL into ONE of these categories for a therapy practice website:

{categories}

Definitions:
- service: a single therapy modality or presenting issue page (anxiety therapy, EMDR, couples counseling, etc.)
- location: a city/state/region-specific page
- bio: an individual therapist or staff member's profile page
- blog_index: blog/news listing page
- blog_post: individual blog/article/in-the-media post
- about: practice about/story/mission (NOT individual bios)
- thank_you: post-form-submission landing page
- other: anything that doesn't fit cleanly

Respond with ONLY a JSON object mapping each URL to its category. No prose, no markdown.

URLs:
{json.dumps(sample, indent=2)}"""

    try:
        async with httpx.AsyncClient(timeout=60) as ac:
            r = await ac.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                logger.warning(f"Anthropic returned {r.status_code}: {r.text[:200]}")
                return {u: "other" for u in unknowns}
            data = r.json()
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            # Strip code fences if any leaked through
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            parsed = json.loads(text)
            # Build result; default any missing or out-of-set to 'other'
            valid_cats = set(c.strip() for c in categories.split(","))
            result = {}
            for u in unknowns:
                cat = parsed.get(u, "other") if isinstance(parsed, dict) else "other"
                result[u] = cat if cat in valid_cats else "other"
            return result
    except Exception as e:
        logger.warning(f"LLM classify failed, defaulting to 'other': {e}")
        return {u: "other" for u in unknowns}


# ── Callback ────────────────────────────────────────────────────────────────

async def _send_callback(callback_url, agent_api_key, task_id, report):
    """POST results back to Client HQ. Logs on non-2xx, does not raise."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            callback_url,
            json={"task_id": task_id, "report": report},
            headers={
                "Authorization": f"Bearer {agent_api_key}",
                "Content-Type": "application/json",
            },
        )
        if r.status_code >= 300:
            logger.warning(f"Callback to {callback_url} returned {r.status_code}: {r.text[:300]}")
        else:
            logger.info(f"Callback OK ({r.status_code}) for task {task_id[:12]}")


# ── Main entry point ────────────────────────────────────────────────────────

async def run_sitemap_scout(task_id, params, status_callback, env):
    """
    params:
        root_url: str          — homepage URL to scout (required)
        client_slug: str       — for logging/callback context (optional)
        callback_url: str      — POST results here when done (optional)

    env:
        AGENT_API_KEY          — bearer for callback
        ANTHROPIC_API_KEY      — for unknown-URL classification (optional;
                                 falls back to 'other' if missing)
    """
    root_url = params.get("root_url", "").strip().rstrip("/")
    client_slug = params.get("client_slug", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")

    if not root_url:
        await status_callback(task_id, "failed", "root_url required")
        return None

    started_at = datetime.now(timezone.utc)

    report = {
        "scout_version": "1.0",
        "scanned_at": started_at.isoformat(),
        "root_url": root_url,
        "client_slug": client_slug,
        "sitemap_source": "",       # 'sitemap.xml' | 'sitemap_index.xml' | 'robots.txt' | 'crawl' | ''
        "total_pages": 0,
        "pages_by_category": {},    # {category: [{url}]}
        "collapsed_categories": {}, # {category: {count, sample_count, all_urls}}
        "errors": [],
        "duration_seconds": 0,
    }

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            # ── Step 1: Discover sitemap ───────────────────────────────────
            await status_callback(task_id, "running", "Discovering sitemap...")
            sitemap_urls, source = await _discover_sitemap_urls(client, root_url)

            urls = []

            if sitemap_urls:
                report["sitemap_source"] = source
                await status_callback(task_id, "running", f"Walking {len(sitemap_urls)} sitemap(s)...")
                urls = await _walk_sitemaps(client, sitemap_urls)
                logger.info(f"sitemap-scout {task_id[:12]}: {source} -> {len(urls)} URLs")
            else:
                # ── Step 2: Bounded crawl fallback ─────────────────────────
                report["sitemap_source"] = "crawl"
                await status_callback(task_id, "running", "No sitemap, crawling...")
                urls = await _bounded_crawl(client, root_url)
                logger.info(f"sitemap-scout {task_id[:12]}: crawl -> {len(urls)} URLs")

            if not urls:
                report["errors"].append("No URLs discovered (sitemap missing and crawl returned 0).")
                report["duration_seconds"] = int((datetime.now(timezone.utc) - started_at).total_seconds())
                await status_callback(task_id, "complete", "No URLs found")
                if callback_url:
                    await _send_callback(callback_url, agent_api_key, task_id, report)
                return report

            # Dedupe while preserving order
            seen = set()
            uniq_urls = []
            for u in urls:
                # Normalize trailing slashes for dedupe purposes
                norm = u.rstrip("/").lower()
                if norm not in seen:
                    seen.add(norm)
                    uniq_urls.append(u)

            report["total_pages"] = len(uniq_urls)

            # ── Step 3: Categorize (regex pass 1) ──────────────────────────
            await status_callback(task_id, "running", f"Categorizing {len(uniq_urls)} URLs...")
            categorized = {}
            unknowns = []

            for u in uniq_urls:
                cat = _categorize_url(u)
                if cat == "_excluded":
                    continue
                if cat == "unknown":
                    unknowns.append(u)
                    continue
                categorized.setdefault(cat, []).append(u)

            # ── Step 4: Parent inheritance (catches custom blog/team paths) ─
            # Many therapist sites use custom names like /selfenergyblog/<slug>.
            # Pass 1 caught the index but left children as unknown. This step
            # reclassifies them based on their parent path.
            categorized, unknowns = _apply_parent_inheritance(uniq_urls, categorized, unknowns)

            # ── Step 5: LLM cleanup on remaining unknowns ──────────────────
            if unknowns:
                await status_callback(task_id, "running", f"Classifying {len(unknowns)} ambiguous URLs...")
                llm_results = await _llm_classify_unknowns(unknowns, anthropic_key)
                for u, cat in llm_results.items():
                    categorized.setdefault(cat, []).append(u)

            # ── Step 6: Collapse oversized categories ──────────────────────
            pages_by_category = {}
            collapsed = {}

            for cat, url_list in categorized.items():
                # Sort by URL depth ascending (shorter paths = more important)
                # then alphabetically for stability
                sorted_urls = sorted(url_list, key=lambda u: (urlparse(u).path.count("/"), u))

                if len(sorted_urls) > COLLAPSE_THRESHOLD and cat in ("blog_post", "blog_index", "bio", "location"):
                    # These are the categories that can legitimately have many entries.
                    # Sample the first N (shallowest) for the configurator preview;
                    # keep the full list under all_urls for expand-on-demand.
                    sample = sorted_urls[:COLLAPSE_THRESHOLD]
                    pages_by_category[cat] = [{"url": u} for u in sample]
                    collapsed[cat] = {
                        "count": len(sorted_urls),
                        "sample_count": len(sample),
                        "all_urls": sorted_urls,
                    }
                else:
                    pages_by_category[cat] = [{"url": u} for u in sorted_urls]

            report["pages_by_category"] = pages_by_category
            report["collapsed_categories"] = collapsed
            report["duration_seconds"] = int((datetime.now(timezone.utc) - started_at).total_seconds())

            # Summary line for logs
            cat_summary = ", ".join(f"{k}={len(v)}" for k, v in sorted(pages_by_category.items()))
            logger.info(f"sitemap-scout {task_id[:12]} complete: {report['total_pages']} pages, {cat_summary}")

            await status_callback(task_id, "complete", f"{report['total_pages']} pages categorized")

            # ── Step 7: Callback ───────────────────────────────────────────
            if callback_url:
                await _send_callback(callback_url, agent_api_key, task_id, report)

            return report

    except Exception as e:
        logger.exception(f"sitemap-scout {task_id[:12]} crashed")
        report["errors"].append(f"Fatal: {str(e)[:300]}")
        report["duration_seconds"] = int((datetime.now(timezone.utc) - started_at).total_seconds())
        await status_callback(task_id, "error", f"Failed: {str(e)[:200]}")
        if callback_url:
            try:
                await _send_callback(callback_url, agent_api_key, task_id, report)
            except Exception:
                pass
        return report
