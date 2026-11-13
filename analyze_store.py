#!/usr/bin/env python3
"""Fetch a bounded, unauthenticated sample from the public Apify Store API
and reproduce catalog concentration & category statistics.

Designed to stay compatible after the 2026-11-15 GET /v2/store backend change:
- Never passes `search` (paging & totals remain intact without `search`).
- Never relies on `sortBy` (ignored after 2026-11-15).
- Never reads removed fields (`bookmarkCount`, `publicActorRunStats30Days`,
  `totalBuilds`, `totalMetamorphs`, `lastRunStartedAt`, or top-level ratings).
- Reads ratings from `stats.actorReviewRating` / `stats.actorReviewCount`.
- Treats `stats.totalUsers30Days` as optional ("may be absent" after Nov 15)
  and always tracks `stats.totalUsers`.
- Keeps `isWhiteListedForAgenticPayments` and `notice` as missing (NA) when the
  API omits them, instead of counting them as False.
- Caps every request so `offset + limit` stays inside the observed 15,000
  offset window, and retries 429/5xx responses with a backoff.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STORE_URL = "https://api.apify.com/v2/store"
PAGE_LIMIT = 1000
WINDOW_CAP = 15000
MIN_DELAY_SEC = 0.5
USER_AGENT = "apify-store-repro/1.0 (unauthenticated public catalog sample)"
RETRY_STATUSES = (429, 500, 502, 503, 504)


def build_session() -> requests.Session:
    """Session with retries for transient 429/5xx responses."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_store_sample(
    max_items: int,
    delay: float,
    include_unrunnable: bool = True,
) -> tuple[list[dict], dict]:
    """Page GET /v2/store sequentially without `search` up to max_items."""
    if delay < MIN_DELAY_SEC:
        raise ValueError(f"--delay must be >= {MIN_DELAY_SEC}s (got {delay})")
    if max_items < 1 or max_items > WINDOW_CAP:
        raise ValueError(f"--max-items must be between 1 and {WINDOW_CAP} (got {max_items})")

    session = build_session()

    items: list[dict] = []
    seen_ids: set[str] = set()
    catalog_total: int | None = None
    requests_made = 0
    last_req_ts = 0.0

    offset = 0
    while len(items) < max_items and offset < WINDOW_CAP:
        batch_limit = min(PAGE_LIMIT, max_items - len(items), WINDOW_CAP - offset)
        params: dict[str, str | int] = {
            "limit": batch_limit,
            "offset": offset,
        }
        if include_unrunnable:
            params["includeUnrunnableActors"] = "true"

        elapsed = time.monotonic() - last_req_ts
        if requests_made > 0 and elapsed < delay:
            time.sleep(delay - elapsed)

        last_req_ts = time.monotonic()
        resp = session.get(STORE_URL, params=params, timeout=30)
        requests_made += 1
        resp.raise_for_status()

        payload = resp.json().get("data") or {}
        if catalog_total is None:
            catalog_total = payload.get("total")
        batch = payload.get("items") or []
        if not batch:
            break

        for it in batch:
            aid = it.get("id")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                items.append(it)

        offset += len(batch)
        if len(batch) < batch_limit:
            break

    meta = {
        "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": STORE_URL,
        "include_unrunnable": include_unrunnable,
        "requests_made": requests_made,
        "catalog_total_reported": catalog_total,
        "items_fetched": len(items),
    }
    return items, meta


def flatten_items(items: list[dict]) -> tuple[pd.DataFrame, str]:
    """Flatten Store API items using only post-2026-11-15 safe fields."""
    rows: list[dict] = []
    saw_new_fields = False
    saw_old_only_fields = False

    for it in items:
        stats = it.get("stats") or {}
        if "isVerified" in it or "readmeSummary" in it:
            saw_new_fields = True
        if "bookmarkCount" in it or "publicActorRunStats30Days" in stats:
            saw_old_only_fields = True

        cpi = it.get("currentPricingInfo") or {}
        pricing_model = cpi.get("pricingModel") or "UNKNOWN"
        cats = [c for c in (it.get("categories") or []) if c]

        u_total = stats.get("totalUsers")
        u_30 = stats.get("totalUsers30Days")
        raw_agentic = it.get("isWhiteListedForAgenticPayments")
        raw_notice = it.get("notice")

        rows.append(
            {
                "id": it.get("id") or "",
                "username": it.get("username") or "",
                "name": it.get("name") or "",
                "title_len": len(it.get("title") or ""),
                "desc_len": len(it.get("description") or ""),
                "categories": "|".join(cats),
                "category_count": len(cats),
                "pricing_model": pricing_model,
                "allows_agentic": pd.NA if raw_agentic is None else bool(raw_agentic),
                "total_users": int(u_total) if u_total is not None else 0,
                "users_30d": int(u_30) if u_30 is not None else pd.NA,
                "review_rating": stats.get("actorReviewRating"),
                "review_count": int(stats.get("actorReviewCount") or 0),
                # The API returns the string "NONE" for Actors without a notice,
                # and omits the field after 2026-11-15.
                "has_notice": pd.NA if raw_notice is None else bool(raw_notice and raw_notice != "NONE"),
            }
        )

    if saw_new_fields and not saw_old_only_fields:
        backend = "new"
    elif saw_old_only_fields and not saw_new_fields:
        backend = "old"
    elif saw_new_fields and saw_old_only_fields:
        backend = "mixed"
    else:
        backend = "unknown"

    df = pd.DataFrame(rows)
    return df, backend


def compute_summaries(df: pd.DataFrame, meta: dict, backend: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Compute author concentration, category summary, and overall metrics."""
    user_col = "total_users"

    # Author concentration
    author_df = (
        df.groupby("username", as_index=False)
        .agg(
            actors=("id", "count"),
            total_users_sum=("total_users", "sum"),
            actors_with_30d=("users_30d", lambda s: int(s.notna().sum())),
            users_30d_sum=("users_30d", lambda s: s.dropna().sum() if s.notna().any() else pd.NA),
        )
        .sort_values(["total_users_sum", "actors", "username"], ascending=[False, False, True])
        .reset_index(drop=True)
    )
    author_df["rank"] = author_df.index + 1
    grand_users = float(author_df["total_users_sum"].sum())
    author_df["share_total_users"] = (
        author_df["total_users_sum"] / grand_users if grand_users > 0 else 0.0
    )
    author_df["cum_share_total_users"] = author_df["share_total_users"].cumsum()

    # Top author shares
    n_authors = len(author_df)
    top_1pct_n = max(1, int(round(n_authors * 0.01)))
    top_10pct_n = max(1, int(round(n_authors * 0.10)))
    top_1pct_share = float(author_df.iloc[:top_1pct_n]["share_total_users"].sum()) if n_authors else 0.0
    top_10pct_share = float(author_df.iloc[:top_10pct_n]["share_total_users"].sum()) if n_authors else 0.0

    # Category summary (an Actor with multiple categories counts in each)
    cat_rows = []
    for _, r in df.iterrows():
        cats = r["categories"].split("|") if r["categories"] else ["(none)"]
        for c in cats:
            cat_rows.append(
                {
                    "category": c,
                    "total_users": r["total_users"],
                    "is_10plus": int(r["total_users"] >= 10),
                }
            )
    cat_long = pd.DataFrame(cat_rows)
    cat_df = (
        cat_long.groupby("category", as_index=False)
        .agg(
            actors=("total_users", "count"),
            total_users_sum=("total_users", "sum"),
            mean_users=("total_users", "mean"),
            median_users=("total_users", "median"),
            actors_10plus=("is_10plus", "sum"),
        )
        .sort_values("total_users_sum", ascending=False)
        .reset_index(drop=True)
    )
    cat_df["pct_10plus"] = (cat_df["actors_10plus"] / cat_df["actors"] * 100).round(2)
    total_cat_actors = len(df)
    total_cat_users = float(df["total_users"].sum())
    if total_cat_actors > 0 and total_cat_users > 0:
        cat_df["demand_supply_ratio"] = (
            (cat_df["total_users_sum"] / total_cat_users) / (cat_df["actors"] / total_cat_actors)
        ).round(3)
    else:
        # No cumulative users in the sample: the ratio is undefined, but a
        # column of NaN would later break the chart's x limits.
        cat_df["demand_supply_ratio"] = 0.0

    users30_present = int(df["users_30d"].notna().sum())
    agentic_present = df["allows_agentic"].dropna()
    u = df[user_col]
    buckets = {
        "0": int((u == 0).sum()),
        "1": int((u == 1).sum()),
        "2-9": int(((u >= 2) & (u <= 9)).sum()),
        "10-99": int(((u >= 10) & (u <= 99)).sum()),
        "100-999": int(((u >= 100) & (u <= 999)).sum()),
        "1000+": int((u >= 1000).sum()),
    }

    summary = {
        **meta,
        "backend_detected": backend,
        "unique_authors": int(n_authors),
        "users_30d_present_count": users30_present,
        "users_30d_present_pct": round(users30_present / len(df) * 100, 2) if len(df) else 0.0,
        "total_users_sum": int(grand_users),
        "top_1pct_authors_count": top_1pct_n,
        "top_1pct_author_user_share_pct": round(top_1pct_share * 100, 2),
        "top_10pct_authors_count": top_10pct_n,
        "top_10pct_author_user_share_pct": round(top_10pct_share * 100, 2),
        "user_buckets": buckets,
        "pricing_model_counts": df["pricing_model"].value_counts().to_dict(),
        "no_category_pct": round(float((df["category_count"] == 0).mean() * 100), 2) if len(df) else 0.0,
        # Share of the rows that carry the field, so a missing field after
        # 2026-11-15 cannot silently read as 0%.
        "agentic_present_count": int(len(agentic_present)),
        "agentic_pct": (
            round(float(agentic_present.astype(bool).mean() * 100), 2) if len(agentic_present) else None
        ),
    }
    if users30_present > 0:
        u30 = df["users_30d"].dropna()
        summary["users_30d_sum_present"] = int(u30.sum())
        summary["users_30d_zero_pct_present"] = round(float((u30 == 0).mean() * 100), 2)
        summary["users_30d_one_pct_present"] = round(float((u30 == 1).mean() * 100), 2)
        summary["users_30d_10plus_pct_present"] = round(float((u30 >= 10).mean() * 100), 2)

    return author_df, cat_df, summary


def plot_charts(df: pd.DataFrame, author_df: pd.DataFrame, cat_df: pd.DataFrame, summary: dict, out_dir: Path) -> None:
    """Render two clean PNG charts into out_dir."""
    date_tag = summary["fetched_at_utc"][:10]
    n_items = summary["items_fetched"]
    cat_total = summary["catalog_total_reported"]
    # The API may omit `total` or return null after 2026-11-15.
    cat_total_str = f"{cat_total:,}" if isinstance(cat_total, int) else "n/a"

    # 1. User distribution & author cumulative concentration
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.6), dpi=150)

    buckets = summary["user_buckets"]
    labels = list(buckets.keys())
    counts = list(buckets.values())
    pcts = [c / n_items * 100 if n_items else 0 for c in counts]
    bars = ax1.bar(labels, pcts, color="#1A226A", width=0.62)
    ax1.set_title(f"Actors by Cumulative Users (sample n={n_items:,})", fontsize=10.5, fontweight="bold")
    ax1.set_xlabel("stats.totalUsers bucket")
    ax1.set_ylabel("Share of sampled Actors (%)")
    ax1.set_ylim(0, max(pcts) * 1.18 if pcts else 100)
    ax1.grid(axis="y", alpha=0.25, linestyle="--")
    for b, p in zip(bars, pcts):
        ax1.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.8, f"{p:.1f}%", ha="center", va="bottom", fontsize=8.5)

    n_auth = len(author_df)
    # Start at the origin so the cumulative curve reads as a concentration
    # curve (top-ranked authors accumulate share from 0%).
    x_pct = [0.0] + [((i + 1) / n_auth * 100) for i in range(n_auth)]
    y_pct = [0.0] + (author_df["cum_share_total_users"] * 100).tolist()
    ax2.plot(x_pct, y_pct, color="#F8A919", linewidth=2.2, label="Cumulative author share")
    ax2.plot([0, 100], [0, 100], color="#888888", linestyle="--", linewidth=1.0, label="Equal split")
    ax2.set_title(f"Author Concentration ({n_auth:,} authors in sample)", fontsize=10.5, fontweight="bold")
    ax2.set_xlabel("Top % of authors (ranked by totalUsers)")
    ax2.set_ylabel("Cumulative % of totalUsers")
    ax2.set_xlim(0, 100)
    ax2.set_ylim(0, 102)
    ax2.grid(alpha=0.25, linestyle="--")
    ax2.legend(loc="lower right", fontsize=8.5)

    fig.suptitle(
        f"Apify Store Public API Sample ({date_tag} UTC | catalog total={cat_total_str})",
        fontsize=11.5,
        y=0.99,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out_dir / "user_concentration.png")
    plt.close(fig)

    # 2. Top 12 categories by Actor count: share of Actors vs demand/supply ratio
    top_cats = cat_df[cat_df["category"] != "(none)"].sort_values("actors", ascending=False).head(12)
    top_cats = top_cats.sort_values("demand_supply_ratio", ascending=True)

    fig, ax = plt.subplots(figsize=(9.5, 5.2), dpi=150)
    colors = ["#1A226A" if r >= 1.0 else "#9AA3B2" for r in top_cats["demand_supply_ratio"]]
    bars = ax.barh(top_cats["category"], top_cats["demand_supply_ratio"], color=colors, height=0.65)
    ax.axvline(1.0, color="#F8A919", linestyle="--", linewidth=1.5, label="Parity (1.0)")
    ax.set_title(
        f"Category Demand / Supply Ratio in Sample (top 12 categories by count, {date_tag})",
        fontsize=10.5,
        fontweight="bold",
    )
    ax.set_xlabel("Share of totalUsers / Share of Actors (>1.0 = above-average users per Actor)")
    ax.grid(axis="x", alpha=0.25, linestyle="--")
    ax.legend(loc="lower right", fontsize=8.5)
    for b, r in zip(bars, top_cats["demand_supply_ratio"]):
        # Keep labels clear of the parity line at x = 1.0 by placing labels of
        # bars just below parity inside the bar, right-aligned.
        if 0.80 <= r < 1.0:
            label_x, ha = r - 0.03, "right"
        else:
            label_x, ha = r + 0.03, "left"
        ax.text(label_x, b.get_y() + b.get_height() / 2, f"{r:.2f}x",
                va="center", ha=ha, fontsize=8.5, color="#111827")
    ax.set_xlim(0, max(float(top_cats["demand_supply_ratio"].max()) * 1.18, 1.2) if len(top_cats) else 2.0)
    fig.tight_layout()
    fig.savefig(out_dir / "category_distribution.png")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce Apify Store user concentration & category stats via public GET /v2/store."
    )
    parser.add_argument("--max-items", type=int, default=5000,
                        help="Max Actors to fetch (1..15000, default 5000)")
    parser.add_argument("--delay", type=float, default=0.6,
                        help="Minimum seconds between requests (>= 0.5, default 0.6)")
    parser.add_argument("--default-listing", action="store_true",
                        help="Omit includeUnrunnableActors=true (default includes full catalog)")
    parser.add_argument("--out-dir", type=Path, default=Path("out"),
                        help="Output directory for CSV, JSON, and PNG files (default: out)")
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    items, meta = fetch_store_sample(
        max_items=args.max_items,
        delay=args.delay,
        include_unrunnable=not args.default_listing,
    )
    if not items:
        print("No items returned from Store API.", file=sys.stderr)
        return 1

    df, backend = flatten_items(items)
    author_df, cat_df, summary = compute_summaries(df, meta, backend)

    df.to_csv(out_dir / "store_sample.csv", index=False, encoding="utf-8")
    author_df.to_csv(out_dir / "author_concentration.csv", index=False, encoding="utf-8")
    cat_df.to_csv(out_dir / "category_summary.csv", index=False, encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    plot_charts(df, author_df, cat_df, summary, out_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
