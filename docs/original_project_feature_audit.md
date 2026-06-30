# Original Project Feature Audit

Source reviewed: `/data/github/HysterAuth/ContinuousAuthentication`.

| ID | Original Module | Existing Function | Prompt Fit | Decision |
|---|---|---|---|---|
| OLD-01 | `ui/compose/*`, legacy `ui/fragments/*` | Sensor/config/detail/privacy UI variants | Partial | Replaced with ContextAuthLab Compose screens: Consent, Onboarding, Home, Tasks, Settings, ResearcherSettings, Diagnostics |
| OLD-02 | `service/DataCollectionService.kt` | Foreground data collection notification | High | Kept concept; simplified to visible collection notification and stop action |
| OLD-03 | `sensor/*` | Motion sensor collection | High | Reimplemented fixed 100 Hz accelerometer/gyroscope/magnetic field collector |
| OLD-04 | `crypto/*`, `security/*` | AES/Tink, key rotation, attestation, public-key envelope | Out of scope | Removed/avoided; current stage uses no content encryption |
| OLD-05 | `network/GrpcManager.kt`, `proto/sensor_data.proto` | gRPC/protobuf upload protocol | Out of scope | Removed/avoided; current upload is REST JSON envelope |
| OLD-06 | `detection/*` | Anomaly detection and trigger logic | Out of scope | Removed/avoided; no anomaly/risk scoring module |
| OLD-07 | `processing/*`, `chunking/*`, `compression/*` | Older packet/chunk/compress/encrypt pipeline | Partial | Replaced with one JSON + LZ4 frame + SHA-256 envelope path |
| OLD-08 | `storage/FileQueueManager.kt`, `database/*` | File queue and Room metadata | Partial | Replaced with filesDir/upload_queue plus `upload_queue.db` metadata |
| OLD-09 | `privacy/*` | Privacy consent and deletion flows | Partial | Replaced with explicit consent, onboarding, visible status, and no hidden collection |
| OLD-10 | `policy/*` | Remote policy including sampling/anomaly settings | Out of scope | Removed/avoided; sampling is source-code constant at 100 Hz |
| OLD-11 | `observability/*`, `monitor/*` | Diagnostics, performance, metrics | Partial | Replaced with local Diagnostics screen and server Prometheus metrics |
| OLD-12 | Usage stats / foreground app detectors | Foreground app context | Not required | Avoided; Accessibility context is the source of UI/component data |

Removed or avoided by design: proto/gRPC, AES/PBKDF2/AAD/RSA/public-key envelope, anomaly detection, authentication model/inference/training, Dashboard/Web UI, remote control or Accessibility actions.
