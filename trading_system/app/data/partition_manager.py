from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PartitionRange:
    name: str
    start: datetime
    end: datetime


class PartitionManager:
    """Maintain future PostgreSQL range partitions for raw time-series tables."""

    PARTITIONED_TABLES = ("raw_market_data", "raw_trade_ticks")

    def __init__(self, session: Session) -> None:
        self.session = session

    def next_week_range(self, today: date | None = None) -> tuple[datetime, datetime]:
        """Return the UTC start and end timestamps for the next calendar week."""
        current_date = today or datetime.now(UTC).date()
        days_until_next_monday = (7 - current_date.weekday()) % 7
        if days_until_next_monday == 0:
            days_until_next_monday = 7
        start_date = current_date + timedelta(days=days_until_next_monday)
        end_date = start_date + timedelta(days=7)
        return self._as_utc_datetime(start_date), self._as_utc_datetime(end_date)

    def create_next_week_partitions(self, today: date | None = None) -> dict[str, str]:
        """Create next week's partitions for each high-volume raw table."""
        partition_start, partition_end = self.next_week_range(today=today)
        created: dict[str, str] = {}
        for table_name in self.PARTITIONED_TABLES:
            partition = self._weekly_partition(table_name, partition_start, partition_end)
            self.session.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "{partition.name}" '
                    f'PARTITION OF "{table_name}" '
                    "FOR VALUES FROM (:partition_start) TO (:partition_end)"
                ),
                {
                    "partition_start": partition.start,
                    "partition_end": partition.end,
                },
            )
            created[table_name] = partition.name
        self.session.commit()
        return created

    def _weekly_partition(
        self, table_name: str, partition_start: datetime, partition_end: datetime
    ) -> PartitionRange:
        iso_year, iso_week, _ = partition_start.isocalendar()
        partition_name = f"{table_name}_y{iso_year}w{iso_week:02d}"
        return PartitionRange(name=partition_name, start=partition_start, end=partition_end)

    @staticmethod
    def _as_utc_datetime(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=UTC)
