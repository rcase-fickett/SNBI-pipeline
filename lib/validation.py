"""
lib/validation.py — SNBI data validation engine.

Checks evidence values against FHWA SNBI Data Validation Rules for items on
the SNBI Item Guide (lib/snbi_items.py).

Severity levels (per FHWA SNBI_Data_Validation_Rules.xlsx):
  Safety   — structural safety issue (closed/posted check)
  Critical — data must be corrected and resubmitted within 15 business days
  Error    — confirm/correct prior to next year's submittal
  Flag     — verify not an error; correct if needed

validate_bridge(conn, bridge_id) → sorted list of violation dicts
  Each dict: {severity, rule_name, snbi_id, feature_id, description, explanation}
"""

import re as _re

from lib.snbi_items import ITEM_BY_ID

_SEV_ORDER = {"Safety": 0, "Critical": 1, "Error": 2, "Flag": 3}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _eff(row):
    """Effective value: user_determination (if filled) → plan_value → brm_value."""
    if row is None:
        return None
    ud = (row.get("user_determination") or "").strip()
    if ud:
        return ud
    pv = (row.get("plan_value") or "").strip()
    pc = (row.get("plan_confidence") or "").strip()
    if pv and pc not in ("PENDING", "NOT_FOUND", ""):
        return pv
    bv = (row.get("brm_value") or "").strip()
    return bv or None


def _is_numeric(val):
    try:
        float(str(val).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _is_int_in_range(val, lo, hi):
    try:
        v = int(float(str(val)))
        return lo <= v <= hi
    except (TypeError, ValueError):
        return False


def _v(severity, rule_name, snbi_id, feature_id, description, explanation=""):
    return {
        "severity":    severity,
        "rule_name":   rule_name,
        "snbi_id":     snbi_id,
        "feature_id":  feature_id,
        "description": description,
        "explanation": explanation or "",
    }


def _is_resolved(row):
    """True if the reviewer explicitly marked this item INCORRECT — treat as acknowledged."""
    return row is not None and (row.get("status") or "") == "INCORRECT"


def _null_check(fev, item_id, fid, severity, rule, desc, expl="", needs_field=False):
    """Violation if item row is absent or has no effective value.
    Returns [] immediately if the reviewer has marked the row INCORRECT."""
    row = fev.get(item_id)
    if _is_resolved(row):
        return []
    if row is None:
        sev = "Flag" if needs_field else severity
        return [_v(sev, rule + "-MISS", item_id, fid,
                   f"{desc} — item row is missing entirely.", expl)]
    val = _eff(row)
    if val is None:
        sev = "Flag" if needs_field else severity
        return [_v(sev, rule + "-NULL", item_id, fid,
                   f"{desc} — value is null or still pending.", expl)]
    return []


def _code_token(val):
    """
    Extract the leading code token from a value.
    BrM exports often include description text after the code, e.g. "0 No median" → "0",
    "N Not a sidehill bridge" → "N", "CU Curved" → "CU".
    Returns the first whitespace-delimited token in upper case.
    """
    if not val:
        return val
    return val.strip().split()[0].upper()


def _coded_check(fev, item_id, fid, severity, rule, desc, valid_vals, expl="", needs_field=False):
    """Null check + coded value range check. Accepts BrM values with appended descriptions."""
    viol = _null_check(fev, item_id, fid, severity, rule, desc, expl, needs_field)
    if viol:
        return viol
    raw = _eff(fev[item_id])
    tok = _code_token(raw)
    upper_valid = [v.upper() for v in valid_vals]
    if tok and tok not in upper_valid:
        return [_v(severity, rule + "-VAL", item_id, fid,
                   f"{desc} — '{raw}' is not valid. Accepted: {', '.join(valid_vals)}.", expl)]
    return []


def _numeric_check(fev, item_id, fid, severity, rule, desc, expl="",
                   allow_special=None, needs_field=False):
    """Null check + numeric format check. allow_special: extra non-numeric accepted values.
    Strips leading numeric token from BrM values that have appended descriptions."""
    viol = _null_check(fev, item_id, fid, severity, rule, desc, expl, needs_field)
    if viol:
        return viol
    raw = _eff(fev[item_id])
    if raw:
        tok      = raw.strip().split()[0]
        specials = [s.lower() for s in (allow_special or [])]
        if tok.lower() not in specials and not _is_numeric(tok):
            return [_v(severity, rule + "-NUM", item_id, fid,
                       f"{desc} — '{raw}' is not numeric.", expl)]
    return []


def _check_max(fev, item_id, fid, max_val, severity, rule, label, expl=""):
    """Return a violation if the numeric effective value exceeds max_val.
    Silently skips resolved, missing, null, or non-numeric rows (those are caught elsewhere)."""
    row = fev.get(item_id)
    if _is_resolved(row) or row is None:
        return []
    val = _eff(row)
    if not val:
        return []
    try:
        num = float(val.strip().split()[0].replace(",", ""))
        if num > max_val:
            max_str = f"{max_val:g}"
            return [_v(severity, rule, item_id, fid,
                       f"{label} value '{val}' exceeds the maximum of {max_str}.",
                       expl)]
    except (ValueError, TypeError):
        pass
    return []


def _check_one_decimal(fev, item_id, fid, severity, rule, label, expl="",
                       allow_special=None):
    """Return a violation if the effective numeric value does not have exactly one decimal place.
    Silently skips resolved, missing, null, non-numeric, and allow_special values
    (those are caught or exempted by other checks)."""
    row = fev.get(item_id)
    if _is_resolved(row) or row is None:
        return []
    val = _eff(row)
    if not val:
        return []
    tok = val.strip().split()[0]
    specials = [s.lower() for s in (allow_special or [])]
    if tok.lower() in specials:
        return []
    if not _is_numeric(tok):
        return []  # non-numeric already caught by _numeric_check
    if "." not in tok:
        return [_v(severity, rule, item_id, fid,
                   f"{label} value '{val}' must be reported to the nearest tenth "
                   f"(e.g. '{tok}.0').",
                   expl)]
    decimal_digits = tok.split(".")[1]
    if len(decimal_digits) != 1:
        return [_v(severity, rule, item_id, fid,
                   f"{label} value '{val}' must have exactly one decimal place "
                   "(nearest tenth of a foot).",
                   expl)]
    return []


# ── Bridge validation ─────────────────────────────────────────────────────────

def validate_bridge(conn, bridge_id):
    """
    Run all applicable SNBI validation rules for bridge_id.
    Returns a list of violation dicts, sorted Safety → Critical → Error → Flag.
    """
    bridge = conn.execute("SELECT * FROM bridges WHERE bridge_id=?",
                          (bridge_id,)).fetchone()
    if not bridge:
        return []
    bridge = dict(bridge)

    rows = conn.execute(
        "SELECT * FROM evidence WHERE bridge_id=?", (bridge_id,)
    ).fetchall()

    # Index: feature_id → {item_id: row_dict}
    by_feature = {}
    for row in rows:
        fid = row["feature_id"]
        if fid not in by_feature:
            by_feature[fid] = {}
        by_feature[fid][row["item_id"]] = dict(row)

    violations = []

    # ── Structural: at least one feature must be defined ─────────────────────
    non_primary = [f for f in by_feature
                   if f != "PRIMARY" and not f.startswith("WORK:")]
    if not non_primary:
        violations.append(_v(
            "Critical", "BF-STRUCT", "B.F.01", "",
            "No feature datasets found for this bridge.",
            "At least one Features dataset (B.F.01) must be reported for all bridges."
        ))

    # ── PRIMARY checks ────────────────────────────────────────────────────────
    pev = by_feature.get("PRIMARY", {})

    # B.G.01 NBIS Bridge Length — numeric; nearest tenth (1 decimal); 20 < value ≤ 999,999.9
    violations += _numeric_check(pev, "B.G.01", "PRIMARY", "Error", "BG01",
        "NBIS Bridge Length is null or not valid",
        "Must be numeric (measured along roadway centerline, nearest tenth of a foot).")
    violations += _check_one_decimal(pev, "B.G.01", "PRIMARY", "Error", "BG01-DEC",
        "NBIS Bridge Length",
        "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
    bg01_row = pev.get("B.G.01")
    if not _is_resolved(bg01_row):
        bg01_val = _eff(bg01_row)
        if bg01_val:
            try:
                bg01_num = float(bg01_val.strip().split()[0].replace(",", ""))
                # BG01-3 (Error): length ≤ 20 ft — may not be NBIS reportable
                if bg01_num <= 20:
                    violations.append(_v("Error", "BG01-3", "B.G.01", "PRIMARY",
                        f"NBIS Bridge Length '{bg01_val}' is ≤ 20 ft. Bridges shorter than 20 ft "
                        "may not be NBIS reportable.",
                        "NBIS bridge length must exceed 20 feet (6.1 m) to qualify for NBIS reporting."))
                # BG01-4 (Critical): length > 999,999.9 ft
                if bg01_num > 999999.9:
                    violations.append(_v("Critical", "BG01-4", "B.G.01", "PRIMARY",
                        f"NBIS Bridge Length '{bg01_val}' exceeds the maximum of 999,999.9 ft.",
                        "Numeric value must not exceed 999,999.9 feet."))
            except (ValueError, TypeError):
                pass

    # B.G.05 Bridge Width Out-to-Out — numeric; nearest tenth; > 0; max 999.9  (BG05-2 = Critical)
    violations += _numeric_check(pev, "B.G.05", "PRIMARY", "Critical", "BG05",
        "Bridge Width Out-to-Out is null or not valid",
        "Minimum out-to-out width perpendicular to CL, nearest tenth of a foot.")
    violations += _check_one_decimal(pev, "B.G.05", "PRIMARY", "Critical", "BG05-DEC",
        "Bridge Width Out-to-Out",
        "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
    bg05_row = pev.get("B.G.05")
    if not _is_resolved(bg05_row):
        bg05_val = _eff(bg05_row)
        if bg05_val:
            try:
                bg05_num = float(bg05_val.strip().split()[0].replace(",", ""))
                # BG05-2 (Critical): width must be > 0
                if bg05_num <= 0:
                    violations.append(_v("Critical", "BG05-2", "B.G.05", "PRIMARY",
                        f"Bridge Width Out-to-Out '{bg05_val}' must be greater than 0.",
                        "Out-to-out bridge width must be a positive value greater than zero."))
                # BG05-3 (Critical): width > 999.9 ft
                if bg05_num > 999.9:
                    violations.append(_v("Critical", "BG05-3", "B.G.05", "PRIMARY",
                        f"Bridge Width Out-to-Out '{bg05_val}' exceeds the maximum of 999.9 ft.",
                        "Numeric value must not exceed 999.9 feet."))
            except (ValueError, TypeError):
                pass

    # B.G.07 Left Curb or Sidewalk Width — numeric; nearest tenth; max 99.9
    violations += _numeric_check(pev, "B.G.07", "PRIMARY", "Error", "BG07",
        "Left Curb or Sidewalk Width is null or not valid",
        "Report 0.0 when there is no left curb or sidewalk.")
    violations += _check_one_decimal(pev, "B.G.07", "PRIMARY", "Error", "BG07-DEC",
        "Left Curb or Sidewalk Width",
        "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
    violations += _check_max(pev, "B.G.07", "PRIMARY", 99.9, "Critical", "BG07-3",
        "Left Curb or Sidewalk Width",
        "Numeric value must not exceed 99.9 feet.")

    # B.G.08 Right Curb or Sidewalk Width — numeric; nearest tenth; max 99.9
    violations += _numeric_check(pev, "B.G.08", "PRIMARY", "Error", "BG08",
        "Right Curb or Sidewalk Width is null or not valid",
        "Report 0.0 when there is no right curb or sidewalk.")
    violations += _check_one_decimal(pev, "B.G.08", "PRIMARY", "Error", "BG08-DEC",
        "Right Curb or Sidewalk Width",
        "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
    violations += _check_max(pev, "B.G.08", "PRIMARY", 99.9, "Critical", "BG08-3",
        "Right Curb or Sidewalk Width",
        "Numeric value must not exceed 99.9 feet.")

    violations += _coded_check(pev, "B.G.10", "PRIMARY", "Error", "BG10",
        "Bridge Median code is null or not valid",
        ["0", "1", "2", "3"],
        "0=No median, 1=Open, 2=Closed mountable, 3=Closed non-mountable.")

    violations += _coded_check(pev, "B.G.12", "PRIMARY", "Error", "BG12",
        "Curved Bridge code is null or not valid",
        ["CU", "CP", "CK", "N"],
        "CU=Curved, CP=Piecewise straight (chorded), CK=Kinked, N=Not curved.")

    # B.G.13 Maximum Bridge Height — must be a whole number (nearest foot, no decimals)
    violations += _numeric_check(pev, "B.G.13", "PRIMARY", "Error", "BG13",
        "Maximum Bridge Height is null or not valid",
        "Deck top to water surface or ground line, nearest foot.")
    bg13_row = pev.get("B.G.13")
    if not _is_resolved(bg13_row):
        bg13_val = _eff(bg13_row)
        if bg13_val:
            try:
                bg13_num = float(bg13_val.strip().split()[0].replace(",", ""))
                # BG13-3 (Error): must be a whole number — reported to nearest foot
                if bg13_num != int(bg13_num):
                    violations.append(_v("Error", "BG13-3", "B.G.13", "PRIMARY",
                        f"Maximum Bridge Height '{bg13_val}' must be a whole number (nearest foot, "
                        "no decimal places).",
                        "B.G.13 is reported to the nearest foot. Decimal values are not valid."))
            except (ValueError, TypeError):
                pass

    violations += _coded_check(pev, "B.G.14", "PRIMARY", "Error", "BG14",
        "Sidehill Bridge code is null or not valid",
        ["Y", "N"],
        "Y if roadway is partially on structure and partially on cut/fill.")

    violations += _coded_check(pev, "B.CL.04", "PRIMARY", "Error", "BCL04",
        "Historic Significance code is null or not valid",
        ["1", "2", "3", "4", "5", "6", "7", "N", "T"],
        "Valid codes: 1–7, N (not eligible), or T (temporary, accepted through 2027).")

    violations += _null_check(pev, "B.RH.01", "PRIMARY", "Error", "BRH01",
        "Bridge Railings code is null or missing",
        "Must identify railing crash-test status. Codes: M###, 35##, S##, I, N, 0.")

    violations += _null_check(pev, "B.RH.02", "PRIMARY", "Error", "BRH02",
        "Transitions code is null or missing",
        "Must identify transition railing crash-test status. Same codes as B.RH.01.")

    # B.W.01 Year Built — 4-digit year (BrM may export "1962 Year built" etc.)
    bw01_row = pev.get("B.W.01")
    if not _is_resolved(bw01_row):
        if bw01_row is None:
            violations.append(_v("Error", "BW01-MISS", "B.W.01", "PRIMARY",
                "Year Built (B.W.01) row is missing.",
                "Original construction year must be reported."))
        else:
            raw = _eff(bw01_row)
            if raw is None:
                violations.append(_v("Error", "BW01-NULL", "B.W.01", "PRIMARY",
                    "Year Built (B.W.01) is null or still pending.",
                    "Original construction year must be reported."))
            else:
                tok = raw.strip().split()[0]
                if not _is_int_in_range(tok, 1700, 2100):
                    violations.append(_v("Error", "BW01-VAL", "B.W.01", "PRIMARY",
                        f"Year Built value '{raw}' is not a valid calendar year (1700–2100).",
                        "Must be a 4-digit calendar year."))

    # ── Feature-level checks ─────────────────────────────────────────────────
    _VALID_FID = _re.compile(r'^[HWRP]\d+$')
    feature_ids = sorted(f for f in by_feature
                         if f != "PRIMARY"
                         and not f.startswith("WORK:")
                         and _VALID_FID.match(f))

    for fid in feature_ids:
        fev    = by_feature[fid]
        ftype  = fid[0] if fid else ""

        # B.F.01 Feature Type — must exist
        violations += _null_check(fev, "B.F.01", fid, "Critical", "BF01",
            f"Feature {fid}: Feature Type (B.F.01) is missing",
            "B.F.01 must have a value for every feature.")

        # B.F.02 Feature Location — C/B/A
        violations += _coded_check(fev, "B.F.02", fid, "Critical", "BF02",
            f"Feature {fid}: Feature Location (B.F.02) is null or not valid",
            ["C", "B", "A"],
            "C=Carried on bridge, B=Below bridge, A=Above bridge.")

        # B.F.03 Feature Name — must exist; max 300 characters (BF03-2)
        violations += _null_check(fev, "B.F.03", fid, "Error", "BF03",
            f"Feature {fid}: Feature Name (B.F.03) is missing",
            "Common name(s) must be entered for all features.")
        bf03_row = fev.get("B.F.03")
        if not _is_resolved(bf03_row) and bf03_row is not None:
            bf03_val = _eff(bf03_row)
            if bf03_val and len(bf03_val) > 300:
                violations.append(_v("Error", "BF03-2", "B.F.03", fid,
                    f"Feature {fid}: Feature Name (B.F.03) is {len(bf03_val)} characters, "
                    "exceeding the 300-character maximum.",
                    "Feature name must not exceed 300 characters."))

        # ── H* feature checks ────────────────────────────────────────────────
        if ftype == "H":
            bf02_val   = _eff(fev.get("B.F.02"))
            is_carried = (bf02_val == "C")
            is_below   = (bf02_val == "B")

            # B.H.18: if set for THIS feature, clearance/lane items are not required
            # for that same feature (BH08-2, BH12-2, BH13-2, BH14-2, BH15-2, BH16-2).
            # Evaluated per-feature — other H features in the same bridge are unaffected.
            bh18_val = _eff(fev.get("B.H.18"))
            bh18_set = bool(bh18_val)

            # B.H.08 Lanes on Highway — required, integer 0-99
            # Critical for carried-on (BH08-1); Error for below-bridge (BH08-1a)
            bh08_row = fev.get("B.H.08")
            if not bh18_set and not _is_resolved(bh08_row):
                bh08_sev = "Critical" if is_carried else "Error"
                if bh08_row is None:
                    violations.append(_v(bh08_sev, "BH08-MISS", "B.H.08", fid,
                        f"Feature {fid}: Lanes on Highway (B.H.08) row is missing.",
                        "Lanes on highway is required. Valid range: 0–99."))
                else:
                    val = _eff(bh08_row)
                    if val is None:
                        violations.append(_v(bh08_sev, "BH08-NULL", "B.H.08", fid,
                            f"Feature {fid}: Lanes on Highway (B.H.08) is null or pending.",
                            "Lanes on highway is required. Valid range: 0–99."))
                    elif not _is_int_in_range(val, 0, 99):
                        violations.append(_v("Error", "BH08-VAL", "B.H.08", fid,
                            f"Feature {fid}: Lanes on Highway '{val}' is not valid (must be integer 0–99).",
                            "Lanes on highway must be a whole number between 0 and 99."))

            # B.H.12 Max Usable Vertical Clearance — numeric; nearest tenth; max 99.9
            # 99.9 expected for carried-on features
            bh12_row = fev.get("B.H.12")
            if not bh18_set and not _is_resolved(bh12_row):
                if bh12_row is None:
                    violations.append(_v("Error", "BH12-MISS", "B.H.12", fid,
                        f"Feature {fid}: Highway Max Usable Vertical Clearance (B.H.12) row is missing.",
                        "Required. Use 99.9 for carried-on features."))
                else:
                    val = _eff(bh12_row)
                    if val and not _is_numeric(val):
                        violations.append(_v("Error", "BH12-NUM", "B.H.12", fid,
                            f"Feature {fid}: B.H.12 value '{val}' is not numeric.",
                            "Must be numeric with 1 decimal place. Use 99.9 for carried-on features."))
                    elif is_carried and val and _code_token(val) != "99.9":
                        violations.append(_v("Flag", "BH12-CARR", "B.H.12", fid,
                            f"Feature {fid}: B.H.12 = '{val}' but feature is carried-on (B.F.02=C). Expected 99.9.",
                            "Highway Max Usable Vertical Clearance should be 99.9 for carried-on features."))
                    elif val:
                        try:
                            if float(val.strip().split()[0]) > 99.9:
                                violations.append(_v("Critical", "BH12-4", "B.H.12", fid,
                                    f"Feature {fid}: B.H.12 value '{val}' exceeds the maximum of 99.9 ft.",
                                    "Highway Max Usable Vertical Clearance must not exceed 99.9 feet."))
                        except (ValueError, TypeError):
                            pass
            if not bh18_set:
                violations += _check_one_decimal(fev, "B.H.12", fid, "Error", "BH12-DEC",
                    f"Feature {fid}: Highway Max Usable Vertical Clearance (B.H.12)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).")

            # B.H.13 Min Vertical Clearance — numeric; nearest tenth; max 99.9
            # 99.9 expected for carried-on features
            bh13_row = fev.get("B.H.13")
            if not bh18_set and not _is_resolved(bh13_row):
                if bh13_row is None:
                    violations.append(_v("Error", "BH13-MISS", "B.H.13", fid,
                        f"Feature {fid}: Highway Min Vertical Clearance (B.H.13) row is missing.",
                        "Required. Use 99.9 for carried-on features."))
                else:
                    val = _eff(bh13_row)
                    if val and not _is_numeric(val):
                        violations.append(_v("Error", "BH13-NUM", "B.H.13", fid,
                            f"Feature {fid}: B.H.13 value '{val}' is not numeric.",
                            "Must be numeric. Use 99.9 for carried-on features."))
                    elif is_carried and val and _code_token(val) != "99.9":
                        violations.append(_v("Flag", "BH13-CARR", "B.H.13", fid,
                            f"Feature {fid}: B.H.13 = '{val}' but feature is carried-on (B.F.02=C). Expected 99.9.",
                            "Highway Min Vertical Clearance should be 99.9 for carried-on features."))
                    elif val:
                        try:
                            if float(val.strip().split()[0]) > 99.9:
                                violations.append(_v("Critical", "BH13-4", "B.H.13", fid,
                                    f"Feature {fid}: B.H.13 value '{val}' exceeds the maximum of 99.9 ft.",
                                    "Highway Min Vertical Clearance must not exceed 99.9 feet."))
                        except (ValueError, TypeError):
                            pass
            if not bh18_set:
                violations += _check_one_decimal(fev, "B.H.13", fid, "Error", "BH13-DEC",
                    f"Feature {fid}: Highway Min Vertical Clearance (B.H.13)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).")

            # B.H.14 / B.H.15 — required (numeric; nearest tenth; max 99.9) for below;
            # N/A for carried-on
            for itm, lbl in [("B.H.14", "Min Horizontal Clearance Left"),
                              ("B.H.15", "Min Horizontal Clearance Right")]:
                row  = fev.get(itm)
                rtag = itm.replace(".", "")
                if bh18_set or _is_resolved(row):
                    pass
                elif is_below:
                    if row is None:
                        violations.append(_v("Error", rtag + "-MISS", itm, fid,
                            f"Feature {fid}: {lbl} ({itm}) row is missing for below-bridge feature.",
                            "Required for below-highway features. Must be numeric (feet)."))
                    else:
                        val = _eff(row)
                        if val and not _is_numeric(val) and not val.lower().startswith("not reported"):
                            violations.append(_v("Error", rtag + "-NUM", itm, fid,
                                f"Feature {fid}: {itm} value '{val}' is not numeric.",
                                "Must be a decimal number in feet for below-highway features."))
                        elif val and _is_numeric(val.strip().split()[0]):
                            try:
                                if float(val.strip().split()[0]) > 99.9:
                                    violations.append(_v("Critical", rtag + "-4", itm, fid,
                                        f"Feature {fid}: {itm} value '{val}' exceeds the maximum of 99.9 ft.",
                                        f"{lbl} must not exceed 99.9 feet."))
                            except (ValueError, TypeError):
                                pass
                    # Decimal check only applies to numeric (non "Not reported") values
                    violations += _check_one_decimal(fev, itm, fid, "Error", rtag + "-DEC",
                        f"Feature {fid}: {lbl} ({itm})",
                        "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
                elif is_carried and row is not None:
                    val = _eff(row)
                    if val and _is_numeric(val):
                        violations.append(_v("Flag", rtag + "-CARR", itm, fid,
                            f"Feature {fid}: {itm} = '{val}' (numeric) but feature is carried-on. "
                            "Expected 'Not reported (carried-on feature)'.",
                            "Horizontal clearances do not apply for carried-on highway features."))

            # B.H.16 Max Usable Surface Width — numeric; nearest tenth; max 99.9
            if not bh18_set:
                violations += _numeric_check(fev, "B.H.16", fid, "Error", "BH16",
                    f"Feature {fid}: Highway Max Usable Surface Width (B.H.16) is null or not valid",
                    "Curb-to-curb width including stabilized shoulders, nearest tenth of a foot.")
                violations += _check_one_decimal(fev, "B.H.16", fid, "Error", "BH16-DEC",
                    f"Feature {fid}: Highway Max Usable Surface Width (B.H.16)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
                violations += _check_max(fev, "B.H.16", fid, 99.9, "Critical", "BH16-4",
                    f"Feature {fid}: Highway Max Usable Surface Width (B.H.16)",
                    "Highway Max Usable Surface Width must not exceed 99.9 feet.")

        # ── W* feature checks ─────────────────────────────────────────────────
        elif ftype == "W":
            bn01_row = fev.get("B.N.01")
            bn01_val = _eff(bn01_row)

            # B.N.01 Navigable Waterway — required, Y/N/U
            if not _is_resolved(bn01_row):
                if bn01_row is None:
                    violations.append(_v("Critical", "BN01-MISS", "B.N.01", fid,
                        f"Feature {fid}: Navigable Waterway (B.N.01) row is missing.",
                        "At least one navigation dataset must be submitted for all waterway features. "
                        "Valid values: Y, N, or U."))
                else:
                    if bn01_val is None:
                        violations.append(_v("Critical", "BN01-NULL", "B.N.01", fid,
                            f"Feature {fid}: Navigable Waterway (B.N.01) is null or pending.",
                            "Valid values: Y (navigable), N (non-navigable), U (unknown)."))
                    elif bn01_val.upper() not in ("Y", "N", "U"):
                        violations.append(_v("Error", "BN01-VAL", "B.N.01", fid,
                            f"Feature {fid}: B.N.01 value '{bn01_val}' is not valid.",
                            "Valid values: Y (navigable), N (non-navigable), U (unknown)."))

            # B.N.02–06 required when B.N.01=Y; skip when N or when B.N.01 is resolved
            if not _is_resolved(bn01_row) and bn01_val and bn01_val.upper() == "Y":
                # B.N.02 Navigation Min Vertical Clearance — numeric; nearest tenth; max 999.9
                violations += _numeric_check(fev, "B.N.02", fid, "Error", "BN02",
                    f"Feature {fid}: Navigation Min Vertical Clearance (B.N.02) is null or not valid",
                    "Required when B.N.01=Y. Must be numeric (feet).",
                    needs_field=True)
                violations += _check_one_decimal(fev, "B.N.02", fid, "Error", "BN02-DEC",
                    f"Feature {fid}: Navigation Min Vertical Clearance (B.N.02)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
                violations += _check_max(fev, "B.N.02", fid, 999.9, "Critical", "BN02-2",
                    f"Feature {fid}: Navigation Min Vertical Clearance (B.N.02)",
                    "Navigation Min Vertical Clearance must not exceed 999.9 feet.")

                # B.N.03 Movable Bridge Max Nav Vertical Clearance — numeric; nearest tenth; max 999.9
                # 999.9 is a valid special value (unlimited clearance for movable bridges)
                violations += _numeric_check(fev, "B.N.03", fid, "Error", "BN03",
                    f"Feature {fid}: Movable Bridge Max Nav Vertical Clearance (B.N.03) is null or not valid",
                    "Required when B.N.01=Y. Use 999.9 for bascule/swing/tilt bridges (unlimited clearance).",
                    allow_special=["999.9"],
                    needs_field=True)
                violations += _check_one_decimal(fev, "B.N.03", fid, "Error", "BN03-DEC",
                    f"Feature {fid}: Movable Bridge Max Nav Vertical Clearance (B.N.03)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).",
                    allow_special=["999.9"])

                # BN03-2 (Critical): value must not exceed 999.9
                bn03_row = fev.get("B.N.03")
                if not _is_resolved(bn03_row):
                    bn03_val = _eff(bn03_row)
                    if bn03_val:
                        try:
                            if float(bn03_val.strip().split()[0]) > 999.9:
                                violations.append(_v("Critical", "BN03-2", "B.N.03", fid,
                                    f"Feature {fid}: B.N.03 value '{bn03_val}' exceeds the maximum of 999.9.",
                                    "Numeric value must not exceed 999.9. Use 999.9 for unlimited clearance "
                                    "(bascule/swing/tilt bridges)."))
                        except (ValueError, TypeError):
                            pass  # non-numeric already caught above

                # B.N.04 Navigation Channel Width — numeric; nearest tenth; max 9999.9
                violations += _numeric_check(fev, "B.N.04", fid, "Error", "BN04",
                    f"Feature {fid}: Navigation Channel Width (B.N.04) is null or not valid",
                    "Required when B.N.01=Y. Must be numeric (feet).",
                    needs_field=True)
                violations += _check_one_decimal(fev, "B.N.04", fid, "Error", "BN04-DEC",
                    f"Feature {fid}: Navigation Channel Width (B.N.04)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
                violations += _check_max(fev, "B.N.04", fid, 9999.9, "Critical", "BN04-2",
                    f"Feature {fid}: Navigation Channel Width (B.N.04)",
                    "Navigation Channel Width must not exceed 9999.9 feet.")

                # B.N.05 Navigation Channel Min Horizontal Clearance — numeric; nearest tenth; max 9999.9
                # 9999.9 is valid (no piers in waterway)
                violations += _numeric_check(fev, "B.N.05", fid, "Error", "BN05",
                    f"Feature {fid}: Navigation Channel Min Horizontal Clearance (B.N.05) is null or not valid",
                    "Required when B.N.01=Y. Use 9999.9 if no piers in waterway; 0 if piers form channel boundary.",
                    allow_special=["9999.9"],
                    needs_field=True)
                violations += _check_one_decimal(fev, "B.N.05", fid, "Error", "BN05-DEC",
                    f"Feature {fid}: Navigation Channel Min Horizontal Clearance (B.N.05)",
                    "Must be reported to the nearest tenth of a foot (exactly one decimal place).",
                    allow_special=["9999.9"])
                violations += _check_max(fev, "B.N.05", fid, 9999.9, "Critical", "BN05-2",
                    f"Feature {fid}: Navigation Channel Min Horizontal Clearance (B.N.05)",
                    "Navigation Channel Min Horizontal Clearance must not exceed 9999.9 feet.")

                # B.N.06 Substructure Navigation Protection — coded; "1-T" accepted through 2027
                violations += _coded_check(fev, "B.N.06", fid, "Error", "BN06",
                    f"Feature {fid}: Substructure Navigation Protection (B.N.06) is null or not valid",
                    ["0", "1", "2", "3", "4", "5", "1-T"],
                    "Required when B.N.01=Y. Valid codes: 0–5, or 1-T (temporary, accepted through 2027).",
                    needs_field=True)

        # ── R* feature checks ─────────────────────────────────────────────────
        elif ftype == "R":
            violations += _coded_check(fev, "B.RR.01", fid, "Error", "BRR01",
                f"Feature {fid}: Railroad Service Type (B.RR.01) is null or not valid",
                ["F", "P", "M", "FE", "PE", "ME", "I"],
                "F=Freight, P=Passenger, M=Mixed, FE/PE/ME=Electrified, I=Not in service.")

            # B.RR.02 Railroad Min Vertical Clearance — numeric; nearest tenth; max 99.9
            violations += _numeric_check(fev, "B.RR.02", fid, "Error", "BRR02",
                f"Feature {fid}: Railroad Min Vertical Clearance (B.RR.02) is null or not valid",
                "Must be numeric. Top of rail to lowest restriction (feet).")
            violations += _check_one_decimal(fev, "B.RR.02", fid, "Error", "BRR02-DEC",
                f"Feature {fid}: Railroad Min Vertical Clearance (B.RR.02)",
                "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
            violations += _check_max(fev, "B.RR.02", fid, 99.9, "Error", "BRR02-2",
                f"Feature {fid}: Railroad Min Vertical Clearance (B.RR.02)",
                "Railroad Min Vertical Clearance must not exceed 99.9 feet.")

            # B.RR.03 Railroad Min Horizontal Offset — numeric; nearest tenth; max 99.9
            violations += _numeric_check(fev, "B.RR.03", fid, "Error", "BRR03",
                f"Feature {fid}: Railroad Min Horizontal Offset (B.RR.03) is null or not valid",
                "Must be numeric. CL of track to nearest substructure face (feet).")
            violations += _check_one_decimal(fev, "B.RR.03", fid, "Error", "BRR03-DEC",
                f"Feature {fid}: Railroad Min Horizontal Offset (B.RR.03)",
                "Must be reported to the nearest tenth of a foot (exactly one decimal place).")
            violations += _check_max(fev, "B.RR.03", fid, 99.9, "Error", "BRR03-2",
                f"Feature {fid}: Railroad Min Horizontal Offset (B.RR.03)",
                "Railroad Min Horizontal Offset must not exceed 99.9 feet.")

    # ── WORK record checks ───────────────────────────────────────────────────────
    work_ids = sorted(f for f in by_feature if f.startswith("WORK:"))

    # Valid B.W.03 codes per SNBI Tables 29–33
    _BW03_VALID = {
        # Table 29 – Bridge replacement
        "BR1",
        # Table 30 – Bridge improvement
        "IP1", "IP2", "IP3", "IP4",
        # Table 31 – Rehabilitation (deck / super / sub / culvert)
        "DK1", "DK2", "DK3",
        "SP1", "SP2", "SP3",
        "SB1", "SB2", "SB3",
        "CU2", "CU3",
        # Table 32 – Preservation (deck / super / sub / culvert)
        "DK4", "DK5", "DK6", "DK7",
        "SP5", "SP6", "SP7",
        "SB5", "SB6", "SB7",
        "CU4", "CU5", "CU6", "CU7",
        # Table 33 – Other preservation
        "BG1", "BG2",    # bearings
        "JT1", "JT2",    # deck joints
        "RT1", "RT2",    # railings/transitions
        "SC1", "SC2",    # scour countermeasures
        "CP1", "CP2",    # channel protection
        "CH1",           # channel improvement
        # Special: no work / unclassified
        "0",
    }

    # Year Built from PRIMARY for cross-check
    bw01_year = None
    bw01_raw  = _eff(pev.get("B.W.01"))
    if bw01_raw:
        try:
            bw01_year = int(float(bw01_raw.strip().split()[0]))
        except (ValueError, TypeError):
            pass

    for wid in work_ids:
        wev = by_feature[wid]

        # ── B.W.02 Year Work Performed ──────────────────────────────────────────
        bw02_row = wev.get("B.W.02")

        if not _is_resolved(bw02_row):
            if bw02_row is None:
                violations.append(_v("Error", "BW02-MISS", "B.W.02", wid,
                    f"{wid}: Year Work Performed (B.W.02) row is missing.",
                    "A year must be reported for every work record."))
            else:
                val = _eff(bw02_row)
                if val is None:
                    violations.append(_v("Error", "BW02-NULL", "B.W.02", wid,
                        f"{wid}: Year Work Performed (B.W.02) is null or pending.",
                        "A year must be reported for every work record."))
                else:
                    tok = val.strip().split()[0]
                    if not _is_int_in_range(tok, 1700, 2100):
                        violations.append(_v("Error", "BW02-FMT", "B.W.02", wid,
                            f"{wid}: Year Work Performed '{val}' is not a valid calendar year (1700–2100).",
                            "Must be a 4-digit calendar year."))
                    elif bw01_year is not None:
                        work_year = int(float(tok))
                        if work_year <= bw01_year:
                            violations.append(_v("Flag", "BW02-3", "B.W.02", wid,
                                f"{wid}: Year Work Performed ({work_year}) is earlier than or equal to "
                                f"Year Built ({bw01_year}) — B.W.02 <= B.W.01.",
                                "Work performed year should be after the original year built."))

        # ── B.W.03 Work Performed ───────────────────────────────────────────────
        bw03_row = wev.get("B.W.03")

        if _is_resolved(bw03_row):
            pass  # reviewer acknowledged this item
        elif bw03_row is None:
            violations.append(_v("Error", "BW03-MISS", "B.W.03", wid,
                f"{wid}: Work Performed (B.W.03) row is missing.",
                "Work performed codes must be reported for every work record."))
        else:
            raw = _eff(bw03_row)
            if raw is None:
                violations.append(_v("Error", "BW03-NULL", "B.W.03", wid,
                    f"{wid}: Work Performed (B.W.03) is null or pending.",
                    "Report at least one work code, or '0' if no reportable work was performed."))
            else:
                # Length check (BW03-2)
                if len(raw) > 120:
                    violations.append(_v("Error", "BW03-2", "B.W.03", wid,
                        f"{wid}: Work Performed value exceeds 120 characters ({len(raw)}).",
                        "Maximum field length is 120 characters."))

                # Parse pipe-delimited codes
                codes = [c.strip().upper() for c in raw.split("|") if c.strip()]

                # BW03-1: invalid code check
                bad_codes = [c for c in codes if c not in _BW03_VALID]
                if bad_codes:
                    violations.append(_v("Error", "BW03-1", "B.W.03", wid,
                        f"{wid}: Work Performed contains unrecognized code(s): "
                        f"{', '.join(bad_codes)}.",
                        "Valid codes are defined in SNBI Tables 29–33 "
                        "(BR1; IP1–IP4; DK/SP/SB/CU codes; BG1–BG2; JT1–JT2; "
                        "RT1–RT2; SC1–SC2; CP1–CP2; CH1; or 0)."))

                code_set = set(codes)

                # BW03-3 (Error): BR1 cannot appear with other codes
                if "BR1" in code_set and len(code_set) > 1:
                    others = ", ".join(sorted(code_set - {"BR1"}))
                    violations.append(_v("Error", "BW03-3", "B.W.03", wid,
                        f"{wid}: BR1 (bridge replaced) is reported with other codes ({others}). "
                        "No other codes should accompany BR1.",
                        "When BR1 is used, no other work codes should be reported in that dataset."))

                # BW03-4 (Flag): DK1 with DK2 or DK3
                if "DK1" in code_set and code_set & {"DK2", "DK3"}:
                    violations.append(_v("Flag", "BW03-4", "B.W.03", wid,
                        f"{wid}: Deck replacement (DK1) is reported with deck rehabilitation codes "
                        f"(DK2/DK3). Replacement supersedes rehabilitation.",
                        "Do not report DK1 with DK2 or DK3."))

                # BW03-5 (Flag): SP1 with SP2 or SP3
                if "SP1" in code_set and code_set & {"SP2", "SP3"}:
                    violations.append(_v("Flag", "BW03-5", "B.W.03", wid,
                        f"{wid}: Superstructure replacement (SP1) is reported with superstructure "
                        "rehabilitation codes (SP2/SP3). Replacement supersedes rehabilitation.",
                        "Do not report SP1 with SP2 or SP3."))

                # BW03-6 (Flag): SB1 with SB2 or SB3
                if "SB1" in code_set and code_set & {"SB2", "SB3"}:
                    violations.append(_v("Flag", "BW03-6", "B.W.03", wid,
                        f"{wid}: Substructure replacement (SB1) is reported with substructure "
                        "rehabilitation codes (SB2/SB3). Replacement supersedes rehabilitation.",
                        "Do not report SB1 with SB2 or SB3."))

                # BW03-7 (Flag): both DK2 and DK3
                if {"DK2", "DK3"} <= code_set:
                    violations.append(_v("Flag", "BW03-7", "B.W.03", wid,
                        f"{wid}: Both DK2 (major rehab) and DK3 (minor rehab) are reported. "
                        "Report only DK2 when both major and minor deck rehab occurred.",
                        "Report only the major rehabilitation code when both major and minor "
                        "rehabilitation were completed on the same component."))

                # BW03-8 (Flag): both SP2 and SP3
                if {"SP2", "SP3"} <= code_set:
                    violations.append(_v("Flag", "BW03-8", "B.W.03", wid,
                        f"{wid}: Both SP2 (major rehab) and SP3 (minor rehab) are reported. "
                        "Report only SP2 when both major and minor superstructure rehab occurred.",
                        "Report only the major rehabilitation code when both major and minor "
                        "rehabilitation were completed on the same component."))

                # BW03-9 (Flag): both SB2 and SB3
                if {"SB2", "SB3"} <= code_set:
                    violations.append(_v("Flag", "BW03-9", "B.W.03", wid,
                        f"{wid}: Both SB2 (major rehab) and SB3 (minor rehab) are reported. "
                        "Report only SB2 when both major and minor substructure rehab occurred.",
                        "Report only the major rehabilitation code when both major and minor "
                        "rehabilitation were completed on the same component."))

                # BW03-10 (Flag): both CU2 and CU3
                if {"CU2", "CU3"} <= code_set:
                    violations.append(_v("Flag", "BW03-10", "B.W.03", wid,
                        f"{wid}: Both CU2 (major rehab) and CU3 (minor rehab) are reported. "
                        "Report only CU2 when both major and minor culvert rehab occurred.",
                        "Report only the major rehabilitation code when both major and minor "
                        "rehabilitation were completed on the same component."))

                # BW03-11 (Flag): scour/channel codes without a below-bridge waterway feature
                _water_codes = {"SC1", "SC2", "CP1", "CP2", "CH1"}
                if code_set & _water_codes:
                    has_below_waterway = any(
                        fid.startswith("W") and _eff(by_feature[fid].get("B.F.02")) == "B"
                        for fid in by_feature
                    )
                    if not has_below_waterway:
                        used = ", ".join(sorted(code_set & _water_codes))
                        violations.append(_v("Flag", "BW03-11", "B.W.03", wid,
                            f"{wid}: Scour/channel code(s) {used} are reported but no waterway "
                            "feature with location 'B' (below bridge) exists.",
                            "SC1, SC2, CP1, CP2, and CH1 should only be reported when there is "
                            "a waterway feature below the bridge (B.F.02 = B)."))

                # BW03-12 (Flag): railing/transition codes without railings on the bridge
                _railing_codes = {"RT1", "RT2"}
                if code_set & _railing_codes:
                    rh01_val = _eff(pev.get("B.RH.01"))
                    # "0" or null means no railings are present
                    no_railings = (rh01_val is None or _code_token(rh01_val) in ("0", "N", ""))
                    if no_railings:
                        used = ", ".join(sorted(code_set & _railing_codes))
                        violations.append(_v("Flag", "BW03-12", "B.W.03", wid,
                            f"{wid}: Railing/transition code(s) {used} are reported but "
                            "B.RH.01 indicates no bridge railing exists.",
                            "RT1 and RT2 should only be reported when the bridge has "
                            "railings or transitions (B.RH.01 not null/zero)."))

                # BW03-13 / BW03-14: require B.SP.01 (Span Configuration) which is not
                # tracked in this pipeline's item guide — skipped.

    # Sort: Safety → Critical → Error → Flag, then by item ID and feature ID
    violations.sort(key=lambda v: (
        _SEV_ORDER.get(v["severity"], 9),
        v["snbi_id"],
        v["feature_id"],
    ))
    return violations


def validate_bridges(conn, bridge_ids):
    """
    Run validation for a list of bridge IDs.
    Returns {bridge_id: [violations]} dict.
    """
    return {bid: validate_bridge(conn, bid) for bid in bridge_ids}
