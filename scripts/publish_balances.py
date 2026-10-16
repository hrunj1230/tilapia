"""
scripts/publish_balances.py - GitHub Actions에서 팀원과 공유할 금액 리포트를 생성하는 스크립트.

기존 app.providers의 fetch_runpod, fetch_openrouter를 재사용하여
잔액 및 비용을 조회하고, dist/index.html 및 GITHUB_STEP_SUMMARY 마크다운 리포트를 만듭니다.
보안 규칙:
- 키 원문, Authorization 헤더, URL 쿼리의 키를 HTML, 마크다운, 표준 출력에 절대 출력하지 않습니다.
- 렌더러는 허용된 표시 필드(balance, cost, error, note, scope 등)만 사용합니다.
- 시크릿이 비어 있어도 실패로 프로세스를 종료하지 않고, 리포트에 누락된 키를 한국어로 표시하며 종료 코드 0으로 마칩니다.
"""

import asyncio
from datetime import datetime, timezone
import html
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
import httpx

# 프로젝트 루트를 sys.path에 추가하여 어디서 실행해도 app 모듈을 임포트할 수 있도록 함
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.providers import (
    OPENROUTER_ACCOUNT_NOTE,
    OPENROUTER_KEY_NOTE,
    fetch_openrouter,
    fetch_runpod,
)
from app.settings import SettingsManager


def format_usd(val: Any) -> str:
    """금액 숫자를 USD 형식 문자열로 변환합니다. 값이 없거나 유효하지 않으면 '-'를 반환합니다."""
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return "-"
    f_val = float(val)
    s = f"{f_val:,.4f}"
    int_part, frac_part = s.split(".")
    frac_part = frac_part.rstrip("0")
    if len(frac_part) < 2:
        frac_part = frac_part.ljust(2, "0")
    return f"${int_part}.{frac_part} USD"


def openrouter_limit_text(or_data: Dict[str, Any]) -> str:
    """한도 칸. limit null은 무제한이고, 남은 한도가 없으면 effective budget을 쓴다."""
    if or_data.get("keyLimitUnlimited") is True:
        return "무제한"
    if or_data.get("keyLimitRemainingUsd") is not None:
        return format_usd(or_data.get("keyLimitRemainingUsd"))
    if or_data.get("budgetRemainingUsd") is not None:
        return format_usd(or_data.get("budgetRemainingUsd"))
    return "-"


def safe_escape(val: Any) -> str:
    """외부 문자열을 HTML 안전하게 이스케이프합니다."""
    if val is None:
        return ""
    return html.escape(str(val), quote=True)


PAGE_REFRESH_MARKUP = r"""<style>
.refresh-panel {
  margin: 0 0 24px;
  padding: 16px;
  border: 1px solid var(--card-border);
  border-radius: 12px;
  background: var(--card-bg);
  text-align: left;
}
.refresh-help {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 12px;
}
.refresh-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.refresh-row input {
  width: min(100%, 280px);
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--card-border);
  background: var(--bg);
  color: var(--text-main);
}
.refresh-row button {
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid var(--card-border);
  background: var(--bg);
  color: var(--text-main);
  cursor: pointer;
}
#refresh-balances {
  background: var(--accent);
  color: #0f172a;
  border-color: transparent;
  font-weight: 700;
}
#refresh-status {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-main);
  min-height: 1.2em;
}
</style>
<section class="refresh-panel" aria-label="금액 다시 가져오기">
  <p class="refresh-help">금액은 이 버튼을 누를 때만 다시 가져옵니다. GitHub 토큰은 이 브라우저 탭에만 저장되고, 저장소와 페이지 주소에는 들어가지 않습니다. 토큰이 없는 사람은 마지막 금액을 봅니다. 채팅에 토큰을 붙여 넣지 마세요. 이 저장소의 Actions를 실행할 수 있는 별도 토큰을 만드세요. fine-grained 토큰이면 Actions 읽기와 쓰기만 주세요.</p>
  <div class="refresh-row">
    <label>GitHub 토큰 <input id="token-input" type="password" autocomplete="off" spellcheck="false"></label>
    <button type="button" id="save-token">이 탭에 저장</button>
    <button type="button" id="clear-token">토큰 지우기</button>
    <button type="button" id="refresh-balances">금액 다시 가져오기</button>
  </div>
  <p id="refresh-status"></p>
</section>
<script>
(function () {
  var STORAGE_KEY = "tilapia.githubToken";
  var DISPATCH_URL = "https://api.github.com/repos/hrunj1230/tilapia/actions/workflows/balances.yml/dispatches";
  var RUNS_URL = "https://api.github.com/repos/hrunj1230/tilapia/actions/workflows/balances.yml/runs?per_page=5";
  var input = document.getElementById("token-input");
  var statusEl = document.getElementById("refresh-status");
  var refreshBtn = document.getElementById("refresh-balances");
  var busy = false;

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function readToken() {
    try {
      return sessionStorage.getItem(STORAGE_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  function showSavedState() {
    if (readToken()) {
      setStatus("이 탭에 토큰이 있습니다. 금액 다시 가져오기를 누르면 조회가 시작됩니다.");
    }
  }

  document.getElementById("save-token").addEventListener("click", function () {
    var value = (input.value || "").trim();
    if (!value || /\s/.test(value)) {
      setStatus("토큰을 입력하세요. 공백이 있으면 저장하지 않습니다.");
      return;
    }
    try {
      sessionStorage.setItem(STORAGE_KEY, value);
    } catch (err) {
      setStatus("이 탭에 토큰을 저장하지 못했습니다.");
      return;
    }
    input.value = "";
    setStatus("이 탭에 토큰을 저장했습니다. 페이지 주소와 저장소에는 넣지 않습니다.");
  });

  document.getElementById("clear-token").addEventListener("click", function () {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch (err) {}
    input.value = "";
    setStatus("이 탭의 토큰을 지웠습니다.");
  });

  function githubMessage(status) {
    if (status === 401 || status === 403) {
      return "토큰이 거부되었습니다. 이 저장소의 Actions 쓰기 권한이 있는 토큰을 다시 넣으세요.";
    }
    if (status === 404) {
      return "워크플로를 찾지 못했습니다.";
    }
    return "요청이 거절되었습니다 (HTTP " + status + ").";
  }

  function safeRunUrl(url) {
    if (typeof url === "string" && url.indexOf("https://github.com/hrunj1230/tilapia/actions/runs/") === 0) {
      return url;
    }
    return "https://github.com/hrunj1230/tilapia/actions/workflows/balances.yml";
  }

  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function pickNewRun(runs, knownIds, startedMs, hadBaseline) {
    var skew = hadBaseline ? 60000 : 5000;
    for (var i = 0; i < runs.length; i++) {
      var id = String(runs[i].id);
      if (knownIds[id]) continue;
      var created = Date.parse(runs[i].created_at || "");
      if (isNaN(created) || created < startedMs - skew) continue;
      if (!hadBaseline && runs[i].status === "completed" && created < startedMs) continue;
      return runs[i];
    }
    return null;
  }

  async function pollSelected(pick) {
    var deadline = Date.now() + 4 * 60 * 1000;
    var trackedId = null;
    while (Date.now() < deadline) {
      var response = await fetch(RUNS_URL, {
        headers: { "Accept": "application/vnd.github+json" },
        cache: "no-store"
      });
      if (response.ok) {
        var body = await response.json();
        var runs = body.workflow_runs || [];
        var match = null;
        if (trackedId === null) {
          match = pick(runs);
          if (match) trackedId = String(match.id);
        } else {
          for (var i = 0; i < runs.length; i++) {
            if (String(runs[i].id) === trackedId) {
              match = runs[i];
              break;
            }
          }
        }
        if (match && match.status === "completed") {
          if (match.conclusion === "success") return match;
          setStatus("조회가 실패했습니다. Actions 기록을 확인하세요. " + safeRunUrl(match.html_url));
          return null;
        }
        if (trackedId !== null) {
          setStatus("금액을 가져오는 중입니다. 끝나면 이 페이지를 다시 엽니다.");
        }
      }
      await wait(5000);
    }
    setStatus("아직 끝나지 않았습니다. 잠시 뒤 페이지를 다시 여세요.");
    return null;
  }

  async function waitForNewPage() {
    var current = document.getElementById("generated-at").textContent;
    var deadline = Date.now() + 2 * 60 * 1000;
    while (Date.now() < deadline) {
      var url = window.location.pathname + "?v=" + Date.now();
      try {
        var response = await fetch(url, { cache: "no-store" });
        var text = await response.text();
        var doc = new DOMParser().parseFromString(text, "text/html");
        var next = doc.getElementById("generated-at");
        if (next && next.textContent && next.textContent !== current) {
          window.location.replace(url);
          return;
        }
      } catch (err) {}
      await wait(5000);
    }
    setStatus("조회는 끝났습니다. 페이지를 다시 열면 새 금액이 보입니다.");
  }

  refreshBtn.addEventListener("click", async function () {
    if (busy) return;
    var token = readToken();
    if (!token) {
      setStatus("이 탭에 토큰이 없습니다. Actions 쓰기 권한이 있는 GitHub 토큰을 입력하세요.");
      input.focus();
      return;
    }
    busy = true;
    refreshBtn.disabled = true;
    var startedMs = Date.now();
    setStatus("조회를 요청하는 중입니다.");
    try {
      var knownIds = {};
      var hadBaseline = false;
      var active = null;
      var runsResponse = await fetch(RUNS_URL, {
        headers: { "Accept": "application/vnd.github+json" },
        cache: "no-store"
      });
      if (runsResponse.ok) {
        var existing = await runsResponse.json();
        var existingRuns = existing.workflow_runs || [];
        hadBaseline = true;
        for (var i = 0; i < existingRuns.length; i++) {
          knownIds[String(existingRuns[i].id)] = true;
          if (!active && (existingRuns[i].status === "queued" || existingRuns[i].status === "in_progress")) {
            active = existingRuns[i];
          }
        }
        if (active) {
          setStatus("이미 조회가 진행 중입니다. 끝나면 이 페이지를 다시 엽니다.");
          var activeId = String(active.id);
          var doneActive = await pollSelected(function (runs) {
            for (var j = 0; j < runs.length; j++) {
              if (String(runs[j].id) === activeId) return runs[j];
            }
            return null;
          });
          if (doneActive) await waitForNewPage();
          return;
        }
      }
      var response = await fetch(DISPATCH_URL, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": "Bearer " + token,
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": "2022-11-28"
        },
        body: JSON.stringify({ ref: "main" }),
        redirect: "error"
      });
      if (response.status !== 204) {
        setStatus(githubMessage(response.status));
        return;
      }
      setStatus("조회를 시작했습니다. 끝나면 이 페이지를 다시 엽니다.");
      var finished = await pollSelected(function (runs) {
        return pickNewRun(runs, knownIds, startedMs, hadBaseline);
      });
      if (finished) await waitForNewPage();
    } catch (err) {
      setStatus("GitHub에 연결하지 못했습니다. 잠시 뒤 다시 누르세요.");
    } finally {
      busy = false;
      refreshBtn.disabled = false;
    }
  });

  showSavedState();
})();
</script>"""


def render_html(data: Dict[str, Any]) -> str:
    """조회 결과 dict를 순수하게 받아 독립 실행 가능한 HTML 문자열을 생성합니다.

    보안:
    - 키 관련 필드는 절대 참조하거나 출력하지 않습니다.
    - 허용된 표시 필드(balance, cost, error, note, scope 등)만 사용합니다.
    - 모든 외부 문자열은 html.escape 처리됩니다.
    """
    page_refresh_markup = PAGE_REFRESH_MARKUP
    rp = data.get("runpod") or {}
    or_data = data.get("openrouter") or {}
    missing_secrets: List[str] = data.get("missingSecrets") or []
    gen_time_str = safe_escape(data.get("generatedAt") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    # 1. Runpod 데이터 가공
    rp_ok = bool(rp.get("ok"))
    rp_status_class = "ok" if rp_ok else "err"
    rp_status_text = "정상" if rp_ok else "오류"
    rp_balance = format_usd(rp.get("balanceUsd"))
    rp_error = safe_escape(rp.get("error"))
    rp_charges_error = safe_escape(rp.get("chargesError"))
    rp_spend_hr = format_usd(rp.get("spendPerHourUsd"))
    if rp_spend_hr != "-":
        rp_spend_hr += "/hr"
    rp_lifetime = format_usd(rp.get("lifetimeSpendUsd"))
    rp_min_bal = format_usd(rp.get("minBalanceUsd"))
    rp_alert_th = format_usd(rp.get("alertThresholdUsd"))
    rp_under = bool(rp.get("underBalance"))
    rp_email = safe_escape(rp.get("email"))
    rp_daily: List[Dict[str, Any]] = rp.get("daily") or []

    # 2. OpenRouter 데이터 가공
    or_ok = bool(or_data.get("ok"))
    or_status_class = "ok" if or_ok else "err"
    or_status_text = "정상" if or_ok else "오류"
    or_scope = or_data.get("scope")
    if or_scope == "account":
        or_scope_text = "Management 키 (계정 모드)"
        or_main_label = "남은 크레딧"
        or_main_amount = format_usd(or_data.get("remainingUsd"))
        or_note = safe_escape(or_data.get("note") or OPENROUTER_ACCOUNT_NOTE)
    elif or_scope == "key":
        or_scope_text = "추론용 키 (키 제한 모드)"
        if or_data.get("keyLimitRemainingUsd") is not None:
            or_main_label = "이 키의 남은 한도"
            or_main_amount = format_usd(or_data.get("keyLimitRemainingUsd"))
        else:
            or_main_label = "이 키의 이번 달 사용"
            or_main_amount = format_usd(or_data.get("keyUsageMonthlyUsd"))
        or_note = safe_escape(or_data.get("keyNote") or OPENROUTER_KEY_NOTE)
    else:
        or_scope_text = "계정 확인 중..." if not or_ok else "확인 완료"
        or_main_label = "남은 크레딧"
        or_main_amount = format_usd(or_data.get("remainingUsd"))
        or_note = safe_escape(OPENROUTER_ACCOUNT_NOTE)

    or_error = safe_escape(or_data.get("error"))
    or_credits_error = safe_escape(or_data.get("creditsError"))
    or_key_error = safe_escape(or_data.get("keyError"))
    or_daily_error = safe_escape(or_data.get("dailyError"))
    or_total_credits = format_usd(or_data.get("totalCreditsUsd"))
    or_total_usage = format_usd(or_data.get("totalUsageUsd"))
    or_account_today = format_usd(or_data.get("todayUsageUsd"))
    or_key_today = format_usd(or_data.get("keyUsageDailyUsd"))
    or_key_limit = openrouter_limit_text(or_data)
    or_daily: List[Dict[str, Any]] = or_data.get("daily") or []

    # 미설정 시크릿 배너 조립
    missing_banner_html = ""
    if missing_secrets:
        items = "".join(f"<li><code>{safe_escape(s)}</code></li>" for s in missing_secrets)
        missing_banner_html = f"""
      <div class="missing-banner">
        <div class="banner-title">⚠️ 설정되지 않은 시크릿 안내</div>
        <div class="banner-desc">
          다음 시크릿이 GitHub Actions에 등록되지 않았습니다:
          <ul>{items}</ul>
          저장소의 <strong>Settings → Secrets and variables → Actions</strong>에 등록해주세요.
        </div>
      </div>
        """

    # Runpod 일별 테이블 HTML 조립
    rp_daily_rows_html = ""
    if rp_daily:
        for row in rp_daily:
            st = safe_escape(row.get("start", "").split("T")[0])
            et = safe_escape(row.get("end", "").split("T")[0])
            lbl = st if (st == et or not et) else f"{st} ~ {et}"
            amt = format_usd(row.get("totalAmountUsd"))
            rp_daily_rows_html += f"<tr><td>{lbl}</td><td class='daily-amount'>{amt}</td></tr>"
    else:
        rp_daily_rows_html = "<tr><td colspan='2' class='empty-state'>내역이 없습니다.</td></tr>"

    # OpenRouter 일별 테이블 HTML 조립
    or_daily_rows_html = ""
    if or_daily:
        for row in or_daily:
            dt = safe_escape(row.get("date", ""))
            u_amt = format_usd(row.get("usageUsd"))
            b_amt = format_usd(row.get("byokUsd"))
            or_daily_rows_html += (
                f"<tr><td>{dt}</td><td class='daily-amount'>{u_amt}</td>"
                f"<td class='daily-amount'>{b_amt}</td></tr>"
            )
    else:
        or_daily_rows_html = (
            "<tr><td colspan='3' class='empty-state'>"
            "최근 30일의 완료된 사용 내역이 없습니다.</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tilapia - 팀 비용 & 선불 잔액 리포트</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --green: #4ade80;
      --red: #f87171;
      --yellow: #fbbf24;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg);
      color: var(--text-main);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      line-height: 1.5;
      padding: 32px 16px;
      max-width: 1200px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
    }}
    .title-area h1 {{
      font-size: 24px;
      font-weight: 700;
      color: #fff;
    }}
    .title-area p {{
      font-size: 13px;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .meta-area {{
      font-size: 12px;
      color: var(--text-muted);
      text-align: right;
    }}
    .missing-banner {{
      background-color: rgba(251, 191, 36, 0.1);
      border: 1px solid rgba(251, 191, 36, 0.4);
      border-left: 4px solid var(--yellow);
      padding: 14px 18px;
      border-radius: 6px;
      margin-bottom: 24px;
      font-size: 13px;
    }}
    .banner-title {{
      font-weight: 700;
      color: var(--yellow);
      margin-bottom: 6px;
    }}
    .missing-banner ul {{
      margin: 6px 0 6px 20px;
    }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 24px;
    }}
    .card {{
      background-color: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 12px;
    }}
    .card-title-group h2 {{
      font-size: 18px;
      font-weight: 700;
    }}
    .card-title-group .sub {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 2px;
    }}
    .status-badge {{
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 9999px;
    }}
    .status-badge.ok {{
      background-color: rgba(74, 222, 128, 0.15);
      color: var(--green);
      border: 1px solid rgba(74, 222, 128, 0.3);
    }}
    .status-badge.err {{
      background-color: rgba(248, 113, 113, 0.15);
      color: var(--red);
      border: 1px solid rgba(248, 113, 113, 0.3);
    }}
    .note-box {{
      background-color: rgba(56, 189, 248, 0.1);
      border-left: 4px solid var(--accent);
      padding: 10px 14px;
      border-radius: 4px;
      font-size: 13px;
      color: #e0f2fe;
      line-height: 1.4;
    }}
    .error-box {{
      background-color: rgba(248, 113, 113, 0.1);
      border-left: 4px solid var(--red);
      padding: 10px 14px;
      border-radius: 4px;
      font-size: 13px;
      color: #fecaca;
      word-break: break-all;
    }}
    .warning-badge {{
      display: inline-block;
      background-color: var(--red);
      color: #fff;
      font-size: 11px;
      font-weight: bold;
      padding: 2px 6px;
      border-radius: 4px;
      margin-left: 6px;
    }}
    .main-amount-block {{
      background-color: #0f172a;
      padding: 16px;
      border-radius: 8px;
      border: 1px solid var(--card-border);
    }}
    .main-amount-label {{
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .main-amount-val {{
      font-size: 30px;
      font-weight: 800;
      color: #fff;
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 12px;
    }}
    .stat-item {{
      background-color: rgba(15, 23, 42, 0.5);
      border: 1px solid var(--card-border);
      padding: 10px 12px;
      border-radius: 6px;
    }}
    .stat-item .label {{
      font-size: 11px;
      color: var(--text-muted);
    }}
    .stat-item .val {{
      font-size: 14px;
      font-weight: 600;
      color: var(--text-main);
      margin-top: 2px;
    }}
    .daily-section {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .daily-section h3 {{
      font-size: 13px;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .daily-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .daily-table th {{
      text-align: left;
      padding: 6px 8px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
      font-weight: 500;
    }}
    .daily-table td {{
      padding: 6px 8px;
      border-bottom: 1px solid rgba(51, 65, 85, 0.4);
    }}
    .daily-amount {{
      text-align: right;
      font-family: monospace;
      font-size: 13px;
      font-weight: 600;
    }}
    .empty-state {{
      font-size: 12px;
      color: var(--text-muted);
      text-align: center;
      padding: 12px 0;
    }}
    footer {{
      margin-top: 40px;
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
      border-top: 1px solid var(--card-border);
      padding-top: 20px;
    }}
  </style>
</head>
<body>

  <header>
    <div class="title-area">
      <h1>Tilapia 잔액 & 비용 리포트</h1>
      <p>Runpod 선불 잔액과 OpenRouter 크레딧 요약</p>
    </div>
    <div class="meta-area">
      <div>생성 시각: <strong id="generated-at">{gen_time_str}</strong></div>
      <div>출처: 페이지의 가져오기 버튼</div>
    </div>
  </header>

  {page_refresh_markup}

  {missing_banner_html}

  <main class="cards-grid">

    <!-- Runpod 카드 -->
    <article class="card">
      <div class="card-header">
        <div class="card-title-group">
          <h2>Runpod</h2>
          <div class="sub">{rp_email if rp_email else "선불 크레딧 &amp; GPU 인스턴스"}</div>
        </div>
        <div class="status-badge {rp_status_class}">{rp_status_text}</div>
      </div>

      {f'<div class="error-box">오류: {rp_error}</div>' if rp_error else ''}
      {f'<div class="error-box">일별 요금 오류: {rp_charges_error}</div>' if rp_charges_error else ''}

      <div class="main-amount-block">
        <div class="main-amount-label">
          보유 금액 (선불 크레딧)
          {f'<span class="warning-badge">잔액 부족 경고</span>' if rp_under else ''}
        </div>
        <div class="main-amount-val">{rp_balance}</div>
      </div>

      <div class="stats-grid">
        <div class="stat-item">
          <div class="label">시간당 소진 요금</div>
          <div class="val">{rp_spend_hr}</div>
        </div>
        <div class="stat-item">
          <div class="label">평생 누적 사용액</div>
          <div class="val">{rp_lifetime}</div>
        </div>
        <div class="stat-item">
          <div class="label">최소 잔액 (Min Balance)</div>
          <div class="val">{rp_min_bal}</div>
        </div>
        <div class="stat-item">
          <div class="label">크레딧 알림 임계값</div>
          <div class="val">{rp_alert_th}</div>
        </div>
      </div>

      <div class="daily-section">
        <h3>최근 14일 일별 요금</h3>
        <table class="daily-table">
          <thead>
            <tr>
              <th>기간</th>
              <th style="text-align: right;">금액 (USD)</th>
            </tr>
          </thead>
          <tbody>
            {rp_daily_rows_html}
          </tbody>
        </table>
      </div>
    </article>

    <!-- OpenRouter 카드 -->
    <article class="card">
      <div class="card-header">
        <div class="card-title-group">
          <h2>OpenRouter</h2>
          <div class="sub">{or_scope_text}</div>
        </div>
        <div class="status-badge {or_status_class}">{or_status_text}</div>
      </div>

      <div class="note-box">
        ※ <strong>안내</strong>: {or_note}
      </div>

      {f'<div class="error-box">오류: {or_error}</div>' if or_error else ''}
      {f'<div class="error-box">크레딧 오류: {or_credits_error}</div>' if or_credits_error else ''}
      {f'<div class="error-box">키 오류: {or_key_error}</div>' if or_key_error else ''}
      {f'<div class="error-box">일별 오류: {or_daily_error}</div>' if or_daily_error else ''}

      <div class="main-amount-block">
        <div class="main-amount-label">{or_main_label}</div>
        <div class="main-amount-val">{or_main_amount}</div>
      </div>

      <div class="stats-grid">
        <div class="stat-item">
          <div class="label">누적 구매 (Total Credits)</div>
          <div class="val">{or_total_credits}</div>
        </div>
        <div class="stat-item">
          <div class="label">누적 사용 (Total Usage)</div>
          <div class="val">{or_total_usage}</div>
        </div>
        <div class="stat-item">
          <div class="label">오늘(UTC) 계정 사용</div>
          <div class="val">{or_account_today}</div>
        </div>
        <div class="stat-item">
          <div class="label">이 키의 오늘 사용</div>
          <div class="val">{or_key_today}</div>
        </div>
        <div class="stat-item" style="grid-column: 1 / -1;">
          <div class="label">이 키의 남은 한도</div>
          <div class="val">{or_key_limit}</div>
        </div>
      </div>

      <div class="daily-section">
        <h3>최근 30일 일별 사용량</h3>
        <table class="daily-table">
          <thead>
            <tr>
              <th>날짜</th>
              <th style="text-align: right;">크레딧 사용 (USD)</th>
              <th style="text-align: right;">BYOK 추정 (USD)</th>
            </tr>
          </thead>
          <tbody>
            {or_daily_rows_html}
          </tbody>
        </table>
      </div>
    </article>

  </main>

  <footer>
    <p>본 리포트는 저장소 시크릿을 통해 안전하게 빌드되었으며, 브라우저로 API 키 원문이 전달되지 않습니다. 페이지에 입력한 GitHub 토큰은 이 브라우저 탭에만 남고, 리포트 파일에는 저장되지 않습니다.</p>
  </footer>

</body>
</html>
"""


def render_markdown(data: Dict[str, Any]) -> str:
    """조회 결과 dict를 순수하게 받아 GitHub Step Summary용 마크다운 문자열을 생성합니다."""
    rp = data.get("runpod") or {}
    or_data = data.get("openrouter") or {}
    missing_secrets: List[str] = data.get("missingSecrets") or []
    gen_time_str = safe_escape(data.get("generatedAt") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    lines: List[str] = []
    lines.append("# 💰 Tilapia 잔액 & 비용 리포트")
    lines.append(f"> **생성 시각**: `{gen_time_str}` (UTC)")
    lines.append("")

    if missing_secrets:
        lines.append("> [!WARNING]")
        lines.append("> **설정되지 않은 시크릿 안내**:")
        for s in missing_secrets:
            lines.append(f"> - `{safe_escape(s)}`")
        lines.append(">")
        lines.append("> 저장소의 **Settings → Secrets and variables → Actions**에 등록해주세요.")
        lines.append("")

    # 1. Runpod
    rp_ok = bool(rp.get("ok"))
    rp_balance = format_usd(rp.get("balanceUsd"))
    lines.append(f"## 1. Runpod ({'🟢 정상' if rp_ok else '🔴 오류'})")
    if rp.get("email"):
        lines.append(f"- **계정 이메일**: `{safe_escape(rp.get('email'))}`")
    lines.append(f"- **보유 금액 (선불 크레딧)**: **`{rp_balance}`**")
    if rp.get("underBalance"):
        lines.append("- ⚠️ **잔액 부족 경고**: `underBalance == true`")
    if rp.get("spendPerHourUsd") is not None:
        lines.append(f"- 시간당 소진 요금: `{format_usd(rp.get('spendPerHourUsd'))}/hr`")
    if rp.get("lifetimeSpendUsd") is not None:
        lines.append(f"- 평생 누적 사용액: `{format_usd(rp.get('lifetimeSpendUsd'))}`")
    if rp.get("minBalanceUsd") is not None:
        lines.append(f"- 최소 잔액: `{format_usd(rp.get('minBalanceUsd'))}`")
    if rp.get("alertThresholdUsd") is not None:
        lines.append(f"- 크레딧 알림 임계값: `{format_usd(rp.get('alertThresholdUsd'))}`")
    if rp.get("error"):
        lines.append(f"- 🔴 **오류**: `{safe_escape(rp.get('error'))}`")
    if rp.get("chargesError"):
        lines.append(f"- ⚠️ **일별 요금 오류**: `{safe_escape(rp.get('chargesError'))}`")
    lines.append("")

    # 2. OpenRouter
    or_ok = bool(or_data.get("ok"))
    or_scope = or_data.get("scope")
    lines.append(f"## 2. OpenRouter ({'🟢 정상' if or_ok else '🔴 오류'})")
    if or_scope == "account":
        lines.append("- **모드**: `Management 키 (계정 모드)`")
        lines.append(f"- **남은 크레딧**: **`{format_usd(or_data.get('remainingUsd'))}`**")
        lines.append(f"- 누적 충전 (Total Credits): `{format_usd(or_data.get('totalCreditsUsd'))}`")
        lines.append(f"- 누적 사용 (Total Usage): `{format_usd(or_data.get('totalUsageUsd'))}`")
        if or_data.get("todayUsageUsd") is not None:
            lines.append(f"- 오늘 계정 사용 (UTC): `{format_usd(or_data.get('todayUsageUsd'))}`")
        lines.append(f"- 이 키의 남은 한도: `{openrouter_limit_text(or_data)}`")
        lines.append(f"> ℹ️ *{safe_escape(or_data.get('note') or OPENROUTER_ACCOUNT_NOTE)}*")
    elif or_scope == "key":
        lines.append("- **모드**: `추론용 키 (키 제한 모드)`")
        if or_data.get("keyLimitRemainingUsd") is not None:
            lines.append(f"- **이 키의 남은 한도**: **`{format_usd(or_data.get('keyLimitRemainingUsd'))}`**")
        elif or_data.get("budgetRemainingUsd") is not None:
            lines.append(f"- **이 키의 남은 한도**: **`{format_usd(or_data.get('budgetRemainingUsd'))}`**")
        else:
            lines.append(f"- **이 키의 이번 달 사용**: **`{format_usd(or_data.get('keyUsageMonthlyUsd'))}`**")
        lines.append(f"- 이 키의 오늘 사용: `{format_usd(or_data.get('keyUsageDailyUsd'))}`")
        lines.append(f"- 이 키의 남은 한도: `{openrouter_limit_text(or_data)}`")
        lines.append(f"> ℹ️ *{safe_escape(or_data.get('keyNote') or OPENROUTER_KEY_NOTE)}*")
    else:
        lines.append(f"- **남은 크레딧**: **`{format_usd(or_data.get('remainingUsd'))}`**")
        lines.append(f"> ℹ️ *{safe_escape(OPENROUTER_ACCOUNT_NOTE)}*")

    if or_data.get("error"):
        lines.append(f"- 🔴 **오류**: `{safe_escape(or_data.get('error'))}`")
    if or_data.get("creditsError"):
        lines.append(f"- ⚠️ **크레딧 오류**: `{safe_escape(or_data.get('creditsError'))}`")
    if or_data.get("keyError"):
        lines.append(f"- ⚠️ **키 정보 오류**: `{safe_escape(or_data.get('keyError'))}`")
    if or_data.get("dailyError"):
        lines.append(f"- ⚠️ **일별 오류**: `{safe_escape(or_data.get('dailyError'))}`")
    lines.append("")

    return "\n".join(lines)


async def collect_balances(
    settings: SettingsManager,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """SettingsManager 설정을 바탕으로 API 데이터를 비동기 조회합니다.

    네트워크 호출은 이 함수에서만 발생합니다.
    """
    missing_secrets: List[str] = []
    if not settings.runpod_api_key or not settings.runpod_api_key.strip():
        missing_secrets.append("RUNPOD_API_KEY")
    if not settings.openrouter_api_key or not settings.openrouter_api_key.strip():
        missing_secrets.append("OPENROUTER_API_KEY")

    rp_coro = fetch_runpod(settings.runpod_api_key, client=client)
    or_coro = fetch_openrouter(settings.openrouter_api_key, client=client)

    rp_res, or_res = await asyncio.gather(
        rp_coro, or_coro, return_exceptions=True
    )

    if isinstance(rp_res, Exception):
        rp_data = {"ok": False, "error": f"Runpod 조회 예외: {type(rp_res).__name__}"}
    else:
        rp_data = rp_res

    if isinstance(or_res, Exception):
        or_data = {"ok": False, "error": f"OpenRouter 조회 예외: {type(or_res).__name__}"}
    else:
        or_data = or_res

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return {
        "runpod": rp_data,
        "openrouter": or_data,
        "missingSecrets": missing_secrets,
        "generatedAt": now_utc,
    }


async def main_async() -> int:
    """메인 비동기 실행 루틴."""
    try:
        settings = SettingsManager()
        data = await collect_balances(settings)

        # 1. HTML 리포트 생성 및 저장
        html_content = render_html(data)
        dist_file = PROJECT_ROOT / "dist" / "index.html"
        dist_file.parent.mkdir(parents=True, exist_ok=True)
        dist_file.write_text(html_content, encoding="utf-8")
        print(f"[Tilapia] HTML 리포트 생성 완료: {dist_file}")

        # 2. 마크다운 리포트 생성 및 GITHUB_STEP_SUMMARY 추가
        markdown_content = render_markdown(data)
        step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary_path:
            with open(step_summary_path, "a", encoding="utf-8") as f:
                f.write(markdown_content + "\n")
            print(f"[Tilapia] GitHub Step Summary 마크다운 추가 완료: {step_summary_path}")

        return 0
    except Exception as exc:
        # 워크플로는 시크릿 누락이나 네트워크 예외로 실패 코드를 반환하지 않고 0을 반환합니다.
        print(f"[Tilapia] 리포트 생성 중 예외 발생 (종료 코드 0 유지): {type(exc).__name__}")
        return 0


def main() -> int:
    """스크립트 진입점."""
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
