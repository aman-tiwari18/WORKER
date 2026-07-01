import pandas as pd
import logging

def load_camera_configs(excel_path):
    try:
        df = pd.read_excel(excel_path)

        required_cols = ['TC NAME', 'Camera Name', 'CCTV URL']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Missing column: {col}")

        configs = []

        for _, row in df.iterrows():
            rtsp = str(row['CCTV URL']).strip()

            if not rtsp.startswith("rtsp"):
                continue

            configs.append({
                "device_id": str(row['TC NAME']).strip(),
                "camera_no": str(row['Camera Name']).strip(),
                "rtsp_url": rtsp
            })

        logging.info(f"Loaded {len(configs)} cameras from Excel")
        return configs

    except Exception as e:
        logging.error(f"Excel load error: {e}")
        return []
