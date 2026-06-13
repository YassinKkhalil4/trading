from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import Settings
from trading_system.app.core.enums import EnvironmentMode, OrderStatus
from trading_system.app.data.collectors.alpaca_bars import AlpacaBarsResult
from trading_system.app.data.collectors.alpaca_stream import AlpacaMarketDataStream
from trading_system.app.data.collectors.news_rss import NewsRssCollector
from trading_system.app.data.collectors.sec_edgar import SecEdgarCollector
from trading_system.app.data.collectors.yahoo_chart import YahooChartResult
from trading_system.app.db import models
from trading_system.app.db.base import Base
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import build_engine
from trading_system.app.execution.alpaca_paper_adapter import AlpacaPaperCancelResult, AlpacaPaperSyncResult
from trading_system.app.execution.fill_reconciliation import FillReconciliationLoop
from trading_system.app.execution.order_manager import OrderManager
from trading_system.app.execution.paper_execution import PaperOrder
from trading_system.app.ops.coordination import CoordinationLockManager, LockHandle
from trading_system.app.risk.live_readiness import LiveReadinessService
from trading_system.app.services.scheduler import ScheduledCollectorRunner
from trading_system.app.services.runtime import TradingRuntimeService


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload=None,
    ) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses

    def get(self, url: str, **_kwargs):
        for key, response in self.responses.items():
            if key in url:
                return response
        return FakeResponse(status_code=404, payload={})


class FakeAlpacaAdapter:
    def __init__(self, sync_result: AlpacaPaperSyncResult) -> None:
        self.sync_result = sync_result

    def sync(self) -> AlpacaPaperSyncResult:
        return self.sync_result


class FakePaperCancelAdapter:
    calls: list[str] = []

    def __init__(self, _settings: Settings) -> None:
        pass

    def cancel_order(self, broker_order_id: str) -> AlpacaPaperCancelResult:
        self.__class__.calls.append(broker_order_id)
        return AlpacaPaperCancelResult(
            configured=True,
            success=True,
            reason="fake paper cancel accepted",
            payload={"broker_order_id": broker_order_id},
        )


class FakeAlpacaBarsFailureCollector:
    calls: list[str] = []

    def __init__(self, _repository: TradingRepository, _settings: Settings) -> None:
        pass

    def collect(self, symbol: str) -> AlpacaBarsResult:
        self.__class__.calls.append(symbol)
        return AlpacaBarsResult(
            configured=True,
            success=False,
            symbol=symbol,
            candles_seen=0,
            raw_stored=0,
            clean_stored=0,
            invalid_stored=0,
            reason="fake Alpaca failure",
        )


class FakeYahooCollector:
    calls: list[str] = []

    def __init__(self, _repository: TradingRepository) -> None:
        pass

    def collect(self, symbol: str) -> YahooChartResult:
        self.__class__.calls.append(symbol)
        return YahooChartResult(
            symbol=symbol,
            candles_seen=1,
            raw_stored=1,
            clean_stored=1,
            invalid_stored=0,
            reason="fake Yahoo fallback",
        )


def _repo() -> TradingRepository:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return TradingRepository(Session())


def _store_submitted_test_order(repo: TradingRepository) -> None:
    repo.store_order(
        PaperOrder(
            symbol="AMD",
            side="buy",
            quantity=10,
            order_type="limit",
            limit_price=100.0,
            stop_loss=97.0,
            idempotency_key="paper-order-1",
            status=OrderStatus.SUBMITTED,
            reason="test submitted order",
            created_at=datetime.now(UTC),
        ),
        signal_id=None,
        strategy_id="VWAP_RECLAIM",
        environment_mode=EnvironmentMode.PAPER.value,
        source_timestamp=datetime.now(UTC),
    )


def test_runtime_primary_collection_uses_alpaca_without_yahoo_fallback(monkeypatch):
    repo = _repo()
    FakeAlpacaBarsFailureCollector.calls = []
    monkeypatch.setattr(
        "trading_system.app.services.runtime.AlpacaBarsCollector",
        FakeAlpacaBarsFailureCollector,
    )
    result = TradingRuntimeService(
        repo,
        settings=Settings(environment_mode=EnvironmentMode.PAPER),
    ).collect_symbol_primary("AMD")

    assert isinstance(result, AlpacaBarsResult)
    assert result.success is False
    assert FakeAlpacaBarsFailureCollector.calls == ["AMD"]


def test_scheduler_market_data_uses_alpaca_without_yahoo_fallback(monkeypatch):
    repo = _repo()
    FakeAlpacaBarsFailureCollector.calls = []
    monkeypatch.setattr(
        "trading_system.app.services.scheduler.AlpacaBarsCollector",
        FakeAlpacaBarsFailureCollector,
    )
    result = ScheduledCollectorRunner(
        repo,
        settings=Settings(environment_mode=EnvironmentMode.PAPER),
    ).run_once("market_data", symbols=["AMD"])

    assert result.success is False
    assert "fallback" not in result.payload["results"][0]
    assert "fallback_allowed" not in result.payload["results"][0]
    assert "Alpaca Market Data is the exclusive production OHLCV source" in result.reason
    assert FakeAlpacaBarsFailureCollector.calls == ["AMD"]


def test_news_rss_collector_stores_raw_clean_duplicate_and_rumor_flags():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    xml = """
    <rss><channel>
      <item>
        <title>AMD reportedly wins a large AI customer</title>
        <link>https://example.com/amd-1</link>
        <pubDate>Wed, 03 Jun 2026 14:30:00 GMT</pubDate>
        <source>Example Wire</source>
      </item>
      <item>
        <title>AMD reportedly wins a large AI customer</title>
        <link>https://example.com/amd-2</link>
        <pubDate>Wed, 03 Jun 2026 14:31:00 GMT</pubDate>
        <source>Example Wire</source>
      </item>
    </channel></rss>
    """
    settings = Settings(news_rss_feeds="https://example.com/{symbol}.xml")
    result = NewsRssCollector(
        repo,
        settings,
        http=FakeHttp({"AMD.xml": FakeResponse(text=xml, payload={})}),
    ).collect(["AMD"])

    assert result.success is True
    assert result.headlines_seen == 2
    assert result.duplicates_seen == 1
    row = repo.latest_clean_news(1)[0]
    assert row["duplicate_headline"] is True
    assert row["rumor_flag"] is True


def test_sec_edgar_collector_stores_filings_and_filing_events():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    ticker_payload = {"0": {"cik_str": 2488, "ticker": "AMD", "title": "Advanced Micro Devices"}}
    submissions_payload = {
        "name": "Advanced Micro Devices",
        "filings": {
            "recent": {
                "accessionNumber": ["0000002488-26-000001"],
                "filingDate": ["2026-06-03"],
                "form": ["8-K"],
                "primaryDocument": ["amd-8k.htm"],
            }
        },
    }
    settings = Settings(sec_requests_per_second=1000)
    http = FakeHttp(
        {
            "company_tickers": FakeResponse(payload=ticker_payload),
            "CIK0000002488": FakeResponse(payload=submissions_payload),
        }
    )

    result = SecEdgarCollector(repo, settings, http=http).collect(["AMD"], max_filings_per_symbol=1)

    assert result.success is True
    assert result.raw_filings_stored == 1
    assert repo.latest_filings(1)[0]["form_type"] == "8-K"


def test_alpaca_stream_processes_bar_into_stream_event_and_clean_candle():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    event = {
        "T": "b",
        "S": "AMD",
        "t": "2026-06-03T14:31:00Z",
        "o": 100.0,
        "h": 102.0,
        "l": 99.5,
        "c": 101.5,
        "v": 500000,
        "n": 1200,
        "vw": 101.0,
    }

    candles = AlpacaMarketDataStream(repo, Settings()).process_event(event)

    assert candles == 1
    assert repo.counts()["stream_events"] == 1
    assert repo.latest_clean_candles(1)[0]["provider"] == "alpaca_market_data"


def test_raw_payload_archive_is_optional_for_local_storage(monkeypatch):
    monkeypatch.delenv("RAW_ARCHIVE_BUCKET", raising=False)
    repo = _repo()
    source_timestamp = datetime(2026, 6, 3, 14, 31, tzinfo=UTC)

    raw_id = repo.store_raw_candle(
        {
            "provider": "alpaca_market_data",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": source_timestamp,
            "raw_payload": {"c": 100.5},
        }
    )

    assert raw_id
    assert repo.counts()["clean_candles"] == 0
    assert repo.latest_audit_logs(1) == []


def test_order_manager_replaces_open_order_with_new_internal_order():
    repo = _repo()
    _store_submitted_test_order(repo)
    order = repo.latest_orders(1)[0]

    result = OrderManager(repo).request_replace_order(
        order_id=order["id"],
        new_limit_price=99.5,
        new_stop_loss=96.5,
        reason="improve limit after stale quote",
        actor="ops-trader",
    )
    orders = repo.latest_orders(2)
    statuses = {row["status"] for row in orders}
    replacement = next(row for row in orders if row["status"] == OrderStatus.SUBMITTED.value)

    assert result.success is True
    assert result.orders_changed == 2
    assert statuses == {OrderStatus.CANCELLED.value, OrderStatus.SUBMITTED.value}
    assert replacement["limit_price"] == 99.5
    assert replacement["stop_loss"] == 96.5
    assert replacement["broker_order_id"] is None
    assert repo.latest_decisions(1)[0]["outcome"] == "CHANGED"
    audit = repo.latest_audit_logs(1)[0]
    assert audit["event_type"] == "ORDER_REPLACED"
    assert audit["actor"] == "ops-trader"
    assert audit["entity_id"] == replacement["id"]
    assert audit["payload"]["previous_order_id"] == order["id"]


def test_order_manager_cancels_existing_broker_order_before_replacement(monkeypatch):
    repo = _repo()
    _store_submitted_test_order(repo)
    order = repo.session.get(models.Order, repo.latest_orders(1)[0]["id"])
    order.broker_order_id = "paper-broker-replace-1"
    repo.session.commit()
    FakePaperCancelAdapter.calls = []
    monkeypatch.setattr(
        "trading_system.app.execution.order_manager.AlpacaPaperAdapter",
        FakePaperCancelAdapter,
    )

    result = OrderManager(repo, Settings(environment_mode=EnvironmentMode.PAPER)).request_replace_order(
        order_id=order.id,
        new_limit_price=99.5,
        reason="replace after stale quote",
    )
    orders = repo.latest_orders(2)
    statuses = {row["status"] for row in orders}

    assert result.success is True
    assert result.payload["broker_cancel"]["success"] is True
    assert FakePaperCancelAdapter.calls == ["paper-broker-replace-1"]
    assert statuses == {OrderStatus.CANCELLED.value, OrderStatus.SUBMITTED.value}


def test_order_manager_blocks_live_replacement_when_existing_broker_order_cannot_be_cancelled():
    repo = _repo()
    order = models.Order(
        signal_id=None,
        idempotency_key="live-replace-order-1",
        environment_mode=EnvironmentMode.LIVE.value,
        execution_environment="LIVE",
        broker="alpaca_live",
        broker_order_id="live-broker-replace-1",
        symbol="AMD",
        side="buy",
        quantity=10,
        order_type="limit",
        limit_price=100.0,
        stop_loss=97.0,
        status=OrderStatus.SUBMITTED.value,
        expected_price=100.0,
        source_timestamp=datetime.now(UTC),
    )
    repo.session.add(order)
    repo.session.commit()

    result = OrderManager(repo, Settings(environment_mode=EnvironmentMode.LIVE_DISABLED)).request_replace_order(
        order_id=order.id,
        new_limit_price=99.5,
        reason="replace live order",
    )
    orders = repo.latest_orders(5)
    error = repo.latest_execution_errors(1)[0]

    assert result.success is False
    assert result.orders_changed == 0
    assert len(orders) == 1
    assert orders[0]["status"] == OrderStatus.SUBMITTED.value
    assert error["order_id"] == order.id
    assert error["error_type"] == "REPLACE_ORDER_CANCEL_FAILED"
    assert "environment_mode_live" in error["payload"]["broker_cancel"]["gate_decision"]["blockers"]


def test_order_manager_blocks_replacement_for_terminal_order():
    repo = _repo()
    _store_submitted_test_order(repo)
    order = repo.session.get(models.Order, repo.latest_orders(1)[0]["id"])
    order.status = OrderStatus.FILLED.value
    repo.session.commit()

    result = OrderManager(repo).request_replace_order(
        order_id=order.id,
        reason="too late",
        actor="ops-trader",
    )

    assert result.success is False
    assert result.orders_changed == 0
    assert repo.counts()["orders"] == 1
    assert repo.latest_decisions(1)[0]["outcome"] == "BLOCKED"
    audit = repo.latest_audit_logs(1)[0]
    assert audit["event_type"] == "ORDER_REPLACE_BLOCKED"
    assert audit["actor"] == "ops-trader"
    assert audit["entity_id"] == order.id


def test_order_manager_cancels_stale_paper_order_at_broker_before_internal_cancel(monkeypatch):
    repo = _repo()
    _store_submitted_test_order(repo)
    order = repo.session.get(models.Order, repo.latest_orders(1)[0]["id"])
    order.broker_order_id = "paper-broker-stale-1"
    order.created_at = datetime.now(UTC) - timedelta(minutes=10)
    repo.session.commit()
    FakePaperCancelAdapter.calls = []
    monkeypatch.setattr(
        "trading_system.app.execution.order_manager.AlpacaPaperAdapter",
        FakePaperCancelAdapter,
    )

    result = OrderManager(repo, Settings(environment_mode=EnvironmentMode.PAPER)).cancel_stale_orders()
    updated = repo.latest_orders(1)[0]

    assert result.orders_changed == 1
    assert updated["status"] == OrderStatus.STALE_CANCELLED.value
    assert updated["cancelled_at"] is not None
    assert FakePaperCancelAdapter.calls == ["paper-broker-stale-1"]
    assert result.payload["orders"][0]["broker_cancel"]["success"] is True


def test_order_manager_blocks_live_stale_broker_cancel_when_live_gates_fail():
    repo = _repo()
    order = models.Order(
        signal_id=None,
        idempotency_key="live-stale-order-1",
        environment_mode=EnvironmentMode.LIVE.value,
        execution_environment="LIVE",
        broker="alpaca_live",
        broker_order_id="live-broker-stale-1",
        symbol="AMD",
        side="buy",
        quantity=10,
        order_type="limit",
        limit_price=100.0,
        stop_loss=97.0,
        status=OrderStatus.SUBMITTED.value,
        expected_price=100.0,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        source_timestamp=datetime.now(UTC) - timedelta(minutes=10),
    )
    repo.session.add(order)
    repo.session.commit()

    result = OrderManager(repo, Settings(environment_mode=EnvironmentMode.LIVE_DISABLED)).cancel_stale_orders()
    updated = repo.latest_orders(1)[0]
    error = repo.latest_execution_errors(1)[0]

    assert result.orders_seen == 1
    assert result.orders_changed == 0
    assert updated["status"] == OrderStatus.SUBMITTED.value
    assert error["order_id"] == order.id
    assert error["error_type"] == "STALE_ORDER_CANCEL_FAILED"
    assert "environment_mode_live" in error["payload"]["gate_decision"]["blockers"]


def test_fill_reconciliation_without_keys_logs_broker_sync():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()

    result = FillReconciliationLoop(repo, Settings()).run_once()

    assert result.configured is False
    assert repo.latest_broker_sync_logs(1)[0]["success"] is False


def test_fill_reconciliation_ignores_duplicate_fill_and_triggers_slippage_kill_switch():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    _store_submitted_test_order(repo)
    broker_order = {
        "id": "broker-order-1",
        "client_order_id": "paper-order-1",
        "symbol": "AMD",
        "side": "buy",
        "qty": "10",
        "type": "limit",
        "limit_price": "100.00",
        "status": "filled",
        "filled_qty": "10",
        "filled_avg_price": "100.50",
        "filled_at": "2026-06-04T10:00:00Z",
        "updated_at": "2026-06-04T10:00:01Z",
    }
    sync = AlpacaPaperSyncResult(
        configured=True,
        success=True,
        reason="test sync",
        account={"equity": "100000"},
        positions=[],
        orders=[broker_order],
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="key",
        alpaca_paper_secret_key="secret",
        max_slippage_bps=25.0,
    )
    loop = FillReconciliationLoop(repo, settings, adapter=FakeAlpacaAdapter(sync))

    first = loop.run_once()
    second = loop.run_once()

    fill = repo.latest_fills(1)[0]
    account_snapshot = repo.latest_broker_account_snapshots(1)[0]
    latest_audit = repo.latest_audit_logs(1)[0]
    kill_switch = repo.latest_kill_switches(1)[0]

    assert first.fills_recorded == 1
    assert second.fills_recorded == 0
    assert repo.counts()["fills"] == 1
    assert round(fill["slippage_bps"], 2) == 50.0
    assert account_snapshot["broker"] == "alpaca_paper"
    assert account_snapshot["equity"] == 100000.0
    assert repo.active_kill_switch_count() == 1
    assert kill_switch["event_type"] == "SLIPPAGE_BREACH"
    assert latest_audit["event_type"] == "DUPLICATE_FILL_IGNORED"


def test_fill_reconciliation_records_only_incremental_partial_fill_updates():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    _store_submitted_test_order(repo)
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="key",
        alpaca_paper_secret_key="secret",
        max_slippage_bps=1_000.0,
    )
    first_order = {
        "id": "broker-partial-1",
        "client_order_id": "paper-order-1",
        "symbol": "AMD",
        "side": "buy",
        "qty": "10",
        "type": "limit",
        "limit_price": "100.00",
        "status": "partially_filled",
        "filled_qty": "5",
        "filled_avg_price": "100.00",
        "filled_at": "2026-06-04T10:00:00Z",
        "updated_at": "2026-06-04T10:00:01Z",
    }
    second_order = {
        **first_order,
        "status": "filled",
        "filled_qty": "10",
        "filled_avg_price": "100.50",
        "filled_at": "2026-06-04T10:05:00Z",
        "updated_at": "2026-06-04T10:05:01Z",
    }

    first = FillReconciliationLoop(
        repo,
        settings,
        adapter=FakeAlpacaAdapter(
            AlpacaPaperSyncResult(
                configured=True,
                success=True,
                reason="first partial sync",
                account={"equity": "100000"},
                positions=[],
                orders=[first_order],
            )
        ),
    ).run_once()
    second = FillReconciliationLoop(
        repo,
        settings,
        adapter=FakeAlpacaAdapter(
            AlpacaPaperSyncResult(
                configured=True,
                success=True,
                reason="second partial sync",
                account={"equity": "100000"},
                positions=[],
                orders=[second_order],
            )
        ),
    ).run_once()

    fills = sorted(repo.latest_fills(10), key=lambda row: row["source_timestamp"])
    order = repo.latest_orders(1)[0]

    assert first.fills_recorded == 1
    assert second.fills_recorded == 1
    assert repo.counts()["fills"] == 2
    assert [fill["quantity"] for fill in fills] == [5.0, 5.0]
    assert [round(fill["price"], 2) for fill in fills] == [100.0, 101.0]
    assert order["status"] == OrderStatus.FILLED.value


def test_fill_reconciliation_records_rejected_broker_order_once():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    _store_submitted_test_order(repo)
    broker_order = {
        "id": "broker-rejected-1",
        "client_order_id": "paper-order-1",
        "symbol": "AMD",
        "side": "buy",
        "qty": "10",
        "type": "limit",
        "limit_price": "100.00",
        "status": "rejected",
        "failed_reason": "insufficient buying power",
        "filled_qty": "0",
        "filled_avg_price": None,
        "updated_at": "2026-06-04T10:00:01Z",
    }
    sync = AlpacaPaperSyncResult(
        configured=True,
        success=True,
        reason="rejected order sync",
        account={"equity": "100000"},
        positions=[],
        orders=[broker_order],
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="key",
        alpaca_paper_secret_key="secret",
    )
    loop = FillReconciliationLoop(repo, settings, adapter=FakeAlpacaAdapter(sync))

    first = loop.run_once()
    second = loop.run_once()
    order = repo.latest_orders(1)[0]
    errors = repo.latest_execution_errors(10)

    assert first.fills_recorded == 0
    assert second.fills_recorded == 0
    assert order["status"] == OrderStatus.REJECTED.value
    assert order["rejection_reason"] == "insufficient buying power"
    assert len(errors) == 1
    assert errors[0]["error_type"] == "BROKER_ORDER_REJECTED"
    assert errors[0]["reason"] == "insufficient buying power"


def test_fill_reconciliation_triggers_kill_switch_on_broker_sync_failure():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    sync = AlpacaPaperSyncResult(
        configured=True,
        success=False,
        reason="broker unavailable",
        account=None,
        positions=[],
        orders=[],
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="key",
        alpaca_paper_secret_key="secret",
    )

    result = FillReconciliationLoop(repo, settings, adapter=FakeAlpacaAdapter(sync)).run_once()
    kill_switch = repo.latest_kill_switches(1)[0]

    assert result.success is False
    assert kill_switch["event_type"] == "BROKER_SYNC_FAILURE"
    assert repo.active_kill_switch_count() == 1


def test_fill_reconciliation_triggers_kill_switch_on_unexpected_broker_position():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    sync = AlpacaPaperSyncResult(
        configured=True,
        success=True,
        reason="test sync",
        account={"equity": "100000"},
        positions=[{"symbol": "AMD", "qty": "5", "avg_entry_price": "100.00"}],
        orders=[],
    )
    settings = Settings(
        environment_mode=EnvironmentMode.PAPER,
        alpaca_paper_api_key="key",
        alpaca_paper_secret_key="secret",
    )

    result = FillReconciliationLoop(repo, settings, adapter=FakeAlpacaAdapter(sync)).run_once()
    position = repo.latest_positions(1)[0]
    kill_switch = repo.latest_kill_switches(1)[0]

    assert result.mismatch_detected is True
    assert position["quantity"] == 0.0
    assert position["broker_quantity"] == 5.0
    assert position["reconciliation_status"] == "MISMATCH_PENDING_REVIEW"
    assert kill_switch["event_type"] == "FAILED_RECONCILIATION"


def test_scheduler_run_once_records_job_result_without_provider_keys():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()

    result = ScheduledCollectorRunner(repo, Settings()).run_once("fill_reconciliation")

    assert result.job_name == "fill_reconciliation"
    assert repo.counts()["scheduler_runs"] == 1


def test_coordination_lock_blocks_duplicate_holder_and_releases():
    manager = CoordinationLockManager(Settings(scheduler_lock_ttl_seconds=30))
    key = f"unit-test-lock:{uuid4()}"

    first = manager.acquire(key)
    second = manager.acquire(key)
    released = manager.release(first)
    third = manager.acquire(key)

    assert first.acquired is True
    assert second.acquired is False
    assert released is True
    assert third.acquired is True
    assert manager.release(third) is True


def test_scheduler_skips_job_when_coordination_lock_is_held(monkeypatch):
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()

    class LockedManager:
        def __init__(self, _settings):
            pass

        def acquire(self, key: str, *, ttl_seconds: int | None = None):
            return LockHandle(
                key=f"trading:{key}",
                token="held",
                acquired=False,
                backend="memory",
                ttl_seconds=ttl_seconds or 300,
                reason="test lock held",
            )

        def release(self, _handle) -> bool:
            return False

    monkeypatch.setattr(
        "trading_system.app.services.scheduler.CoordinationLockManager",
        LockedManager,
    )

    result = ScheduledCollectorRunner(repo, Settings()).run_once("fill_reconciliation")
    scheduler_run = repo.latest_scheduler_runs(1)[0]

    assert result.success is True
    assert "skipped" in result.reason
    assert result.payload["skipped"] is True
    assert scheduler_run["payload"]["skipped"] is True
    assert scheduler_run["payload"]["coordination_lock"]["acquired"] is False


def test_live_readiness_report_is_blocked_before_mvp5():
    repo = _repo()
    repo.create_schema()
    repo.seed_defaults()
    timestamp = datetime(2026, 6, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    raw_id = repo.store_raw_candle(
        {
            "provider": "yahoo_chart",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": timestamp,
            "raw_payload": {},
        }
    )
    repo.store_clean_candle(
        {
            "raw_market_data_id": raw_id,
            "provider": "yahoo_chart",
            "symbol": "AMD",
            "timeframe": "1Min",
            "source_timestamp": timestamp,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000000,
            "trade_count": None,
            "vwap": None,
            "data_quality_status": "VALID",
            "quality_reason": "test",
        }
    )

    result = LiveReadinessService(repo, Settings()).generate_report()

    assert result.live_allowed is False
    assert result.overall_status == "BLOCKED"
    assert result.blockers >= 1
    assert repo.counts()["live_readiness_reports"] == 1
