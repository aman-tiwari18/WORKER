"""
Server Room Detection — Constants
Source: serverroom Threshold_CTGD 2026 version 1.3.xlsx

TC_CODE_THRESHOLDS maps each tc_code (str) to its
'Maximum Server Room Threshold' value from that (exam-independent) sheet.

Sheets issued for ONE exam are kept out of that global table and registered per
exam in EXAM_TC_CODE_THRESHOLDS instead — centre codes repeat across exam
bodies, so a global entry would retune every other exam using the same code.

get_min_persons(tc_code, exam_id=None, exam_date=None, shift_no=None) resolves
the exam's own sheet first and falls back to the global table, then to
DEFAULT_MIN_PERSONS.
"""

import os
import re

# ===== DEFAULT FALLBACK =====
DEFAULT_MIN_PERSONS = 1   # Trigger on any person when TC code is unknown

# ======= PER-TC-CODE THRESHOLDS (Maximum Server Room Threshold) =======
# Source: serverroom Threshold_CTGD 2026 version 1.3.xlsx
TC_CODE_THRESHOLDS = {
    '100104':  6,
    '100107':  7,
    '100112':  5,
    '100113':  5,
    '100121':  7,
    '100122':  6,
    '100126':  6,
    '100127': 14,
    '100128': 11,
    '100133':  6,
    '10202':   4,
    '110103':  4,
    '120103':  7,
    '120104':  4,
    '120105':  4,
    '120106':  4,
    '120109':  6,
    '120302': 11,
    '120501':  5,
    '120603':  4,
    '120703':  7,
    '120704':  5,
    '130201':  7,
    '150205':  5,
    '150301':  5,
    '160103':  7,
    '160208':  6,
    '160209':  6,
    '160406':  7,
    '160504':  6,
    '1605101': 5,
    '1605102': 5,
    '160512':  6,
    '160513':  6,
    '170402':  6,
    '170501':  4,
    '170602':  5,
    '170701':  4,
    '170802':  4,
    '180101':  4,
    '180103':  4,
    '180201':  4,
    '180304':  4,
    '180401':  4,
    '180505':  5,
    '180708':  4,
    '180803':  5,
    '200201':  4,
    '20802':   7,
    '20901':   5,
    '210202':  8,
    '210203':  7,
    '210205':  5,
    '210206':  6,
    '210207':  9,
    '210209':  7,
    '2102101': 5,
    '210302':  5,
    '210303':  5,
    '210402':  7,
    '210407':  7,
    '210408':  6,
    '210409':  7,
    '210412':  7,
    '210503':  6,
    '210504':  6,
    '210506':  5,
    '210802':  5,
    '210803':  7,
    '21106':   5,
    '21107':   5,
    '21108':   5,
    '211103':  7,
    '220102':  5,
    '220103':  4,
    '220105':  5,
    '220202':  5,
    '220304':  5,
    '220306':  4,
    '220307':  5,
    '220401':  5,
    '220402':  4,
    '220503':  5,
    '220507':  4,
    '220513':  6,
    '220604':  5,
    '220606':  6,
    '220608':  5,
    '220609':  6,
    '220705':  4,
    '220802':  7,
    '220901':  5,
    '220909':  5,
    '220910':  4,
    '230101':  4,
    '230203':  5,
    '230301':  4,
    '240101':  4,
    '250101':  4,
    '260101':  5,
    '270104':  7,
    '270203':  6,
    '270307':  6,
    '270407':  7,
    '270506':  7,
    '270602':  5,
    '290502':  5,
    '290703': 10,
    '291101':  5,
    '300302':  5,
    '300303':  8,
    '300401':  9,
    '300404': 10,
    '300408':  7,
    '300409':  6,
    '300410':  8,
    '300413':  7,
    '300414':  7,
    '300415':  7,
    '300418':  7,
    '300501':  6,
    '300704':  5,
    '300705':  6,
    '300706':  6,
    '30103':   4,
    '310104':  5,
    '320206':  5,
    '330107':  5,
    '330111':  7,
    '330112':  6,
    '330113': 12,
    '330202':  5,
    '330501':  6,
    '340101':  4,
    '340104':  5,
    '340107':  5,
    '340109':  4,
    '350111':  5,
    '350301':  8,
    '350306':  6,
    '350502':  5,
    '350505':  5,
    '350607':  5,
    '350702':  5,
    '350703':  7,
    '350711':  6,
    '350806':  8,
    '350808':  7,
    '350815':  6,
    '350901':  7,
    '350904':  5,
    '351206':  6,
    '351212':  8,
    '351213':  7,
    '351214':  6,
    '351218':  5,
    '351601': 36,
    '351603': 32,
    '351802':  7,
    '360102':  5,
    '360106':  5,
    '360107':  5,
    '360109':  5,
    '360205':  5,
    '360207':  5,
    '360302':  5,
    '360303':  5,
    '360304':  5,
    '370201':  5,
    '370302':  4,
    '370603':  7,
    '370618': 10,
    '370701':  5,
    '371101':  5,
    '40101':   4,
    '40102':  11,
    '40103':   5,
    '40201':   6,
    '40204':   5,
    '40205':   4,
    '40208':   4,
    '40209':   7,
    '40213':   5,
    '40301':   6,
    '40303':  10,
    '40404':   4,
    '50106':  12,
    '50204':   9,
    '50302':  13,
    '50410':  13,
    '50501':   8,
    '50606':  13,
    '50701':  12,
    '50737':  24,
    '50801':   9,
    '50901':   9,
    '70102':   5,
    '70104':   7,
    '70202':   5,
    '70303':   6,
}


# ============================================================================
# EXAM-SCOPED THRESHOLDS
# ============================================================================
#
# TC_CODE_THRESHOLDS above is keyed by tc_code alone, so anything added to it
# applies to EVERY exam. Centre codes are reused across exam bodies, so a
# sheet issued for one exam must not retune another one that happens to use
# the same code. Per-exam sheets therefore live here, keyed by exam_id, and
# the global table stays exactly as the CTGD sheet left it.

# ── Re-examination UGC NET June 2026 (CDIS) — 09 & 10 September 2026 ──
# Source: Server Room Threshold Re examination-UGC NET June 2026 - CDIS.xlsx
#
# The sheet gives a threshold per DATE + SHIFT, not one per centre: 21 of its
# 118 centres use a different limit across the three sittings. Both shapes are
# kept — the exact per-sitting figure, and a flat per-centre view built from
# the LOWEST scheduled value, which is what a caller with only a tc_code gets.
# Lowest rather than highest: the stricter limit alerts a notch early on the
# sittings that allow more people, which beats missing a real breach on the
# sitting that allows fewer. Pass exam_date + shift_no for the exact figure.

SHIFT_WINDOWS = {
    ('2026-09-09', 1): ('09:00', '12:00'),
    ('2026-09-09', 2): ('15:00', '18:00'),
    ('2026-09-10', 1): ('09:00', '12:00'),
}

UGC_REEXAM_SEP2026_SHIFT_THRESHOLDS = {
    ('2026-09-09', 1): {   # 104 centres running this sitting
        '201': 4,
        '213': 4,
        '222': 4,
        '225': 4,
        '232': 4,
        '234': 4,
        '756': 4,
        '772': 4,
        '840': 4,
        '943': 4,
        '1026': 4,
        '1074': 4,
        '1282': 4,
        '1419': 4,
        '1575': 4,
        '1894': 4,
        '1961': 4,
        '2048': 4,
        '2090': 4,
        '2238': 4,
        '2380': 4,
        '2566': 4,
        '3215': 4,
        '3431': 4,
        '3707': 4,
        '3737': 4,
        '4443': 4,
        '4596': 4,
        '4638': 4,
        '7760': 4,
        '7926': 4,
        '7953': 4,
        '7959': 4,
        '7960': 4,
        '7967': 6,
        '7974': 4,
        '7975': 4,
        '7981': 6,
        '7997': 6,
        '8030': 6,
        '8050': 9,
        '8132': 4,
        '8560': 5,
        '8572': 4,
        '8594': 4,
        '8743': 6,
        '8749': 8,
        '8773': 4,
        '8774': 5,
        '8775': 4,
        '8816': 5,
        '8836': 5,
        '8849': 4,
        '9490': 4,
        '9496': 5,
        '9498': 5,
        '9637': 4,
        '9678': 4,
        '9687': 4,
        '9689': 4,
        '11394': 4,
        '11398': 5,
        '12175': 5,
        '12176': 7,
        '12309': 4,
        '14952': 4,
        '15141': 4,
        '15794': 4,
        '15803': 4,
        '15836': 5,
        '15841': 4,
        '23726': 4,
        '23729': 4,
        '23754': 4,
        '23880': 4,
        '26453': 4,
        '26808': 4,
        '26848': 4,
        '28839': 4,
        '28923': 4,
        '28936': 4,
        '28971': 4,
        '28980': 4,
        '28995': 4,
        '29001': 4,
        '30063': 4,
        '31355': 4,
        '33458': 4,
        '33635': 4,
        '33931': 4,
        '37297': 4,
        '37404': 4,
        '38987': 4,
        '39109': 4,
        '40041': 5,
        '40099': 4,
        '42057': 7,
        '42139': 4,
        '42496': 4,
        '42522': 6,
        '42560': 4,
        '42561': 4,
        '42562': 4,
        '42572': 4,
    },
    ('2026-09-09', 2): {   # 91 centres running this sitting
        '193': 4,
        '201': 5,
        '213': 4,
        '222': 4,
        '232': 4,
        '234': 4,
        '250': 4,
        '772': 6,
        '1026': 4,
        '1074': 4,
        '1282': 4,
        '1419': 4,
        '1435': 4,
        '1575': 4,
        '2090': 4,
        '2238': 4,
        '2380': 4,
        '2566': 4,
        '3431': 4,
        '4443': 4,
        '4638': 4,
        '7814': 5,
        '7844': 4,
        '7926': 4,
        '7953': 4,
        '7959': 4,
        '7960': 4,
        '7967': 6,
        '7974': 4,
        '7975': 4,
        '7981': 5,
        '7997': 6,
        '8030': 6,
        '8050': 9,
        '8132': 4,
        '8152': 4,
        '8560': 5,
        '8594': 5,
        '8743': 7,
        '8749': 5,
        '8773': 4,
        '8774': 4,
        '8816': 4,
        '8836': 5,
        '9090': 4,
        '9490': 4,
        '9496': 5,
        '9498': 5,
        '9637': 4,
        '9687': 4,
        '9689': 4,
        '11394': 4,
        '11398': 5,
        '12175': 4,
        '12176': 7,
        '12309': 4,
        '15141': 4,
        '15794': 4,
        '15803': 4,
        '15832': 4,
        '15836': 4,
        '15841': 4,
        '23726': 4,
        '23729': 4,
        '23754': 4,
        '26453': 4,
        '26808': 4,
        '26848': 4,
        '28839': 4,
        '28923': 4,
        '28936': 4,
        '28971': 4,
        '28980': 4,
        '28995': 4,
        '30063': 4,
        '31355': 4,
        '33635': 4,
        '33931': 4,
        '37404': 4,
        '37505': 4,
        '40041': 5,
        '40099': 4,
        '40168': 5,
        '42057': 7,
        '42139': 4,
        '42496': 4,
        '42522': 6,
        '42561': 4,
        '42562': 4,
        '42572': 4,
        '42597': 9,
    },
    ('2026-09-10', 1): {   # 47 centres running this sitting
        '201': 4,
        '222': 4,
        '232': 4,
        '1074': 4,
        '1575': 4,
        '2090': 4,
        '2566': 4,
        '3431': 4,
        '4443': 4,
        '4638': 4,
        '7953': 4,
        '7959': 4,
        '7974': 4,
        '7975': 4,
        '7997': 4,
        '8030': 4,
        '8050': 4,
        '8152': 4,
        '8560': 4,
        '8594': 4,
        '8743': 4,
        '8749': 4,
        '8773': 4,
        '8774': 4,
        '8816': 4,
        '8836': 4,
        '9490': 4,
        '9496': 4,
        '9498': 4,
        '9637': 4,
        '11394': 4,
        '11398': 4,
        '12175': 4,
        '12176': 4,
        '12309': 4,
        '15841': 4,
        '26453': 4,
        '28936': 4,
        '28974': 4,
        '28980': 4,
        '28995': 4,
        '30063': 4,
        '33647': 4,
        '33931': 4,
        '40041': 4,
        '41023': 4,
        '42522': 4,
    },
}

# Flat view: the lowest threshold each centre is scheduled with.
UGC_REEXAM_SEP2026_THRESHOLDS = {
    '193': 4,   # Amruta Institute of Engineering and Management
    '201': 4,   # Bapuji Institute of Engineering & Technology
    '213': 4,   # Erode Sengunthar Engineering College
    '222': 4,   # PSNA College of Engineering & Technology
    '225': 4,   # V.S.B. Engineering College
    '232': 4,   # Dr. Sivanthi Aditanar College of Engineering
    '234': 4,   # Vivekanandha College of Engineering for Women
    '250': 4,   # M S Engineering College
    '756': 4,   # PA Aziz College of Engineering and Technology
    '772': 4,   # Cambridge Institute Of Technology
    '840': 4,   # St.Xaviers Catholic College of Engineering
    '943': 4,   # Rajadhani Institute of Engineering And Technol
    '1026': 4,   # Malnad College of Engineering
    '1074': 4,   # Adichunchanagiri Institute of Technology
    '1282': 4,   # Royal College of Engineering & Technology
    '1419': 4,   # LBS College Of Engineering
    '1435': 4,   # Ballari Institute of Technology & Management
    '1575': 4,   # Global Business School
    '1894': 4,   # St. Thomas College of Engineering and Technolo
    '1961': 4,   # Srinivasan Engineering College
    '2048': 4,   # Syed Ammal Engineering College
    '2090': 4,   # Arasu Engineering College
    '2238': 4,   # P.S.R Engineering College
    '2380': 4,   # Lord Jegannath College Of Engineering And Tech
    '2566': 4,   # N.S.N. College of Engineering and Technology
    '3215': 4,   # Valiya Koonambaikulathamma College of Engineer
    '3431': 4,   # Ambal Professional Group of Institutions
    '3707': 4,   # Al Ameen Engineering College
    '3737': 4,   # ssm institute of engineering and technology
    '4443': 4,   # Sri Venkateswara College of Engineering and Te
    '4596': 4,   # Trichy Engineering College
    '4638': 4,   # Jayalakshmi Institute of Technology
    '7760': 4,   # Mar Ephraem College of Engineering and Technol
    '7814': 5,   # Reverend Technologies
    '7844': 4,   # Jain Institute of Technology
    '7926': 4,   # Sri Chandrasekharendra Saraswathi Viswa Mahavi
    '7953': 4,   # iON Digital Zone iDZ Kozhikode
    '7959': 4,   # iON Digital Zone iDZ Sivathapuram
    '7960': 4,   # iON Digital Zone iDZ Kidaripatti
    '7967': 6,   # Rajadhani Institute of Engineering And Technol
    '7974': 4,   # iON Digital Zone iDZ Rajarajeswari Medical Col
    '7975': 4,   # iON Digital Zone iDZ Milk Diary Road
    '7981': 5,   # iON Digital Zone iDZ Manimangalam
    '7997': 4,   # iON Digital Zone iDZ Thondamuthur
    '8030': 4,   # iON Digital Zone iDZ Akshay Colony
    '8050': 4,   # iON Digital Zone iDZ Kovilambakkam
    '8132': 4,   # College of Engineering Trikaripur
    '8152': 4,   # Rajeev Polytechnic
    '8560': 4,   # iON Digital Zone iDZ Machenahalli
    '8572': 4,   # N.M.S S Vellaichamy Nadar College
    '8594': 4,   # iON Digital Zone Bondel
    '8743': 4,   # iON Digital Zone iDZ Hebbal
    '8749': 4,   # iON Digital Zone iDZ Tirunelveli
    '8773': 4,   # iON Digital Zone iDZ Madurai IV
    '8774': 4,   # iON Digital Zone IDZ Trichy II
    '8775': 4,   # iON Digital Zone iDZ Madurai III
    '8816': 4,   # CARMEL COLLEGE OF ENGINEERING&TECHNOLOGY
    '8836': 4,   # ION Digital Zone IDZ Thrissur
    '8849': 4,   # ION Digital Zone IDZ KIOT-Campus
    '9090': 4,   # SJPN Trusts BCA College
    '9490': 4,   # RiO Tech
    '9496': 4,   # iON Digital Zone iDZ Kottayam
    '9498': 4,   # iON Digital Zone iDZ Kollam
    '9637': 4,   # Centos Assessment School
    '9678': 4,   # Udaya Institute Of Technology
    '9687': 4,   # Chetan Business School
    '9689': 4,   # College of Engineering Aranmula
    '11394': 4,   # iON Digital Zone iDZ Kannur
    '11398': 4,   # iON Digital Zone iDZ Airport Road
    '12175': 4,   # iON Digital Zone iDZ Vellore
    '12176': 4,   # iON Digital Zone iDZ Aluva
    '12309': 4,   # Sree Narayana Institute Of Technology Adoor
    '14952': 4,   # College of Engineering and Technology Payyanur
    '15141': 4,   # Chinmaya Institute Of Technology
    '15794': 4,   # Mookambika Technical Campus
    '15803': 4,   # College Of Engineering Kallooppara
    '15832': 4,   # Sri Bhuvanendra College
    '15836': 4,   # Mount Carmel Residential School
    '15841': 4,   # Jayamatha Engineering College
    '23726': 4,   # Balanagar ITI
    '23729': 4,   # CK College Of Engineering and Technology
    '23754': 4,   # Orphanage Polytechnic College
    '23880': 4,   # Vidya Academy of Science & Technology Technica
    '26453': 4,   # Sri Vinayaga Polytechnic College
    '26808': 4,   # W.M.O.ARTS & SCIENCE COLLEGE
    '26848': 4,   # Adhiyamaan College of Agriculture and Research
    '28839': 4,   # Marygiri E Park
    '28923': 4,   # Ammaiappar Polytechnic College
    '28936': 4,   # Waywin Technologies LLP
    '28971': 4,   # University College of Engineering Panruti
    '28974': 4,   # Thiruvalluvar Arts and Science College
    '28980': 4,   # Surya College of Engineering
    '28995': 4,   # J J College of Arts and Science
    '29001': 4,   # Syed Hameedha Arts and Science College
    '30063': 4,   # Government Polytechnic College Meenangadi
    '31355': 4,   # St.Mary's College
    '33458': 4,   # VSA Group of Institutions
    '33635': 4,   # Ganapathy Chettiar College of Engineering and 
    '33647': 4,   # Meenakshi Ammal Polytechnic College
    '33931': 4,   # SAFI Institute of Advanced Study
    '37297': 4,   # AGN INFOTECH
    '37404': 4,   # Rajadhani Institute Of Science and Technology
    '37505': 4,   # Infojiva IT Services LLP
    '38987': 4,   # Bishop Jerome Institute
    '39109': 4,   # Nehru Memorial College
    '40041': 4,   # Sharanabasaweshwar Vidya Vardhak Sangha
    '40099': 4,   # Imayam College Of Engineering
    '40168': 5,   # Digimetrics
    '41023': 4,   # Triplee A Matriculation School
    '42057': 7,   # iON Digital Zone iDZ Harohalli
    '42139': 4,   # Test Hub Online Examination Centre
    '42496': 4,   # Chinmaya Vidyalaya Kannur
    '42522': 4,   # iON Digital Zone Kanjiramkulam
    '42560': 4,   # KMCT College of Engineering for Emerging Techn
    '42561': 4,   # St. Marys College
    '42562': 4,   # KMCT College of Engineering for Emerging Techn
    '42572': 4,   # KMCT Institute of Emerging Technology and Mana
    '42597': 9,   # iON Digital Zone iDZ Anekal
}


def _env_exam_ids(var_name, default=()):
    """Exam ids for a sheet, overridable per deployment without a code change."""
    raw = os.environ.get(var_name, "")
    ids = tuple(int(p) for p in re.split(r"[,\s]+", raw.strip()) if p.isdigit())
    return ids or tuple(default)


# ── Registry: exam_id → that exam's sheet ───────────────────────────────────
# The re-exam's own exam_id is the only thing tying this sheet to the run. Only
# "UGC NET June 2026 Re Exam - Mock" (35) exists so far, so set the real id here
# — or, without touching deployed code on the fleet, via the env var:
#   SERVER_ROOM_UGC_REEXAM_EXAM_IDS="35,36"
UGC_REEXAM_SEP2026_EXAM_IDS = _env_exam_ids(
    "SERVER_ROOM_UGC_REEXAM_EXAM_IDS", default=(35,)
)

# exam_id (str) → {tc_code: threshold}
EXAM_TC_CODE_THRESHOLDS = {
    str(_eid): UGC_REEXAM_SEP2026_THRESHOLDS
    for _eid in UGC_REEXAM_SEP2026_EXAM_IDS
}

# exam_id (str) → {(exam_date, shift_no): {tc_code: threshold}}
EXAM_SHIFT_TC_CODE_THRESHOLDS = {
    str(_eid): UGC_REEXAM_SEP2026_SHIFT_THRESHOLDS
    for _eid in UGC_REEXAM_SEP2026_EXAM_IDS
}


def get_min_persons(tc_code, exam_id=None, exam_date=None, shift_no=None):
    """Return the server-room person threshold for a centre.

    Resolved most specific first:
      1. The exam's own sheet, for this exact sitting  (exam_id + exam_date + shift_no)
      2. The exam's own sheet, flat per centre          (exam_id)
      3. The global CTGD table                          (tc_code alone)
      4. DEFAULT_MIN_PERSONS

    exam_id is what keeps one exam's sheet off another's centres: the same
    tc_code can appear in two exams with different limits. Callers that pass no
    exam_id keep the pre-existing behaviour exactly — global table, then default.
    """
    if not tc_code:
        return DEFAULT_MIN_PERSONS
    key = str(tc_code).strip()

    if exam_id is not None:
        eid = str(exam_id).strip()

        if exam_date and shift_no is not None:
            sittings = EXAM_SHIFT_TC_CODE_THRESHOLDS.get(eid)
            if sittings:
                try:
                    sitting = sittings.get((str(exam_date), int(shift_no)))
                except (TypeError, ValueError):
                    sitting = None
                if sitting and key in sitting:
                    return sitting[key]

        exam_table = EXAM_TC_CODE_THRESHOLDS.get(eid)
        if exam_table and key in exam_table:
            return exam_table[key]

    if key in TC_CODE_THRESHOLDS:
        return TC_CODE_THRESHOLDS[key]
    return DEFAULT_MIN_PERSONS
