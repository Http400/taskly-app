---
name: frontend-unit-tests
description: Write focused Vitest tests for Taskly TypeScript and React behavior.
---

# Frontend unit tests

1. Follow the colocated `*.test.ts` and `*.test.tsx` conventions under
   `frontend/src`.
2. Use Vitest and Testing Library already present in the repository.
3. Test observable return values, errors, DOM behavior, and user interactions.
4. Mock browser or network boundaries, not the function being tested.
5. For `src/lib/http.ts`, mock `globalThis.fetch`, restore mocks after each
   test, and cover successful JSON, 204, server errors, validation errors,
   malformed error bodies, network failure, and abort propagation.
6. Assert meaningful error messages where they are user-visible behavior.
7. Keep tests deterministic and type-safe. Do not add dependencies, snapshots,
   sleeps, skipped tests, or focused-only tests.

Proposed paths must end in `.test.ts` or `.test.tsx` under `frontend/src`.
