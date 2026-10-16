# Tilapia - 팀 비용 & 선불 잔액 모니터

Langfuse 추적 비용과 Runpod 선불 잔액, OpenRouter 크레딧을 127.0.0.1 로컬 웹 브라우저에서 실시간으로 확인하고 모니터링하는 프로그램입니다. OpenRouter 계정 남은 크레딧 및 일별 사용량, 키 사용량과 한도 모니터링도 함께 지원합니다.

---

## 1. 사전 준비 및 API 키 발급 위치

### Runpod API Key
- **발급 위치**: [Runpod 콘솔 Settings - API Keys](https://www.runpod.io/console/user/settings)
- **용도**: Runpod 계정 선불 잔액(`myself.clientBalance`), 시간당 소진율, 최근 14일 일별 청구 요금 조회에 사용됩니다.

### Langfuse API Keys (Public Key & Secret Key)
- **발급 위치**: [Langfuse Cloud](https://cloud.langfuse.com) 로그인 후 각 프로젝트의 **Settings** -> **API Keys** 메뉴
- **용도**: 프로젝트 정보 및 Metrics API v2를 통한 LLM 비용 추적 조회에 사용됩니다.
- **호스트**: 기본 `https://cloud.langfuse.com` 외에 미국(`us.cloud.langfuse.com`), 유럽(`eu.cloud.langfuse.com`), 일본(`jp.cloud.langfuse.com`), HIPAA(`hipaa.cloud.langfuse.com`) 공식 호스트만 지원합니다.

### OpenRouter API Key
- **발급 위치**: [OpenRouter Management Keys](https://openrouter.ai/settings/management-keys)
- **용도 및 권한 구분**:
  - **Management API 키**: 계정 크레딧 및 일별 사용량(`GET /api/v1/credits`, `GET /api/v1/activity`) 조회를 위해 반드시 Management API 키가 필요합니다.
  - **추론용 키**: 추론용(일반) 키는 계정 크레딧과 일별 사용량을 보지 못하며(credits 및 activity 조회 시 403 반환), 해당 키 자체의 이번 달 사용량 및 한도 정보(`GET /api/v1/key`)만 표시됩니다.
- **남은 크레딧 계산식**:
  - OpenRouter API는 별도의 잔액 필드를 제공하지 않으므로, 남은 크레딧은 누적 충전분(`total_credits`)에서 누적 사용액(`total_usage`)을 뺀 값(`total_credits - total_usage`)으로 계산하여 표시합니다.
- **일별 사용량 및 BYOK 안내**:
  - 최근 14일 일별 사용량은 UTC 기준으로 완료된 일자만 제공되며, 진행 중인 오늘은 activity 응답에 없을 수 있습니다.
  - BYOK(Bring Your Own Key) 사용량은 크레딧 사용과 별도 열로 표기되며, 추정 금액(`byok_usage_inference`)입니다.

> [!IMPORTANT]
> **Langfuse 잔액 표시 제한 사항**:
> Langfuse Cloud의 공개 API에는 선불 잔액이나 청구서 잔액 엔드포인트가 제공되지 않습니다.
> 따라서 본 프로그램의 Langfuse 카드에 표시되는 금액은 **"이번 달 추적된 LLM 비용"**(Metrics API v2 `observations.totalCost` 합계)입니다.

> [!WARNING]
> **Langfuse Hobby 플랜 API 호출 한도 주의**:
> Langfuse Hobby 플랜은 Metrics API 호출이 **하루 100회**로 제한되어 있습니다.
> 기본 설정인 5분 주기로 계속 조회할 경우 하루 약 288회 조회가 발생하며, 1회 조회당 Langfuse Metrics API를 2번(이번 달, 최근 14일) 호출하므로 Metrics API만 하루 약 576회 호출이 발생합니다 (projects 1회는 Metrics 한도와 별도). 따라서 일일 한도를 넘겨 429 오류가 발생할 수 있으니, Hobby 플랜을 사용 중인 경우 설정에서 조회 주기를 늘리거나 필요할 때만 '지금 조회' 버튼을 눌러 수동 새로고침하는 것을 권장합니다.

---

## 2. 설치 및 실행 방법

### 환경 설정 및 패키지 설치
```bash
# 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate

# 의존성 패키지 설치
pip install -r requirements.txt
```

### 실행 방법
서버는 보안을 위해 `127.0.0.1:8765`에만 바인드됩니다 (0.0.0.0 바인드 금지).

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

실행 후 브라우저에서 [http://127.0.0.1:8765](http://127.0.0.1:8765) 로 접속합니다.

---

## 3. 설정 관리 및 우선순위

- **설정 항목**:
  - `LANGFUSE_HOST`: Langfuse 호스트 URL (기본값: `https://cloud.langfuse.com`)
  - `LANGFUSE_PUBLIC_KEY`: Langfuse Public Key
  - `LANGFUSE_SECRET_KEY`: Langfuse Secret Key
  - `RUNPOD_API_KEY`: Runpod API Key
  - `OPENROUTER_API_KEY`: OpenRouter API Key
  - `REFRESH_MINUTES`: 자동 조회 주기 (1~60분, 기본값: 5)

- **우선순위**:
  1. `data/settings.json`의 비어 있지 않은 값 (웹 UI에서 저장한 키, 주기, 호스트)
  2. 이미 설정된 프로세스 환경 변수
  3. `.env` 파일
  4. 기본값

- **팀 공유 절차**:
  - 저장소에는 `.env.example`만 올립니다.
  - 각자 `.env.example`을 `.env`로 복사해 자기 키를 넣습니다.
  - `.env`와 `data/`는 Git에 올리지 않습니다.
  - 웹 화면에 키를 저장하면 gitignore된 `data/settings.json`에만 저장되고, 그 비어 있지 않은 값이 `.env`보다 우선합니다.

- **보안**:
  - `GET /api/settings` API는 키 원문을 절대 반환하지 않으며, 설정 여부와 끝 4자리만 반환합니다.


---

## 4. CI에서 팀원과 보기

GitHub Actions 워크플로를 통해 로컬 서버를 상시 띄워두지 않고도 팀원들과 안전하게 최신 금액 리포트를 공유하고 확인할 수 있습니다.

- **키 등록**:
  - API 키는 GitHub 저장소의 **Settings → Secrets and variables → Actions**에 등록합니다.
    - `LANGFUSE_PUBLIC_KEY` (Secret)
    - `LANGFUSE_SECRET_KEY` (Secret)
    - `RUNPOD_API_KEY` (Secret)
    - `OPENROUTER_API_KEY` (Secret)
    - `LANGFUSE_HOST` (Variable, 없어도 됨)
  - 소스 코드, 커밋, 워크플로 YAML 파일 및 `.env.example`에는 키 원문을 절대 넣지 않습니다.

- **공개 홈페이지**:
  - 워크플로가 성공하면 금액 페이지가 GitHub Pages로 배포됩니다.
  - 주소: https://hrunj1230.github.io/tilapia/
  - 이 주소는 공개입니다. 링크를 아는 사람은 금액을 볼 수 있습니다.
  - 페이지는 수동 실행 직후, 그리고 약 30분마다 다시 배포됩니다.
  - Actions 실행 페이지의 요약과 `balances-report` 아티팩트에도 같은 금액이 있습니다.
  - 페이지, 실행 요약, 아티팩트에는 API 키 원문을 넣지 않습니다.

- **보안**:
  - API 키는 Actions 시크릿에만 둡니다. 공개 페이지와 소스에는 키 원문이 없습니다.
  - 리포트에는 금액과 함께 Runpod 계정 이메일이 포함될 수 있습니다.
  - 시크릿이 비어 있어도 워크플로가 실패로 프로세스를 종료하지 않으며, 리포트에 누락된 키를 한국어로 안내하고 종료 코드 0으로 완료됩니다.

---

## 5. 테스트 실행

```bash
python3 -m pytest -q
```
외부 네트워크 연결 없이 `httpx.MockTransport`를 통해 모든 비즈니스 로직과 API 예외 처리를 검증합니다.
