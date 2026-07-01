import os
import re
import zipfile
import requests
import time

# ── Config ──────────────────────────────────────────────────────────────────
SOURCE_DIR = "/data/cctv_dev/cctv/script-server/scripts/detection/results"
BASE_URL   = "https://samar.iitk.ac.in/cctv"
LOGIN_URL  = f"{BASE_URL}/api/auth/login"
DEST_BASE  = "/home/samarth/CCTV_APP/Backend/proof_of_work/CTGD"
USERNAME   = "siddarth"
PASSWORD   = "siddarth123"

# temp zips yahan banenge (/tmp root FS full hai, isliye /data par rakho)
TMP_DIR    = "/data/tmp/ctgd_upload_zips"

# CTGD exam dates (May 2026).  date token (filename)  ->  server folder name
# 2026-05-28 jaan-boojh kar nahi hai (skip).
DATE_NAMES = {
    "20260504": "2026-05-04",
    "20260505": "2026-05-05",
    "20260506": "2026-05-06",
    "20260507": "2026-05-07",
    "20260508": "2026-05-08",
    "20260509": "2026-05-09",
    "20260518": "2026-05-18",
    "20260519": "2026-05-19",
    "20260520": "2026-05-20",
    "20260521": "2026-05-21",
    "20260522": "2026-05-22",
    "20260525": "2026-05-25",
    "20260526": "2026-05-26",
    "20260529": "2026-05-29",
    "20260530": "2026-05-30",
}

# shift folder order
SHIFT_ORDER = ["Shift 1", "Shift 2", "Shift 3"]

# ── Per-zip chunk limits (jab tak limit hit na ho, ek hi zip) ────────────────
CHUNK_MAX_FILES = 400                  # max files per zip
CHUNK_MAX_BYTES = 200 * 1024 * 1024    # ~200 MB (uncompressed) per zip

# filename se date+time nikalta hai:  ..._YYYYMMDD_HHMMSS_...
DT_RE = re.compile(r"(?<!\d)(\d{8})_(\d{6})(?!\d)")


def _sec(hhmm):
    """'HH:MM' -> seconds of day."""
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def win(*pairs):
    """[('10:00','11:00'), ...] -> [(start_sec, end_sec), ...]  (end exclusive)."""
    return [(_sec(a), _sec(b)) for a, b in pairs]


def shifts(s1=None, s2=None, s3=None):
    """per-shift windows -> {'Shift N': windows}; None shift skip."""
    d = {}
    if s1:
        d["Shift 1"] = s1
    if s2:
        d["Shift 2"] = s2
    if s3:
        d["Shift 3"] = s3
    return d


# Standard 3 shifts: 10-11, 13-14, 16:30-17:30
STD = shifts(win(("10:00", "11:00")), win(("13:00", "14:00")), win(("16:30", "17:30")))
# Crowd at entry gate ke alag windows (har shift se thoda pehle)
CROWD = shifts(win(("09:30", "10:15")), win(("12:30", "13:15")), win(("16:00", "16:45")))
# Intruder pre-exam check 05:00-06:00 -> Shift 1 ke saath rakha
INTRUDER = shifts(win(("05:00", "06:00")), None, None)

# ── Detection folder -> {shift: windows}  ("Improper furniture" exclude) ──────
DETECTIONS = {
    "intruder_detection":         INTRUDER,   # Intruder in exam lab + server room
    "crowd_detection":            CROWD,       # Crowd >10 at entry gate
    "invigilator_detection":      STD,         # Invigilator not moving
    "proximity_detection":        STD,         # Two+ close proximity
    "server_room_detection":      STD,         # Server room threshold
    "phone_detection":            STD,         # Mobile phone
    "entry_exit_detection":       STD,         # Movement at entry/exit
    "camera_inversion_detection": STD,         # Camera feed inverted
    "zero_person_detection":      STD,         # 0 candidate in a room
    "crowd_detection_threshold":  STD,         # Less than 5 candidates in a room
    "blind_spot_detection":       STD,         # Blind spot inside lab
}


# ── Login ────────────────────────────────────────────────────────────────────
def get_token():
    response = requests.post(LOGIN_URL, json={"username": USERNAME, "password": PASSWORD})
    if response.status_code == 200:
        token = response.cookies.get("token")
        if token:
            print("✅ Login successful")
            return token
        raise Exception("No token in cookie")
    raise Exception(f"Login failed: {response.status_code} {response.text}")


# ── Create folder on server ──────────────────────────────────────────────────
def create_folder(parent_path, folder_name, token):
    url = f"{BASE_URL}/api/files/create-folder"
    response = requests.post(
        url,
        json={"path": parent_path, "folderName": folder_name},
        cookies={"token": token}
    )
    if response.status_code in [200, 201, 409]:
        print(f"  📁 Folder ready: {folder_name}")
    else:
        print(f"  ❌ Folder create failed: {response.text}")


# ── Collect files for a date + time-window inside one detection folder ────────
def files_for_window(detection_path, date_token, windows):
    """Return full paths whose filename date == date_token and time in windows."""
    matches = []
    for root, _dirs, files in os.walk(detection_path):
        for file in files:
            m = DT_RE.search(file)
            if not m or m.group(1) != date_token:
                continue
            t = m.group(2)
            s = int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])
            if any(a <= s < b for a, b in windows):
                matches.append(os.path.join(root, file))
    return sorted(matches)


# ── Split a file list into size/count capped chunks ──────────────────────────
def make_chunks(file_paths):
    chunk, chunk_bytes = [], 0
    for p in file_paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        if chunk and (len(chunk) >= CHUNK_MAX_FILES or chunk_bytes + size > CHUNK_MAX_BYTES):
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(p)
        chunk_bytes += size
    if chunk:
        yield chunk


# ── Zip a chunk of files (flat names -> server unzips into detection folder) ─
def zip_chunk(file_paths, zip_path):
    seen = set()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for p in file_paths:
            arcname = os.path.basename(p)
            # naam clash (nested subfolders) ho to unique bana do
            if arcname in seen:
                stem, ext = os.path.splitext(arcname)
                i = 1
                while f"{stem}_{i}{ext}" in seen:
                    i += 1
                arcname = f"{stem}_{i}{ext}"
            seen.add(arcname)
            zipf.write(p, arcname)
    return zip_path


# ── Upload one zip; server unzips it at dest_path ────────────────────────────
def upload_zip(zip_path, dest_path, token):
    upload_url = f"{BASE_URL}/api/files/upload?path={requests.utils.quote(dest_path, safe='')}"
    with open(zip_path, "rb") as f:
        response = requests.post(
            upload_url,
            files={"files": (os.path.basename(zip_path), f)},
            cookies={"token": token}
        )
    return response.status_code == 200, response


# ── Upload all files of one detection (chunked) into dest_path ────────────────
def upload_detection(detection, date_name, shift, matches, dest_path, token):
    print(f"      🔍 {detection}: {len(matches)} files")
    chunks = list(make_chunks(matches))
    sent = 0
    safe_shift = shift.replace(" ", "")
    for idx, chunk in enumerate(chunks, 1):
        zip_path = os.path.join(TMP_DIR, f"{detection}_{date_name}_{safe_shift}_part{idx}.zip")
        zip_chunk(chunk, zip_path)
        ok, resp = upload_zip(zip_path, dest_path, token)
        os.remove(zip_path)
        if ok:
            sent += len(chunk)
            print(f"          ✅ part {idx}/{len(chunks)}  ({sent}/{len(matches)} files)")
        else:
            print(f"          ❌ part {idx} failed ({len(chunk)} files): "
                  f"{resp.status_code} {resp.text[:200]}")
        time.sleep(0.5)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    token = get_token()

    # ensure "CTGD" base folder exists
    parent = os.path.dirname(DEST_BASE)
    base_name = os.path.basename(DEST_BASE)
    create_folder(parent, base_name, token)

    for date_token, date_name in sorted(DATE_NAMES.items()):
        # pre-compute: {shift: {detection: [files]}}
        by_shift = {s: {} for s in SHIFT_ORDER}
        for detection, shift_wins in DETECTIONS.items():
            dp = os.path.join(SOURCE_DIR, detection)
            if not os.path.isdir(dp):
                continue
            for shift, windows in shift_wins.items():
                m = files_for_window(dp, date_token, windows)
                if m:
                    by_shift[shift][detection] = m

        if not any(by_shift[s] for s in SHIFT_ORDER):
            print(f"\n📅 {date_name}: koi file nahi, skip")
            continue

        print(f"\n📅 Exam date: {date_name}  (token {date_token})")
        create_folder(DEST_BASE, date_name, token)
        date_dest = f"{DEST_BASE}/{date_name}"

        for shift in SHIFT_ORDER:
            dets = by_shift[shift]
            if not dets:
                continue
            print(f"\n  🕑 {shift}")
            create_folder(date_dest, shift, token)
            shift_dest = f"{date_dest}/{shift}"

            for detection in sorted(dets):
                create_folder(shift_dest, detection, token)
                detection_dest = f"{shift_dest}/{detection}"
                upload_detection(detection, date_name, shift, dets[detection], detection_dest, token)

    print("\n🎉 All done!")


if __name__ == "__main__":
    main()
