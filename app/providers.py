import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import httpx

from app.settings import validate_langfuse_host

TIMEOUT_SECONDS = 20.0
LANGFUSE_NOTE = "잔액이 아니라 이번 달 추적된 LLM 비용입니다 (Metrics API observations.totalCost)"
OPENROUTER_ACCOUNT_NOTE = (
    "남은 크레딧은 구매분(total_credits)에서 누적 사용(total_usage)을 뺀 값입니다. "
    "API는 잔액 필드를 따로 주지 않습니다."
)
OPENROUTER_KEY_NOTE = "이 숫자는 지금 넣은 키 자체의 사용량과 한도입니다."
OPENROUTER_MANAGEMENT_KEY_REQUIRED_NOTE = (
    "계정 크레딧과 일별 사용량은 Management API 키가 필요합니다. "
    "아래는 이 키의 사용량과 한도입니다."
)


def _is_number(val: Any) -> bool:
    """int 또는 float인지 검사 (bool은 제외)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _extract_langfuse_cost(row: Dict[str, Any]) -> float:
    """Langfuse 메트릭스 행에서 sum_totalCost, totalCost_sum, sum(totalCost) 중 있는 값을 추출."""
    for key in ("sum_totalCost", "totalCost_sum", "sum(totalCost)"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (ValueError, TypeError):
                continue
    return 0.0


def _has_known_cost_field(row: Dict[str, Any]) -> bool:
    """행에 지원하는 비용 필드(sum_totalCost, totalCost_sum, sum(totalCost))가 존재하는지 검사."""
    return any(
        k in row and row[k] is not None
        for k in ("sum_totalCost", "totalCost_sum", "sum(totalCost)")
    )


def _clean_secret(value: Optional[str]) -> str:
    """시크릿 앞뒤 공백, 감싼 따옴표, Bearer 접두, 줄바꿈을 제거합니다. 원문은 로그에 남기지 않습니다."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if text[:7].lower() == "bearer ":
        text = text[7:].strip()
    return "".join(text.split())


LANGFUSE_REGION_HOSTS = (
    "https://cloud.langfuse.com",
    "https://us.cloud.langfuse.com",
    "https://eu.cloud.langfuse.com",
    "https://jp.cloud.langfuse.com",
    "https://hipaa.cloud.langfuse.com",
)
RUNPOD_MYSELF_QUERY = (
    "query { myself { id email clientBalance currentSpendPerHr "
    "clientLifetimeSpend underBalance minBalance creditAlertThreshold } }"
)
RUNPOD_MYSELF_MIN_QUERY = (
    "query { myself { clientBalance currentSpendPerHr clientLifetimeSpend "
    "underBalance minBalance creditAlertThreshold } }"
)


def _langfuse_candidate_hosts(preferred: str) -> List[str]:
    hosts: List[str] = []
    for host in (preferred, *LANGFUSE_REGION_HOSTS):
        if host not in hosts:
            hosts.append(host)
    return hosts


def _graphql_unauthorized(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except Exception:
        return False
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return False
    for err in errors:
        message = str(err.get("message", err) if isinstance(err, dict) else err)
        if "unauthorized" in message.lower():
            return True
    return False


def _usable_myself(response: httpx.Response) -> Optional[Dict[str, Any]]:
    """clientBalance가 숫자인 myself만 사용한다. 일부 필드 오류는 잔액을 지우지 않는다."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    myself = data.get("myself")
    if not isinstance(myself, dict):
        return None
    balance = myself.get("clientBalance")
    if isinstance(balance, bool) or balance is None:
        return None
    try:
        float(balance)
    except (TypeError, ValueError):
        return None
    return myself


async def fetch_runpod(
    api_key: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Runpod 계정 잔액 및 일별 사용 요금을 조회합니다.

    - API 키가 없으면 네트워크 호출 없이 즉시 오류 반환.
    - GraphQL myself 조회
    - GET /v2/billing?bucketSize=day&lastN=14 일별 요금 조회
    - GraphQL 잔액 조회가 성공하고 GET /v2/billing 이 200이 아니면 ok는 true,
      balanceUsd 등 잔액 필드는 유지, chargesError 문자열 저장, daily는 빈 배열.
    """
    key = _clean_secret(api_key)
    if not key:
        return {
            "ok": False,
            "error": "API 키가 없습니다",
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

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "tilapia-balance-monitor",
    }

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        own_client = True

    try:
        # 1. GraphQL myself 조회. 키는 URL이 아니라 Authorization 헤더에만 둔다.
        #    잔액이 있으면 일부 필드 오류는 무시한다. 잔액이 없고 Unauthorized면 잔액 필드만 다시 조회한다.
        res_gql = await client.post(
            "https://api.runpod.io/graphql",
            headers=headers,
            json={"query": RUNPOD_MYSELF_QUERY},
        )
        myself = _usable_myself(res_gql)
        if myself is None and _graphql_unauthorized(res_gql):
            res_gql = await client.post(
                "https://api.runpod.io/graphql",
                headers=headers,
                json={"query": RUNPOD_MYSELF_MIN_QUERY},
            )
            myself = _usable_myself(res_gql)

        if myself is None:
            if res_gql.status_code == 429:
                return {
                    "ok": False,
                    "error": "요청 한도 초과 (HTTP 429)",
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

            if res_gql.status_code != 200:
                return {
                    "ok": False,
                    "error": f"Runpod GraphQL HTTP 오류 ({res_gql.status_code})",
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

            gql_data = res_gql.json()
            if isinstance(gql_data, dict) and gql_data.get("errors"):
                err_msg = "; ".join(str(e.get("message", e)) for e in gql_data["errors"])
                if "unauthorized" in err_msg.lower():
                    err_msg = (
                        "API 키가 거부되었습니다. GitHub Actions의 RUNPOD_API_KEY에 "
                        "Runpod 콘솔 API Keys 값을 따옴표나 Bearer 없이 다시 넣으세요."
                    )
                return {
                    "ok": False,
                    "error": f"Runpod GraphQL 오류: {err_msg}",
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

            return {
                "ok": False,
                "error": "Runpod data.myself 가 응답에 없습니다",
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

        # 2. 일별 요금 조회: GET https://api.runpod.io/v2/billing?bucketSize=day&lastN=14
        billing_headers = {
            "Authorization": f"Bearer {key}",
        }
        charges_error = None
        daily_list: List[Dict[str, Any]] = []

        try:
            res_billing = await client.get(
                "https://api.runpod.io/v2/billing?bucketSize=day&lastN=14",
                headers=billing_headers,
            )

            if res_billing.status_code == 200:
                charges_error = None
                billing_data = res_billing.json()
                records = billing_data.get("records") or []
                for r in records:
                    start = r.get("startTime") or ""
                    end = r.get("endTime") or ""
                    amt = r.get("totalAmount")
                    daily_list.append(
                        {
                            "start": str(start),
                            "end": str(end),
                            "totalAmountUsd": float(amt) if amt is not None else 0.0,
                        }
                    )
            else:
                charges_error = f"일별 요금 조회 실패 (HTTP {res_billing.status_code})"
                daily_list = []
        except Exception as bill_err:
            charges_error = f"일별 요금 조회 실패 ({type(bill_err).__name__})"
            daily_list = []

        balance_usd = (
            float(myself["clientBalance"])
            if myself.get("clientBalance") is not None
            else None
        )
        spend_hr = (
            float(myself["currentSpendPerHr"])
            if myself.get("currentSpendPerHr") is not None
            else None
        )
        lifetime = (
            float(myself["clientLifetimeSpend"])
            if myself.get("clientLifetimeSpend") is not None
            else None
        )
        min_bal = (
            float(myself["minBalance"])
            if myself.get("minBalance") is not None
            else None
        )
        alert_th = (
            float(myself["creditAlertThreshold"])
            if myself.get("creditAlertThreshold") is not None
            else None
        )

        return {
            "ok": True,
            "error": None,
            "chargesError": charges_error,
            "email": myself.get("email"),
            "balanceUsd": balance_usd,
            "spendPerHourUsd": spend_hr,
            "lifetimeSpendUsd": lifetime,
            "underBalance": bool(myself.get("underBalance")),
            "minBalanceUsd": min_bal,
            "alertThresholdUsd": alert_th,
            "daily": daily_list,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Runpod 조회 실패: {type(exc).__name__}",
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
    finally:
        if own_client:
            await client.aclose()


async def fetch_langfuse(
    host: Optional[str],
    public_key: Optional[str],
    secret_key: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Langfuse 프로젝트 및 추적된 LLM 비용을 조회합니다.

    - API 키가 없으면 네트워크 호출 없이 오류 반환.
    - 호스트 검증.
    - Basic 인증 (pk, sk).
    - 이번 달 합계 조회가 200일 때만 ok true.
    - 오늘 비용은 별도 호출 없이 최근 14일 day 버킷 중 UTC 오늘 날짜의 amount 사용.
    - 14일 조회가 200이 아니면 todayCostUsd null, daily 빈 배열, dailyError 사유 기록, 이번달 금액 유지 및 ok true.
    - 이번달 조회가 실패하면 ok false.
    - 빈 data는 여전히 0이고 오류가 아님.
    """
    public_key = _clean_secret(public_key)
    secret_key = _clean_secret(secret_key)
    if not public_key or not secret_key:
        return {
            "ok": False,
            "error": "API 키가 없습니다",
            "dailyError": None,
            "projectName": None,
            "projectCount": None,
            "monthCostUsd": None,
            "todayCostUsd": None,
            "note": LANGFUSE_NOTE,
            "daily": [],
        }

    try:
        valid_host = validate_langfuse_host(host or "https://cloud.langfuse.com")
    except ValueError as val_err:
        return {
            "ok": False,
            "error": f"Langfuse 호스트 오류: {val_err}",
            "dailyError": None,
            "projectName": None,
            "projectCount": None,
            "monthCostUsd": None,
            "todayCostUsd": None,
            "note": LANGFUSE_NOTE,
            "daily": [],
        }

    auth = httpx.BasicAuth(public_key, secret_key)
    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        own_client = True

    try:
        # 1. 프로젝트 정보 조회. 기본 호스트가 401이면 허용된 리전을 순서대로 시도한다.
        res_proj = None
        auth_rejected = 0
        for candidate in _langfuse_candidate_hosts(valid_host):
            try:
                probed = await client.get(
                    f"{candidate}/api/public/projects",
                    auth=auth,
                )
            except Exception:
                continue
            if probed.status_code == 200:
                valid_host = candidate
                res_proj = probed
                break
            if probed.status_code == 429:
                res_proj = probed
                break
            if probed.status_code in (401, 403):
                auth_rejected += 1
                res_proj = probed
                continue
            res_proj = probed
            break

        if res_proj is None:
            return {
                "ok": False,
                "error": "Langfuse 프로젝트 조회에 연결하지 못했습니다",
                "dailyError": None,
                "projectName": None,
                "projectCount": None,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }
        if res_proj.status_code == 429:
            return {
                "ok": False,
                "error": "요청 한도 초과 (HTTP 429)",
                "dailyError": None,
                "projectName": None,
                "projectCount": None,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }
        if res_proj.status_code != 200:
            region_note = ""
            if auth_rejected > 1:
                region_note = " 허용된 Langfuse 리전을 모두 확인했습니다. 키 쌍을 다시 등록하세요."
            return {
                "ok": False,
                "error": f"Langfuse 프로젝트 조회 HTTP 오류 ({res_proj.status_code}).{region_note}",
                "dailyError": None,
                "projectName": None,
                "projectCount": None,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }

        proj_body = res_proj.json()
        if isinstance(proj_body, list):
            projects = proj_body
        elif isinstance(proj_body, dict):
            projects = proj_body.get("data") or []
        else:
            projects = []

        project_count = len(projects)
        project_name = None
        if projects and isinstance(projects[0], dict):
            project_name = projects[0].get("name")

        now = datetime.now(timezone.utc)
        to_ts = now.isoformat()

        # 2. 이번 달 UTC 1일 00:00:00부터 현재까지
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        month_query = {
            "view": "observations",
            "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
            "dimensions": [],
            "filters": [],
            "fromTimestamp": month_start,
            "toTimestamp": to_ts,
        }
        res_month = await client.get(
            f"{valid_host}/api/public/v2/metrics",
            params={"query": json.dumps(month_query)},
            auth=auth,
        )
        if res_month.status_code == 429:
            return {
                "ok": False,
                "error": "요청 한도 초과 (HTTP 429)",
                "dailyError": None,
                "projectName": project_name,
                "projectCount": project_count,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }
        if res_month.status_code != 200:
            return {
                "ok": False,
                "error": f"Langfuse 이번달 메트릭스 HTTP 오류 ({res_month.status_code})",
                "dailyError": None,
                "projectName": project_name,
                "projectCount": project_count,
                "monthCostUsd": None,
                "todayCostUsd": None,
                "note": LANGFUSE_NOTE,
                "daily": [],
            }

        month_data = res_month.json().get("data") or []
        for row in month_data:
            if isinstance(row, dict) and bool(row) and not _has_known_cost_field(row):
                return {
                    "ok": False,
                    "error": "Langfuse 응답에 비용 필드(sum_totalCost 등)가 없습니다",
                    "dailyError": None,
                    "projectName": project_name,
                    "projectCount": project_count,
                    "monthCostUsd": None,
                    "todayCostUsd": None,
                    "note": LANGFUSE_NOTE,
                    "daily": [],
                }
        month_cost = sum(_extract_langfuse_cost(row) for row in month_data)

        # 3. 최근 14일 일별 메트릭스 조회 (오늘 비용은 별도 호출 없이 14일 버킷에서 추출)
        d14_start = (now - timedelta(days=14)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        daily_query = {
            "view": "observations",
            "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
            "dimensions": [],
            "filters": [],
            "fromTimestamp": d14_start,
            "toTimestamp": to_ts,
            "timeDimension": {"granularity": "day"},
            "orderBy": [{"field": "time_dimension", "direction": "asc"}],
        }

        daily_error = None
        daily_list: List[Dict[str, Any]] = []
        today_cost_usd: Optional[float] = None

        try:
            res_daily = await client.get(
                f"{valid_host}/api/public/v2/metrics",
                params={"query": json.dumps(daily_query)},
                auth=auth,
            )

            if res_daily.status_code == 200:
                daily_rows = res_daily.json().get("data") or []
                has_unknown_cost = any(
                    isinstance(row, dict) and bool(row) and not _has_known_cost_field(row)
                    for row in daily_rows
                )
                if has_unknown_cost:
                    daily_error = "Langfuse 일별 응답에 비용 필드(sum_totalCost 등)가 없습니다"
                    daily_list = []
                    today_cost_usd = None
                else:
                    daily_error = None
                    today_utc_str = now.strftime("%Y-%m-%d")
                    today_sum = 0.0

                    for row in daily_rows:
                        date_val = str(row.get("time_dimension") or "")
                        if "T" in date_val:
                            date_val = date_val.split("T")[0]
                        cost = _extract_langfuse_cost(row)
                        daily_list.append(
                            {
                                "date": date_val,
                                "amountUsd": cost,
                            }
                        )
                        if date_val == today_utc_str:
                            today_sum += cost

                    today_cost_usd = round(today_sum, 4)
            else:
                daily_error = f"최근 14일 일별 조회 실패 (HTTP {res_daily.status_code})"
                daily_list = []
                today_cost_usd = None
        except Exception as d_err:
            daily_error = f"최근 14일 일별 조회 실패 ({type(d_err).__name__})"
            daily_list = []
            today_cost_usd = None

        return {
            "ok": True,
            "error": None,
            "dailyError": daily_error,
            "projectName": project_name,
            "projectCount": project_count,
            "monthCostUsd": round(month_cost, 4),
            "todayCostUsd": today_cost_usd,
            "note": LANGFUSE_NOTE,
            "daily": daily_list,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"Langfuse 조회 실패: {type(exc).__name__}",
            "dailyError": None,
            "projectName": None,
            "projectCount": None,
            "monthCostUsd": None,
            "todayCostUsd": None,
            "note": LANGFUSE_NOTE,
            "daily": [],
        }
    finally:
        if own_client:
            await client.aclose()


async def fetch_openrouter(
    api_key: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """OpenRouter 계정 크레딧, 일별 사용량, 키 정보를 조회합니다.

    - API 키가 없거나 공백이면 네트워크 호출 없이 ok: False 반환.
    - 베이스 URL: https://openrouter.ai 고정.
    - GET /api/v1/credits, GET /api/v1/activity, GET /api/v1/key 동시 호출.
    - 리다이렉트 응답(3xx)은 실패로 처리.
    """
    key = _clean_secret(api_key)
    if not key:
        return {
            "ok": False,
            "error": "API 키가 없습니다",
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

    headers = {
        "Authorization": f"Bearer {key}",
    }

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        own_client = True

    try:
        coros = [
            client.get(
                "https://openrouter.ai/api/v1/credits",
                headers=headers,
                follow_redirects=False,
            ),
            client.get(
                "https://openrouter.ai/api/v1/activity",
                headers=headers,
                follow_redirects=False,
            ),
            client.get(
                "https://openrouter.ai/api/v1/key",
                headers=headers,
                follow_redirects=False,
            ),
        ]
        res_credits, res_activity, res_key = await asyncio.gather(
            *coros, return_exceptions=True
        )

        # 1. Key 응답 파싱
        key_ok = False
        key_error = None
        key_usage_usd = None
        key_usage_daily_usd = None
        key_usage_weekly_usd = None
        key_usage_monthly_usd = None
        key_limit_usd = None
        key_limit_remaining_usd = None
        key_limit_unlimited = False
        is_management_key = None
        budget_remaining_usd = None
        key_status = None

        if isinstance(res_key, Exception):
            key_error = f"키 정보 조회 실패 ({type(res_key).__name__})"
        elif 300 <= res_key.status_code < 400:
            key_error = f"키 정보 조회 실패 (리다이렉트 거부 HTTP {res_key.status_code})"
            key_status = res_key.status_code
        elif res_key.status_code == 200:
            key_status = 200
            try:
                key_json = res_key.json()
                key_data = (
                    key_json.get("data")
                    if isinstance(key_json, dict) and isinstance(key_json.get("data"), dict)
                    else (key_json if isinstance(key_json, dict) else {})
                )
                u = key_data.get("usage")
                ud = key_data.get("usage_daily")
                uw = key_data.get("usage_weekly")
                um = key_data.get("usage_monthly")

                if _is_number(u) and _is_number(ud) and _is_number(uw) and _is_number(um):
                    key_usage_usd = round(float(u), 4)
                    key_usage_daily_usd = round(float(ud), 4)
                    key_usage_weekly_usd = round(float(uw), 4)
                    key_usage_monthly_usd = round(float(um), 4)

                    has_limit_key = "limit" in key_data
                    lim = key_data.get("limit")
                    if has_limit_key and lim is None:
                        key_limit_unlimited = True
                        key_limit_usd = None
                    elif _is_number(lim):
                        key_limit_unlimited = False
                        key_limit_usd = round(float(lim), 4)
                    else:
                        key_limit_unlimited = False
                        key_limit_usd = None

                    lim_rem = key_data.get("limit_remaining")
                    if lim_rem is None:
                        key_limit_remaining_usd = None
                    elif _is_number(lim_rem):
                        key_limit_remaining_usd = round(float(lim_rem), 4)
                    else:
                        key_limit_remaining_usd = None

                    budget = key_data.get("effective_budget")
                    if isinstance(budget, dict) and _is_number(budget.get("remaining_usd")):
                        budget_remaining_usd = round(float(budget["remaining_usd"]), 4)
                    else:
                        budget_remaining_usd = None

                    is_mgmt = key_data.get("is_management_key")
                    is_management_key = is_mgmt if isinstance(is_mgmt, bool) else None

                    key_ok = True
                else:
                    key_error = "키 정보 응답에 유효한 사용량 숫자가 없습니다"
            except Exception as e:
                key_error = f"키 정보 파싱 실패 ({type(e).__name__})"
        elif res_key.status_code in (401, 403):
            key_status = res_key.status_code
            key_error = f"키 정보 조회 권한 없음 (HTTP {res_key.status_code})"
        elif res_key.status_code == 429:
            key_status = 429
            key_error = "키 정보 조회 실패 (HTTP 429)"
        else:
            key_status = res_key.status_code
            key_error = f"키 정보 조회 실패 (HTTP {res_key.status_code})"

        # 2. Credits 응답 파싱
        credits_ok = False
        credits_error = None
        remaining_usd = None
        total_credits_usd = None
        total_usage_usd = None
        credits_status = None

        if isinstance(res_credits, Exception):
            credits_error = f"크레딧 조회 실패 ({type(res_credits).__name__})"
        elif 300 <= res_credits.status_code < 400:
            credits_error = f"크레딧 조회 실패 (리다이렉트 거부 HTTP {res_credits.status_code})"
            credits_status = res_credits.status_code
        elif res_credits.status_code == 200:
            credits_status = 200
            try:
                cr_json = res_credits.json()
                cr_data = (
                    cr_json.get("data")
                    if isinstance(cr_json, dict) and isinstance(cr_json.get("data"), dict)
                    else None
                )
                if cr_data and _is_number(cr_data.get("total_credits")) and _is_number(cr_data.get("total_usage")):
                    tot_cred = float(cr_data["total_credits"])
                    tot_use = float(cr_data["total_usage"])
                    total_credits_usd = round(tot_cred, 4)
                    total_usage_usd = round(tot_use, 4)
                    remaining_usd = round(tot_cred - tot_use, 4)
                    credits_ok = True
                else:
                    credits_error = "크레딧 데이터 형식이 올바르지 않습니다"
            except Exception as e:
                credits_error = f"크레딧 파싱 실패 ({type(e).__name__})"
        elif res_credits.status_code in (401, 403):
            credits_status = res_credits.status_code
            credits_error = f"크레딧 조회 권한 없음 (HTTP {res_credits.status_code})"
        elif res_credits.status_code == 429:
            credits_status = 429
            credits_error = "크레딧 조회 실패 (HTTP 429)"
        else:
            credits_status = res_credits.status_code
            credits_error = f"크레딧 조회 실패 (HTTP {res_credits.status_code})"

        # 3. Credits 결과에 따른 분기
        # Case A: credits 성공 (Scope: account)
        if credits_ok:
            daily_list = []
            daily_error = None
            today_usage_usd = None

            if isinstance(res_activity, Exception):
                daily_error = f"일별 사용량 조회 실패 ({type(res_activity).__name__})"
            elif 300 <= res_activity.status_code < 400:
                daily_error = f"일별 사용량 조회 실패 (리다이렉트 거부 HTTP {res_activity.status_code})"
            elif res_activity.status_code == 429:
                daily_error = "일별 사용량 조회 실패 (HTTP 429)"
            elif res_activity.status_code != 200:
                daily_error = f"일별 사용량 조회 실패 (HTTP {res_activity.status_code})"
            else:
                try:
                    act_json = res_activity.json()
                    act_data = act_json.get("data") if isinstance(act_json, dict) else None
                    if not isinstance(act_data, list):
                        daily_error = "일별 사용량 데이터 형식이 올바르지 않습니다"
                    elif len(act_data) == 0:
                        daily_error = None
                        daily_list = []
                    else:
                        today_utc = datetime.now(timezone.utc).date()
                        # activity의 최근 30 완료일이 잘리지 않도록 오늘과 그 이전 30일을 남긴다.
                        min_date = today_utc - timedelta(days=30)
                        date_map = {}
                        has_format_error = False

                        for row in act_data:
                            if not isinstance(row, dict):
                                continue
                            if not row:
                                continue
                            u_val = row.get("usage")
                            if not _is_number(u_val):
                                has_format_error = True
                                break

                            byok_val = row.get("byok_usage_inference")
                            if byok_val is None:
                                byok_usd = 0.0
                            elif _is_number(byok_val):
                                byok_usd = float(byok_val)
                            else:
                                has_format_error = True
                                break

                            d_str = str(row.get("date") or "")
                            try:
                                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
                            except ValueError:
                                continue

                            if min_date <= d_obj <= today_utc:
                                if d_str not in date_map:
                                    date_map[d_str] = {"usage": 0.0, "byok": 0.0}
                                date_map[d_str]["usage"] += float(u_val)
                                date_map[d_str]["byok"] += byok_usd

                        if has_format_error:
                            daily_error = "일별 사용량 데이터 형식 오류"
                            daily_list = []
                            today_usage_usd = None
                        else:
                            daily_error = None
                            daily_list = [
                                {
                                    "date": d,
                                    "usageUsd": round(date_map[d]["usage"], 4),
                                    "byokUsd": round(date_map[d]["byok"], 4),
                                }
                                for d in sorted(date_map.keys())
                            ]
                            today_str = today_utc.strftime("%Y-%m-%d")
                            if today_str in date_map:
                                today_usage_usd = round(date_map[today_str]["usage"], 4)
                            else:
                                today_usage_usd = None
                except Exception as e:
                    daily_error = f"일별 사용량 파싱 실패 ({type(e).__name__})"

            return {
                "ok": True,
                "error": None,
                "scope": "account",
                "remainingUsd": remaining_usd,
                "totalCreditsUsd": total_credits_usd,
                "totalUsageUsd": total_usage_usd,
                "creditsError": None,
                "note": OPENROUTER_ACCOUNT_NOTE,
                "todayUsageUsd": today_usage_usd,
                "daily": daily_list,
                "dailyError": daily_error,
                "keyUsageUsd": key_usage_usd,
                "keyUsageDailyUsd": key_usage_daily_usd,
                "keyUsageWeeklyUsd": key_usage_weekly_usd,
                "keyUsageMonthlyUsd": key_usage_monthly_usd,
                "keyLimitUsd": key_limit_usd,
                "keyLimitRemainingUsd": key_limit_remaining_usd,
                "keyLimitUnlimited": key_limit_unlimited,
                "budgetRemainingUsd": budget_remaining_usd,
                "isManagementKey": is_management_key,
                "keyNote": OPENROUTER_KEY_NOTE if key_ok else None,
                "keyError": key_error if not key_ok else None,
                "snapshotAmountUsd": remaining_usd,
                "snapshotBasis": "account_remaining",
            }

        # Case B: credits 401 or 403이고 key가 200 (Scope: key)
        if credits_status in (401, 403) and key_ok:
            if key_limit_remaining_usd is not None:
                snapshot_amt = key_limit_remaining_usd
                snapshot_basis = "key_limit_remaining"
            else:
                snapshot_amt = key_usage_monthly_usd
                snapshot_basis = "key_usage_monthly"
            return {
                "ok": True,
                "error": None,
                "scope": "key",
                "remainingUsd": None,
                "totalCreditsUsd": None,
                "totalUsageUsd": None,
                "creditsError": OPENROUTER_MANAGEMENT_KEY_REQUIRED_NOTE,
                "note": None,
                "todayUsageUsd": None,
                "daily": [],
                "dailyError": None,
                "keyUsageUsd": key_usage_usd,
                "keyUsageDailyUsd": key_usage_daily_usd,
                "keyUsageWeeklyUsd": key_usage_weekly_usd,
                "keyUsageMonthlyUsd": key_usage_monthly_usd,
                "keyLimitUsd": key_limit_usd,
                "keyLimitRemainingUsd": key_limit_remaining_usd,
                "keyLimitUnlimited": key_limit_unlimited,
                "budgetRemainingUsd": budget_remaining_usd,
                "isManagementKey": is_management_key,
                "keyNote": OPENROUTER_KEY_NOTE,
                "keyError": None,
                "snapshotAmountUsd": snapshot_amt,
                "snapshotBasis": snapshot_basis,
            }

        # Case C: credits와 key가 모두 실패 (또는 계정/키 성공 조건 미충족)
        # 401/403은 한국어로 권한이 없다고 알린다. 그 외 HTTP는 상태 코드를 포함한다.
        error_msg = None
        if credits_status in (401, 403) or key_status in (401, 403):
            status_code_for_msg = credits_status if credits_status in (401, 403) else key_status
            if status_code_for_msg == 401:
                error_msg = "API 키 인증에 실패했습니다 (HTTP 401)"
            else:
                error_msg = "API 키에 필요한 권한이 없습니다 (HTTP 403)"
        elif credits_error:
            error_msg = credits_error
        elif key_error:
            error_msg = key_error
        else:
            error_msg = "OpenRouter 조회 실패"

        return {
            "ok": False,
            "error": error_msg,
            "scope": None,
            "remainingUsd": None,
            "totalCreditsUsd": None,
            "totalUsageUsd": None,
            "creditsError": credits_error,
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
            "keyError": key_error,
            "snapshotAmountUsd": None,
            "snapshotBasis": None,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": f"OpenRouter 조회 실패: {type(exc).__name__}",
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
    finally:
        if own_client:
            await client.aclose()
