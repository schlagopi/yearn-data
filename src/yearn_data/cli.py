"""Command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import run_lifetime_yield
from .config import CHAINS, load_environment, normalize_chain_key
from .discovery import discover
from .exports import export_analysis
from .indexing import index_all_reports
from .pricing import price_unpriced_reports
from .storage import DEFAULT_DB_PATH, connect, init_db, seed_chains


def progress(message: str) -> None:
    print(message, flush=True)


def _chains(values: list[str] | None) -> list[str]:
    if not values:
        return list(CHAINS)
    return [normalize_chain_key(value) for value in values]


def open_db(path: str | Path):
    conn = connect(path)
    init_db(conn)
    seed_chains(conn, CHAINS)
    return conn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yearn-data")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--env", action="append", default=[], help="Extra .env file to load before defaults")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

    discover_p = sub.add_parser("discover")
    discover_p.add_argument("--chains", nargs="+", help="Chains to discover")
    discover_p.add_argument(
        "--find-deployment",
        action="store_true",
        help="Find deployment blocks now. Slower, but reduces later scan ranges.",
    )

    index_p = sub.add_parser("index-events")
    index_p.add_argument("--chains", nargs="+", help="Chains to index")
    index_p.add_argument("--to-block", type=int, help="Stop block for every selected chain")
    index_p.add_argument("--chunk-size", type=int, default=50_000)

    price_p = sub.add_parser("price")
    price_p.add_argument("--limit", type=int, help="Maximum distinct token/timestamp prices to fetch")
    price_p.add_argument("--source", choices=["yprice", "defillama"], default="defillama")
    price_p.add_argument("--fallback", choices=["yprice", "defillama", "none"], default="yprice")

    analyze_p = sub.add_parser("analyze")
    analyze_p.add_argument("job", choices=["lifetime-yield"])

    export_p = sub.add_parser("export")
    export_p.add_argument("job", choices=["lifetime-yield"])
    export_p.add_argument("--out", default="exports")

    run_p = sub.add_parser("run")
    run_p.add_argument("job", choices=["lifetime-yield"])
    run_p.add_argument("--chains", nargs="+", help="Chains to discover/index")
    run_p.add_argument("--to-block", type=int)
    run_p.add_argument("--chunk-size", type=int, default=50_000)
    run_p.add_argument("--price-limit", type=int)
    run_p.add_argument("--price-source", choices=["yprice", "defillama"], default="defillama")
    run_p.add_argument("--price-fallback", choices=["yprice", "defillama", "none"], default="yprice")
    run_p.add_argument("--find-deployment", action="store_true")
    run_p.add_argument("--out", default="exports")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment(args.env)
    conn = open_db(args.db)

    if args.command == "init-db":
        print(f"initialized {args.db}")
        return 0

    if args.command == "discover":
        count = discover(conn, _chains(args.chains), find_deployment=args.find_deployment)
        print(f"discovered/upserted {count} vault rows")
        return 0

    if args.command == "index-events":
        count = index_all_reports(
            conn,
            _chains(args.chains),
            to_block=args.to_block,
            chunk_size=args.chunk_size,
            progress=progress,
        )
        print(f"indexed {count} strategy report logs")
        return 0

    if args.command == "price":
        fallback = None if args.fallback == "none" else args.fallback
        count = price_unpriced_reports(conn, limit=args.limit, source=args.source, fallback=fallback)
        print(f"priced/recorded {count} token timestamp rows")
        return 0

    if args.command == "analyze":
        run_id = run_lifetime_yield(conn)
        print(f"analysis run {run_id} complete")
        return 0

    if args.command == "export":
        paths = export_analysis(conn, args.job, args.out)
        for path in paths:
            print(path)
        return 0

    if args.command == "run":
        chains = _chains(args.chains)
        count = discover(conn, chains, find_deployment=args.find_deployment)
        print(f"discovered/upserted {count} vault rows")
        count = index_all_reports(conn, chains, to_block=args.to_block, chunk_size=args.chunk_size, progress=progress)
        print(f"indexed {count} strategy report logs")
        fallback = None if args.price_fallback == "none" else args.price_fallback
        count = price_unpriced_reports(
            conn,
            limit=args.price_limit,
            source=args.price_source,
            fallback=fallback,
        )
        print(f"priced/recorded {count} token timestamp rows")
        run_id = run_lifetime_yield(conn)
        print(f"analysis run {run_id} complete")
        for path in export_analysis(conn, args.job, args.out):
            print(path)
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
