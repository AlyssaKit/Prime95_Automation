# Prime95 + HWiNFO Telemetry Orchestrator

A Windows Python automation project that runs a Prime95 **Small FFT**
torture workload and records host-side CPU telemetry from **HWiNFO
Shared Memory**.

The current script supports two modes:

-   **Full run:** Prime95 uses the configured 73K--213K Small FFT range
    with 6 minutes per FFT size. The script tracks Prime95 worker FFT
    transitions and stops after both workers complete one observed
    FFT-ladder cycle and return to their starting FFT size.
-   **Diagnostic run:** Prime95 uses 1 minute per FFT size and stops
    after both workers advance from their initial FFT size to a
    different FFT size. A 10-minute safety cap prevents an indefinite
    diagnostic.

The orchestrator also supports `Ctrl+C` cleanup so collected telemetry
is finalized and summarized before Prime95 is shut down.

## What the Project Measures

The script reads the following HWiNFO sensors once per second:

  Measurement           HWiNFO sensor
  --------------------- -------------------------------
  Core temperature      `CPU Core`
  Effective frequency   `Average Effective Clock`
  Thermal throttling    `Thermal Throttling (HTC)`
  Package power         `CPU Package Power`
  Vcore                 `CPU Core Voltage (SVI2 TFN)`

It also records optional HWiNFO throttle indicators when available,
including PROCHOT and power/thermal/current throttle reasons.

## Requirements

-   Windows
-   Python 3
-   [Prime95](https://www.mersenne.org/download/) installed at `C:\Prime95\prime95.exe`
-   [HWiNFO64](https://www.hwinfo.com/download/) installed
-   [HWiNFO **Shared Memory Support**](https://www.hwinfo.com/forum/threads/shared-memory-support-i-cant-find.7286/) enabled
-   HWiNFO sensor names compatible with the mappings above

The script uses Python's standard library only; no API keys, cloud
services, or Python package credentials are required.

If Prime95 or HWiNFO is installed somewhere else, update the
configuration paths near the top of the script. `HWINFO_EXE = None`
enables HWiNFO auto-detection from common installation locations.

## Running the Program

For the full experiment:

``` powershell
py main_p95_auto.py
```

For the short diagnostic test:

``` powershell
py main_p95_auto.py --diagnostic
```

The diagnostic mode verifies HWiNFO access, Prime95 startup, per-worker
FFT detection, automatic stopping, telemetry collection, and output
finalization before committing to a long run.

You can also press `Ctrl+C` during telemetry collection. The script
preserves the telemetry collected so far, creates the summary, and
requests a normal Prime95 shutdown.

## Full-Run Configuration

The script programmatically configures Prime95 with:

``` text
StressTester=1
TortureThreads=0
MinTortureFFT=73
MaxTortureFFT=213
TortureMem=0
TortureTime=6
```

The 73K--213K values define the allowed FFT range. Prime95 does not
necessarily execute every integer FFT size in that interval.

In the completed full experiment, the distinct runtime FFT sizes
observed were:

``` text
80K, 84K, 96K, 100K, 112K, 120K, 128K,
144K, 160K, 168K, 192K, 200K
```

Prime95 did not simply execute those sizes once in a perfectly linear
sequence. Some FFT sizes repeated and the two workers transitioned at
different times. The automation therefore monitors each worker's actual
runtime output rather than assuming a fixed hard-coded ladder.

The completed run ended when both workers returned to their starting 80K
FFT after progressing through the upper portion of the configured range.
The observed FFT sizes were all within the configured 73K--213K range.

## How the Automation Works

At startup, the script checks that an old Prime95 process is not already
running. It then connects to an existing HWiNFO Shared Memory interface
or attempts to locate and launch HWiNFO64 in Sensors mode. If HWiNFO is
launched by the script, Windows may display a UAC prompt.

Before Prime95 starts, an existing `results.txt` is archived with a
timestamp. This keeps runtime FFT evidence from the current experiment
separate from previous runs.

The script writes and validates the Prime95 Small FFT configuration,
launches a fresh `prime95.exe -t` process, verifies that the exact
process is alive and consuming CPU time, verifies the required HWiNFO
sensors, and begins one-second telemetry collection.

Prime95 `results.txt` is monitored while the workload runs. FFT
observations are associated with their Prime95 worker thread so
asynchronous worker transitions do not falsely signal completion.

In full mode, the automatic stop condition requires both workers to
progress through the upper portion of the configured ladder and return
to their respective starting FFT size. In diagnostic mode, both workers
must advance from their starting FFT size to a different FFT size.

After the stop condition, safety limit, or keyboard interrupt, the
program finalizes telemetry, requests a normal Prime95 shutdown, writes
the FFT transition log, and performs runtime FFT validation.

## Completed Full-Run Results

A completed full-mode experiment produced the following results:

  Result                      Observed value
  --------------------------- -------------------------
  Prime95 mode                Small FFTs
  Configured FFT range        73K--213K
  Time per FFT size           6 minutes
  Workers monitored           2
  Full-cycle elapsed time     5,234 seconds (1:27:14)
  Valid telemetry samples     5,234
  Missing telemetry samples   0
  Runtime FFT validation      PASSED
  Prime95 exit code           0
  Final program status        RUN COMPLETED

The distinct FFT sizes observed during this run were 80K, 84K, 96K,
100K, 112K, 120K, 128K, 144K, 160K, 168K, 192K, and 200K. Both workers
eventually returned to 80K, which satisfied the program's full-cycle
stop condition.

The six-hour full-run value is only a safety cap. It was not the actual
test duration: the measured representative cycle completed automatically
after approximately 87 minutes.

## Output Files

  ---------------------------------------------------------------------
  File                               Purpose
  ---------------------------------- ----------------------------------
  `telemetry.csv`                    One-second raw HWiNFO measurements

  `telemetry_summary.txt`            Run information and
                                     min/average/max telemetry
                                     statistics

  `fft_transition_log.txt`           FFT transitions observed during
                                     the run

  `fft_validation.txt`               Runtime verification that observed
                                     FFT sizes were within the
                                     configured range
  ---------------------------------------------------------------------

Prime95 writes the current run's `results.txt` in `C:\Prime95`. Older
results are archived as timestamped `results_before_YYYYMMDD_HHMMSS.txt`
files before a new run.

## Safety and Cleanup

The script does not intentionally begin the Prime95 workload unless
HWiNFO telemetry is available and the Prime95 configuration passes
validation. If Prime95 is already running, the automated run is rejected
to avoid collecting data from an unrelated process.

When the run ends normally, Prime95 receives a normal Windows close
request, with fallback termination handling if necessary. If the script
started HWiNFO itself, it also attempts to close that HWiNFO process. An
HWiNFO instance that was already running is left open.

## Public Repository / Security Review

The current Python source was reviewed for common credential patterns.
It does **not** require or contain API keys, passwords, access tokens,
private keys, or service credentials.

Before publishing the repository, avoid committing machine-specific or
generated runtime artifacts unless they are intentionally being shared.
Review or exclude:

``` text
telemetry.csv
telemetry_summary.txt
fft_transition_log.txt
fft_validation.txt
results.txt
results_before_*.txt
prime.txt
prime.ini
local.txt
*.log
```

Some Prime95 configuration files can contain machine-specific
identifiers or local configuration values. Raw telemetry/results can
reveal timestamps, local filesystem paths, hardware characteristics, or
environmental details.

A suggested `.gitignore` is:

``` gitignore
# Generated telemetry/results
telemetry.csv
telemetry_summary.txt
fft_transition_log.txt
fft_validation.txt
results.txt
results_before_*.txt
*.log

# Prime95 machine/local configuration
prime.txt
prime.ini
local.txt

# Python artifacts
__pycache__/
*.py[cod]
.venv/
venv/

# Editor/OS artifacts
.vscode/
.idea/
.DS_Store
Thumbs.db

# Secrets/environment files if added later
.env
.env.*
*.pem
*.key
```

Do not put passwords, API keys, tokens, private keys, or other
credentials directly into source code. If a future version needs
credentials, keep them outside version control (for example, in
environment variables) and ignore any local secret files.

## Suggested Repository Layout

``` text
Prime95Proj/
├── main_p95_auto.py
├── README.md
├── AI_USE.md
└── .gitignore
```

The repository should contain the automation source and documentation
rather than machine-specific Prime95 configuration or raw local
telemetry. Sanitized example output can be added separately if needed
for demonstration.

## Notes

-   HWiNFO Shared Memory Support must already be enabled.
-   Sensor labels can vary by CPU/platform or HWiNFO version. If a
    required label differs, update `resolve_sensors()`.
-   The full-run duration is workload-dependent. In the completed test
    described above, the representative cycle took 5,234 seconds (1
    hour, 27 minutes, 14 seconds). The six-hour value is only a safety
    cap.
-   Diagnostic mode intentionally differs from the final
    six-minute-per-FFT experiment and should not be used as the final
    dataset.
