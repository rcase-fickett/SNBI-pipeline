# ============================================================
#  SNBI PIPELINE — CONFIGURATION
#  Edit the paths in this file before running anything else.
# ============================================================

import os

# ----------------------------------------------------------
# 1. Root folder containing all bridge subfolders
#    Each subfolder is named after the bridge_id and contains:
#      - {bridge_id} Plans.pdf
#      - metadata.json  (optional but strongly preferred)
#      - {bridge_id}_BC_*.pdf  (vertical clearance docs, optional)
#
#    Override by setting the SNBI_BRIDGES_ROOT environment variable
#    (the setup script does this automatically for each team member).
# ----------------------------------------------------------
_DEFAULT_BRIDGES_ROOT = r"C:\Users\rcase\OneDrive - Fickett Structural Solutions\25071 ODOT SNBIT, Oregon Department of Transportation\3) Plans, Specs, Photos\1 Bridges"
BRIDGES_ROOT = os.environ.get("SNBI_BRIDGES_ROOT", _DEFAULT_BRIDGES_ROOT)

# ----------------------------------------------------------
# 2. Input Excel files — derived from BRIDGES_ROOT automatically
# ----------------------------------------------------------
BRM_EXPORT_PATH   = os.path.join(BRIDGES_ROOT, "Bridge_List_Export.xlsx")
BRIDGE_LIST_PATH  = os.path.join(BRIDGES_ROOT, "Bridge List.xlsx")
VC_LIST_PATH      = os.path.join(BRIDGES_ROOT, "Vertical Clearance List.xlsx")

# ----------------------------------------------------------
# 3. Output database path
# ----------------------------------------------------------
DB_PATH = r"snbi_evidence.db"

# ----------------------------------------------------------
# 4. Output Excel review workbook
# ----------------------------------------------------------
REVIEW_EXCEL_PATH = r"SNBI_Review.xlsx"

# ----------------------------------------------------------
# 5. Anthropic API key
#    Set this as an environment variable (preferred):
#      Windows: setx ANTHROPIC_API_KEY "sk-ant-..."
#    Or paste it here (less secure):
# ----------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ----------------------------------------------------------
# 6. Processing settings
# ----------------------------------------------------------
CLAUDE_MODEL      = "claude-sonnet-4-6"
IMAGE_DPI         = 150          # DPI for PDF → image conversion (150 is good balance)
MAX_IMAGE_PX      = 3000         # Max dimension in pixels before downscaling
BATCH_DELAY_SEC   = 1.0          # Seconds between API calls (rate limit buffer)
SKIP_COMPLETED    = True         # Skip bridges already in DB with PLANS_DONE status

# ----------------------------------------------------------
# 7. Which bridges to process
#    Set to None to process all 600 script+pdf bridges
#    Or provide a list: ["02283", "05C01A", ...]
# ----------------------------------------------------------
BRIDGE_FILTER = None

# ----------------------------------------------------------
# 8. (Legacy — no longer used for filtering)
#    Bridge_List.xlsx filter settings kept for reference only.
# ----------------------------------------------------------
COMPLETE_COL    = "Complete"
COMPLETE_VALUE  = "Completed via script with pdf"
BRIDGE_ID_COL   = "STRUCT_ID"

# ----------------------------------------------------------
# 9. Reference documents sent to Claude API for context
#    Paths relative to this config file.
#    Set to None to disable a document.
# ----------------------------------------------------------
_CFG_DIR = os.path.dirname(os.path.abspath(__file__))
SNBI_ERRATA_PDF    = os.path.join(_CFG_DIR, "SNBI March 2022 Errata 01.pdf")
DATACROSSWALK_PATH = os.path.join(_CFG_DIR, "datacrosswalk.xlsx")
