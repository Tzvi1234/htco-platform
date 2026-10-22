"""Run the worker: `python -m htco_worker` (settings from `HTCO_*` environment variables)."""

from htco_worker.main import main

raise SystemExit(main())
