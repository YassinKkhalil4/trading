from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.data.provider_capabilities import assert_provider_usage
from trading_system.app.db.repositories import TradingRepository


SEC_PROVIDER = "sec_edgar"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass(frozen=True)
class SecCollectionResult:
    success: bool
    symbols_seen: int
    filings_seen: int
    raw_filings_stored: int
    filing_events_stored: int
    skipped_symbols: int
    reason: str


class SecEdgarCollector:
    """Slow SEC filing context collector.

    This intentionally collects filing metadata for catalyst/fundamental context,
    not intraday execution decisions.
    """

    def __init__(
        self,
        repository: TradingRepository,
        settings: Settings | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.http = http or requests.Session()

    def collect(
        self,
        symbols: list[str] | None = None,
        *,
        max_filings_per_symbol: int = 10,
    ) -> SecCollectionResult:
        assert_provider_usage(SEC_PROVIDER, research=True, enrichment=True)
        symbols = [symbol.upper() for symbol in (symbols or self.repository.active_symbols())]
        try:
            ticker_map = self._fetch_ticker_map()
        except requests.RequestException as exc:
            return SecCollectionResult(
                False,
                len(symbols),
                0,
                0,
                0,
                len(symbols),
                f"SEC ticker map fetch failed: {exc}",
            )

        filings_seen = raw_stored = events_stored = skipped = 0
        errors: list[str] = []
        for symbol in symbols:
            mapping = ticker_map.get(symbol)
            if not mapping:
                skipped += 1
                continue
            self._respect_rate_limit()
            try:
                submission = self._fetch_submissions(int(mapping["cik"]))
            except requests.RequestException as exc:
                errors.append(f"{symbol}: {exc}")
                skipped += 1
                continue
            recent = submission.get("filings", {}).get("recent", {})
            rows = _recent_filing_rows(recent, max_filings_per_symbol)
            for filing in rows:
                filings_seen += 1
                source_timestamp = _parse_filing_date(filing.get("filingDate")) or datetime.now(UTC)
                accession = filing.get("accessionNumber")
                form_type = filing.get("form")
                raw = self.repository.store_raw_filing(
                    symbol=symbol,
                    accession_number=accession,
                    form_type=form_type,
                    raw_payload={
                        "company": submission.get("name") or mapping.get("title"),
                        "cik": mapping["cik"],
                        **filing,
                    },
                    source_timestamp=source_timestamp,
                )
                raw_stored += 1
                event = self.repository.store_filing_event(
                    raw_filing_id=raw.id,
                    symbol=symbol,
                    form_type=form_type,
                    summary=_filing_summary(symbol, form_type, accession, filing.get("filingDate")),
                    materiality_score=_materiality_score(form_type),
                    reason="SEC filing metadata converted into slow catalyst context.",
                    source_timestamp=source_timestamp,
                )
                if event:
                    events_stored += 1

        success = not errors or raw_stored > 0
        reason = "SEC EDGAR filing collection completed."
        if errors:
            reason += f" Errors: {'; '.join(errors[:3])}"
        return SecCollectionResult(
            success=success,
            symbols_seen=len(symbols),
            filings_seen=filings_seen,
            raw_filings_stored=raw_stored,
            filing_events_stored=events_stored,
            skipped_symbols=skipped,
            reason=reason,
        )

    def _fetch_ticker_map(self) -> dict[str, dict[str, Any]]:
        started = time.monotonic()
        response = self.http.get(COMPANY_TICKERS_URL, headers=self._headers(), timeout=20)
        duration_ms = (time.monotonic() - started) * 1000
        self.repository.log_api_call(
            provider=SEC_PROVIDER,
            endpoint=COMPANY_TICKERS_URL,
            status_code=response.status_code,
            success=response.ok,
            reason="SEC company ticker map fetched." if response.ok else "SEC ticker map failed.",
            duration_ms=duration_ms,
        )
        response.raise_for_status()
        payload = response.json()
        values = payload.values() if isinstance(payload, dict) else payload
        return {
            str(item["ticker"]).upper(): {
                "cik": int(item["cik_str"]),
                "title": item.get("title"),
            }
            for item in values
            if item.get("ticker") and item.get("cik_str") is not None
        }

    def _fetch_submissions(self, cik: int) -> dict[str, Any]:
        url = SUBMISSIONS_URL.format(cik=f"{cik:010d}")
        started = time.monotonic()
        response = self.http.get(url, headers=self._headers(), timeout=20)
        duration_ms = (time.monotonic() - started) * 1000
        self.repository.log_api_call(
            provider=SEC_PROVIDER,
            endpoint=url,
            status_code=response.status_code,
            success=response.ok,
            reason="SEC submissions metadata fetched." if response.ok else "SEC submissions fetch failed.",
            duration_ms=duration_ms,
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }

    def _respect_rate_limit(self) -> None:
        requests_per_second = max(0.1, float(self.settings.sec_requests_per_second))
        time.sleep(1.0 / requests_per_second)


def _recent_filing_rows(recent: dict[str, list[Any]], limit: int) -> list[dict[str, Any]]:
    accessions = recent.get("accessionNumber") or []
    rows: list[dict[str, Any]] = []
    for index in range(min(limit, len(accessions))):
        rows.append({key: values[index] for key, values in recent.items() if index < len(values)})
    return rows


def _parse_filing_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


def _materiality_score(form_type: str | None) -> float:
    form = (form_type or "").upper()
    if form in {"8-K", "6-K"}:
        return 75.0
    if form in {"10-K", "10-Q", "20-F"}:
        return 65.0
    if form in {"S-1", "S-3", "424B", "424B5"}:
        return 60.0
    if form in {"4", "3", "5"}:
        return 45.0
    return 30.0


def _filing_summary(
    symbol: str,
    form_type: str | None,
    accession: str | None,
    filing_date: str | None,
) -> str:
    return f"{symbol} filed {form_type or 'unknown form'} on {filing_date or 'unknown date'} ({accession or 'no accession'})."
