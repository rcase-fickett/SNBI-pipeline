PRIMARY = "PRIMARY"
WORK    = "WORK"
FEATURE = "FEATURE"

HIGH             = "HIGH"
APPROX           = "APPROX"
FIELD_REQ        = "FIELD_REQ"
NA               = "NA"
PENDING          = "PENDING"
UNABLE_TO_PARSE  = "UNABLE_TO_PARSE"

PAGE_PLAN      = "PLAN"
PAGE_SECTION   = "SECTION"
PAGE_RAIL      = "RAIL"
PAGE_NOTES     = "NOTES"
PAGE_VICINITY  = "VICINITY"
PAGE_BENT      = "BENT"
PAGE_CLEARANCE = "CLEARANCE"

ITEMS = [
    dict(id="B.CL.04", name="Historic Significance",                      table=PRIMARY,  brm_col="HistoricalSignificance", page_types=[],                                errata_pages=[],          notes="Always sourced from BrM — never found on plans. Phase 2 skips this item."),
    dict(id="B.G.01",  name="NBIS Bridge Length",                         table=PRIMARY,  brm_col="NBISLength",             page_types=[PAGE_PLAN, PAGE_VICINITY],         errata_pages=[120],       notes="Measure along roadway centerline between undercopings of abutments. For arches: between spring lines. For filled/closed-spandrel arches: inside faces of exterior spring lines. For bridges under fill: inside faces of exterior walls. Vaulted abutments and enclosed spans/sections are included. Do NOT copy the BrM NBISLength value directly — verify the measurement against plan dimensions. When B.G.02 < 30 ft, leave FIELD_REQ (field measure required). When B.G.02 >= 30 ft, estimate from plans using the difference between B.G.02 or B.G.03 and the NBIS bridge definition (opening > 20 ft measured along roadway centerline)."),
    dict(id="B.G.05",  name="Bridge Width Out-to-Out",                    table=PRIMARY,  brm_col="WidthOutToOut",          page_types=[PAGE_PLAN, PAGE_SECTION],          errata_pages=[133],       notes="Minimum out-to-out width perpendicular to CL, nearest tenth of a foot. Prioritize measuring from plan cross-sections — do not accept BrM value uncritically. Double-deck bridges inventoried as one: report sum of all levels. Bridges under fill: measure out-to-out of headwalls or barrel ends. Sidehill bridges: measure out-to-out structure width. Multiple service types (highway + railroad + pedestrian): out-to-out width encompassing all service types. When headwalls are not parallel (bridges under fill): report minimum out-to-out width."),
    dict(id="B.G.07",  name="Left Curb or Sidewalk Width",                table=PRIMARY,  brm_col="LeftCurbWidth",          page_types=[PAGE_SECTION, PAGE_PLAN],          errata_pages=[140],       notes="Minimum width from face of bridge rail to face of curb, perpendicular to CL, nearest tenth of a foot. Measure from plan cross-sections — BrM value is a fallback only. Report 0.0 when curb face does not extend beyond rail face, or when there is no left curb/sidewalk. Left is determined by direction of inventoried route (commonly west-to-east or south-to-north). When a longitudinal joint separates curb and sidewalk (e.g. granite curb + concrete sidewalk), measure to face of the granite curb only."),
    dict(id="B.G.08",  name="Right Curb or Sidewalk Width",               table=PRIMARY,  brm_col="RightCurbWidth",         page_types=[PAGE_SECTION, PAGE_PLAN],          errata_pages=[142],       notes="Minimum width from face of bridge rail to face of curb, perpendicular to CL, nearest tenth of a foot. Measure from plan cross-sections — BrM value is a fallback only. Report 0.0 when curb face does not extend beyond rail face, or when there is no right curb/sidewalk. Right is determined by direction of inventoried route (commonly west-to-east or south-to-north). When a longitudinal joint separates curb and sidewalk, measure to face of the curb (not the sidewalk edge)."),
    dict(id="B.G.10",  name="Bridge Median",                              table=PRIMARY,  brm_col="Median",                 page_types=[PAGE_SECTION, PAGE_PLAN],          errata_pages=[146],       notes="Determine from plan cross-sections — BrM value is a fallback only. Codes: 0=No median (including lanes separated only by centerline/edge/channelization striping), 1=Open median (traffic cannot safely traverse the gap), 2=Closed mountable (flush or mountable curb/barrier, including striped medians), 3=Closed non-mountable (barrier/curb > 6 inches high). For bridges with a longitudinal joint: use code 1 if traffic cannot safely traverse the joint; otherwise use codes 0/2/3 based on median type — joint condition alone does not change the code."),
    dict(id="B.G.12",  name="Curved Bridge",                              table=PRIMARY,  brm_col="CurvedBridge",           page_types=[PAGE_PLAN],                        errata_pages=[148],       notes="Determine from plan view girder geometry — BrM value is a fallback only. Codes: CU=at least one girder line is curved, CP=piecewise straight girders whose axis changes orientation at supports (segmented/chorded curve), CK=kinked girders (axis changes orientation between supports), N=not curved. Key rule: use N if deck geometry is curved or striped as curved but girders do not form a curve. Diaphragms and cross-frames in horizontally curved bridges are primary structural members."),
    dict(id="B.G.13",  name="Maximum Bridge Height",                      table=PRIMARY,  brm_col="MaxHeight",              page_types=[PAGE_PLAN, PAGE_VICINITY],         errata_pages=[150],       notes="Deck top to water surface or ground line (larger). Nearest foot."),
    dict(id="B.G.14",  name="Sidehill Bridge",                            table=PRIMARY,  brm_col="SidehillBridge",         page_types=[PAGE_PLAN, PAGE_SECTION],          errata_pages=[152],       notes="Y if roadway partially on structure and partially on cut/fill."),
    dict(id="B.RH.01", name="Bridge Railings",                            table=PRIMARY,  brm_col=None,                     page_types=[PAGE_RAIL, PAGE_SECTION, PAGE_PLAN], errata_pages=[113, 114], notes="DO NOT use BrM value — it is always unreliable for this item. Identify railing type from plans (railing details sheet), then look up its crash-test status. Covers all bridge railings: parapets, median barriers, structure-mounted railings, and railings over culverts. When multiple railing types are present, use the highest-ranked code going from bottom to top of Table 6. Code format (4 chars): MYYY=MASH (YY=last 2 digits of publication year, Y=test level, e.g. M093=MASH 2009 TL-3), 35YY=NCHRP 350 (e.g. 3504=NCHRP 350 TL-4), SYY=agency standard (not crash-tested), I=no crash-test info known or overlay changed rail height, N=not required, 0=required but absent. Default to I when railing type is visible but crash-test status cannot be determined from plans."),
    dict(id="B.RH.02", name="Transitions",                                table=PRIMARY,  brm_col=None,                     page_types=[PAGE_RAIL, PAGE_NOTES],            errata_pages=[113, 115],  notes="DO NOT use BrM value — it is always unreliable for this item. Transition railing connects the roadside approach guardrail to the bridge railing; it must be firmly attached and anchored to develop full tension on impact. Identify transition type from plans (railing details or notes sheet). Same Table 6 codes as B.RH.01 (MYYY, 35YY, SYY, I, N, 0). When multiple transition types are present, use the highest-ranked code. Use code I when no crash-test info is known, or when an overlay has changed the rail height from original geometry. For one-way traffic bridges where only a departure-end connection is needed (not a full warranted transition), the departure-end crash-test level does not need to be reported if it is lower than the approach end."),
    dict(id="B.W.01",  name="Year Built",                                 table=PRIMARY,  brm_col="YearBuilt",              page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], errata_pages=[337],    notes="Original construction year. Does NOT change for widening/rehab if original elements remain."),

    dict(id="B.W.02",  name="Year Work Performed",                        table=WORK,     brm_col=None,                     page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], errata_pages=[338],    notes="Plan dates are DESIGN dates. Look for revision blocks, as-constructed stamps."),
    dict(id="B.W.03",  name="Work Performed",                             table=WORK,     brm_col=None,                     page_types=[PAGE_PLAN, PAGE_NOTES],            errata_pages=[339],       notes="SNBI codes: BR1, SP1/SP2/SP3, SB1/SB2/SB3, DK1-7, IP1-4, RT1/RT2. Pipe-delimited."),

    dict(id="B.F.01",  name="Feature Type",                               table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN, PAGE_NOTES],            errata_pages=[160],       notes="H##=Highway, R##=Railroad, P##=Pathway, W##=Waterway, F##=Relief for waterway, B##=Urban feature, D##=Dry terrain/side slope, X##=Other. Carried-on features numbered first."),
    dict(id="B.F.02",  name="Feature Location",                           table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN],                        errata_pages=[162],       notes="C=carried on, B=below, A=above."),
    dict(id="B.F.03",  name="Feature Name",                               table=FEATURE,  brm_col=None, applies_to="ALL",   page_types=[PAGE_PLAN, PAGE_NOTES, PAGE_VICINITY], errata_pages=[163],    notes="Common name(s), pipe-delimited. Route number first."),
    dict(id="B.H.08",  name="Lanes on Highway",                           table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_SECTION, PAGE_PLAN],          errata_pages=[180],       notes="All full-width traffic lanes. Not sidewalks/bike paths."),
    dict(id="B.H.12",  name="Highway Max Usable Vertical Clearance",      table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        errata_pages=[184],       notes="BELOW bridge only. 10-ft envelope maximum. 99.9 if carried on."),
    dict(id="B.H.13",  name="Highway Minimum Vertical Clearance",         table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        errata_pages=[186],       notes="BELOW bridge only. Over traveled way + shoulders. 99.9 if carried on."),
    dict(id="B.H.14",  name="Highway Min Horizontal Clearance Left",      table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        errata_pages=[188],       notes="BELOW bridge only. Not reported for carried features. 0 for undivided 2-way."),
    dict(id="B.H.15",  name="Highway Min Horizontal Clearance Right",     table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        errata_pages=[191],       notes="BELOW bridge only. Not reported for carried features."),
    dict(id="B.H.16",  name="Highway Max Usable Surface Width",           table=FEATURE,  brm_col="WidthCurbToCurb", applies_to="H", page_types=[PAGE_SECTION, PAGE_PLAN], errata_pages=[194],       notes="Curb-to-curb including stabilized shoulders."),
    dict(id="B.H.18",  name="Crossing Bridge Number",                     table=FEATURE,  brm_col=None, applies_to="H",     page_types=[PAGE_PLAN, PAGE_VICINITY],         errata_pages=[198],       notes="Only when another bridge stacks directly above or below."),
    dict(id="B.N.01",  name="Navigable Waterway",                         table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_NOTES, PAGE_PLAN, PAGE_VICINITY], errata_pages=[209],    notes="Y/N/U. APPROX pre-filled by Phase 10 (USCG 13th District list). Look for USCG permit refs in plans → HIGH override."),
    dict(id="B.N.02",  name="Navigation Min Vertical Clearance",          table=FEATURE,  brm_col=None, applies_to="W",     page_types=[],                                 errata_pages=[],          needs_field=True, notes="Only if B.N.01=Y. Sourced from BrM (InfoBridge NBI 39) — plan extraction skipped. Field checkbox auto-ticked for inspector verification."),
    dict(id="B.N.03",  name="Movable Bridge Max Nav Vertical Clearance",  table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN],                        errata_pages=[212],       needs_field=True, notes="Only if B.N.01=Y AND movable bridge. Bascule/swing/tilt/pivot/retractable bridges pre-filled 999.9 (unlimited clearance). Lift bridges use InfoBridge NBI 116 value. Field verification always required."),
    dict(id="B.N.04",  name="Navigation Channel Width",                   table=FEATURE,  brm_col=None, applies_to="W",     page_types=[],                                 errata_pages=[],          needs_field=True, notes="Only if B.N.01=Y. Sourced from BrM (InfoBridge NBI 40) — plan extraction skipped. Field checkbox auto-ticked for inspector verification."),
    dict(id="B.N.05",  name="Navigation Channel Min Horizontal Clearance",table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN, PAGE_BENT],             errata_pages=[215],       needs_field=True, notes="Only if B.N.01=Y. Measure the minimum horizontal distance from the edge of the navigation channel to the nearest substructure unit (pier or abutment face). Three coding rules: (1) 9999.9 — if NO substructure units exist within the waterway (e.g. single-span bridge with both abutments on dry ground or bank), report 9999.9; this can usually be confirmed from the general plan view or bent drawings. (2) 0 — if the substructure units themselves form the lateral boundaries of the navigation channel (piers are the channel walls), report 0; this is difficult to confirm from plans alone — if you suspect this is the case, set plan_confidence=APPROX. (3) Measured value (ft) — if piers exist in the waterway but are set back from the channel edge, report the minimum horizontal clearance in feet. For ALL three codes, always include in plan_reasoning: 'Recommend obtaining USCG permit plans from the USCG Northwest District Office to confirm navigation channel limits.' When uncertain between codes, prefer 9999.9 if plans show no piers in the water, and flag 0 only when pier placement strongly suggests they bound the channel."),
    dict(id="B.N.06",  name="Substructure Navigation Protection",         table=FEATURE,  brm_col=None, applies_to="W",     page_types=[PAGE_PLAN, PAGE_BENT],             errata_pages=[217],       needs_field=True, notes="Only if B.N.01=Y. Field inspection usually required."),
    dict(id="B.RR.01", name="Railroad Service Type",                      table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN, PAGE_NOTES],            errata_pages=[201],       notes="F/P/M/FE/PE/ME/I."),
    dict(id="B.RR.02", name="Railroad Min Vertical Clearance",            table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN, PAGE_CLEARANCE],        errata_pages=[203],       notes="BELOW bridge only. Top of rail to lowest restriction."),
    dict(id="B.RR.03", name="Railroad Min Horizontal Offset",             table=FEATURE,  brm_col=None, applies_to="R",     page_types=[PAGE_PLAN],                        errata_pages=[205],       notes="BELOW bridge only. CL of track to nearest substructure face."),
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
