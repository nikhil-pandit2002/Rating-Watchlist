"""Parsing and normalisation shared across agencies.

Every agency writes ratings, outlooks and rating actions differently. All of
that divergence is isolated here so the adapters stay thin.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Optional

from rapidfuzz import fuzz

# --------------------------------------------------------------------------
# NaBFID detection
# --------------------------------------------------------------------------

# NaBFID appears in lender lists under several spellings. Keep this list
# conservative: a false positive silently attaches a loan that isn't ours.
NABFID_PATTERNS = [
    r"\bnabfid\b",
    r"national\s+bank\s+for\s+financing\s+infrastructure(\s+and\s+development)?",
    r"national\s+bank\s+for\s+infrastructure\s+financing",
]
_NABFID_RE = re.compile("|".join(NABFID_PATTERNS), re.I)

NABFID_CANONICAL = "National Bank for Financing Infrastructure and Development"


def is_nabfid(name: str) -> bool:
    """True if a lender name refers to NaBFID."""
    if not name:
        return False
    return bool(_NABFID_RE.search(squash_space(name)))


def squash_space(s: str) -> str:
    """Collapse whitespace and normalise unicode punctuation."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = s.replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Rating symbol / outlook / action
# --------------------------------------------------------------------------

AGENCY_PREFIXES = {
    "CARE": r"CARE",
    "ICRA": r"\[ICRA\]|ICRA",
    "CRISIL": r"CRISIL",
    "India Ratings": r"IND",
    "Infomerics": r"IVR",
    "Brickwork": r"BWR",
}

# Long-term grades (AAA..D) and short-term grades (A1+..A4, D).
_GRADE = r"(?:AAA|AA|A|BBB|BB|B|C|D)[+-]?"
_ST_GRADE = r"(?:A1|A2|A3|A4|D)[+-]?"

OUTLOOKS = ["Stable", "Positive", "Negative", "Developing"]
_OUTLOOK_RE = re.compile(r"\b(Stable|Positive|Negative|Developing)\b", re.I)

_WATCH_RE = re.compile(
    r"(?:rating|credit)\s+watch\s+with\s+(developing|positive|negative)\s+implications", re.I
)

# Rating actions, longest/most specific first so "Removed from ISSUER NOT
# COOPERATING" wins over a bare "Reaffirmed" appearing in the same cell.
ACTION_PATTERNS = [
    # Infomerics writes both NOT and NON, and their PDFs carry a recurring
    # typo - 'COPERATING', missing an o - so the second o is optional.
    (r"removed\s+from\s+issuer\s+(?:not|non)\s+co[-\s]?o?perating", "Removed from Issuer Not Cooperating"),
    (r"continues?\s+to\s+remain\s+under\s+issuer\s+(?:not|non)\s+co[-\s]?o?perating", "Continues under Issuer Not Cooperating"),
    (r"issuer\s+(?:not|non)\s+co[-\s]?o?perating", "Issuer Not Cooperating"),
    (r"\bupgrade[d]?\b", "Upgraded"),
    (r"\bdowngrade[d]?\b", "Downgraded"),
    (r"\bwithdraw[nl]?\b|\bwithdrawal\b", "Withdrawn"),
    (r"\breaffirm(?:ed|ation)?\b", "Reaffirmed"),
    # India Ratings writes "Affirmed" where the others write "Reaffirmed".
    # The \b prevents this matching inside "reaffirmed".
    (r"\baffirm(?:ed|ation)?\b", "Reaffirmed"),
    (r"\bassign(?:ed|ment)\b", "Assigned"),
    (r"\bre-?affirmed\b", "Reaffirmed"),
    (r"\benhanced\b", "Enhanced"),
    (r"\bplaced\s+on\b", "Placed on Watch"),
    (r"\brevised\b", "Revised"),
    (r"\bmigrated\b", "Migrated"),
    (r"\bsuspended\b", "Suspended"),
    (r"\bprovisional\b", "Provisional"),
]


def split_rating(raw: str, agency: str = "") -> tuple[str, str]:
    """Split an agency rating string into (rating, outlook).

    Handles the six house styles:
        CARE AAA; Stable        -> ("CARE AAA", "Stable")
        [ICRA]AAA(Stable)       -> ("[ICRA]AAA", "Stable")
        CRISIL AAA/Stable       -> ("CRISIL AAA", "Stable")
        IND AA-/Negative        -> ("IND AA-", "Negative")
        BWRAAA/Stable           -> ("BWR AAA", "Stable")
        CARE A1+                -> ("CARE A1+", "")     short term, no outlook
    """
    raw = squash_space(raw)
    if not raw:
        return "", ""

    outlook = ""

    watch = _WATCH_RE.search(raw)
    if watch:
        outlook = f"Rating Watch with {watch.group(1).title()} Implications"
        raw = _WATCH_RE.sub("", raw)

    # Outlook inside parentheses: [ICRA]AAA(Stable)
    paren = re.search(r"\(\s*(Stable|Positive|Negative|Developing)\s*\)", raw, re.I)
    if paren and not outlook:
        outlook = paren.group(1).title()
        raw = raw[: paren.start()] + raw[paren.end():]

    # Outlook after ';' or '/'
    if not outlook:
        m = _OUTLOOK_RE.search(raw)
        if m:
            outlook = m.group(1).title()
            raw = raw[: m.start()] + raw[m.end():]

    rating = raw
    # Agencies often pack the rating action into the same cell
    # ('[ICRA]AAA (Stable); Reaffirmed'). The action belongs in "Development",
    # so strip it out of the rating symbol here.
    for pattern, _ in ACTION_PATTERNS:
        rating = re.sub(pattern, " ", rating, flags=re.I)

    # Strip separators left behind by the outlook/action removal. Never strip a
    # trailing '-' or '+' that belongs to the grade: 'BWR BBB-' and 'BWR BBB'
    # are different notches.
    rating = re.sub(r"\(\s*\)", " ", rating)
    # Agencies chain actions with a conjunction ('[ICRA]A1+; and withdrawn').
    # Removing the action leaves a dangling 'and' on the rating symbol.
    rating = re.sub(r"[\s;,/]+and\s*$", "", rating.strip(), flags=re.I)
    # Infomerics footnotes its symbols with '*'. Brackets must NOT be stripped
    # blindly: 'BWR BB (SO)' is a structured-obligation suffix and part of the
    # symbol. Only a bracket left unbalanced by the action removal is dropped.
    rating = re.sub(r"[\s;/,*]+$", "", rating.strip())
    rating = re.sub(r"^[\s;/,*]+", "", rating)
    if rating.count("(") > rating.count(")"):
        rating = re.sub(r"\s*\([^()]*$", "", rating).strip()
    elif rating.count(")") > rating.count("("):
        rating = re.sub(r"^[^()]*\)\s*", "", rating).strip()
    rating = re.sub(r"\s*[;/]\s*[;/]\s*", " / ", rating)
    rating = squash_space(rating)

    # Brickwork PDFs lose spaces: "BWRAAA" -> "BWR AAA"
    rating = re.sub(r"^(BWR|CARE|CRISIL|IVR|IND)([A-D])", r"\1 \2", rating)

    return rating.strip(" ;/,"), outlook


def extract_action(text: str) -> str:
    """Map free text to a normalised rating action ('Development')."""
    t = squash_space(text)
    if not t:
        return ""
    for pattern, label in ACTION_PATTERNS:
        if re.search(pattern, t, re.I):
            return label
    return ""


def classify_instrument(name: str) -> str:
    """Bucket an instrument into a coarse category."""
    n = squash_space(name).lower()
    if not n:
        return ""
    if re.search(r"commercial\s+paper|\bcp\b", n):
        return "Commercial Paper"
    if re.search(r"certificate\s+of\s+deposit", n):
        return "Certificate of Deposit"
    if re.search(
        r"non[- ]?convertible|debenture|\bncd\b|\bbond|subordinat|perpetual"
        r"|tier\s*(i|ii|1|2)|borrowing\s+programme|market\s+borrowing",
        n,
    ):
        return "Debt Instrument"
    if re.search(r"fixed\s+deposit|\bfd\b", n):
        return "Fixed Deposit"
    has_lt = bool(re.search(r"long[- ]?term", n))
    has_st = bool(re.search(r"short[- ]?term", n))
    if has_lt and has_st:
        return "Long Term / Short Term Bank Facilities"
    if has_st:
        return "Short Term Bank Facilities"
    if has_lt:
        return "Long Term Bank Facilities"
    if re.search(r"term\s+loan|cash\s+credit|working\s+capital|fund[- ]?based|non[- ]?fund|bank\s+(facilit|loan)|overdraft|letter\s+of\s+credit|bank\s+guarantee", n):
        return "Bank Facilities"
    if re.search(r"securitis|pass\s+through|\bptc\b", n):
        return "Securitisation"
    # Non-corporate issuers carry two instrument kinds that corporates rarely do.
    # Both used to fall through to 'Other', which loses the distinction between
    # an entity-level opinion and a specific facility.
    if re.search(r"issuer\s+rating|issuer\s+credit|\bcorporate\s+credit\s+rating\b", n):
        return "Issuer Rating"
    if re.search(r"\binvit\b|\breit\b|units?\s+of\s+(the\s+)?(invit|reit|trust)", n):
        return "InvIT Units"
    return "Other"


# --------------------------------------------------------------------------
# Amounts and units
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"(\d[\d,]*\.?\d*)")


def parse_amount(text: str) -> Optional[float]:
    """Pull the leading numeric amount out of a table cell.

    '45,000.00 (Enhanced from 25,000.00)' -> 45000.0
    """
    if text is None:
        return None
    t = squash_space(str(text))
    if not t or t.lower() in {"-", "nil", "na", "n.a.", "not applicable"}:
        return None
    # Ignore anything inside brackets - that is usually the previous amount.
    t = re.sub(r"\([^)]*\)", " ", t)
    m = _NUM_RE.search(t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def detect_unit(text: str, default: str = "Rs. Crore") -> str:
    """Identify the currency unit stated in a header or cell."""
    t = squash_space(text).lower()
    # Brickwork writes 'Rs Crs'; CARE/ICRA write 'crore' or '₹ cr'.
    if re.search(r"\bcrore|\bcrs?\b|₹\s*cr", t):
        return "Rs. Crore"
    if re.search(r"\blakh|\blac\b", t):
        return "Rs. Lakh"
    if re.search(r"\bmillion|\bmn\b", t):
        return "Rs. Million"
    if re.search(r"\bbillion|\bbn\b", t):
        return "Rs. Billion"
    return default


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_DATE_FORMATS = [
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d",
    "%d.%m.%Y",
]

_DATE_HINT = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})"
    r"|([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
    r"|(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})"
    r"|(\d{4}-\d{2}-\d{2})"
    r"|(\d{1,2}-[A-Za-z]{3,9}-\d{4})"
)


def parse_date(text: str) -> Optional[date]:
    """Best-effort date parse across the formats these sites use."""
    if not text:
        return None
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    t = squash_space(str(text))

    m = _DATE_HINT.search(t)
    candidate = next((g for g in m.groups() if g), None) if m else t
    candidate = (candidate or t).replace(",", " ").strip()
    candidate = re.sub(r"\s+", " ", candidate)

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    # last resort: 'YYYY-MM-DD HH:MM:SS.mmm'
    try:
        return datetime.fromisoformat(t.split(".")[0]).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Company-name matching
# --------------------------------------------------------------------------

_SUFFIXES = [
    "private limited", "pvt ltd", "pvt limited", "limited", "ltd",
    # 'pvt' alone: agencies write 'Pvt. Ltd.', and once the full stops become
    # spaces the two words are stripped separately, so 'pvt' must be a suffix
    # in its own right or it survives into the comparison.
    "pvt",
    "llp", "corporation", "corp", "company", "co", "&", "and",
]
_SUFFIX_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(_SUFFIXES, key=len, reverse=True)) + r")\b",
    re.I,
)

# Ceiling applied when one name is wholly contained in the other. Sits below the
# 88.0 auto-accept bar in best_match(), so a subset is always offered for review
# and never confirmed on its own.
SUBSET_CAP = 85.0


_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]{1,40})\)\s*$")


def _strip_acronym_paren(name: str) -> str:
    """Drop a trailing '(SMC)' that merely abbreviates the name itself.

    Agencies append the borrower's own initials constantly - 'Surat Municipal
    Corporation (SMC)' - and left in place the bracket makes the name a superset
    of itself, which the subset rule then caps below auto-accept.

    Only an actual acronym is removed. A trailing bracket often carries the
    identity instead ('Clean Solar Power (Baniyana)', where the location is the
    only thing separating five sibling SPVs), so the contents are dropped solely
    when they spell out the initials of the words in front of them.
    """
    m = _TRAILING_PAREN_RE.search(name or "")
    if not m:
        return name
    inner = re.split(r"[/,]", m.group(1))[0]
    inner = re.sub(r"[^A-Za-z]", "", inner).lower()
    if not inner:
        return name
    head = name[: m.start()]
    words = re.findall(r"[A-Za-z]+", head)
    initials = "".join(w[0] for w in words).lower()
    # A word that is already an initialism contributes all of its letters:
    # 'KJ Foundation (KJF)' abbreviates to KJF, not KF.
    expanded = "".join(w if w.isupper() and len(w) <= 6 else w[0] for w in words).lower()
    return head.rstrip() if inner in (initials, expanded) else name


# Agencies misspell their own entities, and a stray letter is enough to make a
# name look like a different company. India Ratings indexes 'MSIDC NASHIK REGION
# ROAD DEVELOPMENT CORPORTION LIMITED'; without folding that back the extra word
# survives suffix-stripping, our name becomes a strict subset of theirs, and the
# match is capped below auto-accept for a company that is simply the same one.
_TYPO_FOLD = {
    "corportion": "corporation",
    "corporataion": "corporation",
    "limted": "limited",
    "limtied": "limited",
    "privte": "private",
    "pirvate": "private",
    "trsut": "trust",
    "muncipal": "municipal",
    "munciipal": "municipal",
    "infrastrucutre": "infrastructure",
}
_TYPO_RE = re.compile(r"\b(" + "|".join(_TYPO_FOLD) + r")\b", re.I)


def canonical_name(name: str) -> str:
    """Normalise a company name for comparison."""
    n = squash_space(_strip_acronym_paren(name)).lower()
    n = _TYPO_RE.sub(lambda m: _TYPO_FOLD[m.group(1).lower()], n)
    # Collapse the runs of spaces left by punctuation BEFORE stripping suffixes.
    # 'Pvt. Ltd.' becomes 'pvt  ltd' with two spaces, which the 'pvt ltd'
    # pattern - written with a single space - can never match; 'pvt' then
    # survives and drags every 'Pvt. Ltd.' company's score down. It cost
    # 'Devanahalli Kolar Highway Pvt. Ltd.' 15 points and a match.
    # '_' is a word character, so it survives the punctuation strip and glues
    # two words into one token. Names taken from URLs and PDF filenames are full
    # of them ('Nxt_InfraTrust'), so it is separated explicitly.
    # '&' is dropped here rather than left for the suffix pass: it is not a word
    # character, so the \b anchors in _SUFFIX_RE can never fire against a
    # free-standing '&'. It survived, made 'Power & Light' a superset of
    # 'Power and Light' (where 'and' *is* stripped), and the subset rule then
    # held two spellings of one company at 85.
    n = re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", n))
    n = _SUFFIX_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


# Words that describe what a company does or how it is constituted, rather than
# which company it is. Two names sharing only these are not the same borrower:
# 'Suriya Spinning Mills' and 'S.S. Spinning Mills' scored 90.5 on 'spinning
# mills' alone. Identity lives in the distinctive part of the name.
GENERIC_TOKENS = {
    "spinning", "mills", "mill", "industries", "industry", "industrial",
    "enterprises", "enterprise", "traders", "trading", "exports", "export",
    "imports", "import", "projects", "project", "infra", "infrastructure",
    "engineering", "engineers", "construction", "constructions", "builders",
    "developers", "estates", "realty", "properties", "textiles", "textile",
    "steel", "steels", "cement", "sugar", "power", "energy", "solar",
    "technologies", "technology", "solutions", "services", "systems",
    "international", "global", "india", "indian", "bharat", "national",
    "finance", "financial", "capital", "investments", "holdings", "ventures",
    "products", "foods", "agro", "chemicals", "chemical", "pharma",
    "logistics", "transport", "motors", "auto", "packaging", "plastics",
    "healthcare", "hospitals", "education", "resorts", "hotels",
    "manufacturing", "processing", "trust", "fund", "group", "associates",
    "bank", "housing", "microfinance", "leasing", "securities",
}


# Trusts, InvITs, civic bodies and foundations are named for what they do, so
# GENERIC_TOKENS - built for corporates, where these words are filler - eats
# their identity outright. 'Global Education Foundation' reduces to {foundation}
# and 'National Highways Infra Trust' to {highway}, leaving almost nothing to
# tell one apart from the next. For these entities the trade words ARE the name,
# so they are exempted from the generic list.
ENTITY_IDENTITY_WORDS = {
    "infrastructure", "infra", "energy", "power", "solar", "highways",
    "education", "global", "national", "international", "india", "indian",
    "healthcare", "hospitals", "transport", "logistics", "housing", "capital",
    "investments", "finance", "financial", "projects", "industries", "services",
    "technology", "technologies", "sustainable", "urban", "municipal",
}

# What marks a name as one of those entities rather than an ordinary company.
_NON_CORPORATE_RE = re.compile(
    r"\b(trust|invit|reit|fund|foundation|society|nigam|"
    r"municipal|corporation|nagar|panchayat|parishad|authority|"
    r"board|mission|samiti|sangh)\b",
    re.I,
)


def is_non_corporate(name: str) -> bool:
    """True for trusts, InvITs, municipal bodies, foundations and the like."""
    return bool(_NON_CORPORATE_RE.search(name or ""))


def _stem(token: str) -> str:
    """Crude singular/plural fold so 'Centers' and 'Center' compare equal."""
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _distinctive(tokens: set[str], non_corporate: bool = False) -> set[str]:
    """The part of a name that identifies the company rather than its trade."""
    generic = GENERIC_TOKENS - ENTITY_IDENTITY_WORDS if non_corporate else GENERIC_TOKENS
    return {_stem(t) for t in tokens if t not in generic and len(t) > 1}


def _numbers(tokens: set[str]) -> set[str]:
    """Digits carried in a name, with leading zeros folded away."""
    return {t.lstrip("0") or "0" for t in tokens if t.isdigit()}


def name_score(a: str, b: str) -> float:
    """0-100 similarity between two company names.

    token_set_ratio alone is dangerously permissive when a short name shares one
    distinctive word with a long one: 'National Bank for Financing Infrastructure
    and Development' scored 90.5 against 'R R Infrastructure' purely on the word
    'infrastructure', which is high enough to attach another company's rating to
    a borrower. Overlap of the *combined* vocabulary is therefore folded in, so a
    match on a small slice of the name cannot clear the threshold on its own.
    """
    ca, cb = canonical_name(a), canonical_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 100.0

    # Identical letters, different word breaks. Agencies close up compounds at
    # will - CRISIL indexes 'Oriental Infratrust' and 'Nxt InfraTrust' for what
    # the borrower calls 'Oriental Infra Trust' and 'Nxt-Infra Trust'. Compared
    # as tokens these score 49, far below anything that could match, because
    # 'infratrust' shares no token with 'infra' or 'trust'. The letter sequence
    # being identical is a stronger identity signal than the spacing, and two
    # different companies do not collide this way.
    if ca.replace(" ", "") == cb.replace(" ", ""):
        return 100.0

    base = max(
        fuzz.token_sort_ratio(ca, cb),
        fuzz.token_set_ratio(ca, cb) * 0.97,  # slight penalty: subset matches
    )

    ta, tb = set(ca.split()), set(cb.split())
    if not ta or not tb:
        return base

    # One name wholly contained in the other is the failure mode that produces
    # confidently wrong rows. 'IndiGrid Trust' vs 'Indigrid Infrastructure
    # Trust' and 'BNP PARIBAS' vs 'BNP Paribas India Solutions Private Limited'
    # both scored 97: separate legal entities, no distinctive word in conflict,
    # so every other guard here stays silent. The words that separate them are
    # exactly the ones a corporate-tuned generic list throws away, so no
    # threshold can tell a real subset match from a false one.
    #
    # It is therefore capped rather than rejected - a genuine rename such as
    # 'Vertis Infrastructure Trust (formerly Highways Infrastructure Trust)' is
    # also a subset. Capping below the auto-accept bar keeps both kinds ranked
    # at the top for a human to confirm, while denying either an automatic match.
    if ta < tb or tb < ta:
        return min(base, SUBSET_CAP)

    # A number in an infrastructure name is the identity, not decoration.
    # 'Huoban Energy 1' and 'Huoban Energy 11' are separate borrowers with
    # separate ratings, yet score 96.8 on string similarity alone - and if only
    # one of them is rated, the other would silently inherit its rating. Any
    # disagreement in the digits means a different company, full stop.
    if _numbers(ta) != _numbers(tb):
        return min(base, 50.0)

    # Names that share nothing distinctive are different companies, however
    # similar the strings look. This must also fire when one side has no
    # distinctive part at all - 'S.S. Spinning Mills' reduces to initials, and
    # everything it shares with 'Suriya Spinning Mills' is trade vocabulary.
    non_corp = is_non_corporate(a) or is_non_corporate(b)
    da, db = _distinctive(ta, non_corp), _distinctive(tb, non_corp)
    if (da or db) and not (da & db):
        base *= 0.55

    # Each side carrying a distinctive word the other lacks points at sibling
    # projects rather than one company: 'MSRDC Pune Ring Road Eastern' and
    # '... Western' agree on everything except the word that identifies them.
    if (da - db) and (db - da):
        base *= 0.7

    jaccard = len(ta & tb) / len(ta | tb)
    if jaccard < 0.5:
        base *= 0.5 + jaccard
    return base


def best_match(target: str, candidates: list[str], threshold: float = 88.0):
    """Return (best_candidate, score) or (None, best_score) if below threshold."""
    best, best_s = None, 0.0
    for c in candidates:
        s = name_score(target, c)
        if s > best_s:
            best, best_s = c, s
    return (best, best_s) if best_s >= threshold else (None, best_s)


def rank_matches(target: str, candidates: list[str]) -> list[tuple[str, float]]:
    """Every candidate scored against the target, best first.

    Used to detect ambiguity: a group of near-identical entity names (an issuer
    and its SPVs, say) can all clear the threshold, and silently taking the top
    one attaches the wrong company's rating to a borrower.
    """
    scored = [(c, name_score(target, c)) for c in candidates]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored
