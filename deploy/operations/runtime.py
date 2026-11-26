"""Container entry point: staging is read-only and cannot start core jobs."""
import os
import sys

sys.path.insert(0,"/code")
from app import app,scheduler
from fastapi.responses import JSONResponse

STAGING=os.environ.get("PANEL_STAGE","fresh") == "staging"
if STAGING:
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    scheduler.remove_all_jobs()

    @app.middleware("http")
    async def readonly(request,call_next):
        subscription="/"+os.environ.get("XRAY_SUBSCRIPTION_PATH","sub").strip("/")
        if request.url.path==subscription or request.url.path.startswith(subscription+"/"):
            return JSONResponse({"detail":"订阅入口尚未启用"},status_code=503)
        if request.method not in ("GET","HEAD") and not (request.method=="POST" and request.url.path=="/api/admin/token"):
            return JSONResponse({"detail":"验收模式仅支持查看，接管后才能修改"},status_code=409)
        return await call_next(request)


@app.get("/_panel_health",include_in_schema=False)
def health():
    return {"status":"ok"}


if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=8000,proxy_headers=True,forwarded_allow_ips="*")
