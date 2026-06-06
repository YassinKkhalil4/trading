from __future__ import annotations

from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import SessionLocal


def bootstrap_database() -> dict[str, int]:
    with SessionLocal() as session:
        repo = TradingRepository(session)
        repo.create_schema()
        repo.seed_defaults()
        return repo.counts()


def main() -> None:
    counts = bootstrap_database()
    print("Database initialized and seeded.")
    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()

