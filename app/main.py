import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.poller import Poller
from app.settings import SettingsManager
from app.store import BalanceStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 프로세스 시작 직후 1회 조회
    asyncio.create_task(app.state.poller.refresh())
    # 백그라운드 주기적 폴링 루프 시작
    app.state.poller.start_background()
    yield
    # 종료 시 정리
    app.state.poller.stop_background()


app = FastAPI(title="Tilapia - Team Money Monitor", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)

# 싱글톤 초기화
settings_mgr = SettingsManager()
store = BalanceStore()
poller = Poller(settings_mgr=settings_mgr, store=store)

app.state.settings_mgr = settings_mgr
app.state.store = store
app.state.poller = poller


def check_json_content_type(request: Request) -> None:
    raw_ct = request.headers.get("content-type")
    if not raw_ct:
        raise HTTPException(
            status_code=415,
            detail="Content-Type 헤더가 필요합니다 (application/json).",
        )
    media_type = raw_ct.split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="Content-Type은 application/json 이어야 합니다.",
        )


@app.get("/")
async def get_index():
    """메인 대시보드 HTML 반환."""
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="index.html 파일을 찾을 수 없습니다.")
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8")


@app.get("/api/status")
async def get_status(request: Request):
    """현재 제공자 상태 반환."""
    curr_poller: Poller = request.app.state.poller
    return JSONResponse(content=curr_poller.get_status())


@app.post("/api/refresh")
async def post_refresh(request: Request):
    """즉시 조회 실행 및 최신 상태 반환."""
    check_json_content_type(request)
    curr_poller: Poller = request.app.state.poller
    status = await curr_poller.refresh()
    return JSONResponse(content=status)


@app.get("/api/settings")
async def get_settings(request: Request):
    """마스킹된 설정 반환 (키 원문 제외)."""
    curr_settings: SettingsManager = request.app.state.settings_mgr
    return JSONResponse(content=curr_settings.get_masked())


@app.put("/api/settings")
async def put_settings(request: Request):
    """설정 변경 및 검증:

    - intervalMinutes: 1~60
    - langfuseHost: 허용 호스트 검증
    - 키 필드: 빈 문자열("") 유지, null 삭제, 문자열 갱신
    """
    check_json_content_type(request)
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400, detail="유효한 JSON 요청 본문이 필요합니다."
        )

    curr_settings: SettingsManager = request.app.state.settings_mgr
    curr_poller: Poller = request.app.state.poller
    try:
        updated = curr_settings.update(body)
        if "intervalMinutes" in body and body["intervalMinutes"] is not None:
            curr_poller.update_interval(curr_settings.interval_minutes)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))

    return JSONResponse(content=updated)


if __name__ == "__main__":
    import uvicorn

    # 127.0.0.1:8765 에만 바인드. 0.0.0.0 금지.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
