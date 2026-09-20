"""
fetch.py — Step 1 of the pipeline: find a company's CIK and its 10-K filings on EDGAR.
URL of the main filing document

That URL is what gets handed to Arelle in the next step.

SEC requires a descriptive User-Agent on every request (name + contact email),
and asks that you not hammer their servers (we stay well under their 10 req/sec limit
since this script makes only a couple of requests per lookup).
"""

import time

import requests

USER_AGENT = "Temirlan Yerlanuly research tool (temirlan.eraly1@gmail.com)"
HEADERS = {"User-Agent": USER_AGENT}

MIN_SECONDS_BETWEEN_REQUESTS = 0.15
MAX_ATTEMPTS = 4
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_last_request_time = 0.0


def _get(url: str) -> requests.Response:
    """
    GET with SEC-friendly pacing: never faster than ~6 requests/second, and
    every attempt (retries included) waits its turn. Retries on rate-limit
    and server errors with growing pauses; raises on the final failure.
    """
    global _last_request_time

    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()

        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(2 ** attempt)
            continue

        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()
        return resp

    raise RuntimeError(f"unreachable: {url}")

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def get_cik_for_ticker(ticker: str) -> int:
    """
    Look up a company's CIK number from its stock ticker (e.g. 'AAPL').
    Returns the CIK as an int (e.g. 320193 for Apple).
    """
    data = _get(TICKER_MAP_URL).json()

    ticker = ticker.upper()
    for entry in data.values():
        if entry["ticker"] == ticker:
            return entry["cik_str"]

    raise ValueError(f"No CIK found for ticker '{ticker}'")


def get_recent_10k_filings(cik: int, limit: int = 5) -> list[dict]:
    """
    Given a CIK, return the most recent 10-K filings as a list of dicts:
      { "accession_number": "...", "filing_date": "...", "primary_document": "..." }
    Most recent first.
    """
    url = SUBMISSIONS_URL.format(cik=cik)
    data = _get(url).json()

    recent = data["filings"]["recent"]
    results = []
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            results.append({
                "accession_number": recent["accessionNumber"][i],
                "filing_date": recent["filingDate"][i],
                "primary_document": recent["primaryDocument"][i],
            })
        if len(results) >= limit:
            break

    return results


def build_filing_url(cik: int, accession_number: str, primary_document: str) -> str:
    """
    Build the URL to the main instance document of a filing.
    This is the single entry-point URL we will hand to Arelle later.
    """
    accession_nodash = accession_number.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession_nodash}/{primary_document}"
    )


def build_filing_index_url(cik: int, accession_number: str) -> str:
    """
    URL to the filing's full index page (lists every file in the filing —
    useful for eyeballing what's in there, not needed by Arelle itself).
    """
    accession_nodash = accession_number.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession_nodash}/{accession_number}-index.htm"
    )


if __name__ == "__main__":
    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    print(f"Looking up CIK for {ticker}...")
    cik = get_cik_for_ticker(ticker)
    print(f"  CIK: {cik}")

    print(f"Fetching recent 10-K filings...")
    filings = get_recent_10k_filings(cik, limit=3)

    for f in filings:
        print(f"\n  Filed: {f['filing_date']}")
        print(f"  Accession: {f['accession_number']}")
        url = build_filing_url(cik, f["accession_number"], f["primary_document"])
        index_url = build_filing_index_url(cik, f["accession_number"])
        print(f"  Filing document URL: {url}")
        print(f"  Index (all files):   {index_url}")