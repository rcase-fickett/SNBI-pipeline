"""
app.py — SNBI local review web interface
Run: python app.py
Open: http://localhost:5000
"""
import os, sys, sqlite3, threading, importlib.util, json, time
from pathlib import Path
from functools import lru_cache
from io import BytesIO
from urllib.parse import urlencode
from datetime import datetime

from flask import Flask, render_template, jsonify, request, Response, abort, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BRIDGES_ROOT

# Resolve DB path relative to this file so it works from any working dir
_HERE   = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_HERE, "snbi_evidence.db")

app = Flask(__name__)

# ── Background processing job state ──────────────────────────────────────────

_job = {
    'running':  False,
    'stopping': False,
    'total':    0,
    'done':     0,
    'errors':   0,
    'current':  '',
    'log':      [],
}
_job_lock = threading.Lock()

def _log(msg):
    with _job_lock:
        _job['log'].append(msg)
        if len(_job['log']) > 300:
            _job['log'] = _job['log'][-300:]


# ── Feedback job state ────────────────────────────────────────────────────────

_fb_job = {
    'running': False,
    'done':    0,
    'total':   0,
    'current': '',
    'log':     [],
}
_fb_lock = threading.Lock()

def _fblog(msg):
    with _fb_lock:
        _fb_job['log'].append(msg)
        if len(_fb_job['log']) > 300:
            _fb_job['log'] = _fb_job['log'][-300:]

def _classify_correction(plan_value, plan_confidence, user_det, status):
    # New explicit status values (web UI)
    if status == 'CORRECT':   return 'CONFIRMED'   # clicking ✓ is confirmation regardless of det
    if status == 'INCORRECT': return 'VALUE_WRONG' if (user_det and user_det.strip()) else 'VALUE_WRONG'
    if status == 'PARTIAL':   return 'CONFIDENCE_WRONG' if (user_det and user_det.strip()) else 'CONFIDENCE_WRONG'
    # Legacy values
    if status not in ('REVIEWED', 'FLAGGED', 'APPROVED'):
        return None
    if not user_det or not user_det.strip():
        return None
    if status == 'FLAGGED':  return 'VALUE_WRONG'
    if status == 'APPROVED': return 'CONFIRMED'
    a = (plan_value or '').strip().lower()
    f = user_det.strip().lower()
    if a == f: return 'CONFIRMED'
    try:
        if abs(float(a) - float(f)) <= 0.2: return 'CONFIRMED'
    except ValueError:
        pass
    if plan_confidence == 'NA' and f not in ('na', 'n/a', 'not applicable'):
        return 'NA_WRONG'
    return 'VALUE_WRONG'


def _compute_recommendation(row):
    """Synthesise a recommended value, source tag, and reasoning from AI + BrM data."""
    conf = row.get('plan_confidence') or ''
    plan = (row.get('plan_value')    or '').strip()
    brm  = (row.get('brm_value')     or '').strip()
    ai_r = (row.get('plan_reasoning')or '').strip()

    if conf == 'NA':
        return 'N/A', 'na', ai_r or 'Item is not applicable per SNBI spec.'
    if conf == 'FIELD_REQ':
        return 'Field Required', 'field', ai_r or 'Value requires field measurement per SNBI spec.'
    if plan and conf in ('HIGH', 'APPROX'):
        src = 'plan-high' if conf == 'HIGH' else 'plan-approx'
        return plan, src, ai_r
    if brm:
        if conf == 'PENDING' and not plan:
            reason = (f"No value found in plan drawings; AI extraction returned no result. "
                      f"Recommending BrM export value ({brm}) as best available.")
        else:
            reason = (f"Plan extraction confidence was {conf or 'unknown'}. "
                      f"Recommending BrM export value ({brm}) as fallback.")
        return brm, 'brm', reason
    return None, 'none', ''

def _run_feedback_job(api_key):
    _fblog("Starting feedback import…")
    try:
        import config as cfg
        from lib.db import (get_conn as _gc, migrate_db,
                            get_corrections_for_item, mark_corrections_used,
                            upsert_lesson)
        from lib.snbi_items import ITEM_BY_ID
        import anthropic as _ant

        conn = _gc(DB_PATH)
        migrate_db(DB_PATH)

        # ── Phase A: import corrections from reviewed bridges ─────────
        # Include COMPLETE so re-running after a bug fix still picks up missed rows
        reviewed_bridges = [r[0] for r in conn.execute(
            "SELECT bridge_id FROM bridges WHERE processing_status IN ('REVIEW_DONE','COMPLETE')"
        ).fetchall()]

        _fblog(f"Bridges with completed review: {len(reviewed_bridges)}")
        if not reviewed_bridges:
            _fblog("No bridges marked as review complete. Nothing to import.")
            return

        _fb_job['total'] = len(reviewed_bridges)
        batch_id = datetime.now().strftime("%Y-%m-%d_%H%M")
        total_corr = total_conf = 0

        for i, bid in enumerate(reviewed_bridges):
            _fb_job['current'] = bid
            rows = conn.execute(
                """SELECT * FROM evidence
                   WHERE bridge_id=? AND status IN ('CORRECT','PARTIAL','INCORRECT',
                                                    'REVIEWED','FLAGGED','APPROVED')""",
                (bid,)
            ).fetchall()

            bc = bconf = 0
            for row in rows:
                ctype = _classify_correction(
                    row['plan_value'], row['plan_confidence'],
                    row['user_determination'], row['status']
                )
                if not ctype:
                    continue
                # Skip if this exact item was already imported (prevents duplicates on re-run)
                already = conn.execute(
                    "SELECT 1 FROM corrections WHERE bridge_id=? AND item_id=? AND feature_id=?",
                    (bid, row['item_id'], row['feature_id'])
                ).fetchone()
                if already:
                    continue
                try:
                    conn.execute("""
                        INSERT INTO corrections
                            (bridge_id,item_id,feature_id,
                             ai_value,ai_confidence,ai_reasoning,ai_source_pages,
                             field_value,field_notes,reviewer,reviewed_date,
                             correction_type,import_batch)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (bid, row['item_id'], row['feature_id'],
                          row['plan_value'], row['plan_confidence'],
                          row['plan_reasoning'], row['plan_source_pages'],
                          row['user_determination'], row['user_notes'],
                          row['reviewed_by'], row['reviewed_date'],
                          ctype, batch_id))
                    if ctype == 'CONFIRMED': bconf += 1
                    else:                    bc    += 1
                except Exception:
                    pass

            conn.execute(
                "UPDATE bridges SET processing_status='COMPLETE', updated_at=datetime('now') WHERE bridge_id=?",
                (bid,)
            )
            total_corr += bc
            total_conf += bconf
            _fblog(f"  [{i+1}/{len(reviewed_bridges)}] {bid} — {bc} corrections, {bconf} confirmations")
            _fb_job['done'] = i + 1

        conn.commit()
        _fblog(f"\nImported: {total_corr} corrections, {total_conf} confirmations")

        # ── Phase B: build lessons ────────────────────────────────────
        _fblog("\n─── Building lessons ───")
        items = [r[0] for r in conn.execute(
            "SELECT DISTINCT item_id FROM corrections WHERE used_in_lesson=0"
        ).fetchall()]

        if not items:
            _fblog("No new corrections to build lessons from.")
            conn.close()
            return

        client = _ant.Anthropic(api_key=api_key)
        LESSON_SYS = (
            "You are an expert bridge engineer and AI trainer. "
            "Synthesize inspector corrections into a CONCISE, ACTIONABLE lesson "
            "to prevent the same AI extraction mistakes. "
            "Return ONLY valid JSON: "
            '{"lesson_text":"...","example_json":{"value":"...","confidence":"...","reasoning":"..."},"confidence_score":0.8}'
        )
        built = 0
        for item_id in items:
            all_corr   = get_corrections_for_item(conn, item_id, unused_only=True)
            corrections = [c for c in all_corr if c['correction_type'] != 'CONFIRMED']
            confirms    = [c for c in all_corr if c['correction_type'] == 'CONFIRMED']
            if not corrections:
                continue

            item_def = ITEM_BY_ID.get(item_id, {})
            lines = [
                f"SNBI Item: {item_id} — {item_def.get('name', item_id)}",
                f"Spec guidance: {item_def.get('notes','')}",
                f"\nCORRECTIONS ({len(corrections)}):",
            ]
            for c in corrections[:15]:
                lines.append(f"  {c['bridge_id']}: AI={c['ai_value']!r} ({c['ai_confidence']}) → Inspector={c['field_value']!r}")
                if c['field_notes']:
                    lines.append(f"    Note: {c['field_notes']}")
            if confirms:
                lines.append(f"\nCONFIRMATIONS ({len(confirms)}):")
                for c in confirms[:5]:
                    lines.append(f"  {c['bridge_id']}: AI={c['ai_value']!r} — confirmed")
            lines.append("\nWrite a lesson to prevent these errors.")

            try:
                resp = client.messages.create(
                    model=cfg.CLAUDE_MODEL, max_tokens=600,
                    system=LESSON_SYS,
                    messages=[{"role": "user", "content": "\n".join(lines)}]
                )
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"): raw = raw[4:]
                    raw = raw.strip()
                result = json.loads(raw)
                upsert_lesson(conn,
                    item_id=item_id,
                    lesson_text=result.get('lesson_text',''),
                    example_json=json.dumps(result.get('example_json',{})),
                    correction_count=len(corrections),
                    confirmed_count=len(confirms),
                    confidence_score=float(result.get('confidence_score', 0.5)),
                )
                mark_corrections_used(conn, [c['id'] for c in all_corr])
                conn.commit()
                built += 1
                _fblog(f"  ✓ {item_id} lesson written (conf={result.get('confidence_score',0.5):.2f})")
            except Exception as e:
                _fblog(f"  ✗ {item_id}: {e}")
            time.sleep(0.5)

        conn.close()
        _fblog(f"\n─── Lessons built: {built} / {len(items)} items ───")
        _fblog("Next AI extraction will use updated lessons automatically.")

    except Exception as e:
        import traceback
        _fblog(f"Error: {e}")
        _fblog(traceback.format_exc()[-400:])
    finally:
        _fb_job['running'] = False
        _fb_job['current'] = ''
        _fblog("─── Done ───")

def _run_job(bridge_ids, api_key):
    _log(f"Starting: {len(bridge_ids)} bridge(s) queued")
    try:
        spec = importlib.util.spec_from_file_location(
            "p2", os.path.join(_HERE, "02_process_bridges.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        import config as cfg
        from lib.db import get_conn, get_active_lessons, migrate_db
        from lib.claude_api import ClaudeExtractorWithLessons as ClaudeExtractor

        conn = get_conn(DB_PATH)
        migrate_db(DB_PATH)
        lessons = get_active_lessons(conn)
        if lessons:
            _log(f"Loaded {len(lessons)} active lesson(s) from prior review cycles")

        extractor = ClaudeExtractor(
            api_key=api_key,
            model=cfg.CLAUDE_MODEL,
            delay_sec=cfg.BATCH_DELAY_SEC,
            lessons=lessons,
        )

        _job['total']  = len(bridge_ids)
        _job['done']   = 0
        _job['errors'] = 0

        for i, bid in enumerate(bridge_ids):
            if _job['stopping']:
                _log("Stopped by user.")
                break
            _job['current'] = bid
            _log(f"[{i+1}/{len(bridge_ids)}] {bid} — processing…")
            try:
                ok = mod.process_bridge(conn, bid, extractor, verbose=False)
                if ok:
                    _job['done'] += 1
                    n = conn.execute(
                        "SELECT COUNT(*) FROM evidence "
                        "WHERE bridge_id=? AND plan_confidence != 'PENDING'",
                        (bid,)
                    ).fetchone()[0]
                    _log(f"  ✓ {bid} — {n} items extracted")
                else:
                    _job['errors'] += 1
                    _log(f"  ✗ {bid} — nothing extracted (PDF missing or unreadable)")
            except Exception as e:
                import traceback
                _job['errors'] += 1
                _log(f"  ✗ {bid} — {str(e)[:120]}")
                _log(f"    {traceback.format_exc()[-200:]}")

        conn.close()

    except Exception as e:
        import traceback
        _log(f"Job error: {e}")
        _log(traceback.format_exc()[-300:])
    finally:
        _job['running']  = False
        _job['stopping'] = False
        _job['current']  = ''
        _log(f"─── Finished: {_job['done']} succeeded, {_job['errors']} failed ───")


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_exists():
    return os.path.exists(DB_PATH)


# ── PDF rendering ─────────────────────────────────────────────────────────────

def find_pdf(bridge_id):
    p = Path(BRIDGES_ROOT) / bridge_id / f"{bridge_id} Plans.pdf"
    return str(p) if p.exists() else None


@lru_cache(maxsize=300)
def _render_page(pdf_path, page_index):
    import pypdfium2 as pdfium
    from PIL import Image
    doc   = pdfium.PdfDocument(pdf_path)
    page  = doc[page_index]
    bm    = page.render(scale=150/72.0, rotation=0)
    img   = bm.to_pil()
    doc.close()
    w, h  = img.size
    if max(w, h) > 3200:
        r = 3200 / max(w, h)
        img = img.resize((int(w*r), int(h*r)), resample=Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@app.route("/api/pdf-page")
def api_pdf_page():
    bridge_id = request.args.get("bridge_id", "")
    try:
        page_index = max(0, int(request.args.get("page", 0)))
    except ValueError:
        abort(400)
    pdf_path = find_pdf(bridge_id)
    if not pdf_path:
        abort(404)
    try:
        data = _render_page(pdf_path, page_index)
        return Response(data, mimetype="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
    except Exception:
        abort(500)


@app.route("/api/pdf-info/<bridge_id>")
def api_pdf_info(bridge_id):
    pdf_path = find_pdf(bridge_id)
    if not pdf_path:
        return jsonify({"found": False, "pages": 0})
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_path)
        n   = len(doc)
        doc.close()
        return jsonify({"found": True, "pages": n})
    except Exception:
        return jsonify({"found": False, "pages": 0})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    if not db_exists():
        return render_template("no_db.html")
    conn = get_db()
    bridge_stats   = conn.execute("SELECT processing_status, COUNT(*) n FROM bridges GROUP BY processing_status ORDER BY n DESC").fetchall()
    evidence_stats = conn.execute("SELECT status, COUNT(*) n FROM evidence GROUP BY status ORDER BY n DESC").fetchall()
    conf_stats     = conn.execute("SELECT plan_confidence, COUNT(*) n FROM evidence GROUP BY plan_confidence ORDER BY n DESC").fetchall()
    total_bridges  = conn.execute("SELECT COUNT(*) FROM bridges").fetchone()[0]
    total_evidence = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    pending_review = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE status='PENDING' AND plan_confidence IN ('HIGH','APPROX')"
    ).fetchone()[0]
    flagged = conn.execute("SELECT COUNT(*) FROM evidence WHERE status='FLAGGED'").fetchone()[0]
    review_done = conn.execute(
        "SELECT COUNT(*) FROM bridges WHERE processing_status IN ('REVIEW_DONE','COMPLETE')"
    ).fetchone()[0]
    conn.close()
    return render_template("dashboard.html",
        bridge_stats=bridge_stats, evidence_stats=evidence_stats,
        conf_stats=conf_stats, total_bridges=total_bridges,
        total_evidence=total_evidence, pending_review=pending_review,
        flagged=flagged, review_done=review_done)


# ── Review Queue ──────────────────────────────────────────────────────────────

@app.route("/review")
def review():
    if not db_exists():
        return render_template("no_db.html")

    bridge_filter = request.args.get("bridge", "")
    item_filter   = request.args.get("item", "")
    conf_filter   = request.args.getlist("conf")
    status_filter = request.args.getlist("status")
    page          = max(1, int(request.args.get("page", 1)))
    per_page      = 50

    where, params = [], []
    if bridge_filter:
        where.append("e.bridge_id = ?"); params.append(bridge_filter)
    if item_filter:
        where.append("(e.item_id LIKE ? OR e.item_name LIKE ?)"); params += [f"%{item_filter}%"] * 2
    if conf_filter:
        where.append(f"e.plan_confidence IN ({','.join('?'*len(conf_filter))})"); params += conf_filter
    if status_filter:
        where.append(f"e.status IN ({','.join('?'*len(status_filter))})"); params += status_filter
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn  = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM evidence e {where_sql}", params).fetchone()[0]
    rows  = conn.execute(f"""
        SELECT e.*, b.bridge_name, b.county
        FROM evidence e JOIN bridges b ON e.bridge_id = b.bridge_id
        {where_sql}
        ORDER BY
            CASE e.plan_confidence
                WHEN 'HIGH'      THEN 0 WHEN 'APPROX'    THEN 1
                WHEN 'FIELD_REQ' THEN 2 WHEN 'PENDING'   THEN 3 ELSE 4 END,
            e.bridge_id, e.item_id, e.feature_id
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()
    bridges = conn.execute(
        "SELECT bridge_id, bridge_name FROM bridges ORDER BY bridge_id"
    ).fetchall()
    conn.close()

    total_pages = max(1, (total + per_page - 1) // per_page)

    def page_url(p):
        args = request.args.to_dict(flat=False)
        args["page"] = [str(p)]
        return "/review?" + urlencode(args, doseq=True)

    return render_template("review.html",
        rows=rows, bridges=bridges, total=total, page=page,
        per_page=per_page, total_pages=total_pages,
        bridge_filter=bridge_filter, item_filter=item_filter,
        conf_filter=conf_filter, status_filter=status_filter,
        page_url=page_url)


# ── Bridge Detail ─────────────────────────────────────────────────────────────

@app.route("/bridge/<bridge_id>")
def bridge_detail(bridge_id):
    if not db_exists():
        return render_template("no_db.html")
    conn   = get_db()
    bridge = conn.execute("SELECT * FROM bridges WHERE bridge_id = ?", (bridge_id,)).fetchone()
    if not bridge:
        abort(404)

    rows = conn.execute(
        "SELECT * FROM evidence WHERE bridge_id = ? ORDER BY item_id, feature_id",
        (bridge_id,)
    ).fetchall()

    # Only show items on the approved list (filters out removed items still in DB)
    from lib.snbi_items import ITEM_BY_ID
    rows = [r for r in rows if r["item_id"] in ITEM_BY_ID]

    # Load active lessons for recommendation hints
    from lib.db import get_active_lessons
    lessons = get_active_lessons(conn)

    def enrich(row_list):
        out = []
        for r in row_list:
            d = dict(r)
            d['ai_recommendation'], d['rec_source'], d['rec_reasoning'] = _compute_recommendation(d)
            lesson = lessons.get(d['item_id'])
            d['lesson_hint'] = lesson['lesson_text'][:140] if lesson else ''
            out.append(d)
        return out

    # Split into three groups
    primary_rows = enrich(sorted([r for r in rows if r["feature_id"] == "PRIMARY"],
                          key=lambda r: r["item_id"]))
    feature_rows = enrich(sorted([r for r in rows
                           if r["feature_id"] != "PRIMARY"
                           and not str(r["feature_id"]).startswith("WORK")],
                          key=lambda r: (r["feature_id"], r["item_id"])))
    work_rows    = enrich(sorted([r for r in rows if str(r["feature_id"]).startswith("WORK")],
                          key=lambda r: r["item_id"]))

    # Prev / next bridge for navigation
    all_ids = [r[0] for r in conn.execute(
        "SELECT bridge_id FROM bridges ORDER BY bridge_id"
    ).fetchall()]
    all_bridges = conn.execute(
        "SELECT bridge_id, bridge_name FROM bridges ORDER BY bridge_id"
    ).fetchall()
    idx     = all_ids.index(bridge_id) if bridge_id in all_ids else -1
    prev_id = all_ids[idx - 1] if idx > 0 else None
    next_id = all_ids[idx + 1] if 0 <= idx < len(all_ids) - 1 else None

    conn.close()
    has_pdf = find_pdf(bridge_id) is not None
    return render_template("bridge.html",
        bridge=bridge,
        primary_rows=primary_rows,
        feature_rows=feature_rows,
        work_rows=work_rows,
        has_pdf=has_pdf,
        all_bridges=all_bridges,
        prev_id=prev_id,
        next_id=next_id,
        position=idx + 1,
        total=len(all_ids))


# ── API: update one evidence row ──────────────────────────────────────────────

@app.route("/api/evidence/<int:row_id>", methods=["POST"])
def update_evidence(row_id):
    data    = request.get_json() or {}
    allowed = {"user_determination", "user_notes", "status", "reviewed_by", "needs_field"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"ok": False, "error": "no valid fields"}), 400
    set_sql = ", ".join(f"{k}=?" for k in updates)
    conn    = get_db()
    conn.execute(
        f"UPDATE evidence SET {set_sql}, updated_at=datetime('now') WHERE id=?",
        list(updates.values()) + [row_id]
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── API: delete one evidence row ─────────────────────────────────────────────

@app.route("/api/evidence/<int:row_id>", methods=["DELETE"])
def delete_evidence_row(row_id):
    conn = get_db()
    conn.execute("DELETE FROM evidence WHERE id=?", (row_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── API: screenshots ──────────────────────────────────────────────────────────

_SCREENSHOTS_DIR = os.path.join(_HERE, "static", "screenshots")
os.makedirs(_SCREENSHOTS_DIR, exist_ok=True)

@app.route("/api/evidence/<int:ev_id>/screenshots")
def list_screenshots(ev_id):
    from lib.db import get_screenshots
    conn = get_db()
    rows = get_screenshots(conn, ev_id)
    conn.close()
    return jsonify({"screenshots": [{"id": r["id"], "filename": r["filename"],
                                     "caption": r["caption"]} for r in rows]})


@app.route("/api/evidence/<int:ev_id>/screenshot", methods=["POST"])
def upload_screenshot(ev_id):
    from lib.db import add_screenshot, migrate_db
    migrate_db(DB_PATH)  # ensure table exists
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400
    caption = request.form.get("caption", "")

    # Reserve a row to get the ID, then save the file named after it
    conn = get_db()
    shot_id = add_screenshot(conn, ev_id, "__tmp__", caption or None)
    filename = f"{shot_id}.jpg"
    conn.execute("UPDATE screenshots SET filename=? WHERE id=?", (filename, shot_id))
    conn.commit()
    conn.close()

    try:
        from PIL import Image
        img = Image.open(f.stream).convert("RGB")
        img.save(os.path.join(_SCREENSHOTS_DIR, filename), "JPEG", quality=85)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "id": shot_id, "filename": filename})


@app.route("/api/screenshot/<int:shot_id>")
def serve_screenshot(shot_id):
    conn = get_db()
    row = conn.execute("SELECT filename FROM screenshots WHERE id=?", (shot_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return send_from_directory(_SCREENSHOTS_DIR, row["filename"])


@app.route("/api/screenshot/<int:shot_id>", methods=["DELETE"])
def delete_screenshot_route(shot_id):
    from lib.db import delete_screenshot
    conn = get_db()
    row = conn.execute("SELECT filename FROM screenshots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False}), 404
    path = os.path.join(_SCREENSHOTS_DIR, row["filename"])
    if os.path.exists(path):
        os.unlink(path)
    delete_screenshot(conn, shot_id)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── API: delete entire feature group ─────────────────────────────────────────

@app.route("/api/feature/<bridge_id>/<feature_id>", methods=["DELETE"])
def delete_feature_group(bridge_id, feature_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM evidence WHERE bridge_id=? AND feature_id=?",
        (bridge_id, feature_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── API: add a new feature to a bridge ───────────────────────────────────────

@app.route("/api/bridge/<bridge_id>/add-feature", methods=["POST"])
def add_feature(bridge_id):
    data        = request.get_json() or {}
    feat_type   = str(data.get("feature_type", "")).upper()   # H | W | R
    feat_num    = int(data.get("feature_num", 1))
    location    = str(data.get("location", "B"))              # C | B | A

    if feat_type not in ("H", "W", "R", "P", "F", "B", "D", "X"):
        return jsonify({"ok": False, "error": "Invalid feature type"}), 400

    feature_id = f"{feat_type}{feat_num:02d}"

    try:
        from lib.snbi_items import FEATURE_ITEMS, ITEM_BY_ID
    except ImportError:
        from snbi_items import FEATURE_ITEMS, ITEM_BY_ID

    conn = get_db()

    # Check if feature_id already exists for this bridge
    existing = conn.execute(
        "SELECT id FROM evidence WHERE bridge_id=? AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": False, "error": f"{feature_id} already exists"}), 409

    try:
        from lib.db import upsert_evidence
    except ImportError:
        from db import upsert_evidence

    for item in FEATURE_ITEMS:
        applies = item.get("applies_to", "ALL")
        if applies not in ("ALL", feat_type):
            continue
        row = {
            "bridge_id":    bridge_id,
            "item_id":      item["id"],
            "feature_id":   feature_id,
            "item_name":    item["name"],
            "plan_confidence": "PENDING",
            "status":       "PENDING",
        }
        if item["id"] == "B.F.02":
            row["plan_value"] = location
        upsert_evidence(conn, row)

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "feature_id": feature_id})


# ── API: add a work record to a bridge ───────────────────────────────────────

@app.route("/api/bridge/<bridge_id>/add-work", methods=["POST"])
def add_work(bridge_id):
    data = request.get_json() or {}
    year = data.get("year")
    try:
        year = int(year)
        if not (1800 <= year <= 2100):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid year"}), 400

    feature_id = f"WORK:{year}"

    try:
        from lib.snbi_items import WORK_ITEMS
    except ImportError:
        from snbi_items import WORK_ITEMS

    try:
        from lib.db import upsert_evidence
    except ImportError:
        from db import upsert_evidence

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM evidence WHERE bridge_id=? AND feature_id=?",
        (bridge_id, feature_id)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": False, "error": f"WORK:{year} already exists"}), 409

    for item in WORK_ITEMS:
        row = {
            "bridge_id":       bridge_id,
            "item_id":         item["id"],
            "feature_id":      feature_id,
            "item_name":       item["name"],
            "plan_confidence": "PENDING",
            "status":          "PENDING",
        }
        if item["id"] == "B.W.02":
            row["plan_value"] = str(year)
        upsert_evidence(conn, row)

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "feature_id": feature_id})


# ── API: bulk approve ─────────────────────────────────────────────────────────

@app.route("/api/evidence/bulk-approve", methods=["POST"])
def bulk_approve():
    data     = request.get_json() or {}
    ids      = [int(i) for i in data.get("ids", [])]
    reviewer = str(data.get("reviewer", ""))[:100]
    if not ids:
        return jsonify({"ok": False, "error": "no ids"}), 400
    ph   = ",".join("?" * len(ids))
    conn = get_db()
    conn.execute(
        f"UPDATE evidence SET status='APPROVED', reviewed_by=?, reviewed_date=date('now'),"
        f" updated_at=datetime('now') WHERE id IN ({ph})",
        [reviewer] + ids
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "updated": len(ids)})


# ── Process page ─────────────────────────────────────────────────────────────

@app.route("/process")
def process_page():
    if not db_exists():
        return render_template("no_db.html")
    status_filter = request.args.get("status", "")
    conn    = get_db()
    where   = "WHERE b.processing_status = ?" if status_filter else ""
    params  = [status_filter] if status_filter else []
    bridges = conn.execute(f"""
        SELECT b.bridge_id, b.bridge_name, b.county, b.processing_status, b.updated_at,
               COUNT(e.id) as total_items,
               SUM(CASE WHEN e.plan_confidence != 'PENDING' THEN 1 ELSE 0 END) as extracted
        FROM bridges b LEFT JOIN evidence e ON b.bridge_id = b.bridge_id
        {where}
        GROUP BY b.bridge_id
        ORDER BY b.processing_status, b.bridge_id
    """, params).fetchall()
    # simpler query — the join above is self-referential bug, fix it
    bridges = conn.execute(f"""
        SELECT bridge_id, bridge_name, county, processing_status, updated_at
        FROM bridges {"WHERE processing_status = ?" if status_filter else ""}
        ORDER BY processing_status, bridge_id
    """, params).fetchall()
    status_counts = conn.execute(
        "SELECT processing_status, COUNT(*) n FROM bridges GROUP BY processing_status ORDER BY n DESC"
    ).fetchall()
    review_done_count = conn.execute(
        "SELECT COUNT(*) FROM bridges WHERE processing_status IN ('REVIEW_DONE','COMPLETE')"
    ).fetchone()[0]
    conn.close()
    return render_template("process.html",
        bridges=bridges, status_counts=status_counts,
        status_filter=status_filter,
        api_key_set=bool(os.environ.get("ANTHROPIC_API_KEY")),
        job=dict(_job),
        fb_job=dict(_fb_job),
        review_done_count=review_done_count)


@app.route("/api/process/start", methods=["POST"])
def api_process_start():
    if _job["running"]:
        return jsonify({"ok": False, "error": "A job is already running"})
    data       = request.get_json() or {}
    bridge_ids = data.get("bridge_ids", [])
    api_key    = (data.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not bridge_ids:
        return jsonify({"ok": False, "error": "No bridges selected"})
    if not api_key:
        return jsonify({"ok": False, "error": "Anthropic API key required"})
    with _job_lock:
        _job.update(running=True, stopping=False, total=len(bridge_ids),
                    done=0, errors=0, current="", log=[])
    threading.Thread(target=_run_job, args=(bridge_ids, api_key), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/process/status")
def api_process_status():
    with _job_lock:
        return jsonify(dict(_job))


@app.route("/api/process/stop", methods=["POST"])
def api_process_stop():
    _job["stopping"] = True
    return jsonify({"ok": True})


# ── Mark bridge review complete ───────────────────────────────────────────────

@app.route("/api/bridge/<bridge_id>/complete-review", methods=["POST"])
def complete_review(bridge_id):
    conn = get_db()
    conn.execute(
        "UPDATE bridges SET processing_status='REVIEW_DONE', updated_at=datetime('now') WHERE bridge_id=?",
        (bridge_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Feedback (correction import + lesson build) ───────────────────────────────

@app.route("/api/feedback/start", methods=["POST"])
def api_feedback_start():
    if _fb_job["running"]:
        return jsonify({"ok": False, "error": "Feedback job already running"})
    data    = request.get_json() or {}
    api_key = (data.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "Anthropic API key required"})
    with _fb_lock:
        _fb_job.update(running=True, done=0, total=0, current="", log=[])
    threading.Thread(target=_run_feedback_job, args=(api_key,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/feedback/status")
def api_feedback_status():
    with _fb_lock:
        return jsonify(dict(_fb_job))


if __name__ == "__main__":
    import argparse, socket
    parser = argparse.ArgumentParser(description="SNBI Review App")
    parser.add_argument("--local", action="store_true",
                        help="Bind to localhost only (default: network accessible)")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    host = "127.0.0.1" if args.local else "0.0.0.0"

    print("\n  SNBI Review App")
    print(f"  Database : {DB_PATH}")
    if db_exists():
        from lib.db import migrate_db
        migrate_db(DB_PATH)
    else:
        print("  WARNING  : Database not found — run 01_init_db.py first")

    if host == "0.0.0.0":
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            lan_ip = "<this-machine-ip>"
        print(f"  Local    : http://localhost:{args.port}")
        print(f"  Network  : http://{lan_ip}:{args.port}  ← share this with your team")
    else:
        print(f"  Open     : http://localhost:{args.port}")
    print()

    app.run(debug=False, host=host, port=args.port)
