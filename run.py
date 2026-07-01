import uvicorn
import os
from app.config import HTTP_PORT

if __name__ == "__main__":
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("app.main:app", host="0.0.0.0", port=HTTP_PORT, reload=reload_enabled)
