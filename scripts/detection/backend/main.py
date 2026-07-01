# # backend/main.py

#working to start all and end all

# from fastapi import FastAPI
# from backend.pipeline_manager import (
#     start_pipeline,
#     stop_pipeline,
#     pipeline_status
# )
# from backend.schemas import StatusResponse, MessageResponse

# app = FastAPI(
#     title="CCTV Analytics Control API",
#     description="Start / Stop / Status of CCTV Analytics Pipeline",
#     version="1.0.0"
# )

# @app.post("/start", response_model=MessageResponse)
# def start():
#     ok, msg = start_pipeline()
#     return {"message": msg}

# @app.post("/stop", response_model=MessageResponse)
# def stop():
#     ok, msg = stop_pipeline()
#     return {"message": msg}

# @app.get("/status", response_model=StatusResponse)
# def status():
#     return {"status": pipeline_status()}













#seelcted start working properly but stop is not woprking


# from fastapi import FastAPI
# from backend.schemas import StartRequest, StatusResponse, MessageResponse
# from backend.pipeline_manager import (
#     start_pipeline,
#     stop_pipeline,
#     pipeline_status
# )

# app = FastAPI(
#     title="CCTV Analytics Control API",
#     version="1.0"
# )

# @app.post("/start", response_model=MessageResponse)
# def start(req: StartRequest):
#     ok, msg = start_pipeline(req.analytics)
#     return {"message": msg}

# @app.post("/stop", response_model=MessageResponse)
# def stop():
#     ok, msg = stop_pipeline()
#     return {"message": msg}

# @app.get("/status", response_model=StatusResponse)
# def status():
#     running, analytics = pipeline_status()
#     return {
#         "running": running,
#         "analytics": analytics
#     }




















# from fastapi import FastAPI
# from backend.schemas import (
#     StartRequest,
#     StopRequest,
#     StatusResponse,
#     MessageResponse
# )
# from backend.pipeline_manager import (
#     start_analytics,
#     stop_analytics,
#     status_analytics
# )

# app = FastAPI(
#     title="CCTV Analytics Control API",
#     version="3.0"
# )

# @app.post("/start", response_model=MessageResponse)
# def start(req: StartRequest):
#     started = start_analytics(req.analytics)
#     return {"message": f"Started analytics: {started}"}

# @app.post("/stop", response_model=MessageResponse)
# def stop(req: StopRequest):
#     stopped = stop_analytics(req.analytics)
#     return {"message": f"Stopped analytics: {stopped}"}

# @app.get("/status", response_model=StatusResponse)
# def status():
#     running, stopped = status_analytics()
#     return {"running": running, "stopped": stopped}











# backend/main.py
#all start / all stop wali functionality added nhi he but selected start and stop is working properly(lag)
# from fastapi import FastAPI
# from backend.schemas import (
#     StartRequest,
#     StopRequest,
#     StatusResponse,
#     MessageResponse
# )
# from backend.pipeline_manager import (
#     start_analytics,
#     stop_analytics,
#     status_analytics,
#     cleanup_old_pipelines,
#     processes
# )

# cleanup_old_pipelines()   # 🔥 KILL OLD ORPHANS ON START

# app = FastAPI(
#     title="CCTV Analytics Control API",
#     version="FINAL"
# )

# @app.post("/start", response_model=MessageResponse)
# def start(req: StartRequest):
#     started = start_analytics(req.analytics)
#     return {"message": f"Started analytics: {started}"}

# @app.post("/stop", response_model=MessageResponse)
# def stop(req: StopRequest):
#     stopped = stop_analytics(req.analytics)
#     return {"message": f"Stopped analytics: {stopped}"}

# @app.get("/status", response_model=StatusResponse)
# def status():
#     running, stopped = status_analytics()
#     return {"running": running, "stopped": stopped}

# @app.on_event("shutdown")
# def shutdown_cleanup():
#     print("⛔ Backend shutting down → stopping all analytics")
#     stop_analytics(list(processes.keys()))





















#main.py
from fastapi import FastAPI, HTTPException
from backend.schemas import (
    StartRequest,
    StopRequest,
    StatusResponse,
    MessageResponse
)
from backend.pipeline_manager import (
    start_analytics,
    stop_analytics,
    status_analytics,
    cleanup_old_pipelines,
    processes,
    ALL_ANALYTICS
)

cleanup_old_pipelines()

app = FastAPI(
    title="CCTV Analytics Control API",
    version="FINAL+ALL"
)

@app.post("/start", response_model=MessageResponse)
def start(req: StartRequest):
    if req.all:
        started = start_analytics(ALL_ANALYTICS)
        return {"message": f"Started ALL analytics: {started}"}

    if not req.analytics:
        raise HTTPException(400, "Provide analytics list or set all=true")

    started = start_analytics(req.analytics)
    return {"message": f"Started analytics: {started}"}

@app.post("/stop", response_model=MessageResponse)
def stop(req: StopRequest):
    if req.all:
        stopped = stop_analytics(list(processes.keys()))
        return {"message": f"Stopped ALL analytics: {stopped}"}

    if not req.analytics:
        raise HTTPException(400, "Provide analytics list or set all=true")

    stopped = stop_analytics(req.analytics)
    return {"message": f"Stopped analytics: {stopped}"}

@app.get("/status", response_model=StatusResponse)
def status():
    running, stopped = status_analytics()
    return {"running": running, "stopped": stopped}

@app.on_event("shutdown")
def shutdown_cleanup():
    print("⛔ Backend shutting down → stopping all analytics")
    stop_analytics(list(processes.keys()))
