---
name: backend-unit-tests
description: Write isolated pytest tests for the Taskly FastAPI backend.
---

# Backend unit tests

1. Follow the layout and naming under `backend/tests`.
2. Use pytest fixtures and `monkeypatch` for environment isolation.
3. Mock external boundaries, not the function under test.
4. For configuration tests, isolate every relevant environment variable and
   mock dotenv loading so the developer's local `.env` cannot affect results.
5. Assert returned values, raised exception types, and meaningful messages or
   structured fields where those are part of observable behavior.
6. Cover success and failure branches without starting PostgreSQL or making
   network requests.
7. Restore state through pytest fixtures rather than manual cleanup.
8. Do not use skip, xfail, arbitrary sleeps, or broad exception assertions.

Proposed paths must be Python files named `test_*.py` under `backend/tests`.
