"""`python -m services.alerts.worker` is the documented entrypoint (task brief). This module lets
`python -m services.alerts` do the same thing, delegating entirely to `worker.main()`."""

from __future__ import annotations

from services.alerts.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
