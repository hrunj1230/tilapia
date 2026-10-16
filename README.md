# Tilapia - 팀 비용 & 선불 잔액 모니터

Runpod 선불 잔액과 OpenRouter 크레딧을 127.0.0.1 로컬 웹 브라우저에서 확인하고, GitHub Pages로 팀과 공유하는 프로그램입니다.

---

## 1. 사전 준비 및 API 키 발급 위치

### Runpod API Key
- **발급 위치**: [Runpod 콘솔 Settings - API Keys](https://www.runpod.io/console/user/settings)의 **API Keys** 탭
- **용도**: Runpod 계정 선불 잔액(`myself.clientBalance`), 시간당 소진율, 최근 14일 일별 청구 요금 조회에 사용됩니다.
- **등록**: `rpa_`로 시작하는 키를 복사해 `RUNPOD_API_KEY`에 넣습니다. 따옴표와 `Bearer`는 붙이지 않습니다. 권한은 Read & Write이고 키가 Enabled여야 합니다.

### OpenRouter API Key
- **발급 위치**: [OpenRouter Management Keys](https://openrouter.ai/settings/management-keys)
- **용도 및 권한 구분**:
  - **Management API 키**: 계정 크레딧 및 일별 사용량(`GET /api/v1/credits`, `GET /api/v1/activity`) 조회를 위해 반드시 Management API 키가 필요합니다.
  - **추론용 키**: 추론용(일반) 키는 계정 크레딧과 일별 사용량을 보지 못하며(credits 및 activity 조회 시 403 반환), 해당 키 자체의 이번 달 사용량 및 한도 정보(`GET /api/v1/key`)만 표시됩니다.
- **남은 크레딧 계산식**:
  - OpenRouter API는 별도의 잔액 필드를 제공하지 않으므로, 남은 크레딧은 누적 충전분(`total_credits`)에서 누적 사용액(`total_usage`)을 뺀 값(`total_credits - total_usage`)으로 계산하여 표시합니다.
- **일별 사용량 및 BYOK 안내**:
  - 일별 사용량은 activity가 주는 최근 30 완료일(UTC)입니다. 진행 중인 오늘은 응답에 없을 수 있고, 그 경우 오늘 계정 사용은 0이 아니라 빈 칸입니다.
  - BYOK(Bring Your Own Key) 사용량은 크레딧 사용과 별도 열로 표기되며, 추정 금액(`byok_usage_inference`)입니다.

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
    - `RUNPOD_API_KEY` (Secret)
    - `OPENROUTER_API_KEY` (Secret)
  - 소스 코드, 커밋, 워크플로 YAML 파일 및 `.env.example`에는 키 원문을 절대 넣지 않습니다.

- **공개 홈페이지**:
  - 워크플로가 성공하면 금액 페이지가 GitHub Pages로 배포됩니다.
  - 주소: https://hrunj1230.github.io/tilapia/
  - 이 주소는 공개입니다. 링크를 아는 사람은 금액을 볼 수 있습니다.
  - 30분 자동 배포는 없습니다.
  - 페이지의 금액 다시 가져오기를 누르면, 그 브라우저 탭에 저장한 GitHub 토큰으로 Actions가 한 번 실행되고 새 금액이 배포됩니다.
  - 토큰은 저장소, Actions variable, 페이지 소스에 넣지 않습니다.
  - 토큰이 없는 사람은 마지막으로 배포된 금액을 봅니다.
  - Actions 화면에서 workflow_dispatch로 실행해도 같은 배포가 됩니다.
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
