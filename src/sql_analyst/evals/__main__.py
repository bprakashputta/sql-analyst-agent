"""python -m sql_analyst.evals {download,run,report,safety}"""

from __future__ import annotations

import argparse
import json

import structlog


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m sql_analyst.evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download", help="fetch BIRD mini-dev into data/")
    r = sub.add_parser("run", help="run one configuration on a split")
    r.add_argument("--config", required=True)
    r.add_argument("--split", default="dev", choices=["dev", "test"])
    r.add_argument("--limit", type=int)
    r.add_argument("--workers", type=int, default=6)
    sub.add_parser("report", help="aggregate runs into evals/results.{json,md}")
    s = sub.add_parser("safety", help="run the safety suite")
    s.add_argument("--no-llm", action="store_true", help="only the direct SQL attack corpus")
    args = p.parse_args()

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))  # warnings only
    if args.cmd == "download":
        from .bird import download

        download()
    elif args.cmd == "run":
        from .run import run

        run(args.config, args.split, limit=args.limit, workers=args.workers)
    elif args.cmd == "report":
        from .report import build

        res = build()
        print(json.dumps(res, indent=1))
    elif args.cmd == "safety":
        from .safety import run_suite

        run_suite(with_llm=not args.no_llm)


if __name__ == "__main__":
    main()
