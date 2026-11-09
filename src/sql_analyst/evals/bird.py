"""BIRD mini-dev: download, split and load.

BIRD (BIg Bench for LaRge-scale Database Grounded Text-to-SQL Evaluation), mini-dev release,
CC BY-SA 4.0. Questions, hints ("evidence") and gold SQL come from the 2026 revision on Hugging Face
(`birdsql/bird_mini_dev`, file `data/mini_dev_sqlite.jsonl`, 500 rows); the 11 SQLite databases come
from the official mini-dev archive. Both go into the gitignored `data/` directory.

Split: the 500 question ids are shuffled with a fixed seed. The first 100 are `dev` (used while
building the agent: prompts, linker settings), the other 400 are `test` (scored only for the final
report). The split is committed in `evals/split.json`.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

QUESTIONS_URL = (
    "https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/main/data/mini_dev_sqlite.jsonl"
)
QUESTIONS_SHA256 = "926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606"
DATABASES_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
SPLIT_SEED = 20261108
DEV_SIZE = 100

DATA = Path("data")
RAW = DATA / "raw"
DB_DIR = DATA / "bird" / "dev_databases"
SPLIT_FILE = Path("evals") / "split.json"


@dataclass(frozen=True)
class Question:
    question_id: int
    db_id: str
    question: str
    evidence: str
    gold_sql: str
    difficulty: str

    @property
    def db_path(self) -> Path:
        return DB_DIR / self.db_id / f"{self.db_id}.sqlite"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(*, force: bool = False) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    qfile = RAW / "mini_dev_sqlite.jsonl"
    if force or not qfile.exists():
        print(f"downloading questions from {QUESTIONS_URL}")
        urllib.request.urlretrieve(QUESTIONS_URL, qfile)
    digest = _sha256(qfile)
    if digest != QUESTIONS_SHA256:
        raise SystemExit(f"questions file hash mismatch: {digest}")
    print(f"questions ok: {qfile} (sha256 {digest[:12]}...)")

    if DB_DIR.exists() and not force and any(DB_DIR.glob("*/*.sqlite")):
        print(f"databases already in {DB_DIR}")
        return
    zpath = RAW / "minidev.zip"
    if force or not zpath.exists():
        print(f"downloading databases from {DATABASES_URL} (~800 MB)")
        urllib.request.urlretrieve(DATABASES_URL, zpath)
    print("extracting the SQLite databases")
    tmp = DATA / "bird_tmp"
    with zipfile.ZipFile(zpath) as z:
        members = [m for m in z.namelist() if "/dev_databases/" in m]
        for m in members:
            target = (tmp / m).resolve()
            if not str(target).startswith(str(tmp.resolve())):  # zip-slip guard
                raise SystemExit(f"unsafe path in archive: {m}")
        z.extractall(tmp, members)
    src = next(tmp.rglob("dev_databases"))
    DB_DIR.parent.mkdir(parents=True, exist_ok=True)
    if DB_DIR.exists():
        shutil.rmtree(DB_DIR)
    shutil.move(str(src), DB_DIR)
    shutil.rmtree(tmp)
    print(f"databases ok: {len(list(DB_DIR.glob('*/*.sqlite')))} in {DB_DIR}")


def load_questions() -> list[Question]:
    path = RAW / "mini_dev_sqlite.jsonl"
    if not path.exists():
        raise SystemExit(
            "BIRD mini-dev not found: run `make data` (python -m sql_analyst.evals download)"
        )
    out = []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        out.append(
            Question(
                r["question_id"],
                r["db_id"],
                r["question"],
                r.get("evidence") or "",
                r["SQL"],
                r["difficulty"],
            )
        )
    return out


def make_split(questions: list[Question]) -> dict[str, list[int]]:
    ids = sorted(q.question_id for q in questions)
    random.Random(SPLIT_SEED).shuffle(ids)
    return {"seed": SPLIT_SEED, "dev": sorted(ids[:DEV_SIZE]), "test": sorted(ids[DEV_SIZE:])}


def split(name: str) -> list[Question]:
    questions = load_questions()
    if SPLIT_FILE.exists():
        s = json.loads(SPLIT_FILE.read_text())
    else:
        s = make_split(questions)
        SPLIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPLIT_FILE.write_text(json.dumps(s, indent=1))
    wanted = set(s[name])
    return [q for q in questions if q.question_id in wanted]
