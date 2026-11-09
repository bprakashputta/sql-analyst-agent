"""Command line: `sql-analyst serve` runs the API + UI, `sql-analyst ask "..."` answers one question,
`sql-analyst demo-data` fetches the Chinook sample database."""

from __future__ import annotations

import argparse
import json


def main() -> None:
    p = argparse.ArgumentParser(prog="sql-analyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API and UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("ask", help="answer one question and print the JSON result")
    a.add_argument("question")
    sub.add_parser("demo-data", help="download the Chinook demo database")
    args = p.parse_args()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("sql_analyst.api:create_app", factory=True, host=args.host, port=args.port)
    elif args.cmd == "demo-data":
        from .demo import download_chinook

        download_chinook()
    elif args.cmd == "ask":
        from .api import AppState
        from .config import get_settings

        state = AppState(get_settings())
        result = state.agent.ask(args.question)
        keep = (
            "status",
            "executed_sql",
            "columns",
            "rows",
            "answer",
            "caveats",
            "chart",
            "attempts",
        )
        print(json.dumps({k: result.get(k) for k in keep}, indent=2, default=str))


if __name__ == "__main__":
    main()
