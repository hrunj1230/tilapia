import asyncio
import copy
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import httpx

from app.providers import LANGFUSE_NOTE, fetch_langfuse, fetch_runpod, fetch_openrouter
from app.settings import SettingsManager
from app.store import BalanceStore


class Poller:
    """Runpod, Langfuse 및 OpenRouter 데이터 주기적 / 즉시 조회 및 상태 관리자.

    - 프로세스 시작 직후 1회 조회, 이후 intervalMinutes 마다 자동 조회.
    - 지금 조회 버튼 (refresh)과 자동 주기는 동일한 조회 로직 공유.
    - 중복 조회 방지 (2초 룰: 2초 이내면 진행 중 조회 대기, 2초 초과면 완료 후 추가 1회 실행).
    - 장애 격리: 한 쪽 실패 시 다른 쪽의 마지막 성공 값 유지, 실패한 쪽도 이전 성공 값은 남기고 오류 표기.
    - 429 등 예외로 프로세스 중단 방지.
    """

    def __init__(
        self,
        settings_mgr: SettingsManager,
        store: BalanceStore,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.settings_mgr = settings_mgr
        self.store = store
        self.client = client

        # 마지막 성공 값 저장소
        self.last_successful_runpod: Optional[Dict[str, Any]] = None
        self.last_successful_langfuse: Optional[Dict[str, Any]] = None
        self.last_successful_openrouter: Optional[Dict[str, Any]] = None

        # 현재 상태
        self.status: Dict[str, Any] = {
            "refreshedAt": None,
            "nextRefreshAt": None,
            "intervalMinutes": self.settings_mgr.interval_minutes,
            "langfuse": {
                "ok": False,
                "error": "아직 조회되지 않았습니다",
                "dailyError": None,
                "projectName": None,
                "projectCount": None,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
                "diffFromPreviousUsd": None,
            },
            "runpod": {
                "ok": False,
                "error": "아직 조회되지 않았습니다",
                "chargesError": None,
                "email": None,
                "balanceUsd": None,
                "spendPerHourUsd": None,
                "lifetimeSpendUsd": None,
                "underBalance": None,
                "minBalanceUsd": None,
                "alertThresholdUsd": None,
                "daily": [],
                "diffFromPreviousUsd": None,
            },
            "openrouter": {
                "ok": False,
                "error": "아직 조회되지 않았습니다",
                "scope": None,
                "remainingUsd": None,
                "totalCreditsUsd": None,
                "totalUsageUsd": None,
                "creditsError": None,
                "note": None,
                "todayUsageUsd": None,
                "daily": [],
                "dailyError": None,
                "keyUsageUsd": None,
                "keyUsageDailyUsd": None,
                "keyUsageWeeklyUsd": None,
                "keyUsageMonthlyUsd": None,
                "keyLimitUsd": None,
                "keyLimitRemainingUsd": None,
                "keyLimitUnlimited": False,
                "isManagementKey": None,
                "keyNote": None,
                "keyError": None,
                "snapshotAmountUsd": None,
                "snapshotBasis": None,
                "diffFromPreviousUsd": None,
            },
        }

        # 동시성 제어 필드
        self._lock = asyncio.Lock()
        self._current_task: Optional[asyncio.Task] = None
        self._current_start_time: float = 0.0
        self._pending_future: Optional[asyncio.Future] = None
        self._stopped = False
        self._bg_task: Optional[asyncio.Task] = None

    def get_status(self) -> Dict[str, Any]:
        """현재 상태 반환."""
        res = copy.deepcopy(self.status)
        res["intervalMinutes"] = self.settings_mgr.interval_minutes
        return res

    async def _poll_both(self) -> Dict[str, Any]:
        """서비스들의 데이터를 조회하고 상태를 갱신합니다."""
        # Runpod, Langfuse 및 OpenRouter 동시 호출
        rp_coro = fetch_runpod(
            api_key=self.settings_mgr.runpod_api_key,
            client=self.client,
        )
        lf_coro = fetch_langfuse(
            host=self.settings_mgr.langfuse_host,
            public_key=self.settings_mgr.langfuse_public_key,
            secret_key=self.settings_mgr.langfuse_secret_key,
            client=self.client,
        )
        or_coro = fetch_openrouter(
            api_key=self.settings_mgr.openrouter_api_key,
            client=self.client,
        )

        rp_res, lf_res, or_res = await asyncio.gather(
            rp_coro, lf_coro, or_coro, return_exceptions=True
        )

        # 1. Runpod 결과 처리
        if isinstance(rp_res, Exception):
            rp_result = {
                "ok": False,
                "error": f"Runpod 조회 중 예외 발생: {type(rp_res).__name__}",
                "chargesError": None,
                "email": None,
                "balanceUsd": None,
                "spendPerHourUsd": None,
                "lifetimeSpendUsd": None,
                "underBalance": None,
                "minBalanceUsd": None,
                "alertThresholdUsd": None,
                "daily": [],
            }
        else:
            rp_result = rp_res

        if rp_result["ok"]:
            # 직전 성공 값 대비 증감 계산
            current_bal = rp_result.get("balanceUsd")
            diff = self.store.get_diff_from_previous("runpod", current_bal)
            rp_result["diffFromPreviousUsd"] = diff

            # 스냅샷 저장
            self.store.save_snapshot("runpod", current_bal, rp_result)
            self.last_successful_runpod = copy.deepcopy(rp_result)
            final_rp = rp_result
        else:
            # 실패 시: 이전 성공 값 유지, 오류 문구 표기
            if self.last_successful_runpod is not None:
                final_rp = copy.deepcopy(self.last_successful_runpod)
                final_rp["ok"] = False
                final_rp["error"] = rp_result.get("error") or "조회 실패"
                final_rp["chargesError"] = rp_result.get("chargesError")
            else:
                final_rp = rp_result
                final_rp["diffFromPreviousUsd"] = None

        # 2. Langfuse 결과 처리
        if isinstance(lf_res, Exception):
            lf_result = {
                "ok": False,
                "error": f"Langfuse 조회 중 예외 발생: {type(lf_res).__name__}",
                "dailyError": None,
                "projectName": None,
                "projectCount": None,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }
        else:
            lf_result = lf_res

        if lf_result["ok"]:
            current_cost = lf_result.get("monthCostUsd")
            diff = self.store.get_diff_from_previous("langfuse", current_cost)
            lf_result["diffFromPreviousUsd"] = diff

            self.store.save_snapshot("langfuse", current_cost, lf_result)
            self.last_successful_langfuse = copy.deepcopy(lf_result)
            final_lf = lf_result
        else:
            # 실패 시: 이전 성공 값 유지, 오류 문구 표기
            if self.last_successful_langfuse is not None:
                final_lf = copy.deepcopy(self.last_successful_langfuse)
                final_lf["ok"] = False
                final_lf["error"] = lf_result.get("error") or "조회 실패"
                final_lf["dailyError"] = lf_result.get("dailyError")
            else:
                final_lf = lf_result
                final_lf["diffFromPreviousUsd"] = None

        # 3. OpenRouter 결과 처리
        if isinstance(or_res, Exception):
            or_result = {
                "ok": False,
                "error": f"OpenRouter 조회 중 예외 발생: {type(or_res).__name__}",
                "scope": None,
                "remainingUsd": None,
                "totalCreditsUsd": None,
                "totalUsageUsd": None,
                "creditsError": None,
                "note": None,
                "todayUsageUsd": None,
                "daily": [],
                "dailyError": None,
                "keyUsageUsd": None,
                "keyUsageDailyUsd": None,
                "keyUsageWeeklyUsd": None,
                "keyUsageMonthlyUsd": None,
                "keyLimitUsd": None,
                "keyLimitRemainingUsd": None,
                "keyLimitUnlimited": False,
                "isManagementKey": None,
                "keyNote": None,
                "keyError": None,
                "snapshotAmountUsd": None,
                "snapshotBasis": None,
            }
        else:
            or_result = or_res

        if or_result["ok"]:
            current_amt = or_result.get("snapshotAmountUsd")
            current_basis = or_result.get("snapshotBasis")
            diff = None

            if current_amt is not None and current_basis is not None:
                prev_snap = self.store.get_latest_snapshot("openrouter")
                if prev_snap is not None and prev_snap.get("amount") is not None:
                    prev_basis = prev_snap.get("data", {}).get("snapshotBasis")
                    if prev_basis is not None and prev_basis == current_basis:
                        diff = round(current_amt - float(prev_snap["amount"]), 4)

            or_result["diffFromPreviousUsd"] = diff

            self.store.save_snapshot("openrouter", current_amt, or_result)
            self.last_successful_openrouter = copy.deepcopy(or_result)
            final_or = or_result
        else:
            # 실패 시: 이전 성공 값 유지, 새 결과의 creditsError/dailyError/keyError만 덮어씀
            if self.last_successful_openrouter is not None:
                final_or = copy.deepcopy(self.last_successful_openrouter)
                final_or["ok"] = False
                final_or["error"] = or_result.get("error") or "조회 실패"
                final_or["creditsError"] = or_result.get("creditsError")
                final_or["dailyError"] = or_result.get("dailyError")
                final_or["keyError"] = or_result.get("keyError")
            else:
                final_or = or_result
                final_or["diffFromPreviousUsd"] = None

        now_utc = datetime.now(timezone.utc)
        next_utc = now_utc + timedelta(minutes=self.settings_mgr.interval_minutes)

        self.status = {
            "refreshedAt": now_utc.isoformat(),
            "nextRefreshAt": next_utc.isoformat(),
            "intervalMinutes": self.settings_mgr.interval_minutes,
            "langfuse": final_lf,
            "runpod": final_rp,
            "openrouter": final_or,
        }
        return self.get_status()

    async def _run_poll_cycle(self) -> Dict[str, Any]:
        """단일 폴링 주기 실행 및 체인된 후속 요청 처리."""
        try:
            return await self._poll_both()
        finally:
            async with self._lock:
                pending = self._pending_future
                self._pending_future = None
                if pending and not pending.done():
                    self._current_start_time = time.monotonic()
                    self._current_task = asyncio.create_task(
                        self._run_chained_poll(pending)
                    )
                else:
                    self._current_task = None

    async def _run_chained_poll(self, fut: asyncio.Future) -> Dict[str, Any]:
        """대기 중인 후속 리프레시 요청 실행.

        이 태스크는 _current_task 가 되어 refresh() 가 직접 await 하기도 하므로,
        future 와 같은 결과를 반환하고 예외도 다시 던진다.
        """
        try:
            res = await self._run_poll_cycle()
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        if not fut.done():
            fut.set_result(res)
        return res

    async def refresh(self) -> Dict[str, Any]:
        """POST /api/refresh 및 내부 갱신 호출:

        - 중복 방지
        - 2초 이내: 진행 중 조회 결과 대기
        - 2초 초과: 현재 조회 완료 후 1회 추가 조회
        """
        async with self._lock:
            now = time.monotonic()
            if self._current_task is None or self._current_task.done():
                self._current_start_time = now
                self._current_task = asyncio.create_task(self._run_poll_cycle())
                task_to_wait = self._current_task
            else:
                elapsed = now - self._current_start_time
                if elapsed <= 2.0:
                    task_to_wait = self._current_task
                else:
                    if self._pending_future is None or self._pending_future.done():
                        loop = asyncio.get_running_loop()
                        self._pending_future = loop.create_future()
                    task_to_wait = self._pending_future

        return await task_to_wait

    def update_interval(self, new_interval_minutes: int) -> None:
        """설정 변경 시 호출되어 nextRefreshAt을 재계산하고 상태를 맞춘다:
        nextRefreshAt = refreshedAt + 새 interval.
        refreshedAt이 없으면 now + 새 interval.
        그 시각이 이미 지났으면 백그라운드 루프의 다음 확인 시 곧 조회된다.
        """
        self.settings_mgr.interval_minutes = new_interval_minutes
        self.status["intervalMinutes"] = new_interval_minutes

        now_utc = datetime.now(timezone.utc)
        refreshed_at_str = self.status.get("refreshedAt")
        if refreshed_at_str:
            try:
                refreshed_at = datetime.fromisoformat(refreshed_at_str)
                if refreshed_at.tzinfo is None:
                    refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
                next_refresh = refreshed_at + timedelta(minutes=new_interval_minutes)
            except Exception:
                next_refresh = now_utc + timedelta(minutes=new_interval_minutes)
        else:
            next_refresh = now_utc + timedelta(minutes=new_interval_minutes)

        self.status["nextRefreshAt"] = next_refresh.isoformat()

    async def background_loop(self) -> None:
        """주기적 백그라운드 자동 조회 루프.

        - status["nextRefreshAt"]을 1초 이하 간격으로 다시 읽고, 그 시각이 됐을 때만 refresh() 한다.
        - nextRefreshAt이 아직 없으면 refresh를 즉시 반복하지 말고 잠깐 기다렸다가 다시 본다.
        - 수동 refresh()가 nextRefreshAt을 뒤로 미루면 루프는 그 새 시각을 따른다.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(0.5)
                if self._stopped:
                    break

                next_refresh_str = self.status.get("nextRefreshAt")
                if not next_refresh_str:
                    continue

                try:
                    next_dt = datetime.fromisoformat(next_refresh_str)
                    if next_dt.tzinfo is None:
                        next_dt = next_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                now_utc = datetime.now(timezone.utc)
                if now_utc >= next_dt:
                    await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception:
                # 백그라운드 예외로 루프 종료 방지
                pass

    def start_background(self) -> None:
        """백그라운드 자동 갱신 태스크 시작."""
        self._stopped = False
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = asyncio.create_task(self.background_loop())

    def stop_background(self) -> None:
        """백그라운드 자동 갱신 태스크 중지."""
        self._stopped = True
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
