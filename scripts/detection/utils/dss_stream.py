import requests
import hashlib
import json
import logging

# ================= DSS CONFIG =================
DSS_BASE = "https://d11.camview.io:443"
USERNAME = "system"
PASSWORD = "Sasd@12345"
CLIENT_TYPE = "WINPC_V1"

# ================= LOGGER ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# =====================================================
# SIGNATURE GENERATION (DSS MD5 AUTH)
# =====================================================
def generate_signature(userName, password, realm, randomKey):
    temp = hashlib.md5(password.encode()).hexdigest()
    temp = hashlib.md5((userName + temp).encode()).hexdigest()
    temp = hashlib.md5(temp.encode()).hexdigest()
    temp = hashlib.md5((userName + ":" + realm + ":" + temp).encode()).hexdigest()
    signature = hashlib.md5((temp + ":" + randomKey).encode()).hexdigest()
    return signature


# =====================================================
# DSS LOGIN
# =====================================================
def dss_login():
    try:
        url = f"{DSS_BASE}/admin/API/accounts/authorize"

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Connection": "close",
            "HOST": "d11.camview.io:443"
        }

        payload = {
            "userName": USERNAME,
            "ipAddress": "",
            "clientType": CLIENT_TYPE
        }

        # ---------- step 1 ----------
        r1 = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        r1.raise_for_status()

        data1 = r1.json()
        cookie = r1.headers.get("Set-Cookie")

        realm = data1["realm"]
        randomKey = data1["randomKey"]
        publicKey = data1["publickey"]

        # ---------- signature ----------
        signature = generate_signature(USERNAME, PASSWORD, realm, randomKey)

        payload2 = {
            "mac": "",
            "signature": signature,
            "userName": USERNAME,
            "randomKey": randomKey,
            "publicKey": publicKey,
            "encryptType": "MD5",
            "ipAddress": "",
            "clientType": CLIENT_TYPE,
            "userType": "0"
        }

        headers2 = {
            "Content-Type": "application/json;charset=UTF-8",
            "Connection": "close",
            "Cookie": cookie
        }

        # ---------- step 2 ----------
        r2 = requests.post(url, headers=headers2, data=json.dumps(payload2), timeout=10)
        r2.raise_for_status()

        token = r2.json()["token"]

        logging.info("✅ DSS login successful")
        return token

    except Exception as e:
        logging.error(f"❌ DSS login failed: {e}")
        return None


# =====================================================
# GET RTSP URL FROM DSS
# =====================================================
def get_rtsp_url(token, device_id, camera_no):
    try:
        url = f"{DSS_BASE}/admin/API/MTS/Video/StartVideo?token={token}"

        headers = {
            "Connection": "close",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Subject-Token": token
        }

        payload = {
            "clientType": CLIENT_TYPE,
            "clientMac": "",
            "clientPushId": "",
            "project": "PSDK",
            "method": "MTS.Video.StartVideo",
            "data": {
                "streamType": "1",
                "optional": f"/admin/API/MTS/Video/StartVideo?token={token}",
                "trackId": "",
                "extend": "",
                "channelId": f"{device_id}$1$0${camera_no}",
                "keyCode": "",
                "planId": "",
                "dataType": "2",
                "enableRtsps": "0"
            }
        }

        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        r.raise_for_status()

        resp = r.json()

        if "data" not in resp:
            logging.error(f"❌ No data in DSS response for {device_id}-{camera_no}")
            return None

        raw_url = resp["data"]["url"]
        stream_token = resp["data"]["token"]

        # ---------- handle multi-url ----------
        if "|" in raw_url:
            parts = raw_url.split("|")
            chosen = parts[1] if len(parts) > 1 and parts[1].strip() else parts[0]
        else:
            chosen = raw_url

        final_rtsp = f"{chosen}?token={stream_token}"

        logging.info(f"🎥 RTSP generated for {device_id}-{camera_no}")
        return final_rtsp

    except Exception as e:
        logging.error(f"❌ RTSP fetch failed for {device_id}-{camera_no}: {e}")
        return None
