# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` is the natural key, so it guarantees one record per day and serves per-employee reads, PATCH and every `$lookup`. `(emp_code, punch_in desc)` lets punch-out find the latest punch-in. `(date desc, emp_code)` matches the list sort and the leaderboard's month range. `(status, date desc, emp_code)` serves status-filtered lists. `employees`: unique `emp_code`, `(department, emp_code)`, `joined_on`. I rejected indexing `late_minutes`: the date range already narrows the leaderboard, and every write would pay for it.

2. **Punch-in race.** Both requests compute the same document and call `insert_one`. The unique index admits one; the other raises `DuplicateKeyError`, which becomes 409. Nothing is read first.

3. **Ties.** `$rank` gives competition ranks (1, 2, 2, 4), and I filter `rank <= limit` afterwards. Everyone tied at the cutoff is returned, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` ≤ month end) and looks up their logs. `$unwind` with `preserveNullAndEmptyArrays` keeps zero-log employees, so each employee adds 1.

5. **100x data.** Keep per-employee daily and monthly rollups updated on every write, so analytics read summaries instead of raw logs. I'd also replace `skip` pagination with range-based paging.
