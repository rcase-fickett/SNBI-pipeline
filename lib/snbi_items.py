PRIMARY = "PRIMARY"
WORK    = "WORK"
FEATURE = "FEATURE"

HIGH      = "HIGH"
APPROX    = "APPROX"
FIELD_REQ = "FIELD_REQ"
NA        = "NA"
PENDING   = "PENDING"

PAGE_PLAN      = "PLAN"
PAGE_SECTION   = "SECTION"
PAGE_RAIL      = "RAIL"
PAGE_NOTES     = "NOTES"
PAGE_VICINITY  = "VICINITY"
PAGE_BENT      = "BENT"
PAGE_CLEARANCE = "CLEARANCE"

ITEMS = [
    dict(id="B.CL.04", name="Historic Significance",                      table=PRIMARY,  brm_col="HistoricalSignificance", page_types=[PAGE_NOTES, PAGE_PLAN],            notes="N=not eligible. 7=undetermined. Check BrM first."),
    dict(id="B.G.01",  name="NBIS Bridge Length",                         table=PRIMARY,  brm_col="NBISLength",             page_types=[PAGE_PLAN, PAGE_VICINITY],         notes="Undercoping to undercoping along CL. Field measure if <30 ft."),
    dict(id="B.G.05",  name="Bridge Width Out-to-Out",                    table=PRIMARY,  brm_col="WidthOutToOut",          page_types=[PAGE_PLAN, PAGE_SECTION],          notes="Min out-to-out perpendicular to CL."),
    dict(id="B.G.07",  name="Left Curb or Sidewalk Width",                table=PRIMARY,  brm_col="LeftCurbWidth",          page_types=[PAGE_SECTION, PAGE_PLAN],          notes="Rail face to curb face. 0.0 if none. Check asymmetric sections carefully."),
    dict(id="B.G.08",  name="Right Curb or Sidewalk Width",               table=PRIMARY,  brm_col="RightCurbWidth",         page_types=[PAGE_SECTION, PAGE_PLAN],          notes="Rail face to curb face. 0.0 if none."),
    dict(id="B.G.10",  name="Bridge Median",                              table=PRIMARY,  brm_col="Median",                 page_types=[PAGE_SECTION, PAGE_PLAN],          notes="0=none, 1=open, 2=mountable, 3=non-mountable."),
    dict(id="B.G.12",  name="Curved Bridge",                              table=PRIMARY,  brm_col="CurvedBridge",           page_types=[PAGE_PLAN],                        notes="CU/CP/CK/N. Based on girder geometry not deck striping."),
    dict(id="B.G.13",  name="Maximum Bridge Height",                      table=PRIMARY,  brm_col="MaxHeight",              page_types=[PAGE_PLAN, PAGE_VICINITY],         notes="Deck top to water surface or ground line (larger). Nearest foot."),
    dict(id="B.G.14",  name="Sidehill Bridge",                            table=PRIMARY,  brm_col="SidehillBridge",         page_types=[PAGE_PLAN, PAGE_SECTION],          notes="Y if roadway partially on structure and partially on cut/fill."),
    dict(id="B.RH.01", name="Bridge Railings",                            table=PRIMARY,  brm_col="Railings",               page_types=[PAGE_RAIL, PAGE_SECTION, PAGE_PLAN], notes="NCHRP350/MASH crash-test code. I=unknown."),
    dict(id="B.RH.02", name="Transitions",                                table=PRIMARY,  brm_col="Transitions",            page_types=[PAGE_RAIL, PAGE_NOTES],            notes="Crash-test code for transitions. I=unknown."),
    dict(id="B.W.01",  name="Year Built",                                 table=PRIMARY,  brm_col="YearBuilt",              page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], notes="Original construction year. Does NOT change for widening/rehab if original elements remain."),

    dict(id="B.W.02",  name="Year Work Performed",                        table=WORK,     brm_col=None,                     page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], notes="Plan dates are DESIGN dates. Look for revision blocks, as-constructed stamps."),
    dict(id="B.W.03",  name="Work Performed",                             table=WORK,     brm_col=None,                     page_types=[PAGE_PLAN, PAGE_NOTES],            notes="SNBI codes: BR1, SP1/SP2/SP3, SB1/SB2/SB3, DK1-7, IP1-4, RT1/RT2. Pipe-delimited."),

    dict(id="B.F.01",  name="Feature Type",                               table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN, PAGE_NOTES],            notes="H##=Highway, R##=Railroad, P##=Pathway, W##=Waterway, F##=Relief for waterway, B##=Urban feature, D##=Dry terrain/side slope, X##=Other. Carried-on features numbered first."),
    dict(id="B.F.02",  name="Feature Location",                           table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN],                        notes="C=carried on, B=below, A=above."),
    dict(id="B.F.03",  name="Feature Name",                               table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], notes="Common name(s), pipe-delimited. Route number first."),
    dict(id="B.H.08",  name="Lanes on Highway",                           table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_SECTION, PAGE_PLAN],          notes="All full-width traffic lanes. Not sidewalks/bike paths."),
    dict(id="B.H.12",  name="Highway Max Usable Vertical Clearance",      table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="BELOW bridge only. 10-ft envelope maximum. 99.9 if carried on."),
    dict(id="B.H.13",  name="Highway Minimum Vertical Clearance",         table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="BELOW bridge only. Over traveled way + shoulders. 99.9 if carried on."),
    dict(id="B.H.14",  name="Highway Min Horizontal Clearance Left",      table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="BELOW bridge only. Not reported for carried features. 0 for undivided 2-way."),
    dict(id="B.H.15",  name="Highway Min Horizontal Clearance Right",     table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="BELOW bridge only. Not reported for carried features."),
    dict(id="B.H.16",  name="Highway Max Usable Surface Width",           table=FEATURE,  brm_col="WidthCurbToCurb", applies_to="H", page_types=[PAGE_SECTION, PAGE_PLAN], notes="Curb-to-curb including stabilized shoulders."),
    dict(id="B.H.18",  name="Crossing Bridge Number",                     table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_VICINITY],         notes="Only when another bridge stacks directly above or below."),
    dict(id="B.N.01",  name="Navigable Waterway",                         table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_NOTES, PAGE_PLAN, PAGE_VICINITY], notes="Y/N/U. APPROX pre-filled by Phase 10 (USCG 13th District list). Look for USCG permit refs in plans → HIGH override."),
    dict(id="B.N.02",  name="Navigation Min Vertical Clearance",          table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="Only if B.N.01=Y. Requires USCG permit datum."),
    dict(id="B.N.03",  name="Movable Bridge Max Nav Vertical Clearance",  table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN],                        notes="Only if B.N.01=Y AND movable bridge."),
    dict(id="B.N.04",  name="Navigation Channel Width",                   table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="Only if B.N.01=Y. USCG permit plans or field measurement."),
    dict(id="B.N.05",  name="Navigation Channel Min Horizontal Clearance",table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN],                        notes="Only if B.N.01=Y. 0 if piers form channel limits."),
    dict(id="B.N.06",  name="Substructure Navigation Protection",         table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN, PAGE_BENT],             notes="Only if B.N.01=Y. Field inspection usually required."),
    dict(id="B.RR.01", name="Railroad Service Type",                      table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN, PAGE_NOTES],            notes="F/P/M/FE/PE/ME/I."),
    dict(id="B.RR.02", name="Railroad Min Vertical Clearance",            table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        notes="BELOW bridge only. Top of rail to lowest restriction."),
    dict(id="B.RR.03", name="Railroad Min Horizontal Offset",             table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN],                        notes="BELOW bridge only. CL of track to nearest substructure face."),
]

ITEM_BY_ID    = {i["id"]: i for i in ITEMS}
PRIMARY_ITEMS = [i for i in ITEMS if i["table"] == PRIMARY]
WORK_ITEMS    = [i for i in ITEMS if i["table"] == WORK]
FEATURE_ITEMS = [i for i in ITEMS if i["table"] == FEATURE]

DOC_TYPE_ROUTING = {
    "plan":         PAGE_PLAN,
    "elevation":    PAGE_PLAN,
    "vicinity":     PAGE_VICINITY,
    "profile":      PAGE_VICINITY,
    "typical":      PAGE_SECTION,
    "section":      PAGE_SECTION,
    "deck":         PAGE_SECTION,
    "rail":         PAGE_RAIL,
    "railing":      PAGE_RAIL,
    "barrier":      PAGE_RAIL,
    "general note": PAGE_NOTES,
    "notes":        PAGE_NOTES,
    "bent":         PAGE_BENT,
    "abutment":     PAGE_BENT,
    "pier":         PAGE_BENT,
    "substructure": PAGE_BENT,
    "clearance":    PAGE_CLEARANCE,
}

def classify_doc_type(doc_type_str):
    if not doc_type_str:
        return PAGE_PLAN
    lower = doc_type_str.lower()
    for keyword, page_type in DOC_TYPE_ROUTING.items():
        if keyword in lower:
            return page_type
    return PAGE_PLAN

def classify_feature(feature_intersected):
    if not feature_intersected:
        return "UNKNOWN"
    v = str(feature_intersected).upper()
    if any(x in v for x in ["HWY","I-5","I-84","I-205","I-405","I-82","I-90",
                              "BLVD","ST ","AVE","RD ","DR ",
                              "PKWY","ROUTE","RAMP","STREET","ROAD","LANE"]):
        return "HIGHWAY"
    if any(x in v for x in ["UPRR","BNSF","RR","RAILROAD","RAIL","RAILWAY","SP ","AMTRAK"]):
        return "RAILROAD"
    if any(x in v for x in ["SIDEWALK","PATH","TRAIL","BIKE","PEDESTRIAN","WALKWAY",
                              "MULTI-USE","MULTI USE","GREENWAY"]):
        return "PATHWAY"
    if any(x in v for x in ["CREEK","RIVER","STREAM","CANAL","DITCH","SLOUGH",
                              "FORK","BRANCH","DRAINAGE","POND","LAKE","RUN "]):
        return "WATERWAY"
    return "WATERWAY"
