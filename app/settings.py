import os
import json
import ipaddress
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

ALLOWED_LANGFUSE_HOSTS = {
    "cloud.langfuse.com",
    "us.cloud.langfuse.com",
    "eu.cloud.langfuse.com",
    "jp.cloud.langfuse.com",
    "hipaa.cloud.langfuse.com",
}


def validate_langfuse_host(url_str: str) -> str:
    """Langfuse 호스트를 검증하고 정규화된 URL 문자열을 반환합니다.

    허용:
    - https 프로토콜
    - 호스트명: cloud.langfuse.com, us.cloud.langfuse.com, eu.cloud.langfuse.com,
      jp.cloud.langfuse.com, hipaa.cloud.langfuse.com
    거부:
    - http, IP 주소, 사용자 정보 포함 URL, 비허용 도메인, 포트 지정
    """
    if not url_str or not url_str.strip():
        return "https://cloud.langfuse.com"

    trimmed = url_str.strip()
    parsed = urllib.parse.urlsplit(trimmed)

    if parsed.scheme != "https":
        raise ValueError("Langfuse 호스트는 https 프로토콜만 허용됩니다.")

    if parsed.username or parsed.password:
        raise ValueError("Langfuse 호스트 URL에 사용자 정보를 포함할 수 없습니다.")

    if parsed.port:
        raise ValueError("Langfuse 호스트에 포트 번호를 포함할 수 없습니다.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("유효하지 않은 Langfuse 호스트명입니다.")

    # IP 주소 검사 (IPv4 또는 IPv6 형태 거부)
    try:
        ipaddress.ip_address(hostname)
        raise ValueError("IP 주소 형태의 Langfuse 호스트는 허용되지 않습니다.")
    except ValueError as e:
        if "허용되지 않습니다" in str(e):
            raise
        # IP가 아닌 도메인 이름이면 통과

    if hostname not in ALLOWED_LANGFUSE_HOSTS:
        raise ValueError(
            f"허용되지 않은 Langfuse 호스트입니다: {hostname}. "
            f"허용 목록: {', '.join(sorted(ALLOWED_LANGFUSE_HOSTS))}"
        )

    return f"https://{hostname}"


def validate_interval_minutes(minutes: Any) -> int:
    """조회 주기(분) 검증: 1~60 사이 정수만 허용."""
    try:
        val = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("intervalMinutes는 1~60 사이의 정수여야 합니다.")

    if val < 1 or val > 60:
        raise ValueError(f"intervalMinutes는 1~60 사이여야 합니다. (입력값: {val})")
    return val


def mask_key(key: Optional[str]) -> Dict[str, Any]:
    """키 원문을 노출하지 않고 설정 여부와 끝 4자리만 반환합니다."""
    if not key:
        return {"isSet": False, "last4": ""}
    last4 = key[-4:] if len(key) >= 4 else key
    return {"isSet": True, "last4": last4}


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SENTINEL = object()


def parse_env_file(env_path: Optional[Path]) -> Dict[str, str]:
    """
    .env 파일을 파싱하여 키-값 딕셔너리를 반환합니다.
    - 빈 줄과 '#' 주석은 무시
    - 'export KEY=VALUE' 지원
    - 값 양쪽의 따옴표 한 겹 벗김
    - 쉘 실행 없음 ($(..), 백틱 그대로 유지)
    - 파일이 없어도 빈 dict 반환 (오류 아님)
    - 값이나 파일 내용을 로그에 남기지 않음
    """
    if env_path is None:
        return {}

    path = Path(env_path)
    if not path.is_file():
        return {}

    env_vars: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("export") and len(stripped) > 6 and stripped[6].isspace():
                    stripped = stripped[6:].strip()
                if "=" not in stripped:
                    continue
                key, val = stripped.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2:
                    if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                        val = val[1:-1]
                if key:
                    env_vars[key] = val
    except Exception:
        return {}
    return env_vars


class SettingsManager:
    """설정 관리자.

    우선순위:
    1. settings.json의 비어 있지 않은 값 (웹 UI에서 저장한 키, 주기, 호스트).
    2. 이미 설정된 프로세스 환경 변수.
    3. .env 파일.
    4. 기본값.
    실행 중 변경은 data/settings.json (0o600)에만 저장.
    """

    def __init__(
        self,
        data_dir: Path = Path("data"),
        env_file: Any = _SENTINEL,
    ):
        self.data_dir = data_dir
        self.settings_file = self.data_dir / "settings.json"

        # env_file 결정:
        # env_file이 전달되지 않은 경우(_SENTINEL):
        #   data_dir가 기본값(Path("data"))일 때만 프로젝트 루트의 .env를 읽음.
        #   그 외(예: data_dir=tmp_path)는 실제 .env를 읽지 않음.
        # env_file이 명시적으로 전달된 경우:
        #   전달된 파일 경로를 사용 (None이면 읽지 않음).
        if env_file is _SENTINEL:
            if data_dir == Path("data"):
                self.env_file: Optional[Path] = PROJECT_ROOT / ".env"
            else:
                self.env_file = None
        elif env_file is not None:
            self.env_file = Path(env_file)
        else:
            self.env_file = None

        file_env = parse_env_file(self.env_file)

        # 환경 변수 및 .env 우선순위 적용:
        # 이미 프로세스 환경 변수에 있는 키는 .env가 덮어쓰지 않음
        def _get_val(key: str) -> Optional[str]:
            if key in os.environ:
                return os.environ[key]
            if key in file_env:
                return file_env[key]
            return None

        env_interval = _get_val("REFRESH_MINUTES")
        try:
            default_interval = (
                validate_interval_minutes(env_interval)
                if env_interval is not None
                else 5
            )
        except ValueError:
            default_interval = 5

        env_host = _get_val("LANGFUSE_HOST")
        try:
            default_host = (
                validate_langfuse_host(env_host)
                if env_host is not None
                else "https://cloud.langfuse.com"
            )
        except ValueError:
            default_host = "https://cloud.langfuse.com"

        self.interval_minutes: int = default_interval
        self.langfuse_host: str = default_host
        self.langfuse_public_key: str = _get_val("LANGFUSE_PUBLIC_KEY") or ""
        self.langfuse_secret_key: str = _get_val("LANGFUSE_SECRET_KEY") or ""
        self.runpod_api_key: str = _get_val("RUNPOD_API_KEY") or ""
        self.openrouter_api_key: str = _get_val("OPENROUTER_API_KEY") or ""

        # 파일이 존재하면 파일 값으로 덮어씀 (파일의 비어 있지 않은 값이 환경 변수/.env보다 우선)
        self._load_from_file()

    def _load_from_file(self) -> None:
        if not self.settings_file.exists():
            return

        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # JSON 자체가 깨진 경우에만 파일 전체를 무시하고 환경 변수 값을 유지한다.
            # 이 경우 자동으로 save() 해서 파일을 덮어쓰지 말 것.
            return

        if not isinstance(data, dict):
            return

        if "intervalMinutes" in data and data["intervalMinutes"] is not None:
            try:
                self.interval_minutes = validate_interval_minutes(
                    data["intervalMinutes"]
                )
            except ValueError:
                pass

        if "langfuseHost" in data and data["langfuseHost"] is not None:
            raw_host = str(data["langfuseHost"]).strip()
            if raw_host:
                try:
                    self.langfuse_host = validate_langfuse_host(raw_host)
                except ValueError:
                    pass

        for key_name, attr_name in [
            ("langfusePublicKey", "langfuse_public_key"),
            ("langfuseSecretKey", "langfuse_secret_key"),
            ("runpodApiKey", "runpod_api_key"),
            ("openrouterApiKey", "openrouter_api_key"),
        ]:
            if key_name in data and data[key_name] is not None:
                val = str(data[key_name])
                if val.strip() != "":
                    setattr(self, attr_name, val)

    def save(self) -> None:
        """data/settings.json에 파일 권한 0o600으로 저장."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "intervalMinutes": self.interval_minutes,
            "langfuseHost": self.langfuse_host,
            "langfusePublicKey": self.langfuse_public_key,
            "langfuseSecretKey": self.langfuse_secret_key,
            "runpodApiKey": self.runpod_api_key,
            "openrouterApiKey": self.openrouter_api_key,
        }

        temp_path = self.settings_file.with_suffix(".tmp")
        # 0o600 모드로 파일 생성
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        os.replace(temp_path, self.settings_file)
        try:
            os.chmod(self.settings_file, 0o600)
        except OSError:
            pass

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /api/settings 업데이트 적용:

        - intervalMinutes, langfuseHost 는 검증
        - 키 필드: 빈 문자열("") 또는 공백만 있는 문자열은 기존 유지, null(None)이면 삭제, 문자열이면 갱신
        """
        if "intervalMinutes" in updates:
            new_interval = updates["intervalMinutes"]
            if new_interval is not None:
                self.interval_minutes = validate_interval_minutes(new_interval)

        if "langfuseHost" in updates:
            new_host = updates["langfuseHost"]
            if new_host is not None:
                self.langfuse_host = validate_langfuse_host(new_host)

        # 키 처리:
        # None -> 삭제 ("")
        # "" 또는 공백만 있는 문자열 -> 기존 유지
        # 유효한 문자열 -> 새 값
        for key_name, attr_name in [
            ("langfusePublicKey", "langfuse_public_key"),
            ("langfuseSecretKey", "langfuse_secret_key"),
            ("runpodApiKey", "runpod_api_key"),
            ("openrouterApiKey", "openrouter_api_key"),
        ]:
            if key_name in updates:
                val = updates[key_name]
                if val is None:
                    setattr(self, attr_name, "")
                elif isinstance(val, str):
                    stripped = val.strip()
                    if stripped != "":
                        setattr(self, attr_name, stripped)

        self.save()
        return self.get_masked()

    def get_masked(self) -> Dict[str, Any]:
        """GET /api/settings 응답용: 원문 없이 설정 여부와 끝 4글자만 반환."""
        return {
            "intervalMinutes": self.interval_minutes,
            "langfuseHost": self.langfuse_host,
            "langfusePublicKey": mask_key(self.langfuse_public_key),
            "langfuseSecretKey": mask_key(self.langfuse_secret_key),
            "runpodApiKey": mask_key(self.runpod_api_key),
            "openrouterApiKey": mask_key(self.openrouter_api_key),
        }
