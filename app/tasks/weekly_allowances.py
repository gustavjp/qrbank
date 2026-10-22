"""Invoke periodically from external cron alongside weekly wages, not web workers.

Example: python -m app.tasks.weekly_allowances
"""
from app.services.weekly_allowances import run_due_weekly_allowances


def main() -> int:
    summary = run_due_weekly_allowances()
    print(
        f"Due classrooms: {summary.classrooms_due}; "
        f"posted: {summary.payments_posted}; replayed: {summary.payments_replayed}; "
        f"skipped: {summary.payments_skipped}; failed: {summary.payments_failed}"
    )
    return 1 if summary.payments_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
