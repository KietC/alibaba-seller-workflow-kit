# Synthetic loopback browser harness (not IAB conformance)

The harness under `tests/offline-iab-harness` is loopback-only synthetic evidence. It deliberately never contacts Alibaba and never claims Codex Browser conformance or live provenance. Its request guard covers page HTTP(S) requests routed through Playwright; it does not claim WebSocket coverage (`websocket_egress_covered=false`). Production conformance must prove every transport used by the real page independently.

Run it on Windows:

```powershell
& (Join-Path $SkillRoot 'tests\offline-iab-harness\run.ps1') -OutputDirectory '<NEW_OUTPUT_DIRECTORY>'
```

It launches an installed Chromium-compatible browser through the bundled Playwright package, binds a fixture server to `127.0.0.1` on an ephemeral port, installs the request guard before navigation, and exercises:

- the reviewed set of 18 language labels, including Arabic/Hebrew RTL;
- per-language non-identical text, full DOM replacement, stale-handle rejection and re-resolution;
- current-control platform counter readback and limit checks;
- malformed locale/counter negative cases;
- default-deny unknown/external requests and permanent final-write blocking;
- one visible draft event, one paused request instance, ledger `0 -> 1`, `SAVE_ATTEMPTED`, and one release;
- direct replay/second ledger/second release rejection;
- exactly one synthetic draft delta with online/pending unchanged;
- refresh/reopen persistence across all 18 localized values.

A successful receipt ends at `FINAL_REVIEW_READY_LOCAL_CONTRACT`, contains `browser_live_attested=false`, `alibaba_live_executed=false`, `implementation_ready=false`, and `final_write_enabled=false`. It is a prerequisite for later canaries, never a replacement for them.

The remaining live sequence is: (1) platform-signed IAB conformance capture when the platform exposes it; (2) Alibaba zero-write probe; (3) one separately approved draft canary. No offline result may skip those layers.
