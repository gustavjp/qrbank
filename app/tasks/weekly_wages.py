"""Invoke periodically from an external cron service, not a web worker.

Example: python -m app.tasks.weekly_wages
"""
from app.services.weekly_wages import run_due_weekly_wages


def main() -> int:
    summary = run_due_weekly_wages()
    print(
        f"Due classrooms: {summary.classrooms_due}; "
        f"posted: {summary.payments_posted}; replayed: {summary.payments_replayed}; "
        f"skipped: {summary.payments_skipped}; failed: {summary.payments_failed}"
    )
    return 1 if summary.payments_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
