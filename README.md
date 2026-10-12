# Employee Attendance & Analytics API

FastAPI + MongoDB 6+; all code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env (see .env.example)
uvicorn app.main:app --port 8000
```

Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers, 503 before that.

- `REVIEW.md`: defects found in the starter and how they were fixed (Part A).
- `DECISIONS.md`: index, race, tie, headcount and scaling decisions (Part C).
- It runs on `mongomock`, which does not implement `$setWindowFields`, so the harness emulates that one stage; the `/admin/explain` endpoint is not covered by it and needs a real MongoDB (7.0 recommended) to try.

All endpoints in `openapi.yaml` are implemented.
