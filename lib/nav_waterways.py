"""
nav_waterways.py — USCG 13th Coast Guard District navigability determinations.

Source: Navigability_Determination_for_the_13th_Coast_Guard_District.pdf
        (Exhibit 11-K-1, Thirteenth Coast Guard District)

Usage:
    from lib.nav_waterways import lookup
    result = lookup("Willamette River")   # "Y", "N", or None
    result = lookup("Mill Creek", state="OR")  # disambiguation by state

Returns:
    "Y"   — NAV CG column has X (navigable under USCG jurisdiction)
    "N"   — NON-NAV CG only (explicitly determined non-navigable)
    None  — not listed or ambiguous (no determination made, or conflicting entries)

Waters subject to tidal influence are navigable regardless of this list
(per 33 CFR § 2.05-25), but that determination requires field/GIS knowledge
not applied here.
"""

import re

# ── Source data ────────────────────────────────────────────────────────────
#
# Each entry: (normalized_name, state_str, nav_cg)
#   normalized_name : lowercase, no state suffix, as close to PDF as practical
#   state_str       : state code(s) as shown in PDF (e.g. "OR", "WA", "ID/MT")
#   nav_cg          : True = NAV CG column X; False = NON-NAV CG only
#
# Entries with BOTH NAV and NON-NAV columns (partial navigability) → True.
# Duplicate (name, state) pairs with conflicting nav_cg → both stored;
# lookup() detects the conflict and returns None.
# Aliases (alternate names / common forms) included for matching flexibility.
#
_ENTRIES = [
    # A
    ("albeni reservoir",                  "ID",       True),
    ("alsea bay",                         "OR",       True),
    ("alsea river",                       "OR",       True),
    ("american falls reservoir",          "ID",       False),
    ("ash creek",                         "OR",       False),
    ("bachelor island slough",            "WA",       True),
    ("baker bay",                         "WA",       True),
    ("barrett slough",                    "OR",       False),
    ("bayou st. john",                    "OR",       False),
    # B
    ("bear creek (coos county)",          "OR",       True),
    ("bear creek",                        "WA",       False),
    ("bear lake",                         "ID/UT",    True),
    ("bear river",                        "WA",       True),
    ("beaver creek",                      "OR",       False),
    ("beaver slough",                     "OR",       True),
    ("big beef harbor",                   "WA",       True),
    ("big creek (lane county)",           "OR",       True),
    ("big creek (lincoln county)",        "OR",       False),
    ("big creek slough",                  "OR",       True),
    ("big horn river",                    "MT",       True),
    ("bingen channel",                    "WA",       True),
    ("birch creek",                       "OR",       False),
    ("birnie slough",                     "WA",       True),
    ("blind slough",                      "OR",       True),
    ("blind slough/gnat creek",           "OR",       True),
    ("gnat creek",                        "OR",       True),
    ("blue creek bay",                    "ID",       True),
    ("bogachiel river",                   "WA",       False),
    ("boone slough",                      "OR",       False),
    ("booneville channel",                "OR",       True),
    ("boulder creek",                     "OR",       False),
    ("bradbury slough",                   "OR",       True),
    ("brooks slough",                     "WA",       False),
    ("brownlee reservoir",                "ID",       True),
    ("budd inlet",                        "WA",       True),
    ("burke slough",                      "WA",       True),
    ("burnside channel",                  "OR",       True),
    ("south channel",                     "OR",       True),   # alias for Burnside Channel
    ("burnt bridge creek",                "WA",       False),
    # C
    ("calapooya river",                   "OR",       False),
    ("calender slough",                   "OR",       True),
    ("callahan creek",                    "MT",       False),
    ("canal creek",                       "OR",       True),
    ("canyon ferry reservoir",            "MT",       True),
    ("capitol lake",                      "WA",       True),
    ("carr inlet",                        "WA",       True),
    ("carrols channel",                   "WA",       True),
    ("case inlet",                        "WA",       True),
    ("catching slough",                   "OR",       True),
    ("cathlamet bay",                     "OR",       True),
    ("cathlamet channel",                 "WA",       True),
    ("cedar river",                       "WA",       True),
    ("chambers creek",                    "WA",       False),
    ("charlton channel",                  "WA",       True),
    ("chatcolet lake",                    "ID",       True),
    ("chehalis river",                    "WA",       True),
    ("chelan lake",                       "WA",       False),
    ("lake chelan",                       "WA",       False),
    ("chelan river",                      "WA",       False),
    ("chetco cove",                       "OR",       True),
    ("chetco river",                      "OR",       True),
    ("chinook river",                     "WA",       True),
    ("clackamas river",                   "OR",       False),
    ("clark fork river",                  "ID/MT",    True),
    ("clatskanie river",                  "OR",       True),
    ("clearwater river",                  "ID",       True),
    ("clearwater river (north fork)",     "ID",       True),
    ("north fork clearwater river",       "ID",       True),
    ("clifton channel",                   "OR",       True),
    ("coalbank slough",                   "OR",       True),
    ("coalcreek slough",                  "WA",       True),
    ("coeur d alene river",               "ID",       True),   # apostrophe stripped
    ("colorado lake",                     "OR",       False),
    ("columbia river",                    "WA/OR",    True),
    ("columbia river reservoir",          "WA",       True),
    ("columbia slough",                   "OR",       True),
    ("coos bay",                          "OR",       True),
    ("coos river",                        "OR",       True),
    ("coos river south fork",             "OR",       True),
    ("south fork coos river",             "OR",       True),
    ("cooston channel",                   "OR",       True),
    ("coquille river",                    "OR",       True),
    ("coquille river north fork",         "OR",       True),
    ("north fork coquille river",         "OR",       True),
    ("coweeman river",                    "WA",       True),
    ("cowlitz river",                     "WA",       True),
    ("cozine creek",                      "OR",       False),
    ("crater lake",                       "OR",       False),
    # D
    ("dairy creek",                       "OR",       False),
    ("dean creek",                        "OR",       True),
    ("deep river",                        "WA",       True),
    ("dell creek",                        "WA",       False),
    ("depoe bay",                         "OR",       True),
    ("depoe slough",                      "OR",       True),
    ("deschutes river",                   "WA",       False),
    ("deschutes river",                   "OR",       False),   # both states NON-NAV
    ("detroit reservoir",                 "OR",       False),
    ("dexter reservoir",                  "OR",       False),
    ("diablo lake",                       "WA",       False),
    ("dickey river",                      "WA",       True),
    ("duck creek",                        "WA",       False),
    ("duncan slough",                     "OR",       True),
    ("dungeness river",                   "WA",       False),
    ("duwamish river",                    "WA",       True),
    # E
    ("ebey slough",                       "WA",       True),
    ("edison slough",                     "WA",       True),
    ("elk river",                         "OR",       False),
    ("elochoman river",                   "WA",       True),
    ("elochoman slough",                  "WA",       True),
    ("elwell creek",                      "WA",       False),
    ("entiat river",                      "WA",       False),
    ("eslick creek",                      "OR",       True),
    ("ewauna lake",                       "OR",       False),
    ("lake ewauna",                       "OR",       False),
    # F
    ("falk creek",                        "WA",       False),
    ("ferndale slough",                   "WA",       True),
    ("portage slough",                    "WA",       True),   # alias for Ferndale Slough
    ("fifth street waterway",             "WA",       True),
    ("fisher island slough",              "WA",       True),
    ("flathead lake",                     "MT",       False),
    ("flathead river",                    "MT",       False),
    ("fort peck reservoir",               "MT",       True),
    ("franklin d. roosevelt lake",        "WA",       True),
    ("lake roosevelt",                    "WA",       True),
    ("roosevelt lake",                    "WA",       True),
    # G
    ("gales creek",                       "OR",       False),
    ("gardiner channel",                  "OR",       True),
    ("germany creek",                     "WA",       True),
    ("goble channel",                     "OR",       True),
    ("government island channel",         "OR",       True),
    ("governors lake",                    "WA",       True),
    ("grande ronde river",                "OR",       False),
    ("grays harbor",                      "WA",       True),
    ("grays river",                       "WA",       True),
    ("green lake",                        "WA",       False),
    ("green river",                       "WA",       True),
    # H
    ("hama hama river",                   "WA",       True),
    ("hamilton creek",                    "WA",       True),
    ("hat slough",                        "WA",       True),
    ("hauser dam reservoir",              "MT",       True),
    ("haynes slough",                     "OR",       True),
    ("hidden lake",                       "ID",       True),
    ("hoh river",                         "WA",       True),
    ("hoko river",                        "WA",       True),
    ("hood canal",                        "WA",       True),
    ("hood river",                        "OR",       True),
    ("hoquiam river",                     "WA",       True),
    ("hoquiam river east fork",           "WA",       True),
    ("east fork hoquiam river",           "WA",       True),
    ("horse shoe lake",                   "WA",       False),
    ("horseshoe lake",                    "WA",       False),
    ("horse thief lake",                  "WA",       False),
    ("hudson slough",                     "OR",       True),
    ("humptulips river",                  "WA",       True),
    # I
    ("imnaha river",                      "OR",       False),
    ("isthmus slough",                    "OR",       True),
    # J
    ("joe ney slough",                    "OR",       True),
    ("john day river",                    "OR",       True),   # appears twice, both NAV
    ("johns river",                       "WA",       True),
    ("johnson creek",                     "WA",       False),
    ("johnson creek",                     "OR",       False),  # both states NON-NAV
    ("jump off joe lake",                 "WA",       False),
    # K
    ("kalama river",                      "WA",       True),
    ("kentucky slough",                   "OR",       True),
    ("kilchis river",                     "OR",       True),
    ("klamath river",                     "OR",       False),
    ("klickitat river",                   "WA",       True),
    ("knappa slough",                     "OR",       True),
    ("kootenai river",                    "ID/MT",    True),
    # L
    ("lake koocanusa",                    "MT",       True),
    ("lake coeur d alene",                "ID",       True),   # apostrophe stripped
    ("coeur d alene lake",                "ID",       True),
    ("lake creek",                        "MT",       False),
    ("lake crescent",                     "WA",       False),
    ("lake pend oreille",                 "ID",       True),
    ("pend oreille lake",                 "ID",       True),
    ("lake river",                        "WA",       True),
    ("lake roosevelt",                    "WA",       True),
    ("lake union",                        "WA",       True),
    ("lake washington",                   "WA",       True),
    ("lake washington ship canal",        "WA",       True),
    ("larson slough",                     "OR",       True),
    ("latah creek",                       "WA",       True),
    ("latour creek",                      "ID",       False),
    ("lewis river",                       "WA",       True),
    ("lewis river east fork",             "WA",       True),
    ("east fork lewis river",             "WA",       True),
    ("lewis and clark river",             "OR",       True),
    ("lewis and clark",                   "OR",       True),
    ("little creek",                      "OR",       False),
    ("little lake",                       "OR",       False),
    ("little hoquiam river",              "WA",       True),
    ("little pend oreille river",         "WA",       False),
    ("little schooner creek",             "OR",       False),
    ("long tom river",                    "OR",       False),
    ("lookout point reservoir",           "OR",       False),
    ("lucky gap creek",                   "OR",       False),
    # M
    ("martin island slough",              "WA",       True),
    ("mcallister creek",                  "WA",       True),
    ("mcclellan creek",                   "OR",       False),
    ("mcdonald lake",                     "MT",       False),
    ("mcfee creek",                       "OR",       False),
    ("mcintosh slough",                   "OR",       True),
    ("mckenzie river",                    "OR",       True),
    ("mckenzie",                          "OR",       True),   # listed without "River" in PDF
    ("mcnary reservoir",                  "WA",       True),
    ("methow river",                      "WA",       False),  # two entries, both NON-NAV
    # Mill Creek, OR: one NAV (Umpqua trib, Scottsburg), one NON-NAV (Willamette trib, Salem)
    # Both stored → conflict detected by lookup() → returns None for OR
    ("mill creek",                        "OR",       True),
    ("mill creek",                        "OR",       False),
    ("mill creek",                        "WA",       True),   # two WA entries, both NAV
    ("mill slough",                       "OR",       False),
    ("millican creek",                    "OR",       False),
    ("millicoma river",                   "OR",       True),
    ("millicoma river west fork",         "OR",       False),
    ("west fork millicoma river",         "OR",       False),
    ("millport slough",                   "OR",       True),
    ("missouri river",                    "MT",       True),
    ("molalla river",                     "OR",       True),
    ("moses lake",                        "WA",       False),
    ("moyie river",                       "ID",       True),
    ("mullen slough",                     "WA",       False),
    ("multnomah channel",                 "OR",       True),
    # N
    ("naselle river",                     "WA",       True),
    ("neawanna river",                    "OR",       False),
    ("necanicum river",                   "OR",       True),
    ("nehalem bay",                       "OR",       True),
    ("nehalem river",                     "OR",       True),
    ("nehalem river north fork",          "OR",       True),
    ("north fork nehalem river",          "OR",       True),
    ("nemah river middle fork",           "WA",       True),
    ("middle fork nemah river",           "WA",       True),
    ("nemah river north fork",            "WA",       True),
    ("north fork nemah river",            "WA",       True),
    ("nemah river south fork",            "WA",       True),
    ("south fork nemah river",            "WA",       True),
    ("nestucca bay",                      "OR",       True),
    ("nestucca river",                    "OR",       True),
    ("big nestucca river",                "OR",       True),
    ("little nestucca river",             "OR",       True),
    ("netarts bay",                       "OR",       True),
    ("nisqually river",                   "WA",       True),
    ("noel creek",                        "OR",       True),
    ("nookachamps creek",                 "WA",       False),
    ("nooksack river",                    "WA",       True),
    ("north creek",                       "WA",       False),
    ("north river",                       "WA",       True),
    ("north slough",                      "OR",       True),
    ("nyberg creek",                      "OR",       False),
    # O
    ("ochoco creek",                      "OR",       False),
    ("okanogan river",                    "WA",       False),
    ("ollala bay",                        "WA",       True),
    ("ollala slough",                     "OR",       False),
    ("oregon slough",                     "OR",       True),
    ("north portland harbor",             "OR",       True),   # alias for Oregon Slough
    ("oswego canal",                      "OR",       True),
    ("oswego lake",                       "OR",       True),
    ("lake oswego",                       "OR",       True),
    # P
    ("pack river",                        "ID",       False),
    ("palix river",                       "WA",       True),
    ("palix river middle fork",           "WA",       True),
    ("middle fork palix river",           "WA",       True),
    ("palix river north fork",            "WA",       True),
    ("north fork palix river",            "WA",       True),
    ("palix river south fork",            "WA",       True),
    ("south fork palix river",            "WA",       True),
    ("palouse river",                     "WA",       False),
    ("palouse river south fork",          "WA",       False),
    ("south fork palouse river",          "WA",       False),
    ("palouse slough",                    "OR",       True),
    ("payette lake",                      "ID",       False),
    ("payette river",                     "ID",       False),
    ("payette river north fork",          "ID",       False),
    ("north fork payette river",          "ID",       False),
    ("pelton dam reservoir",              "OR",       False),
    ("lake simtustus",                    "OR",       False),
    ("simtustus lake",                    "OR",       False),
    ("pend oreille river",                "WA/ID",    True),
    ("percival creek",                    "WA",       False),
    ("pickering passage",                 "WA",       True),
    ("pony slough",                       "OR",       True),
    ("poodle creek",                      "OR",       False),
    ("port orford",                       "OR",       True),
    ("prairie channel",                   "OR",       True),
    ("priest lake",                       "ID",       True),
    ("priest river",                      "ID",       True),
    ("pulaski creek",                     "OR",       True),
    ("purdy creek",                       "WA",       False),
    ("puyallup river",                    "WA",       True),
    ("pysht river",                       "WA",       True),
    # Q
    ("queets river",                      "WA",       True),
    ("quilceda creek",                    "WA",       True),
    ("quillayute river",                  "WA",       True),
    # R
    ("randolph slough",                   "OR",       True),
    ("rock creek",                        "WA",       True),
    ("rogue river",                       "OR",       True),
    ("ross lake",                         "WA",       True),
    ("upper skagit river",                "WA",       True),   # Ross Lake entry description
    ("round lake",                        "ID",       True),
    # S
    ("st. joe river",                     "ID",       True),
    ("saint joe river",                   "ID",       True),
    ("st. maries river",                  "ID",       False),
    ("saint maries river",                "ID",       False),
    ("st. marys river",                   "MT",       False),
    ("st. regis river",                   "MT",       False),
    # Salmon Creek WA: NAV (Clark County) vs NON-NAV (Pacific County) → conflict → None
    ("salmon creek",                      "WA",       True),
    ("salmon creek",                      "WA",       False),
    ("salmon river",                      "ID",       True),
    ("salmon river middle fork",          "ID",       False),
    ("middle fork salmon river",          "ID",       False),
    ("salmon river",                      "OR",       False),  # Clackamas County, NON-NAV
    ("samish river",                      "WA",       True),   # both NAV and NON-NAV → partial → True
    ("lake sammamish",                    "WA",       True),
    ("sammamish lake",                    "WA",       True),
    ("sammamish river",                   "WA",       True),
    ("sand creek",                        "ID",       True),
    ("sand lake",                         "OR",       True),
    ("santiam river",                     "OR",       True),
    ("santiam river north fork",          "OR",       False),
    ("north fork santiam river",          "OR",       False),
    ("north santiam river",               "OR",       False),
    ("santiam river south fork",          "OR",       False),
    ("south fork santiam river",          "OR",       False),
    ("south santiam river",               "OR",       False),
    ("sekiu river",                       "WA",       True),
    ("sewell lake",                       "MT",       True),
    ("siletz bay",                        "OR",       True),
    ("siletz river",                      "OR",       True),
    ("siuslaw river",                     "OR",       True),
    ("siuslaw river north fork",          "OR",       True),
    ("north fork siuslaw river",          "OR",       True),
    ("skagit river",                      "WA",       True),
    ("skagit river north fork",           "WA",       True),
    ("north fork skagit river",           "WA",       True),
    ("skamokawa creek",                   "WA",       True),
    ("skamokawa creek left fork",         "WA",       False),
    ("left fork skamokawa creek",         "WA",       False),
    ("skidmore slough",                   "WA",       True),
    ("skipanon river",                    "OR",       True),
    ("skokomish river",                   "WA",       True),
    ("smith river",                       "OR",       True),
    ("snake river",                       "ID/OR/WA", True),
    ("snohomish river",                   "WA",       True),
    ("snoqualmie river",                  "WA",       True),
    ("south slough",                      "OR",       True),
    ("spokane river",                     "WA",       True),
    ("squaw creek",                       "ID",       False),
    ("steamboat slough",                  "WA",       True),
    ("stillaguamish river",               "WA",       True),
    ("stillwater river",                  "MT",       False),
    ("stuck river",                       "WA",       False),
    ("sumas river",                       "WA",       False),
    ("swan island lagoon",                "OR",       True),
    ("swinomish slough",                  "WA",       True),
    ("swinomish channel",                 "WA",       True),
    # T
    ("tacoma harbor",                     "WA",       True),
    ("lake tapps",                        "WA",       False),
    ("tapps lake",                        "WA",       False),
    ("ten mile lake",                     "OR",       False),
    ("teton river",                       "ID",       False),
    ("thornton lake",                     "OR",       False),
    ("lake thornton",                     "OR",       False),
    ("three rivers",                      "OR",       False),
    ("tiber reservoir",                   "MT",       False),
    ("tillamook river",                   "OR",       False),
    ("tolt river",                        "WA",       False),
    ("tongue river",                      "MT",       False),
    ("tualatin river",                    "OR",       True),
    # U
    ("umatilla river",                    "OR",       False),  # two OR entries, both NON-NAV
    ("umpqua river",                      "OR",       True),   # both NAV and NON-NAV → partially NAV → True
    ("south fork umpqua river",           "OR",       True),
    ("union river",                       "WA",       False),
    # W
    ("walker island channel",             "OR",       True),
    ("wallace slough",                    "OR",       True),
    ("wallacut river",                    "WA",       True),
    ("walla walla river",                 "WA",       True),
    ("wallowa river",                     "OR",       False),
    ("wallooskee river",                  "OR",       True),
    ("walluski river",                    "OR",       True),   # alternate spelling
    ("wanapum reservoir",                 "WA",       True),
    ("warren slough",                     "OR",       True),
    ("welcome slough",                    "WA",       True),
    ("wenatchee river",                   "WA",       False),
    ("westport slough",                   "OR",       True),
    ("lake whatcom",                      "WA",       False),
    ("whatcom lake",                      "WA",       False),
    ("whatcom waterway",                  "WA",       True),
    ("white salmon river",                "WA",       True),
    ("willapa bay",                       "WA",       True),
    ("willamette river",                  "OR",       True),
    ("willanch slough",                   "OR",       True),
    ("willapa river",                     "WA",       True),
    ("willapa river north fork",          "WA",       True),
    ("north fork willapa river",          "WA",       True),
    ("willapa river south fork",          "WA",       True),
    ("south fork willapa river",          "WA",       True),
    ("winchester bay",                    "OR",       True),
    ("wind river",                        "WA",       True),
    ("wishkah river",                     "WA",       True),
    ("woodard creek",                     "WA",       False),
    # Y
    ("yachats river",                     "OR",       True),
    ("yakima river",                      "WA",       True),
    ("yamhill river",                     "OR",       True),
    ("yamhill river south fork",          "OR",       False),
    ("south fork yamhill river",          "OR",       False),
    ("south yamhill river",               "OR",       False),
    ("yaquina bay",                       "OR",       True),
    ("yaquina river",                     "OR",       True),   # both NAV and NON-NAV → partially NAV → True
    ("yellowstone river",                 "MT",       True),
    ("yellowtail reservoir",              "MT",       True),
    ("youngs bay",                        "OR",       True),
    ("youngs river",                      "OR",       True),
]


# ── Normalisation ──────────────────────────────────────────────────────────

_STATE_RE = re.compile(
    r',\s*(or|wa|id|mt|ut|id/mt|wa/id|id/or/wa|wa/or|id/ut|id/or)\s*$',
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r'\s*\([^)]*\)')
_FORK_DIRECTIONS = ("north", "south", "east", "west")


def _normalize(name: str) -> str:
    """Lowercase, strip state suffix, normalize whitespace and apostrophes."""
    s = name.strip().lower()
    s = _STATE_RE.sub("", s)
    # Normalize all apostrophe variants to ASCII then strip
    for apos in ("’", "‘", "ʼ", "ʹ"):
        s = s.replace(apos, "\x27")
    s = re.sub(r"d\x27(\w)", r"d \1", s)  # d’X -> d X  (Coeur d’Alene etc.)
    s = s.replace("\x27", "")              # strip remaining apostrophes
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _candidate_forms(name: str):
    """
    Return a list of normalized name forms to try, from most to least specific.
    Handles parenthetical qualifiers and "North/South Fork X" ↔ "X North Fork" etc.
    """
    base = _normalize(name)
    forms = [base]

    # Without parenthetical: "Bear Creek (Coos County)" → "bear creek"
    stripped = _PAREN_RE.sub("", base).strip()
    if stripped != base:
        forms.append(stripped)

    # Fork inversion: "North Fork Alsea River" → "alsea river north fork"
    # and "alsea river north fork" → "north fork alsea river"
    words = base.split()
    if len(words) >= 3:
        # "north fork X Y" → "X Y north fork"
        if words[0] in _FORK_DIRECTIONS and words[1] == "fork":
            direction = words[0]
            rest = " ".join(words[2:])
            forms.append(f"{rest} {direction} fork")
            forms.append(rest)  # bare name without fork as last resort
        # "X Y north fork" → "north fork X Y"
        elif len(words) >= 3 and words[-2] in _FORK_DIRECTIONS and words[-1] == "fork":
            direction = words[-2]
            rest = " ".join(words[:-2])
            forms.append(f"{direction} fork {rest}")
            forms.append(rest)

    # "X River" → try "X" alone for short names (e.g. "McKenzie" in PDF)
    if words[-1] in ("river", "creek", "slough", "lake", "bay", "reservoir", "canal"):
        forms.append(" ".join(words[:-1]))

    # De-duplicate while preserving order
    seen, unique = set(), []
    for f in forms:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


# ── Lookup ─────────────────────────────────────────────────────────────────

def lookup(name: str, state: str = "OR") -> str | None:
    """
    Look up a waterway name in the USCG 13th District navigability list.

    Args:
        name:  Waterway name as it appears in BrM or on plans.
        state: Two-letter state code for disambiguation (default "OR" for ODOT).

    Returns:
        "Y"  — NAV CG determination (navigable under USCG jurisdiction).
        "N"  — NON-NAV CG determination (explicitly not navigable).
        None — Not listed, or ambiguous (conflicting determinations).
    """
    if not name or not name.strip():
        return None

    state_upper = state.upper()

    for form in _candidate_forms(name):
        # 1. State-specific match
        state_hits = [
            nav for n, s, nav in _ENTRIES
            if n == form and state_upper in s.upper().split("/")
        ]
        if state_hits:
            if all(v == state_hits[0] for v in state_hits):
                return "Y" if state_hits[0] else "N"
            return None  # conflicting entries for this state

        # 2. Multi-state entries that include the requested state
        multi_hits = [
            nav for n, s, nav in _ENTRIES
            if n == form and state_upper in s.upper()
        ]
        if multi_hits:
            if all(v == multi_hits[0] for v in multi_hits):
                return "Y" if multi_hits[0] else "N"
            return None

        # 3. Any-state match (fallback — use with caution for state-specific waterways)
        any_hits = [nav for n, _s, nav in _ENTRIES if n == form]
        if any_hits:
            if all(v == any_hits[0] for v in any_hits):
                return "Y" if any_hits[0] else "N"
            return None  # conflicting across states

    return None  # not found


def lookup_with_reasoning(name: str, state: str = "OR") -> tuple[str | None, str]:
    """
    Like lookup(), but also returns a human-readable reasoning string.
    """
    result = lookup(name, state)
    norm_forms = _candidate_forms(name)
    matched_form = None
    for form in norm_forms:
        if any(n == form for n, _s, _nav in _ENTRIES):
            matched_form = form
            break

    if result == "Y":
        return result, f"Found in USCG 13th District NAV CG list (matched as '{matched_form}')"
    elif result == "N":
        return result, f"Found in USCG 13th District NON-NAV CG list (matched as '{matched_form}')"
    elif matched_form:
        return None, f"Found in USCG list as '{matched_form}' but entries conflict — manual review needed"
    else:
        return None, "Not found in USCG 13th District navigability list"
