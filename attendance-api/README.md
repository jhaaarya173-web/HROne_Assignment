# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne assignment. All application code is in `app/main.py`.

## Run

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in .env (see .env.example)
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required (`$densify`, `$setWindowFields`); developed against 7.0
(`docker run -d -p 27017:27017 mongo:7`). Indexes are created at startup, idempotently.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # needs MongoDB at MONGO_URI; uses the throwaway databases attendance_test and attendance_big
```

* `tests/test_api.py`: status codes, R1-R5 edge values (grace second, 29 vs 30 min overtime, 9.125 h, 4.495 h, overnight shifts), validation, PATCH history, and real parallel requests for punch-in, punch-out, employee creation and PATCH.
* `tests/dataset.py` builds ~100k records with the awkward cases (ties, zero-log employees, mid-month and future joiners, weekend records, missing weekdays, legacy documents, orphan logs, stored values that differ from recomputed ones).
* `tests/oracle.py` recomputes every analytic in plain Python, independently of the app; `tests/test_analytics.py` compares all four analytics endpoints with it, checks that `/admin/explain` shows `IXSCAN` and no `COLLSCAN`, and that endpoints answer in < 0.5 s at 100k records.

## Interpretations worth knowing

* **Punch-out** picks the latest record whose `punch_in` is at or before `punched_at`, as specified. A `punched_at` earlier than every punch-in therefore finds no record (404); equal to the punch-in is 422.
* **Trend range**: up to 92 days inclusive is allowed (a full quarter); 93 is 422.
* **Moving average** averages the reported (already rounded) `attendance_rate` values.
* **Rounding** is half-up everywhere. In pipelines it is done in Decimal128 with `$floor(x * 10^n + 0.5)`, because MongoDB's `$round` rounds half to even.
* **PATCH**: "changes nothing" means `status`, `punch_in` and `punch_out` all end up as stored; derived fields in the body are ignored. Concurrent corrections use optimistic concurrency (the write only applies if `history` length and `punch_out` are unchanged); the loser gets 409, so no history entry is lost.
* An explicit `"punched_at": null` is treated like an omitted one (current time).

Nothing is missing from the contract.
