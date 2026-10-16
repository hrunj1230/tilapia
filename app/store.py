import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class BalanceStore:
    """SQLite data/balances.sqlite 스냅샷 저장소.

    - 최대 500개 스냅샷 유지
    - 성공한 조회 직전 값 대비 증감 계산
    """

    def __init__(self, db_path: Path = Path("data/balances.sqlite")):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    amount REAL,
                    data_json TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provider_id ON snapshots(provider, id DESC);"
            )
            conn.commit()

    def save_snapshot(
        self, provider: str, amount: Optional[float], data: Dict[str, Any]
    ) -> None:
        """성공한 조회 스냅샷을 저장하고 전체 최대 500개로 유지합니다."""
        now_iso = datetime.now(timezone.utc).isoformat()
        data_str = json.dumps(data)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO snapshots (created_at, provider, amount, data_json)
                VALUES (?, ?, ?, ?);
                """,
                (now_iso, provider, amount, data_str),
            )
            # 최대 500개 유지 (500개 초과분 삭제)
            conn.execute(
                """
                DELETE FROM snapshots
                WHERE id NOT IN (
                    SELECT id FROM snapshots
                    ORDER BY id DESC
                    LIMIT 500
                );
                """
            )
            conn.commit()

    def get_diff_from_previous(
        self, provider: str, current_amount: Optional[float]
    ) -> Optional[float]:
        """직전 성공 스냅샷과의 차이 (current_amount - previous_amount)를 계산합니다."""
        if current_amount is None:
            return None

        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT amount FROM snapshots
                WHERE provider = ? AND amount IS NOT NULL
                ORDER BY id DESC
                LIMIT 1;
                """,
                (provider,),
            )
            row = cur.fetchone()
            if row is not None and row["amount"] is not None:
                prev = float(row["amount"])
                # 소수점 4자리 반올림 (부동소수점 오차 방지)
                return round(current_amount - prev, 4)
        return None

    def get_latest_snapshot(self, provider: str) -> Optional[Dict[str, Any]]:
        """직전 성공 스냅샷의 amount와 data_json 파싱 결과를 반환합니다."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT amount, data_json FROM snapshots
                WHERE provider = ? AND amount IS NOT NULL
                ORDER BY id DESC
                LIMIT 1;
                """,
                (provider,),
            )
            row = cur.fetchone()
            if row is not None and row["amount"] is not None:
                parsed_data = {}
                if row["data_json"]:
                    try:
                        parsed_data = json.loads(row["data_json"])
                    except Exception:
                        parsed_data = {}
                return {
                    "amount": float(row["amount"]),
                    "data": parsed_data,
                }
        return None
