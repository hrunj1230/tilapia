import asyncio
import json
import os
import sys
from pathlib import Path
import pytest
import httpx

# 프로젝트 루트를 sys.path에 추가 (어디서 pytest를 실행해도 임포트 가능하도록)
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.settings import (
    SettingsManager,
    validate_interval_minutes,
    validate_langfuse_host,
)
from app.store import BalanceStore
from app.providers import fetch_runpod, fetch_langfuse, fetch_openrouter
from app.poller import Poller
from app.main import app
from scripts.publish_balances import (
    render_html,
    render_markdown,
    format_usd,
    collect_balances,
    main_async,
)


# --------------------------------------------------------------------------
# 1. Runpod 잔액과 일별 요금 파싱
# --------------------------------------------------------------------------
def test_runpod_balance_and_daily_billing_parsing():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "graphql" in url:
                data = {
                    "data": {
                        "myself": {
                            "id": "rp-user-1",
                            "email": "user@example.com",
                            "clientBalance": 150.25,
                            "currentSpendPerHr": 0.45,
                            "clientLifetimeSpend": 500.0,
                            "underBalance": False,
                            "minBalance": 5.0,
                            "creditAlertThreshold": 10.0,
                        }
                    }
                }
                return httpx.Response(200, json=data)
            elif "v2/billing" in url:
                data = {
                    "records": [
                        {
                            "startTime": "2026-10-15T00:00:00Z",
                            "endTime": "2026-10-16T00:00:00Z",
                            "totalAmount": 12.34,
                        },
                        {
                            "startTime": "2026-10-14T00:00:00Z",
                            "endTime": "2026-10-15T00:00:00Z",
                            "totalAmount": None,  # 없는 금액 필드는 0으로
                        },
                    ]
                }
                return httpx.Response(200, json=data)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_runpod(api_key="valid-key", client=client)

        assert res["ok"] is True
        assert res["error"] is None
        assert res["email"] == "user@example.com"
        assert res["balanceUsd"] == 150.25
        assert res["spendPerHourUsd"] == 0.45
        assert res["lifetimeSpendUsd"] == 500.0
        assert res["underBalance"] is False
        assert len(res["daily"]) == 2
        assert res["daily"][0]["totalAmountUsd"] == 12.34
        assert res["daily"][1]["totalAmountUsd"] == 0.0

    asyncio.run(run())


# --------------------------------------------------------------------------
# 2. GraphQL errors 와 빈 myself 는 실패
# --------------------------------------------------------------------------
def test_runpod_graphql_errors_and_empty_myself():
    async def run():
        # Case 2-A: GraphQL errors 배열 존재
        def handler_with_errors(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "errors": [{"message": "Invalid authentication token"}],
                    "data": None,
                },
            )

        transport_err = httpx.MockTransport(handler_with_errors)
        async with httpx.AsyncClient(transport=transport_err) as client:
            res = await fetch_runpod(api_key="test-key", client=client)

        assert res["ok"] is False
        assert "Runpod GraphQL 오류" in res["error"]
        assert res["balanceUsd"] is None

        # Case 2-B: data.myself 가 없음 (빈 myself)
        def handler_empty_myself(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"myself": None}})

        transport_empty = httpx.MockTransport(handler_empty_myself)
        async with httpx.AsyncClient(transport=transport_empty) as client:
            res2 = await fetch_runpod(api_key="test-key", client=client)

        assert res2["ok"] is False
        assert "data.myself" in res2["error"]

    asyncio.run(run())


# --------------------------------------------------------------------------
# 3. Langfuse sum_totalCost 와 totalCost_sum 둘 다 합산
# --------------------------------------------------------------------------
def test_langfuse_sum_totalcost_and_totalcost_sum_both_summed():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json={"data": [{"name": "Prod Project"}]})
            elif "api/public/v2/metrics" in url:
                # 다양한 필드명(sum_totalCost, totalCost_sum, sum(totalCost))을 혼합
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"sum_totalCost": 12.50},
                            {"totalCost_sum": 7.25},
                            {"sum(totalCost)": 2.25},
                        ]
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test",
                secret_key="sk-test",
                client=client,
            )

        assert res["ok"] is True
        assert res["projectName"] == "Prod Project"
        assert res["projectCount"] == 1
        # 12.50 + 7.25 + 2.25 = 22.0
        assert res["monthCostUsd"] == 22.0
        assert "추적된 LLM 비용" in res["note"]

    asyncio.run(run())


# --------------------------------------------------------------------------
# 4. Langfuse 빈 data 는 0
# --------------------------------------------------------------------------
def test_langfuse_empty_data_is_zero():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Empty Project"}])
            elif "api/public/v2/metrics" in url:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test",
                secret_key="sk-test",
                client=client,
            )

        assert res["ok"] is True
        assert res["monthCostUsd"] == 0.0
        assert res["todayCostUsd"] == 0.0
        assert res["error"] is None

    asyncio.run(run())


# --------------------------------------------------------------------------
# 5. 키 없음은 네트워크 호출 0
# --------------------------------------------------------------------------
def test_no_keys_makes_zero_network_calls():
    async def run():
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            # Runpod 키 없음
            res_rp1 = await fetch_runpod(api_key=None, client=client)
            assert res_rp1["ok"] is False
            assert res_rp1["error"] == "API 키가 없습니다"

            res_rp2 = await fetch_runpod(api_key="   ", client=client)
            assert res_rp2["ok"] is False
            assert res_rp2["error"] == "API 키가 없습니다"

            # Langfuse 키 없음 (pk 누락 or sk 누락 or 빈 문자열)
            res_lf1 = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="",
                secret_key="sk-test",
                client=client,
            )
            assert res_lf1["ok"] is False
            assert res_lf1["error"] == "API 키가 없습니다"

            res_lf2 = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test",
                secret_key="",
                client=client,
            )
            assert res_lf2["ok"] is False
            assert res_lf2["error"] == "API 키가 없습니다"

        # 네트워크 호출 횟수는 정확히 0이어야 함
        assert call_count == 0

    asyncio.run(run())


# --------------------------------------------------------------------------
# 6. 허용되지 않은 Langfuse 호스트 거부
# --------------------------------------------------------------------------
def test_disallowed_langfuse_host_rejection():
    # 허용 도메인 확인
    assert validate_langfuse_host("https://cloud.langfuse.com") == "https://cloud.langfuse.com"
    assert validate_langfuse_host("https://us.cloud.langfuse.com") == "https://us.cloud.langfuse.com"
    assert validate_langfuse_host("https://eu.cloud.langfuse.com") == "https://eu.cloud.langfuse.com"
    assert validate_langfuse_host("https://jp.cloud.langfuse.com") == "https://jp.cloud.langfuse.com"
    assert validate_langfuse_host("https://hipaa.cloud.langfuse.com") == "https://hipaa.cloud.langfuse.com"

    # http 거부
    with pytest.raises(ValueError, match="https 프로토콜만"):
        validate_langfuse_host("http://cloud.langfuse.com")

    # IP 거부
    with pytest.raises(ValueError):
        validate_langfuse_host("https://127.0.0.1")

    with pytest.raises(ValueError):
        validate_langfuse_host("https://192.168.1.100")

    # 사용자 정보 포함 거부
    with pytest.raises(ValueError, match="사용자 정보"):
        validate_langfuse_host("https://user:pass@cloud.langfuse.com")

    # 포트 포함 거부
    with pytest.raises(ValueError, match="포트 번호"):
        validate_langfuse_host("https://cloud.langfuse.com:8080")

    # 비허용 외부 도메인 거부
    with pytest.raises(ValueError, match="허용되지 않은"):
        validate_langfuse_host("https://attacker.com")

    with pytest.raises(ValueError, match="허용되지 않은"):
        validate_langfuse_host("https://my-self-hosted-langfuse.internal")


# --------------------------------------------------------------------------
# 7. 설정 API가 키 원문을 반환하지 않음
# --------------------------------------------------------------------------
def test_settings_api_does_not_return_raw_keys(tmp_path: Path):
    async def run():
        secret_key = "super-secret-api-key-9999"
        public_key = "super-public-api-key-1111"
        runpod_key = "super-runpod-secret-key-8888"

        settings = SettingsManager(data_dir=tmp_path)
        settings.update(
            {
                "runpodApiKey": runpod_key,
                "langfusePublicKey": public_key,
                "langfuseSecretKey": secret_key,
            }
        )

        masked = settings.get_masked()

        # 원문이 결과 어디에도 포함되어 있지 않아야 함
        json_str = json.dumps(masked)
        assert secret_key not in json_str
        assert public_key not in json_str
        assert runpod_key not in json_str

        # 마스킹 검증: isSet True 및 끝 4자리만 존재
        assert masked["runpodApiKey"]["isSet"] is True
        assert masked["runpodApiKey"]["last4"] == "8888"
        assert masked["langfuseSecretKey"]["isSet"] is True
        assert masked["langfuseSecretKey"]["last4"] == "9999"
        assert masked["langfusePublicKey"]["isSet"] is True
        assert masked["langfusePublicKey"]["last4"] == "1111"

        # PUT 빈 문자열 전달 시 기존 키 유지 확인
        settings.update({"runpodApiKey": ""})
        assert settings.runpod_api_key == runpod_key

        # PUT null 전달 시 키 삭제 확인
        settings.update({"runpodApiKey": None})
        assert settings.runpod_api_key == ""
        assert settings.get_masked()["runpodApiKey"]["isSet"] is False

    asyncio.run(run())


# --------------------------------------------------------------------------
# 8. 한 제공자 실패가 다른 제공자 성공 값을 지우지 않음
# --------------------------------------------------------------------------
def test_one_provider_failure_does_not_clear_other_provider_success(tmp_path: Path):
    async def run():
        settings = SettingsManager(data_dir=tmp_path)
        settings.runpod_api_key = "dummy-rp-key"
        settings.langfuse_public_key = "dummy-lf-pk"
        settings.langfuse_secret_key = "dummy-lf-sk"

        store = BalanceStore(db_path=tmp_path / "balances.sqlite")

        # 1회차: Runpod 성공 ($100.0), Langfuse 성공 ($30.0)
        cycle = 1

        def dynamic_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "runpod.io/graphql" in url:
                if cycle == 2:
                    # 2회차에서 Runpod 실패
                    return httpx.Response(500, text="Internal Server Error")
                return httpx.Response(
                    200, json={"data": {"myself": {"clientBalance": 100.0}}}
                )
            elif "runpod.io/v2/billing" in url:
                return httpx.Response(200, json={"records": []})
            elif "langfuse.com/api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Project"}])
            elif "langfuse.com/api/public/v2/metrics" in url:
                if cycle == 3:
                    # 3회차에서 Langfuse 실패 (429)
                    return httpx.Response(429, text="Rate Limited")
                return httpx.Response(200, json={"data": [{"sum_totalCost": 30.0}]})
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(dynamic_handler))
        poller = Poller(settings_mgr=settings, store=store, client=mock_client)

        # 1회차 실행
        st1 = await poller.refresh()
        assert st1["runpod"]["ok"] is True
        assert st1["runpod"]["balanceUsd"] == 100.0
        assert st1["langfuse"]["ok"] is True
        assert st1["langfuse"]["monthCostUsd"] == 30.0

        # 2회차 실행: Runpod 실패, Langfuse 성공
        cycle = 2
        st2 = await poller.refresh()
        # Runpod는 실패 상태이지만, 이전 성공 금액 $100.0 은 보존되어야 함!
        assert st2["runpod"]["ok"] is False
        assert st2["runpod"]["error"] is not None
        assert st2["runpod"]["balanceUsd"] == 100.0
        # Langfuse는 여전히 성공
        assert st2["langfuse"]["ok"] is True
        assert st2["langfuse"]["monthCostUsd"] == 30.0

        # 3회차 실행: Runpod 복구, Langfuse 429 실패
        cycle = 3
        st3 = await poller.refresh()
        assert st3["runpod"]["ok"] is True
        assert st3["runpod"]["balanceUsd"] == 100.0
        # Langfuse는 429로 실패했으나 프로세스 죽지 않고 이전 성공 값 $30.0 유지
        assert st3["langfuse"]["ok"] is False
        assert "429" in st3["langfuse"]["error"]
        assert st3["langfuse"]["monthCostUsd"] == 30.0

        await mock_client.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# 9. interval 0 과 61 은 거부, 5는 허용
# --------------------------------------------------------------------------
def test_interval_validation_and_api_rejection(tmp_path: Path):
    async def run():
        # 단위 함수 검증
        assert validate_interval_minutes(5) == 5
        assert validate_interval_minutes(1) == 1
        assert validate_interval_minutes(60) == 60

        with pytest.raises(ValueError):
            validate_interval_minutes(0)

        with pytest.raises(ValueError):
            validate_interval_minutes(61)

        with pytest.raises(ValueError):
            validate_interval_minutes(-10)

        with pytest.raises(ValueError):
            validate_interval_minutes("abc")

        # FastAPI 엔드포인트 검증
        test_settings = SettingsManager(data_dir=tmp_path)
        test_store = BalanceStore(db_path=tmp_path / "test.sqlite")
        test_poller = Poller(settings_mgr=test_settings, store=test_store)

        app.state.settings_mgr = test_settings
        app.state.store = test_store
        app.state.poller = test_poller

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 0 입력 시 400 거부
            res_zero = await client.put("/api/settings", json={"intervalMinutes": 0})
            assert res_zero.status_code == 400

            # 61 입력 시 400 거부
            res_61 = await client.put("/api/settings", json={"intervalMinutes": 61})
            assert res_61.status_code == 400

            # 5 입력 시 200 허용
            res_5 = await client.put("/api/settings", json={"intervalMinutes": 5})
            assert res_5.status_code == 200
            data = res_5.json()
            assert data["intervalMinutes"] == 5

    asyncio.run(run())


# --------------------------------------------------------------------------
# 10. 동시 조회 방지 및 2초 초과 시 후속 조회 1회 체이닝 검증
# --------------------------------------------------------------------------
def test_poller_concurrency_and_two_second_rule(tmp_path: Path):
    async def run():
        import time

        settings = SettingsManager(data_dir=tmp_path)
        settings.runpod_api_key = "dummy-rp-key"
        settings.langfuse_public_key = "dummy-lf-pk"
        settings.langfuse_secret_key = "dummy-lf-sk"
        store = BalanceStore(db_path=tmp_path / "balances.sqlite")

        poll_count = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            url = str(request.url)
            if "runpod.io/graphql" in url:
                poll_count += 1
                await asyncio.sleep(0.05)
                return httpx.Response(
                    200, json={"data": {"myself": {"clientBalance": 100.0 + poll_count}}}
                )
            elif "runpod.io/v2/billing" in url:
                return httpx.Response(200, json={"records": []})
            elif "langfuse.com/api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Project"}])
            elif "langfuse.com/api/public/v2/metrics" in url:
                return httpx.Response(200, json={"data": [{"sum_totalCost": 10.0}]})
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_handler))
        poller = Poller(settings_mgr=settings, store=store, client=mock_client)

        # 1. 2초 이내 동시 요청: 1번만 실행되고 동일한 결과를 공유
        task1 = asyncio.create_task(poller.refresh())
        task2 = asyncio.create_task(poller.refresh())

        res1, res2 = await asyncio.gather(task1, task2)
        assert res1["refreshedAt"] == res2["refreshedAt"]
        assert poll_count == 1

        # 2. 2초 초과 시 요청:
        # 첫 번째 작업을 시작하고, 시작 시각을 2.5초 전으로 변경
        first_task = asyncio.create_task(poller._run_poll_cycle())
        poller._current_task = first_task
        poller._current_start_time = time.monotonic() - 2.5

        # 이제 refresh() 호출: elapsed > 2.0 이므로 first_task 완료 후 1회 추가 실행됨
        second_task = asyncio.create_task(poller.refresh())

        await first_task
        res_second = await second_task

        # 첫 번째(1회) + 추가 조회(2회) = 총 3회 실행됨
        assert poll_count == 3
        await mock_client.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# 11. Runpod 잔액 200, 요금 429: ok true, balanceUsd 유지, chargesError 존재, daily 는 빈 배열
# --------------------------------------------------------------------------
def test_runpod_balance_200_billing_429_keeps_balance_and_sets_charges_error():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "graphql" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "myself": {
                                "clientBalance": 200.50,
                                "email": "dev@test.com",
                            }
                        }
                    },
                )
            elif "v2/billing" in url:
                return httpx.Response(429, text="Too Many Requests")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_runpod(api_key="valid-key", client=client)

        assert res["ok"] is True
        assert res["balanceUsd"] == 200.50
        assert res["chargesError"] is not None
        assert "429" in res["chargesError"]
        assert res["daily"] == []

    asyncio.run(run())


# --------------------------------------------------------------------------
# 12. Runpod 요청 URL에 api_key 쿼리가 없고 Authorization 이 "Bearer valid-key" 이다
# --------------------------------------------------------------------------
def test_runpod_request_url_has_no_api_key_query_and_has_bearer_authorization():
    async def run():
        seen_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            url = str(request.url)
            if "graphql" in url:
                return httpx.Response(
                    200,
                    json={"data": {"myself": {"clientBalance": 10.0}}},
                )
            elif "v2/billing" in url:
                return httpx.Response(200, json={"records": []})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await fetch_runpod(api_key="valid-key", client=client)

        assert len(seen_requests) == 2
        for req in seen_requests:
            # 1. URL 쿼리 파라미터에 api_key 가 없어야 함
            assert "api_key" not in req.url.query.decode("utf-8")
            # 2. Authorization 헤더가 "Bearer valid-key" 여야 함
            assert req.headers.get("Authorization") == "Bearer valid-key"

    asyncio.run(run())


# --------------------------------------------------------------------------
# 13. Langfuse 이번 달 200, 14일 조회 500: ok true, monthCostUsd 유지, todayCostUsd 는 null, dailyError 존재
# --------------------------------------------------------------------------
def test_langfuse_month_200_daily_500_keeps_month_cost_null_today_cost_and_sets_daily_error():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Proj"}])
            elif "api/public/v2/metrics" in url:
                # 쿼리에 timeDimension이 있으면 14일 조회임
                query_param = request.url.params.get("query", "")
                if "timeDimension" in query_param:
                    return httpx.Response(500, text="Internal Server Error")
                # 이번 달 메트릭스 조회
                return httpx.Response(200, json={"data": [{"sum_totalCost": 45.50}]})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test",
                secret_key="sk-test",
                client=client,
            )

        assert res["ok"] is True
        assert res["monthCostUsd"] == 45.50
        assert res["todayCostUsd"] is None
        assert res["dailyError"] is not None
        assert "500" in res["dailyError"]
        assert res["daily"] == []

    asyncio.run(run())


# --------------------------------------------------------------------------
# 14. 14일 버킷에 오늘 UTC 날짜가 있으면 todayCostUsd 가 그 금액과 같다
# --------------------------------------------------------------------------
def test_langfuse_today_cost_matches_amount_in_14d_bucket():
    async def run():
        from datetime import datetime, timezone

        today_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Proj"}])
            elif "api/public/v2/metrics" in url:
                query_param = request.url.params.get("query", "")
                if "timeDimension" in query_param:
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "time_dimension": "2026-10-10",
                                    "sum_totalCost": 1.25,
                                },
                                {
                                    "time_dimension": f"{today_utc_str}T00:00:00.000Z",
                                    "totalCost_sum": 4.80,
                                },
                            ]
                        },
                    )
                # 이번 달 메트릭스 조회
                return httpx.Response(200, json={"data": [{"sum_totalCost": 100.0}]})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test",
                secret_key="sk-test",
                client=client,
            )

        assert res["ok"] is True
        assert res["monthCostUsd"] == 100.0
        assert res["todayCostUsd"] == 4.80
        assert res["dailyError"] is None
        assert len(res["daily"]) == 2

    asyncio.run(run())


# --------------------------------------------------------------------------
# 15. 체인된 후속 조회가 시작된 뒤 2초 이내 요청도 상태 dict 를 받는다
# --------------------------------------------------------------------------
def test_refresh_during_chained_poll_returns_status(tmp_path: Path):
    async def run():
        settings = SettingsManager(data_dir=tmp_path)
        settings.runpod_api_key = "dummy-rp-key"
        settings.langfuse_public_key = "dummy-lf-pk"
        settings.langfuse_secret_key = "dummy-lf-sk"
        store = BalanceStore(db_path=tmp_path / "balances.sqlite")

        poll_count = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            url = str(request.url)
            if "runpod.io/graphql" in url:
                poll_count += 1
                await asyncio.sleep(0.05)
                return httpx.Response(
                    200, json={"data": {"myself": {"clientBalance": 100.0 + poll_count}}}
                )
            elif "runpod.io/v2/billing" in url:
                return httpx.Response(200, json={"records": []})
            elif "langfuse.com/api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Project"}])
            elif "langfuse.com/api/public/v2/metrics" in url:
                return httpx.Response(200, json={"data": [{"sum_totalCost": 10.0}]})
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_handler))
        poller = Poller(settings_mgr=settings, store=store, client=mock_client)

        # 1회차 조회 시작 후 시작 시각을 2.5초 전으로 당겨 2초 초과 상태로 만든다
        first = asyncio.create_task(poller.refresh())
        await asyncio.sleep(0.01)
        poller._current_start_time -= 2.5

        # 2초 초과 중 요청 -> 1회차 완료 후 체인된 후속 조회를 기다린다
        chained_waiter = asyncio.create_task(poller.refresh())
        res_first = await first

        # 체인된 후속 조회가 막 시작된 시점(2초 이내)의 요청
        late_waiter = asyncio.create_task(poller.refresh())

        res_chained, res_late = await asyncio.gather(chained_waiter, late_waiter)

        # 세 반환값 모두 dict이고 runpod와 langfuse 키를 가진다 (None이면 실패)
        for r in (res_first, res_chained, res_late):
            assert isinstance(r, dict)
            assert r is not None
            assert "runpod" in r
            assert "langfuse" in r

        assert res_late["refreshedAt"] == res_chained["refreshedAt"]
        assert res_late["runpod"]["balanceUsd"] == 102.0
        assert poll_count == 2
        await mock_client.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# 16. N1. 주기 변경 시 nextRefreshAt 재계산 및 루프의 대기 판단 검증
# --------------------------------------------------------------------------
def test_interval_change_and_background_loop_waiting(tmp_path: Path):
    async def run():
        from datetime import datetime, timedelta, timezone

        settings = SettingsManager(data_dir=tmp_path)
        store = BalanceStore(db_path=tmp_path / "balances.sqlite")
        poller = Poller(settings_mgr=settings, store=store)

        # refreshedAt이 있는 상태에서 interval을 60에서 1로 바꾸면 nextRefreshAt이 refreshedAt+1분이 된다
        refreshed_at = datetime(2026, 10, 16, 10, 0, 0, tzinfo=timezone.utc)
        poller.status["refreshedAt"] = refreshed_at.isoformat()
        poller.status["nextRefreshAt"] = (refreshed_at + timedelta(minutes=60)).isoformat()
        poller.status["intervalMinutes"] = 60
        settings.interval_minutes = 60

        poller.update_interval(1)

        expected_next = (refreshed_at + timedelta(minutes=1)).isoformat()
        assert poller.status["nextRefreshAt"] == expected_next
        assert poller.status["intervalMinutes"] == 1

        # 수동 refresh가 nextRefreshAt을 갱신한 뒤, 루프의 대기 판단이 그 시각 전에는 refresh를 호출하지 않는다
        far_future = datetime.now(timezone.utc) + timedelta(minutes=30)
        poller.status["nextRefreshAt"] = far_future.isoformat()

        refresh_called = False

        async def fake_refresh():
            nonlocal refresh_called
            refresh_called = True
            return poller.get_status()

        poller.refresh = fake_refresh

        loop_task = asyncio.create_task(poller.background_loop())
        await asyncio.sleep(0.05)
        poller._stopped = True
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert refresh_called is False

    asyncio.run(run())


# --------------------------------------------------------------------------
# 17. I1. 설정 파일의 필드 하나가 잘못되어도 키를 유지함
# --------------------------------------------------------------------------
def test_settings_load_invalid_interval_keeps_keys(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    data = {
        "intervalMinutes": 0,
        "runpodApiKey": "rpa_test_cccc",
        "langfuseHost": "https://cloud.langfuse.com",
    }
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    mgr = SettingsManager(data_dir=tmp_path)
    # 키는 로드되고 interval은 1~60의 유효 값이다
    assert mgr.runpod_api_key == "rpa_test_cccc"
    assert 1 <= mgr.interval_minutes <= 60

    # 그 상태로 save() 한 파일에도 가짜 키가 남는다
    mgr.save()
    with open(settings_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["runpodApiKey"] == "rpa_test_cccc"
    assert 1 <= saved["intervalMinutes"] <= 60


# --------------------------------------------------------------------------
# 18. N3. 공백만 있는 키 입력이 키를 지우지 않고 유지하며 null은 삭제함
# --------------------------------------------------------------------------
def test_settings_whitespace_key_keeps_existing_and_null_deletes(tmp_path: Path):
    mgr = SettingsManager(data_dir=tmp_path)
    mgr.update({"runpodApiKey": "rpa_test_initial"})
    assert mgr.runpod_api_key == "rpa_test_initial"

    # 기존 키가 있을 때 "   " 업데이트 후 키가 그대로다
    mgr.update({"runpodApiKey": "   "})
    assert mgr.runpod_api_key == "rpa_test_initial"

    # null이면 삭제된다
    mgr.update({"runpodApiKey": None})
    assert mgr.runpod_api_key == ""


# --------------------------------------------------------------------------
# 19. I2. TrustedHostMiddleware 와 Content-Type 검증
# --------------------------------------------------------------------------
def test_trusted_host_and_content_type_enforcement(tmp_path: Path):
    async def run():
        test_settings = SettingsManager(data_dir=tmp_path)
        test_settings.runpod_api_key = ""
        test_settings.langfuse_public_key = ""
        test_settings.langfuse_secret_key = ""
        test_store = BalanceStore(db_path=tmp_path / "test.sqlite")
        test_poller = Poller(settings_mgr=test_settings, store=test_store)

        app.state.settings_mgr = test_settings
        app.state.store = test_store
        app.state.poller = test_poller

        # Host evil.example 은 400
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://evil.example"
        ) as client:
            res_evil = await client.get("/api/status")
            assert res_evil.status_code == 400

        # Content-Type 없는 POST /api/refresh 는 성공하지 않는다 (415)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            res_no_ct = await client.post("/api/refresh")
            assert res_no_ct.status_code == 415

            # application/json POST 는 상태 dict를 반환한다
            res_json = await client.post(
                "/api/refresh",
                headers={"Content-Type": "application/json"},
                content=b"{}",
            )
            assert res_json.status_code == 200
            res_data = res_json.json()
            assert isinstance(res_data, dict)
            assert "runpod" in res_data
            assert "langfuse" in res_data

    asyncio.run(run())


# --------------------------------------------------------------------------
# 20. I3. Langfuse 비용 필드를 모르면 0으로 숨기지 않고 실패 처리
# --------------------------------------------------------------------------
def test_langfuse_unknown_cost_field_fails_month_and_sets_daily_error():
    async def run():
        # 1. 이번 달 data가 [{"time_dimension": "2026-10-01"}] 뿐이면 ok false
        def handler_month_unknown(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Proj"}])
            elif "api/public/v2/metrics" in url:
                query_param = request.url.params.get("query", "")
                if "timeDimension" in query_param:
                    return httpx.Response(200, json={"data": []})
                return httpx.Response(
                    200, json={"data": [{"time_dimension": "2026-10-01"}]}
                )
            return httpx.Response(404)

        transport_month = httpx.MockTransport(handler_month_unknown)
        async with httpx.AsyncClient(transport=transport_month) as client:
            res_month_unknown = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test-aaaa",
                secret_key="sk-test-bbbb",
                client=client,
            )
            assert res_month_unknown["ok"] is False
            assert "비용 필드" in res_month_unknown["error"]

        # 2. 14일 행만 그렇다면 dailyError가 있고 이번 달 금액은 유지
        def handler_daily_unknown(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Proj"}])
            elif "api/public/v2/metrics" in url:
                query_param = request.url.params.get("query", "")
                if "timeDimension" in query_param:
                    return httpx.Response(
                        200, json={"data": [{"time_dimension": "2026-10-01"}]}
                    )
                return httpx.Response(200, json={"data": [{"sum_totalCost": 42.0}]})
            return httpx.Response(404)

        transport_daily = httpx.MockTransport(handler_daily_unknown)
        async with httpx.AsyncClient(transport=transport_daily) as client:
            res_daily_unknown = await fetch_langfuse(
                host="https://cloud.langfuse.com",
                public_key="pk-test-aaaa",
                secret_key="sk-test-bbbb",
                client=client,
            )
            assert res_daily_unknown["ok"] is True
            assert res_daily_unknown["monthCostUsd"] == 42.0
            assert res_daily_unknown["todayCostUsd"] is None
            assert res_daily_unknown["daily"] == []
            assert res_daily_unknown["dailyError"] is not None
            assert "비용 필드" in res_daily_unknown["dailyError"]

    asyncio.run(run())


# --------------------------------------------------------------------------
# 21. OpenRouter credits 100.5, total_usage 25.75 -> remaining 74.75, activity 두 행 합산, Authorization 헤더
# --------------------------------------------------------------------------
def test_openrouter_credits_and_activity_parsing_and_bearer_auth():
    async def run():
        from datetime import datetime, timezone

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        seen_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "total_credits": 100.5,
                            "total_usage": 25.75,
                        }
                    },
                )
            elif "api/v1/activity" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "date": today_str,
                                "usage": 2.5,
                                "byok_usage_inference": 0.5,
                            },
                            {
                                "date": today_str,
                                "usage": 1.25,
                                "byok_usage_inference": 0.75,
                            },
                        ]
                    },
                )
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 3.75,
                            "usage_daily": 3.75,
                            "usage_weekly": 10.0,
                            "usage_monthly": 20.0,
                            "limit": 100.0,
                            "limit_remaining": 80.0,
                            "is_management_key": True,
                        }
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["ok"] is True
        assert res["scope"] == "account"
        assert res["remainingUsd"] == 74.75
        assert res["totalCreditsUsd"] == 100.5
        assert res["totalUsageUsd"] == 25.75
        assert len(res["daily"]) == 1
        # 같은 날짜 두 행의 usage(2.5 + 1.25 = 3.75)와 byok(0.5 + 0.75 = 1.25)가 각각 합쳐짐
        assert res["daily"][0]["date"] == today_str
        assert res["daily"][0]["usageUsd"] == 3.75
        assert res["daily"][0]["byokUsd"] == 1.25
        assert res["todayUsageUsd"] == 3.75

        # 요청 URL에 키가 없고 Authorization은 Bearer sk-or-test-cccc
        assert len(seen_requests) == 3
        for req in seen_requests:
            assert "sk-or-test-cccc" not in str(req.url)
            assert req.headers.get("Authorization") == "Bearer sk-or-test-cccc"

    asyncio.run(run())


# --------------------------------------------------------------------------
# 22. OpenRouter credits 200, activity 429: ok true, remainingUsd 유지, daily [], dailyError 429
# --------------------------------------------------------------------------
def test_openrouter_credits_200_activity_429():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "total_credits": 50.0,
                            "total_usage": 10.0,
                        }
                    },
                )
            elif "api/v1/activity" in url:
                return httpx.Response(429, text="Rate Limited")
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 10.0,
                            "usage_daily": 1.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 10.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["ok"] is True
        assert res["remainingUsd"] == 40.0
        assert res["daily"] == []
        assert res["todayUsageUsd"] is None
        assert res["dailyError"] is not None
        assert "429" in res["dailyError"]

    asyncio.run(run())


# --------------------------------------------------------------------------
# 23. OpenRouter credits 403, key 200: ok true, remainingUsd null, scope key, Management 문구, snapshotAmount 10
# --------------------------------------------------------------------------
def test_openrouter_credits_403_key_200():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/activity" in url:
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 5.0,
                            "usage_daily": 1.0,
                            "usage_weekly": 2.0,
                            "usage_monthly": 3.5,
                            "limit": 20.0,
                            "limit_remaining": 10.0,
                            "is_management_key": False,
                        }
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["ok"] is True
        assert res["remainingUsd"] is None
        assert res["scope"] == "key"
        assert res["creditsError"] is not None
        assert "Management" in res["creditsError"]
        assert res["snapshotAmountUsd"] == 10.0
        assert res["daily"] == []
        assert res["dailyError"] is None

    asyncio.run(run())


# --------------------------------------------------------------------------
# 24. OpenRouter 키가 없으면 핸들러 호출 0
# --------------------------------------------------------------------------
def test_openrouter_no_key_makes_zero_network_calls():
    async def run():
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res_none = await fetch_openrouter(api_key=None, client=client)
            assert res_none["ok"] is False
            assert res_none["error"] == "API 키가 없습니다"

            res_empty = await fetch_openrouter(api_key="", client=client)
            assert res_empty["ok"] is False
            assert res_empty["error"] == "API 키가 없습니다"

            res_ws = await fetch_openrouter(api_key="   ", client=client)
            assert res_ws["ok"] is False
            assert res_ws["error"] == "API 키가 없습니다"

        assert call_count == 0

    asyncio.run(run())


# --------------------------------------------------------------------------
# 25. OpenRouter credits 200 data {} 이면 ok account 성공 아니고 remainingUsd 0 아님
# --------------------------------------------------------------------------
def test_openrouter_credits_empty_data_fails_account_and_remaining_not_zero():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(200, json={"data": {}})
            elif "api/v1/activity" in url:
                return httpx.Response(200, json={"data": []})
            elif "api/v1/key" in url:
                return httpx.Response(404)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["scope"] != "account"
        assert res["ok"] is False
        assert res["remainingUsd"] != 0
        assert res["remainingUsd"] is None

    asyncio.run(run())


# --------------------------------------------------------------------------
# 26. OpenRouter activity usage 누락 행은 전체 표 실패 처리, credits 유지
# --------------------------------------------------------------------------
def test_openrouter_activity_missing_usage_fails_table_but_keeps_credits():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "total_credits": 100.0,
                            "total_usage": 30.0,
                        }
                    },
                )
            elif "api/v1/activity" in url:
                return httpx.Response(200, json={"data": [{"date": "2026-10-01"}]})
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 30.0,
                            "usage_daily": 0.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 30.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["ok"] is True
        assert res["remainingUsd"] == 70.0
        assert res["dailyError"] is not None
        assert res["daily"] == []

    asyncio.run(run())


# --------------------------------------------------------------------------
# 27. Poller 에서 OpenRouter 만 실패 시 직전 크레딧 보존, 타 제공자 성공 유지
# --------------------------------------------------------------------------
def test_poller_openrouter_failure_preserves_previous_credits_and_other_providers(tmp_path: Path):
    async def run():
        settings = SettingsManager(data_dir=tmp_path)
        settings.runpod_api_key = "dummy-rp-key"
        settings.langfuse_public_key = "dummy-lf-pk"
        settings.langfuse_secret_key = "dummy-lf-sk"
        settings.openrouter_api_key = "sk-or-test-cccc"

        store = BalanceStore(db_path=tmp_path / "balances.sqlite")

        cycle = 1

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "runpod.io/graphql" in url:
                return httpx.Response(
                    200, json={"data": {"myself": {"clientBalance": 100.0}}}
                )
            elif "runpod.io/v2/billing" in url:
                return httpx.Response(200, json={"records": []})
            elif "langfuse.com/api/public/projects" in url:
                return httpx.Response(200, json=[{"name": "Team Project"}])
            elif "langfuse.com/api/public/v2/metrics" in url:
                return httpx.Response(200, json={"data": [{"sum_totalCost": 30.0}]})
            elif "openrouter.ai/api/v1/credits" in url:
                if cycle == 2:
                    return httpx.Response(500, text="Internal Server Error")
                return httpx.Response(
                    200,
                    json={"data": {"total_credits": 200.0, "total_usage": 50.0}},
                )
            elif "openrouter.ai/api/v1/activity" in url:
                return httpx.Response(200, json={"data": []})
            elif "openrouter.ai/api/v1/key" in url:
                if cycle == 2:
                    return httpx.Response(500, text="Internal Server Error")
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 50.0,
                            "usage_daily": 2.0,
                            "usage_weekly": 10.0,
                            "usage_monthly": 50.0,
                        }
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        poller = Poller(settings_mgr=settings, store=store, client=mock_client)

        # 1회차: Runpod, Langfuse, OpenRouter 모두 성공
        st1 = await poller.refresh()
        assert st1["runpod"]["ok"] is True
        assert st1["runpod"]["balanceUsd"] == 100.0
        assert st1["langfuse"]["ok"] is True
        assert st1["langfuse"]["monthCostUsd"] == 30.0
        assert st1["openrouter"]["ok"] is True
        assert st1["openrouter"]["remainingUsd"] == 150.0

        # 2회차: OpenRouter만 실패
        cycle = 2
        st2 = await poller.refresh()
        assert st2["runpod"]["ok"] is True
        assert st2["runpod"]["balanceUsd"] == 100.0
        assert st2["langfuse"]["ok"] is True
        assert st2["langfuse"]["monthCostUsd"] == 30.0

        # OpenRouter는 ok False이지만 남은 크레딧(150.0)은 유지되어야 함
        assert st2["openrouter"]["ok"] is False
        assert st2["openrouter"]["error"] is not None
        assert st2["openrouter"]["remainingUsd"] == 150.0

        await mock_client.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# 28. 설정 마스크에 openrouter 원문 없고, 공백은 유지, null은 삭제
# --------------------------------------------------------------------------
def test_settings_openrouter_key_masking_whitespace_and_null(tmp_path: Path):
    async def run():
        mgr = SettingsManager(data_dir=tmp_path)
        test_key = "sk-or-test-cccc"

        mgr.update({"openrouterApiKey": test_key})
        masked = mgr.get_masked()

        # 1. 설정 GET 마스크에 openrouter 원문이 없음
        json_str = json.dumps(masked)
        assert test_key not in json_str
        assert masked["openrouterApiKey"]["isSet"] is True
        assert masked["openrouterApiKey"]["last4"] == "cccc"

        # 2. "   " (공백 문자열)는 유지
        mgr.update({"openrouterApiKey": "   "})
        assert mgr.openrouter_api_key == test_key
        assert mgr.get_masked()["openrouterApiKey"]["isSet"] is True

        # 3. null은 삭제
        mgr.update({"openrouterApiKey": None})
        assert mgr.openrouter_api_key == ""
        assert mgr.get_masked()["openrouterApiKey"]["isSet"] is False

    asyncio.run(run())


# --------------------------------------------------------------------------
# 29. B1. OpenRouter snapshotBasis가 다르면 diffFromPreviousUsd null, 같으면 diff 계산
# --------------------------------------------------------------------------
def test_openrouter_diff_null_when_basis_changes_and_diff_when_same_basis(tmp_path: Path):
    async def run():
        settings = SettingsManager(data_dir=tmp_path)
        settings.openrouter_api_key = "sk-or-test-cccc"
        store = BalanceStore(db_path=tmp_path / "balances.sqlite")

        # 시나리오 1: 계정 남은 크레딧 150 성공 후 다음 사이클이 키 남은 한도 10이면 diff는 null
        cycle = 1

        def handler_basis_change(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                if cycle == 1:
                    return httpx.Response(200, json={"data": {"total_credits": 200.0, "total_usage": 50.0}})
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/activity" in url:
                if cycle == 1:
                    return httpx.Response(200, json={"data": []})
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/key" in url:
                if cycle == 1:
                    return httpx.Response(200, json={"data": {"usage": 50.0, "usage_daily": 1.0, "usage_weekly": 10.0, "usage_monthly": 50.0, "limit": 200.0, "limit_remaining": 150.0, "is_management_key": True}})
                return httpx.Response(200, json={"data": {"usage": 5.0, "usage_daily": 1.0, "usage_weekly": 2.0, "usage_monthly": 5.0, "limit": 20.0, "limit_remaining": 10.0, "is_management_key": False}})
            return httpx.Response(404)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler_basis_change))
        poller = Poller(settings_mgr=settings, store=store, client=mock_client)

        st1 = await poller.refresh()
        assert st1["openrouter"]["ok"] is True
        assert st1["openrouter"]["scope"] == "account"
        assert st1["openrouter"]["snapshotBasis"] == "account_remaining"
        assert st1["openrouter"]["snapshotAmountUsd"] == 150.0
        assert st1["openrouter"]["diffFromPreviousUsd"] is None

        cycle = 2
        st2 = await poller.refresh()
        assert st2["openrouter"]["ok"] is True
        assert st2["openrouter"]["scope"] == "key"
        assert st2["openrouter"]["snapshotBasis"] == "key_limit_remaining"
        assert st2["openrouter"]["snapshotAmountUsd"] == 10.0
        assert st2["openrouter"]["diffFromPreviousUsd"] is None

        await mock_client.aclose()

        # 시나리오 2: 같은 기준으로 150 다음에 140이면 diff는 -10
        store2 = BalanceStore(db_path=tmp_path / "balances2.sqlite")
        cycle2 = 1

        def handler_same_basis(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                if cycle2 == 1:
                    return httpx.Response(200, json={"data": {"total_credits": 200.0, "total_usage": 50.0}})
                return httpx.Response(200, json={"data": {"total_credits": 200.0, "total_usage": 60.0}})
            elif "api/v1/activity" in url:
                return httpx.Response(200, json={"data": []})
            elif "api/v1/key" in url:
                return httpx.Response(200, json={"data": {"usage": 50.0, "usage_daily": 1.0, "usage_weekly": 10.0, "usage_monthly": 50.0, "limit": None, "limit_remaining": None}})
            return httpx.Response(404)

        mock_client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler_same_basis))
        poller2 = Poller(settings_mgr=settings, store=store2, client=mock_client2)

        st2_1 = await poller2.refresh()
        assert st2_1["openrouter"]["snapshotAmountUsd"] == 150.0
        assert st2_1["openrouter"]["snapshotBasis"] == "account_remaining"

        cycle2 = 2
        st2_2 = await poller2.refresh()
        assert st2_2["openrouter"]["snapshotAmountUsd"] == 140.0
        assert st2_2["openrouter"]["snapshotBasis"] == "account_remaining"
        assert st2_2["openrouter"]["diffFromPreviousUsd"] == -10.0

        await mock_client2.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# 30. B2. 숫자가 아닌 BYOK는 0으로 성공 처리하지 않고 dailyError 및 빈 daily 처리
# --------------------------------------------------------------------------
def test_openrouter_non_numeric_byok_fails_daily_table_but_keeps_credits():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(200, json={"data": {"total_credits": 100.0, "total_usage": 20.0}})
            elif "api/v1/activity" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "date": "2026-10-16",
                                "usage": 1,
                                "byok_usage_inference": "1.5",
                            }
                        ]
                    },
                )
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 20.0,
                            "usage_daily": 1.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 20.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res["ok"] is True
        assert res["remainingUsd"] == 80.0
        assert res["dailyError"] is not None
        assert res["daily"] == []
        assert res["todayUsageUsd"] is None

    asyncio.run(run())


# --------------------------------------------------------------------------
# 31. B3. OpenRouter 실패 경로 테스트 고정
# --------------------------------------------------------------------------
def test_openrouter_failure_paths():
    async def run():
        # 1. total_credits: True 처럼 bool이면 계정 성공이 아니고 remainingUsd는 0이나 1이 아니다
        def handler_bool_credits(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(200, json={"data": {"total_credits": True, "total_usage": 10.0}})
            elif "api/v1/activity" in url:
                return httpx.Response(200, json={"data": []})
            elif "api/v1/key" in url:
                return httpx.Response(404)
            return httpx.Response(404)

        transport_bool = httpx.MockTransport(handler_bool_credits)
        async with httpx.AsyncClient(transport=transport_bool) as client:
            res_bool = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_bool["scope"] != "account"
        assert res_bool["ok"] is False
        assert res_bool["remainingUsd"] != 0
        assert res_bool["remainingUsd"] != 1
        assert res_bool["remainingUsd"] is None

        # 2. credits 403이고 key 200 본문에 usage_monthly가 없으면 ok는 false이고, 사용량 필드를 0으로 채우지 않는다
        def handler_missing_monthly(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/activity" in url:
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 10.0,
                            "usage_daily": 1.0,
                            "usage_weekly": 5.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport_missing = httpx.MockTransport(handler_missing_monthly)
        async with httpx.AsyncClient(transport=transport_missing) as client:
            res_missing = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_missing["ok"] is False
        assert res_missing["keyUsageUsd"] is None
        assert res_missing["keyUsageDailyUsd"] is None
        assert res_missing["keyUsageWeeklyUsd"] is None
        assert res_missing["keyUsageMonthlyUsd"] is None

        # 3. activity에 UTC 오늘 날짜가 없으면 todayUsageUsd는 null이다 (0.0이 아님)
        def handler_no_today(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(200, json={"data": {"total_credits": 100.0, "total_usage": 10.0}})
            elif "api/v1/activity" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "date": "2020-01-01",
                                "usage": 5.0,
                                "byok_usage_inference": 0.0,
                            }
                        ]
                    },
                )
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 10.0,
                            "usage_daily": 1.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 10.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport_notoday = httpx.MockTransport(handler_no_today)
        async with httpx.AsyncClient(transport=transport_notoday) as client:
            res_notoday = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_notoday["ok"] is True
        assert res_notoday["todayUsageUsd"] is None
        assert res_notoday["todayUsageUsd"] != 0.0

    asyncio.run(run())


# --------------------------------------------------------------------------
# 32. 한도 표시: JSON limit: null만 keyLimitUnlimited true이고, 누락/비숫자는 false이며 사용량은 유지
# --------------------------------------------------------------------------
def test_openrouter_key_limit_unlimited_and_non_numeric_handling():
    async def run():
        # Case 1: limit: null -> keyLimitUnlimited is True, keyLimitUsd is None
        def handler_unlimited(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403)
            elif "api/v1/activity" in url:
                return httpx.Response(403)
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 15.0,
                            "usage_daily": 2.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 15.0,
                            "limit": None,
                            "limit_remaining": None,
                        }
                    },
                )
            return httpx.Response(404)

        transport_unlim = httpx.MockTransport(handler_unlimited)
        async with httpx.AsyncClient(transport=transport_unlim) as client:
            res_unlim = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_unlim["ok"] is True
        assert res_unlim["keyLimitUnlimited"] is True
        assert res_unlim["keyLimitUsd"] is None
        assert res_unlim["keyUsageMonthlyUsd"] == 15.0

        # Case 2: limit 필드 누락 -> keyLimitUnlimited is False, keyLimitUsd is None, 사용량 유지
        def handler_missing_limit(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403)
            elif "api/v1/activity" in url:
                return httpx.Response(403)
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 15.0,
                            "usage_daily": 2.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 15.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport_missing_lim = httpx.MockTransport(handler_missing_limit)
        async with httpx.AsyncClient(transport=transport_missing_lim) as client:
            res_missing_lim = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_missing_lim["ok"] is True
        assert res_missing_lim["keyLimitUnlimited"] is False
        assert res_missing_lim["keyLimitUsd"] is None
        assert res_missing_lim["keyUsageMonthlyUsd"] == 15.0

        # Case 3: limit이 문자열 등 비숫자 -> keyLimitUnlimited is False, keyLimitUsd is None, 사용량 유지
        def handler_string_limit(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403)
            elif "api/v1/activity" in url:
                return httpx.Response(403)
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 15.0,
                            "usage_daily": 2.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 15.0,
                            "limit": "not-a-number",
                            "limit_remaining": "not-a-number",
                        }
                    },
                )
            return httpx.Response(404)

        transport_str_lim = httpx.MockTransport(handler_string_limit)
        async with httpx.AsyncClient(transport=transport_str_lim) as client:
            res_str_lim = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_str_lim["ok"] is True
        assert res_str_lim["keyLimitUnlimited"] is False
        assert res_str_lim["keyLimitUsd"] is None
        assert res_str_lim["keyUsageMonthlyUsd"] == 15.0
        assert res_str_lim["keyLimitRemainingUsd"] is None

        # Case 4: limit이 유효 숫자 -> keyLimitUnlimited is False, keyLimitUsd == 50.0
        def handler_num_limit(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "api/v1/credits" in url:
                return httpx.Response(403)
            elif "api/v1/activity" in url:
                return httpx.Response(403)
            elif "api/v1/key" in url:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "usage": 15.0,
                            "usage_daily": 2.0,
                            "usage_weekly": 5.0,
                            "usage_monthly": 15.0,
                            "limit": 50.0,
                            "limit_remaining": 35.0,
                        }
                    },
                )
            return httpx.Response(404)

        transport_num_lim = httpx.MockTransport(handler_num_limit)
        async with httpx.AsyncClient(transport=transport_num_lim) as client:
            res_num_lim = await fetch_openrouter(api_key="sk-or-test-cccc", client=client)

        assert res_num_lim["ok"] is True
        assert res_num_lim["keyLimitUnlimited"] is False
        assert res_num_lim["keyLimitUsd"] == 50.0
        assert res_num_lim["keyLimitRemainingUsd"] == 35.0

    asyncio.run(run())


# --------------------------------------------------------------------------
# 33. .env에 RUNPOD_API_KEY가 있고 환경 변수와 settings.json이 없으면 로드되고 last4 마스킹
# --------------------------------------------------------------------------
def test_env_file_loads_key_and_masks_last4(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("RUNPOD_API_KEY=rpa_test_cccc\n", encoding="utf-8")
    old_env = os.environ.pop("RUNPOD_API_KEY", None)
    try:
        data_dir = tmp_path / "data"
        mgr = SettingsManager(data_dir=data_dir, env_file=env_file)
        assert mgr.runpod_api_key == "rpa_test_cccc"

        masked = mgr.get_masked()
        assert masked["runpodApiKey"]["isSet"] is True
        assert masked["runpodApiKey"]["last4"] == "cccc"
        assert "rpa_test_cccc" not in json.dumps(masked)
    finally:
        if old_env is not None:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 34. 프로세스 환경 변수에 다른 값이 있으면 .env가 덮지 못하고 환경 변수 복원
# --------------------------------------------------------------------------
def test_process_environ_precedes_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("RUNPOD_API_KEY=sk-test-bbbb\n", encoding="utf-8")
    old_env = os.environ.get("RUNPOD_API_KEY")
    try:
        os.environ["RUNPOD_API_KEY"] = "rpa_test_cccc"
        mgr = SettingsManager(data_dir=tmp_path / "data", env_file=env_file)
        assert mgr.runpod_api_key == "rpa_test_cccc"
    finally:
        if old_env is None:
            os.environ.pop("RUNPOD_API_KEY", None)
        else:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 35. .env에 키가 있고 settings.json 키가 ""이면 .env 키 유지
# --------------------------------------------------------------------------
def test_empty_settings_json_key_keeps_env_file_key(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("RUNPOD_API_KEY=rpa_test_cccc\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_file = data_dir / "settings.json"
    settings_file.write_text(json.dumps({"runpodApiKey": ""}), encoding="utf-8")

    old_env = os.environ.pop("RUNPOD_API_KEY", None)
    try:
        mgr = SettingsManager(data_dir=data_dir, env_file=env_file)
        assert mgr.runpod_api_key == "rpa_test_cccc"
    finally:
        if old_env is not None:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 36. settings.json 키가 비어 있지 않으면 그 값이 .env보다 우선
# --------------------------------------------------------------------------
def test_non_empty_settings_json_key_precedes_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("RUNPOD_API_KEY=rpa_test_cccc\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_file = data_dir / "settings.json"
    settings_file.write_text(
        json.dumps({"runpodApiKey": "pk-test-aaaa"}), encoding="utf-8"
    )

    old_env = os.environ.pop("RUNPOD_API_KEY", None)
    try:
        mgr = SettingsManager(data_dir=data_dir, env_file=env_file)
        assert mgr.runpod_api_key == "pk-test-aaaa"
    finally:
        if old_env is not None:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 37. .env 값 $(echo leak)는 그대로 저장되고 쉘이 실행되지 않음
# --------------------------------------------------------------------------
def test_env_file_command_substitution_not_executed(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("RUNPOD_API_KEY=$(echo leak)\n", encoding="utf-8")
    old_env = os.environ.pop("RUNPOD_API_KEY", None)
    try:
        mgr = SettingsManager(data_dir=tmp_path / "data", env_file=env_file)
        assert mgr.runpod_api_key == "$(echo leak)"
    finally:
        if old_env is not None:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 38. 파일이 없어도 SettingsManager 생성이 실패하지 않음
# --------------------------------------------------------------------------
def test_non_existent_env_file_does_not_fail(tmp_path: Path):
    non_existent = tmp_path / "non_existent.env"
    old_env = os.environ.pop("RUNPOD_API_KEY", None)
    try:
        mgr = SettingsManager(data_dir=tmp_path / "data", env_file=non_existent)
        assert mgr.runpod_api_key == ""
    finally:
        if old_env is not None:
            os.environ["RUNPOD_API_KEY"] = old_env


# --------------------------------------------------------------------------
# 39. .env 파서 상세 규칙 검증: 주석, export, 따옴표 한 겹 벗김, 백틱 유지, data_dir만 지정 시 미로드
# --------------------------------------------------------------------------
def test_env_file_parser_rules_and_data_dir_isolation(tmp_path: Path):
    env_content = (
        "# 주석 줄\n"
        "\n"
        "export RUNPOD_API_KEY=\"rpa_test_cccc\"\n"
        "OPENROUTER_API_KEY='sk-or-test-dddd'\n"
        "LANGFUSE_PUBLIC_KEY=`echo pk-test-aaaa`\n"
        "LANGFUSE_SECRET_KEY=\"\"sk-test-bbbb\"\"\n"
        "REFRESH_MINUTES=15\n"
        "LANGFUSE_HOST=\"https://us.cloud.langfuse.com\"\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(env_content, encoding="utf-8")

    saved_env = {}
    for k in [
        "RUNPOD_API_KEY",
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "REFRESH_MINUTES",
        "LANGFUSE_HOST",
    ]:
        if k in os.environ:
            saved_env[k] = os.environ.pop(k)

    try:
        mgr = SettingsManager(data_dir=tmp_path / "data", env_file=env_file)
        assert mgr.runpod_api_key == "rpa_test_cccc"
        assert mgr.openrouter_api_key == "sk-or-test-dddd"
        assert mgr.langfuse_public_key == "`echo pk-test-aaaa`"
        assert mgr.langfuse_secret_key == '"sk-test-bbbb"'
        assert mgr.interval_minutes == 15
        assert mgr.langfuse_host == "https://us.cloud.langfuse.com"

        # data_dir=tmp_path만 쓸 때는 실제 프로젝트 .env를 읽지 않음 검증
        mgr_isolated = SettingsManager(data_dir=tmp_path / "isolated")
        assert mgr_isolated.env_file is None
        assert mgr_isolated.runpod_api_key == ""
    finally:
        for k, v in saved_env.items():
            os.environ[k] = v


# --------------------------------------------------------------------------
# 40. 렌더러: 오류 문구에 가짜 키 문자열(<sk-test-bbbb>)이 들어가도 raw HTML에 없어야 함
# --------------------------------------------------------------------------
def test_render_html_escapes_error_containing_fake_key():
    data = {
        "runpod": {"ok": False, "error": "<sk-test-bbbb>", "chargesError": "<sk-test-bbbb-charges>"},
        "langfuse": {"ok": False, "error": "API 키가 없습니다"},
        "openrouter": {"ok": False, "error": "API 키가 없습니다"},
    }
    html = render_html(data)
    # raw <sk-test-bbbb>는 존재하지 않아야 하고 이스케이프되어 있어야 함
    assert "<sk-test-bbbb>" not in html
    assert "&lt;sk-test-bbbb&gt;" in html
    assert "<sk-test-bbbb-charges>" not in html
    assert "&lt;sk-test-bbbb-charges&gt;" in html


# --------------------------------------------------------------------------
# 41. 렌더러: 결과의 error가 <script>를 포함해도 raw <script>가 없음
# --------------------------------------------------------------------------
def test_render_html_escapes_script_tag_in_error():
    data = {
        "runpod": {
            "ok": True,
            "balanceUsd": 50.0,
            "chargesError": "<script>alert('runpod')</script>",
        },
        "langfuse": {
            "ok": False,
            "error": "<script>alert('langfuse')</script>",
            "dailyError": "<script>alert('daily')</script>",
        },
        "openrouter": {
            "ok": False,
            "error": "<script>alert('openrouter')</script>",
            "creditsError": "<script>alert('credits')</script>",
            "keyError": "<script>alert('key')</script>",
            "dailyError": "<script>alert('daily_or')</script>",
        },
    }
    html = render_html(data)
    assert "<script>" not in html
    assert "</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;/script&gt;" in html


# --------------------------------------------------------------------------
# 42. 렌더러: 키를 출력하는 필드가 렌더 입력에 섞여 있어도 HTML/마크다운에 키가 없음
# --------------------------------------------------------------------------
def test_render_omits_key_fields_when_mixed_in_input():
    data = {
        "apiKey": "rpa_test_cccc",
        "secretKey": "rpa_test_cccc",
        "runpodApiKey": "rpa_test_cccc",
        "Authorization": "Bearer rpa_test_cccc",
        "runpod": {
            "ok": True,
            "balanceUsd": 120.50,
            "apiKey": "rpa_test_cccc",
            "runpodApiKey": "rpa_test_cccc",
            "Authorization": "Bearer rpa_test_cccc",
        },
        "langfuse": {
            "ok": True,
            "monthCostUsd": 45.0,
            "publicKey": "rpa_test_cccc",
            "secretKey": "rpa_test_cccc",
        },
        "openrouter": {
            "ok": True,
            "scope": "account",
            "remainingUsd": 80.0,
            "apiKey": "rpa_test_cccc",
        },
    }
    html = render_html(data)
    assert "rpa_test_cccc" not in html

    md = render_markdown(data)
    assert "rpa_test_cccc" not in md


# --------------------------------------------------------------------------
# 43. 렌더러: 금액 및 미설정 시크릿 한국어 안내 검증
# --------------------------------------------------------------------------
def test_render_html_and_markdown_amounts_and_missing_secrets():
    # 1. 금액 정상 표시 및 미설정 시크릿 배너 검증
    data = {
        "runpod": {
            "ok": True,
            "balanceUsd": 150.25,
            "spendPerHourUsd": 0.45,
            "lifetimeSpendUsd": 500.0,
            "underBalance": False,
        },
        "langfuse": {
            "ok": True,
            "monthCostUsd": 32.50,
            "todayCostUsd": 1.25,
            "note": "잔액이 아니라 이번 달 추적된 LLM 비용입니다 (Metrics API observations.totalCost)",
        },
        "openrouter": {
            "ok": True,
            "scope": "account",
            "remainingUsd": 85.0,
            "totalCreditsUsd": 100.0,
            "totalUsageUsd": 15.0,
        },
        "missingSecrets": ["RUNPOD_API_KEY", "OPENROUTER_API_KEY"],
    }
    html = render_html(data)
    assert "$150.25 USD" in html
    assert "$32.50 USD" in html
    assert "$1.25 USD" in html
    assert "$85.00 USD" in html
    assert "잔액이 아니라 이번 달 추적된 LLM 비용입니다" in html
    assert "설정되지 않은 시크릿 안내" in html
    assert "RUNPOD_API_KEY" in html
    assert "OPENROUTER_API_KEY" in html

    md = render_markdown(data)
    assert "$150.25 USD" in md
    assert "$32.50 USD" in md
    assert "$85.00 USD" in md
    assert "설정되지 않은 시크릿 안내" in md
    assert "RUNPOD_API_KEY" in md

    # 2. 금액이 None일 때 '-' 표시 및 OpenRouter key 모드 검증
    data_empty = {
        "runpod": {"ok": False, "balanceUsd": None, "error": "API 키가 없습니다"},
        "langfuse": {"ok": False, "monthCostUsd": None, "todayCostUsd": None, "error": "API 키가 없습니다"},
        "openrouter": {
            "ok": True,
            "scope": "key",
            "keyLimitRemainingUsd": 15.0,
            "keyUsageMonthlyUsd": 5.0,
        },
        "missingSecrets": [],
    }
    html_empty = render_html(data_empty)
    assert "-" in html_empty
    assert "$15.00 USD" in html_empty

    # format_usd 단위 검증
    assert format_usd(None) == "-"
    assert format_usd(False) == "-"
    assert format_usd("invalid") == "-"
    assert format_usd(0) == "$0.00 USD"
    assert format_usd(12.3456) == "$12.3456 USD"


# --------------------------------------------------------------------------
# 44. publish_balances main_async 실행 및 파일 생성 검증 (네트워크 없이 Mock)
# --------------------------------------------------------------------------
def test_publish_balances_main_generates_files(tmp_path: Path, monkeypatch):
    import scripts.publish_balances as pb

    # 프로젝트 루트 및 dist 경로를 임시 디렉터리로 패치
    monkeypatch.setattr(pb, "PROJECT_ROOT", tmp_path)
    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    # collect_balances를 가짜 함수로 대체
    async def fake_collect(settings, client=None):
        return {
            "runpod": {"ok": True, "balanceUsd": 77.77},
            "langfuse": {"ok": True, "monthCostUsd": 22.22},
            "openrouter": {"ok": True, "scope": "account", "remainingUsd": 99.99},
            "missingSecrets": [],
            "generatedAt": "2026-10-16 12:00:00 UTC",
        }

    monkeypatch.setattr(pb, "collect_balances", fake_collect)

    code = asyncio.run(pb.main_async())
    assert code == 0

    dist_index = tmp_path / "dist" / "index.html"
    assert dist_index.is_file()
    content = dist_index.read_text(encoding="utf-8")
    assert "$77.77 USD" in content
    assert "$22.22 USD" in content
    assert "$99.99 USD" in content

    assert summary_file.is_file()
    summary_content = summary_file.read_text(encoding="utf-8")
    assert "$77.77 USD" in summary_content


# --------------------------------------------------------------------------
# 45. Pages 워크플로는 리포트만 배포하고 시크릿을 출력하지 않음
# --------------------------------------------------------------------------
def test_pages_workflow_deploys_report_without_printing_secrets():
    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "balances.yml"
    ).read_text(encoding="utf-8")
    assert "actions/upload-pages-artifact@v3" in workflow
    assert "actions/deploy-pages@v4" in workflow
    assert "path: dist" in workflow
    assert "secrets.LANGFUSE_PUBLIC_KEY" in workflow
    assert "secrets.LANGFUSE_SECRET_KEY" in workflow
    assert "secrets.RUNPOD_API_KEY" in workflow
    assert "secrets.OPENROUTER_API_KEY" in workflow
    lowered = workflow.lower()
    assert "echo ${{" not in lowered
    assert "echo $" not in lowered






