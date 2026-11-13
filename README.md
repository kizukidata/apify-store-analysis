# Apify Store Catalog Repro

Reproducible Python script that queries the public, unauthenticated `GET https://api.apify.com/v2/store` endpoint without `search`, `sortBy`, or fields deprecated on November 15, 2026.

## Run (3 lines)

```bash
pip install -r requirements.txt
python analyze_store.py --max-items 15000 --delay 0.6
ls out/
```

## Notes

- Uses only `GET https://api.apify.com/v2/store` (no API token required).
- Enforces `--delay >= 0.5` seconds between sequential requests, `--max-items <= 15000` on a single query, and keeps `offset + limit` inside that window, so `--max-items` values below `15000` do not overshoot it.
- Retries `429`, `500`, `502`, `503` and `504` responses up to three times with a backoff, so one transient error does not end the run.
- If a request still fails, `raise_for_status()` stops the run before any output is written, so the next run starts again from the first page.
- Reads review counts and ratings from `stats.actorReviewRating` / `stats.actorReviewCount`, and treats `stats.totalUsers30Days` as optional.
- Keeps `isWhiteListedForAgenticPayments` and `notice` as missing values when the API omits them, and reports `agentic_pct` over the rows that carry the field (`agentic_present_count`). Actors without `currentPricingInfo.pricingModel` are counted as `UNKNOWN`, not `FREE`.
- `author_concentration.csv` includes `actors_with_30d`, the number of an author's Actors that carried `stats.totalUsers30Days`, so a partial sum (`users_30d_sum`) can be told apart from a complete one.
- Writes `store_sample.csv`, `author_concentration.csv`, `category_summary.csv`, `summary.json`, and two PNG charts to `out/`.
- After November 15, 2026, `categories` returns new category IDs assigned from a larger list, so `category_summary.csv` and the category rows in `store_sample.csv` are not comparable with runs made before the switch (see the API changelog of 2026-10-30).

## License

MIT. See `LICENSE`.
