"""Test harness. No real MongoDB is available in the sandbox, so tests run on mongomock.
mongomock lacks $setWindowFields; EmuColl runs the pipeline up to that stage in mongomock,
emulates the window stage in Python (the two shapes the app uses), and runs the tail in mongomock."""
import itertools
import os
import sys

import mongomock
import pytest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import main  # noqa: E402

_ids = itertools.count()

# --- mongomock gaps patched for the tests: $dateFromString and date + number in $add ---
import datetime as _dt  # noqa: E402
from mongomock import aggregate as _agg  # noqa: E402

_orig_date = _agg._Parser._handle_date_operator
_orig_arith = _agg._Parser._handle_arithmetic_operator


def _date_op(self, operator, values):
    if operator == "$dateFromString":
        return _dt.datetime.strptime(self.parse(values["dateString"]), "%Y-%m-%d")
    return _orig_date(self, operator, values)


def _arith_op(self, operator, values):
    if operator == "$add" and isinstance(values, list):
        parsed = [self.parse(v) for v in values]
        if any(isinstance(v, _dt.datetime) for v in parsed):
            base = next(v for v in parsed if isinstance(v, _dt.datetime))
            return base + _dt.timedelta(milliseconds=sum(v for v in parsed if not isinstance(v, _dt.datetime)))
    return _orig_arith(self, operator, values)


_orig_array = _agg._Parser._handle_array_operator


def _array_op(self, operator, value):
    if operator == "$range":
        return list(range(*[self.parse(v) for v in value]))
    return _orig_array(self, operator, value)


_agg._Parser._handle_array_operator = _array_op
_agg._Parser._handle_date_operator = _date_op
_agg._Parser._handle_arithmetic_operator = _arith_op


def emulate_window(docs, spec):
    key, direction = next(iter(spec["sortBy"].items()))
    docs = sorted(docs, key=lambda d: d[key], reverse=direction == -1)
    for name, op in spec["output"].items():
        if "$rank" in op:
            for i, d in enumerate(docs):
                first = next(j for j, x in enumerate(docs) if x[key] == d[key])
                d[name] = first + 1
        else:
            field = op["$sum"][1:]
            lo, hi = op["window"]["documents"]
            for i, d in enumerate(docs):
                vals = [docs[j].get(field) for j in range(max(0, i + lo), i + hi + 1)]
                d[name] = sum(v for v in vals if isinstance(v, (int, float)))
    return docs


class EmuColl:
    def __init__(self, coll, db):
        self._c, self._db = coll, db

    def __getattr__(self, name):
        return getattr(self._c, name)

    def _tmp(self, docs):
        tmp = self._db[f"_tmp{next(_ids)}"]
        if docs:
            tmp.insert_many(docs)
        return EmuColl(tmp, self._db)

    def aggregate(self, pipeline, *a, **k):
        # $lookup with let/pipeline is not implemented in mongomock: emulate per input document
        for i, stage in enumerate(pipeline):
            lk = stage.get("$lookup")
            if lk and "pipeline" in lk:
                out = []
                for d in self.aggregate(pipeline[:i]):
                    env = {f"$${n}": d[ref[1:]] for n, ref in lk.get("let", {}).items()}
                    sub = _subst(lk["pipeline"], env)
                    d[lk["as"]] = list(EmuColl(self._db[lk["from"]], self._db).aggregate(sub))
                    out.append(d)
                return self._tmp(out).aggregate(pipeline[i + 1:])
        idx = next((i for i, s in enumerate(pipeline) if "$setWindowFields" in s), None)
        if idx is None:
            return self._c.aggregate(pipeline, *a, **k)
        docs = emulate_window(list(self._c.aggregate(pipeline[:idx])), pipeline[idx]["$setWindowFields"])
        return self._tmp(docs).aggregate(pipeline[idx + 1:])


def _subst(node, env):
    if isinstance(node, str):
        return env.get(node, node)
    if isinstance(node, list):
        return [_subst(x, env) for x in node]
    if isinstance(node, dict):
        return {k: _subst(v, env) for k, v in node.items()}
    return node


class EmuDB:
    def __init__(self, db):
        self._d = db

    def __getitem__(self, n):
        return EmuColl(self._d[n], self._d)

    __getattr__ = __getitem__


@pytest.fixture()
def api(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    raw = mc["t"]
    monkeypatch.setattr(main, "client", mc)
    monkeypatch.setattr(main, "db", EmuDB(raw))
    monkeypatch.setattr(main, "_indexes_ready", False)
    main.ensure_indexes()
    from fastapi.testclient import TestClient
    return TestClient(main.app, raise_server_exceptions=True), raw
