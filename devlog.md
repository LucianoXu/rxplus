- 2026-02-17
    Configured conda environment.yml.
- 2026-02-16
    Added `StreamingResampler` in audio.
- 2026-02-04
    Added `rxplus.gateway` package - reusable reactive gateway infrastructure
- 2026-02-03
    Update telemetry.py.
- 2026-02-02
    Refined Audio and Video components. Tests constructed.
    Refined reconnection logic of RxWSClient.
    Refined CLI and RxWS components.
- 2026-01-28
    Decided to remove customized logging completely.
- 2026-01-27
    Completely refined Logging logic. One known issue: the first starting log record will be lost sometimes in RxWS.
- 2026-01-25 
    Removed `LogComp` and updated the usage in `RxWS`. 
    Refined RxWS series compnents.
- 2026-01-22 Finished the new logging component supporting OpenTelemetry framework.
- 2026-01-20 
  - Implemented modular installation. Add GitHub Action for sanity check. 
  - Enriched documentation. Improved logging file control automation.
- 2025-09-19 Improved `ws.py` logic.
- 2025-09-08 Added `retry_with_signal`.
- 2025-09-01 Updated `Logger` to be multi-process safe.
- 2025-08-29 `log_redirect_to` now support functions like `print` as the observer.
- 2025-08-28 Update websocket components to use multi-thread.
- 2025-08-28 Add `WSObject` datatype for websocket components, which allows it to transmit python object directly. Related testing tasks implemented.