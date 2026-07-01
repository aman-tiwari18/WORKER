mport os
import zipfile
import requests
import time

# ── Config ──────────────────────────────────────────────────────────────────
SOURCE_DIR = "/data/cctv_backup/cctv-analytics-system/results/proof_of_work"
BASE_URL   = "https://samar.iitk.ac.in/cctv"
LOGIN_URL  = f"{BASE_URL}/api/auth/login"
DEST_BASE  = "/home/samarth/CCTV_APP/Backend/proof_of_work/JEE Mains 2026-Session 2"
USERNAME   = "siddarth"
PASSWORD   = "siddarth123"

UPLOAD_ONLY = {"2026-04-02", "2026-04-04", "2026-04-05" ,"2026-04-06","2026-04-08"}  # ← sirf yahi upload honge

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

# ── Zip only images from all subfolders of a shift ──────────────────────────
def zip_shift_images(shift_path, shift_name):
    zip_path = f"/tmp/{shift_name}.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for subfolder in os.listdir(shift_path):
            subfolder_path = os.path.join(shift_path, subfolder)
            if not os.path.isdir(subfolder_path):
                continue

            for file in os.listdir(subfolder_path):
                full_path = os.path.join(subfolder_path, file)
                if os.path.isfile(full_path):
                    zipf.write(full_path, file)

    print(f"  🗜️  Zipped: {shift_name}.zip")
    return zip_path

# ── Upload zip to a specific path ────────────────────────────────────────────
def upload_zip(zip_path, dest_path, token):
    upload_url = f"{BASE_URL}/api/files/upload?path={requests.utils.quote(dest_path, safe='')}"
    print(f"  ⬆️  Uploading to: {dest_path}")
    with open(zip_path, "rb") as f:
        response = requests.post(
            upload_url,
            files={"files": (os.path.basename(zip_path), f)},
            cookies={"token": token}
        )
    if response.status_code == 200:
        print(f"  ✅ Upload successful!")
    else:
        print(f"  ❌ Upload failed: {response.text}")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    token = get_token()

    for date_folder in sorted(os.listdir(SOURCE_DIR)):
        date_path = os.path.join(SOURCE_DIR, date_folder)
        if not os.path.isdir(date_path):
            continue

        # ── Sirf selected dates hi upload karo ──────────────────────
        if date_folder not in UPLOAD_ONLY:
            print(f"⏭️  Skipping date: {date_folder}")
            continue
        # ────────────────────────────────────────────────────────────

        print(f"\n📅 Processing date: {date_folder}")

        create_folder(DEST_BASE, date_folder, token)
        date_dest = f"{DEST_BASE}/{date_folder}"

        for shift_folder in sorted(os.listdir(date_path)):
            shift_path = os.path.join(date_path, shift_folder)
            if not os.path.isdir(shift_path):
                continue

            print(f"\n  🔄 Processing shift: {shift_folder}")

            create_folder(date_dest, shift_folder, token)
            shift_dest = f"{date_dest}/{shift_folder}"

            zip_path = zip_shift_images(shift_path, shift_folder)

            upload_zip(zip_path, shift_dest, token)

            os.remove(zip_path)

            time.sleep(1)

    print("\n🎉 All done!")

if __name__ == "__main__":
    main()