# Retrieval backend choice execution checkpoints

- Iteration 2 accepted: worker combined suite 267 passed in 218.72s; independent parent combined suite 267 passed in 277.84s. Review found Docker URL-path validation missing; the new regression failed before the shared validation fix, then both URL-path tests passed. Parent dedicated suite after correction: 35 passed in 1.63s. Host probe URL is saved separately from the translated container URL; no API key is persisted. Full commit hook follows.
- Owner explicitly authorized commit, push, merge, tag, install and validate, and requested notification on completion. Notification is armed for this goal; Langfuse remains deferred and ChatGPT Desktop validation excluded.

- 2026-10-07: owner approved implementation and explicitly requested GPT-6 Luna subagents. Writer concurrency is one; independent read-only review may overlap. Task branch feat/retrieval-backend-choice, base5ad396126c5e6f5340e21126f48997b863f40c61.
- Canonical execution checker passed before dispatch. Parent launcher baseline26passed. Jev remains globally disabled as previously observed; deterministic scoped review is used.
- Iteration1 delegated to luna_native_retrieval; only osm_init.py,src/launcher.py,tests/test_launcher.py,tests/test_retrieval_setup.py writable. Root owns plan/checkpoints/CHANGELOG/version/validation.
- Read-only Luna review: CaaSpreflight must precede all setup side effects, local compose env must scrub stale CaaSvars, setup persistence must preserve unrelated .env settings, rebuild missing-key gate precedes version/config/service mutations, host/container URLs need platform-aware tests. Current retryable-error fallback policy is preserved.

- Iteration1 parent verification:33passed in0.07s; native optional settings, strict non-secret validation, compatible same-backend merge and differing-backend suppression reviewed. RED5failed→GREEN33passed. Writer frozen; full commit hook next.
- Iteration1 committed586e485 after fullhook644passed31skipped315.15s. Iteration2 released to GPT-6 Luna worker luna_setup_choice with exclusive osm_init/tests/setupREADME writes; root33testvalidation also passed.
- Iteration2 initialRED5failed8passed; interimGREEN24/25, remainingremote-mountdefaultassertion undercorrection. Two unintendedHTTPattempts from incompleteunitmocks were reported byworker; imports nowpatched and a defaultnetwork-blocking fixture requested. No featureactivation yet.
