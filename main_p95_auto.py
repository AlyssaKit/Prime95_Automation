import argparse
import ctypes
import csv
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

PRIME95_DIR = r"C:\Prime95"
PRIME95_EXE = os.path.join(PRIME95_DIR, "prime95.exe")
PRIME_TXT = os.path.join(PRIME95_DIR, "prime.txt")
RESULTS_TXT = os.path.join(PRIME95_DIR, "results.txt")
SCREEN_LOG = os.path.join(PRIME95_DIR, "screen.log")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_FILE = os.path.join(PROJECT_DIR, "telemetry.csv")
SUMMARY_FILE = os.path.join(PROJECT_DIR, "telemetry_summary.txt")
FFT_VALIDATION_FILE = os.path.join(PROJECT_DIR, "fft_validation.txt")
FFT_TRANSITION_FILE = os.path.join(PROJECT_DIR, "fft_transition_log.txt")

# Set this if HWiNFO is installed somewhere unusual.
# Leave as None to auto-detect common installation paths.
HWINFO_EXE = None

# Total experiment duration controlled by this Python orchestrator.
# Exploratory ladder mapping. The safety cap prevents an indefinite run;
# results.txt observations are used to stop after a detected cycle repeat.
RUN_DURATION_SECONDS = 21600  # 6-hour safety cap
RUN_MODE = "full"

# Match the installed Prime95 Torture Test dialog: 6 minutes per FFT size.
TORTURE_TIME_MINUTES = 6

TELEMETRY_INTERVAL_SECONDS = 1.0
CONSOLE_SAMPLE_INTERVAL = 100

MIN_TORTURE_FFT = 73
MAX_TORTURE_FFT = 213
TORTURE_MEMORY_MB = 0
TORTURE_THREADS = 0

HWINFO_MAP_NAME = "Global\\HWiNFO_SENS_SM2"
FILE_MAP_READ = 0x0004


# ============================================================
# LOGGING
# ============================================================

def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


# ============================================================
# HWiNFO STARTUP
# ============================================================

def hwinfo_is_running():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq HWiNFO64.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (
            result.returncode == 0
            and "HWiNFO64.exe" in result.stdout
        )
    except (OSError, subprocess.SubprocessError):
        return False


def find_hwinfo_exe():
    candidates = []

    if HWINFO_EXE:
        candidates.append(HWINFO_EXE)

    candidates.extend([
        r"C:\Program Files\HWiNFO\HWiNFO64.exe",
        r"C:\Program Files\HWiNFO64\HWiNFO64.exe",
        r"C:\Program Files (x86)\HWiNFO\HWiNFO64.exe",
        r"C:\Program Files (x86)\HWiNFO64\HWiNFO64.exe",
    ])

    for path in candidates:
        if os.path.isfile(path):
            return path

    try:
        result = subprocess.run(
            ["where", "HWiNFO64.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                path = line.strip()
                if os.path.isfile(path):
                    return path
    except (OSError, subprocess.SubprocessError):
        pass

    return None


def launch_hwinfo_elevated(exe_path):
    """Launch HWiNFO Sensors through the Windows UAC prompt."""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p

        result = shell32.ShellExecuteW(
            None,
            "runas",
            exe_path,
            "-sensors",
            os.path.dirname(exe_path),
            1,
        )

        if result is None or int(result) <= 32:
            log(
                "ERROR: Windows did not start HWiNFO "
                f"(ShellExecuteW result: {result})."
            )
            return False

        return True

    except Exception as error:
        log(f"ERROR: Could not launch HWiNFO: {error}")
        return False


def wait_for_hwinfo_shared_memory(timeout_seconds):
    """Wait until HWiNFO's Shared Memory interface can be read."""
    deadline = time.monotonic() + timeout_seconds
    last_error = None

    while time.monotonic() < deadline:
        try:
            sensors = read_hwinfo_sensors()
            if sensors is not None:
                return True, last_error
        except Exception as error:
            last_error = error

        time.sleep(0.5)

    return False, last_error


def start_hwinfo():
    """
    Connect to HWiNFO if it is already available; otherwise launch HWiNFO
    in Sensors mode with a Windows UAC prompt and wait for Shared Memory.

    Returns:
        (pid, started_by_script)

    HWiNFO Shared Memory Support must already be enabled in HWiNFO's
    settings. The script can start HWiNFO, but it does not alter that
    HWiNFO preference.
    """
    log("Checking for HWiNFO Shared Memory...")

    available, last_error = wait_for_hwinfo_shared_memory(3.0)
    if available:
        log("Connected to existing HWiNFO Shared Memory.")
        return get_hwinfo_pid(), False

    # A HWiNFO process may exist while Sensors/Shared Memory is not ready.
    if hwinfo_is_running():
        log("HWiNFO is running; waiting for its Shared Memory interface...")
        available, last_error = wait_for_hwinfo_shared_memory(12.0)
        if available:
            log("Connected to existing HWiNFO Shared Memory.")
            return get_hwinfo_pid(), False

        log("ERROR: HWiNFO is running, but Shared Memory is unavailable.")
        log("Open HWiNFO Sensors and make sure Shared Memory Support is enabled.")
        if last_error is not None:
            log(f"HWiNFO Shared Memory error: {last_error}")
        return None, False

    exe_path = find_hwinfo_exe()
    if exe_path is None:
        log("ERROR: HWiNFO64.exe was not found.")
        log("Install HWiNFO64 or set HWINFO_EXE near the top of this script.")
        return None, False

    log(f"HWiNFO is not running. Starting: {exe_path}")
    log("Approve the Windows UAC prompt if it appears.")

    if not launch_hwinfo_elevated(exe_path):
        return None, False

    # Give Windows time to create the process, then capture its PID so we
    # can close only the HWiNFO instance that this script started.
    pid = None
    pid_deadline = time.monotonic() + 10.0
    while time.monotonic() < pid_deadline:
        pid = get_hwinfo_pid()
        if pid is not None:
            break
        time.sleep(0.5)

    log("Waiting for HWiNFO Sensors / Shared Memory to become ready...")
    available, last_error = wait_for_hwinfo_shared_memory(30.0)

    if not available:
        log("ERROR: HWiNFO started, but Shared Memory did not become available.")
        log("In HWiNFO Sensors settings, enable Shared Memory Support, then run again.")
        if last_error is not None:
            log(f"HWiNFO Shared Memory error: {last_error}")
        return pid, True

    log("HWiNFO Shared Memory is ready.")
    return pid, True


def get_hwinfo_pid():
    """Return the PID of HWiNFO64.exe, or None if it is not running."""
    try:
        result = subprocess.run(
            [
                "tasklist",
                "/FI",
                "IMAGENAME eq HWiNFO64.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            if "HWiNFO64.exe" not in line:
                continue

            row = next(csv.reader([line]))
            if len(row) >= 2:
                try:
                    return int(row[1])
                except ValueError:
                    pass

    except (OSError, subprocess.SubprocessError, csv.Error):
        pass

    return None


def stop_process(process, name):
    if process is None:
        return

    # HWiNFO is launched through ShellExecuteW, so we track its PID.
    if isinstance(process, int):
        log(f"Stopping {name} (PID {process})...")

        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                log(f"{name} stopped.")
            else:
                log(
                    f"WARNING: Could not stop {name} "
                    f"(taskkill exit code {result.returncode})."
                )

        except (OSError, subprocess.SubprocessError) as error:
            log(f"WARNING: Could not stop {name}: {error}")

        return

    # Prime95 still uses a normal Popen object.
    if process.poll() is not None:
        return

    log(f"Stopping {name}...")

    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    log(f"{name} stopped with exit code {process.returncode}.")


# ============================================================
# HWiNFO SHARED MEMORY
# ============================================================

def get_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.OpenFileMappingW.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.OpenFileMappingW.restype = ctypes.c_void_p

    kernel32.MapViewOfFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p

    kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    kernel32.UnmapViewOfFile.restype = ctypes.c_int

    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    return kernel32


def read_u32(data, offset):
    return int.from_bytes(
        data[offset:offset + 4],
        "little",
        signed=False,
    )


def read_hwinfo_sensors():
    kernel32 = get_kernel32()

    handle = kernel32.OpenFileMappingW(
        FILE_MAP_READ,
        False,
        HWINFO_MAP_NAME,
    )

    if not handle:
        return None

    try:
        ptr = kernel32.MapViewOfFile(
            handle,
            FILE_MAP_READ,
            0,
            0,
            0,
        )

        if not ptr:
            return None

        try:
            header = ctypes.string_at(ptr, 44)

            if header[:4] != b"HWiS":
                return None

            reading_offset = read_u32(header, 32)
            reading_size = read_u32(header, 36)
            reading_count = read_u32(header, 40)

            if reading_size <= 0 or reading_count <= 0:
                return None

            total_size = reading_offset + reading_size * reading_count
            data = ctypes.string_at(ptr, total_size)

            sensors = []

            for index in range(reading_count):
                base = reading_offset + index * reading_size

                if base + 292 > len(data):
                    break

                sensor_type = read_u32(data, base)

                label_bytes = data[base + 12:base + 140]
                label = (
                    label_bytes
                    .split(b"\0", 1)[0]
                    .decode("mbcs", errors="replace")
                    .strip()
                )

                if not label:
                    continue

                value = ctypes.c_double.from_buffer_copy(
                    data[base + 284:base + 292]
                ).value

                sensors.append({
                    "index": index,
                    "type": sensor_type,
                    "name": label,
                    "value": value,
                })

            return sensors

        finally:
            kernel32.UnmapViewOfFile(ptr)

    finally:
        kernel32.CloseHandle(handle)


def resolve_sensors(sensors):
    by_name = {
        sensor["name"].strip().lower(): sensor
        for sensor in sensors
    }

    required = {
        "temperature": "cpu core",
        "effective_frequency": "average effective clock",
        "thermal_throttling": "thermal throttling (htc)",
        "package_power": "cpu package power",
        "vcore": "cpu core voltage (svi2 tfn)",
    }

    result = {}

    for key, name in required.items():
        if name not in by_name:
            return None
        result[key] = by_name[name]

    optional = {
        "prochot_cpu": "thermal throttling (prochot cpu)",
        "prochot_ext": "thermal throttling (prochot ext)",
        "throttle_power": "throttle reason - power",
        "throttle_thermal": "throttle reason - thermal",
        "throttle_current": "throttle reason - current",
    }

    for key, name in optional.items():
        result[key] = by_name.get(name)

    return result


def optional_flag(sensor):
    if sensor is None:
        return ""
    return int(sensor["value"] != 0)


def collect_sample():
    sensors = read_hwinfo_sensors()

    if sensors is None:
        return None

    resolved = resolve_sensors(sensors)

    if resolved is None:
        return None

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "core_temperature_c": resolved["temperature"]["value"],
        "effective_frequency_mhz": resolved["effective_frequency"]["value"],
        "thermal_throttling": int(
            resolved["thermal_throttling"]["value"] != 0
        ),
        "package_power_w": resolved["package_power"]["value"],
        "vcore_v": resolved["vcore"]["value"],
        "prochot_cpu": optional_flag(resolved["prochot_cpu"]),
        "prochot_ext": optional_flag(resolved["prochot_ext"]),
        "throttle_reason_power": optional_flag(resolved["throttle_power"]),
        "throttle_reason_thermal": optional_flag(resolved["throttle_thermal"]),
        "throttle_reason_current": optional_flag(resolved["throttle_current"]),
    }


def collect_telemetry(stop_event=None):
    fields = [
        "timestamp_utc",
        "core_temperature_c",
        "effective_frequency_mhz",
        "thermal_throttling",
        "package_power_w",
        "vcore_v",
        "prochot_cpu",
        "prochot_ext",
        "throttle_reason_power",
        "throttle_reason_thermal",
        "throttle_reason_current",
    ]

    log(
        f"Collecting HWiNFO telemetry for "
        f"{RUN_DURATION_SECONDS} seconds..."
    )

    valid = 0
    missing = 0
    samples = []
    interrupted = False
    stop_reason = "Safety time limit reached"
    start = time.monotonic()
    next_sample = start

    with open(
        TELEMETRY_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as csv_file:

        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()

        try:
            while time.monotonic() - start < RUN_DURATION_SECONDS:

                if stop_event is not None and stop_event.is_set():
                    stop_reason = (
                        "Diagnostic FFT transition verified"
                        if RUN_MODE == "diagnostic"
                        else "Full FFT ladder cycle completed by all workers"
                    )
                    log("FFT stop condition reached; ending telemetry collection.")
                    break

                sample = collect_sample()

                if sample is None:
                    missing += 1
                else:
                    writer.writerow(sample)
                    csv_file.flush()
                    samples.append(sample)
                    valid += 1

                    # Keep console output quiet: show sample 1 and every 100th sample.
                    if valid == 1 or valid % CONSOLE_SAMPLE_INTERVAL == 0:
                        log(
                            f"Sample {valid}: "
                            f"{sample['core_temperature_c']:.1f} C | "
                            f"{sample['effective_frequency_mhz']:.0f} MHz | "
                            f"{sample['package_power_w']:.2f} W | "
                            f"{sample['vcore_v']:.3f} V | "
                            f"thermal={sample['thermal_throttling']}"
                        )

                next_sample += TELEMETRY_INTERVAL_SECONDS
                delay = next_sample - time.monotonic()

                if delay > 0:
                    time.sleep(delay)

        except KeyboardInterrupt:
            interrupted = True
            stop_reason = "Keyboard interrupt (Ctrl+C)"
            log("Keyboard interrupt received. Finalizing collected telemetry...")
            if stop_event is not None:
                stop_event.set()

    elapsed = time.monotonic() - start

    log(f"Telemetry saved to: {TELEMETRY_FILE}")
    log(f"Valid samples: {valid}")
    log(f"Missing samples: {missing}")
    log(f"Elapsed: {elapsed:.2f} seconds")

    # Write a human-readable summary into the project folder.
    with open(SUMMARY_FILE, "w", encoding="utf-8") as summary:
        summary.write("Prime95 + HWiNFO Telemetry Summary\n")
        summary.write("=================================\n\n")

        summary.write("RUN INFORMATION\n")
        summary.write("+----------------------+----------------------+\n")
        summary.write("| Item                 | Value                |\n")
        summary.write("+----------------------+----------------------+\n")
        summary.write(f"| {'Execution method':<20} | {'Native Windows':<20} |\n")
        summary.write(f"| {'Stress profile':<20} | {'Small FFTs':<20} |\n")
        summary.write(f"| {'FFT range':<20} | {f'{MIN_TORTURE_FFT}K-{MAX_TORTURE_FFT}K':<20} |\n")
        summary.write(f"| {'Time per FFT size':<20} | {f'{TORTURE_TIME_MINUTES} minutes':<20} |\n")
        summary.write(f"| {'Total run duration':<20} | {f'{RUN_DURATION_SECONDS} seconds':<20} |\n")
        summary.write(f"| {'Measured duration':<20} | {f'{elapsed:.2f} seconds':<20} |\n")
        summary.write(f"| {'Stop reason':<20} | {stop_reason:<20} |\n")
        summary.write(f"| {'Valid samples':<20} | {str(valid):<20} |\n")
        summary.write(f"| {'Missing samples':<20} | {str(missing):<20} |\n")
        summary.write("+----------------------+----------------------+\n\n")

        if samples:
            def stats(key):
                values = [s[key] for s in samples]
                return min(values), sum(values) / len(values), max(values)

            tmin, tavg, tmax = stats("core_temperature_c")
            fmin, favg, fmax = stats("effective_frequency_mhz")
            pmin, pavg, pmax = stats("package_power_w")
            vmin, vavg, vmax = stats("vcore_v")
            throttled = sum(1 for s in samples if s["thermal_throttling"])

            summary.write("TELEMETRY SUMMARY\n")
            summary.write("+---------------------------+------------+------------+------------+\n")
            summary.write("| Metric                    | Minimum    | Average    | Maximum    |\n")
            summary.write("+---------------------------+------------+------------+------------+\n")
            summary.write(f"| {'Core Temperature (C)':<25} | {tmin:>10.2f} | {tavg:>10.2f} | {tmax:>10.2f} |\n")
            summary.write(f"| {'Effective Frequency MHz':<25} | {fmin:>10.2f} | {favg:>10.2f} | {fmax:>10.2f} |\n")
            summary.write(f"| {'Package Power (W)':<25} | {pmin:>10.2f} | {pavg:>10.2f} | {pmax:>10.2f} |\n")
            summary.write(f"| {'Vcore (V)':<25} | {vmin:>10.3f} | {vavg:>10.3f} | {vmax:>10.3f} |\n")
            summary.write("+---------------------------+------------+------------+------------+\n\n")

            summary.write("THERMAL THROTTLING\n")
            summary.write("+---------------------------+----------------------+\n")
            summary.write("| Item                      | Result               |\n")
            summary.write("+---------------------------+----------------------+\n")
            summary.write(f"| {'Thermal Throttling HTC':<25} | {('YES' if throttled else 'NO'):<20} |\n")
            summary.write(f"| {'Throttled samples':<25} | {f'{throttled} of {len(samples)}':<20} |\n")
            summary.write("+---------------------------+----------------------+\n\n")

        summary.write("OUTPUT FILES\n")
        summary.write("+----------------------+------------------------------------------+\n")
        summary.write("| File                 | Location                                 |\n")
        summary.write("+----------------------+------------------------------------------+\n")
        summary.write(f"| {'Telemetry CSV':<20} | {TELEMETRY_FILE:<40} |\n")
        summary.write(f"| {'Summary TXT':<20} | {SUMMARY_FILE:<40} |\n")
        summary.write(f"| {'FFT validation':<20} | {FFT_VALIDATION_FILE:<40} |\n")
        summary.write(f"| {'FFT transitions':<20} | {FFT_TRANSITION_FILE:<40} |\n")
        summary.write(f"| {'Prime95 results.txt':<20} | {RESULTS_TXT:<40} |\n")
        summary.write("+----------------------+------------------------------------------+\n")

    log(f"Summary saved to: {SUMMARY_FILE}")

    # For an automatic ladder stop, validate against the measured duration
    # rather than the 6-hour safety cap.
    minimum = max(1, int(elapsed * 0.90))

    if valid < minimum:
        log(
            f"ERROR: Expected at least approximately {minimum} "
            f"valid HWiNFO samples."
        )
        return False, interrupted

    return True, interrupted


# ============================================================
# PRIME95 RESULTS FILE MANAGEMENT
# ============================================================

def prepare_fresh_results_file():
    """Archive any previous Prime95 results.txt so this run starts clean."""
    if not os.path.exists(RESULTS_TXT):
        log("No previous Prime95 results.txt found. Starting with a fresh results file.")
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = os.path.join(PRIME95_DIR, f"results_before_{stamp}.txt")

    try:
        os.replace(RESULTS_TXT, archive_path)
        log(f"Archived previous Prime95 results.txt to: {archive_path}")
        log("Fresh results.txt will contain only this run's Prime95 output.")
        return archive_path
    except OSError as error:
        log(f"ERROR: Could not archive previous results.txt: {error}")
        log("Prime95 will not be started because clean per-run FFT evidence is required.")
        return False


# ============================================================
# PRIME95 CONFIGURATION
# ============================================================

def configure_prime95():
    settings = {
        "StressTester": "1",
        "TortureThreads": str(TORTURE_THREADS),
        "MinTortureFFT": str(MIN_TORTURE_FFT),
        "MaxTortureFFT": str(MAX_TORTURE_FFT),
        "TortureMem": str(TORTURE_MEMORY_MB),
        "TortureTime": str(TORTURE_TIME_MINUTES),
    }

    if os.path.exists(PRIME_TXT):
        with open(
            PRIME_TXT,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as file:
            existing = file.readlines()
    else:
        existing = []

    output = []
    written = set()

    for line in existing:
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)

        if match:
            key = match.group(1)

            if key in settings:
                if key not in written:
                    output.append(f"{key}={settings[key]}\n")
                    written.add(key)
                continue

        output.append(line)

    for key, value in settings.items():
        if key not in written:
            output.append(f"{key}={value}\n")

    with open(PRIME_TXT, "w", encoding="utf-8") as file:
        file.writelines(output)

    log("Prime95 Small FFT configuration written.")
    log(f"Prime95 TortureTime: {TORTURE_TIME_MINUTES} minute(s) per FFT size.")
    log("Runtime FFT detection will use this run's fresh Prime95 results.txt.")
    if RUN_MODE == "diagnostic":
        log("Diagnostic stop: both workers must advance to a different FFT size.")
        log(f"Diagnostic safety cap: {RUN_DURATION_SECONDS // 60} minutes.")
    else:
        log("Full-run stop: both workers must complete one ladder and wrap to the starting FFT.")
        log(f"Full-run safety cap: {RUN_DURATION_SECONDS // 3600} hours.")


def validate_prime95_config():
    if not os.path.exists(PRIME_TXT):
        return False

    expected = {
        "StressTester": "1",
        "TortureThreads": str(TORTURE_THREADS),
        "MinTortureFFT": str(MIN_TORTURE_FFT),
        "MaxTortureFFT": str(MAX_TORTURE_FFT),
        "TortureMem": str(TORTURE_MEMORY_MB),
        "TortureTime": str(TORTURE_TIME_MINUTES),
    }

    found = {}

    with open(
        PRIME_TXT,
        "r",
        encoding="utf-8",
        errors="replace",
    ) as file:
        for line in file:
            match = re.match(
                r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$",
                line,
            )
            if match:
                found[match.group(1)] = match.group(2)

    return (
        all(found.get(k) == v for k, v in expected.items())
    )


# ============================================================
# PRIME95 START / STOP
# ============================================================

def get_prime95_pids():
    """Return all currently running prime95.exe process IDs."""
    pids = []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq prime95.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return pids

        for line in result.stdout.splitlines():
            if "prime95.exe" not in line.lower():
                continue
            try:
                row = next(csv.reader([line]))
                if len(row) >= 2:
                    pids.append(int(row[1]))
            except (csv.Error, ValueError, StopIteration):
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def get_process_cpu_seconds(process):
    """Read total kernel + user CPU time for the exact Popen process."""
    if process is None or process.poll() is not None:
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        get_times.restype = ctypes.c_int

        creation = ctypes.c_uint64()
        exit_time = ctypes.c_uint64()
        kernel = ctypes.c_uint64()
        user = ctypes.c_uint64()

        if not get_times(
            ctypes.c_void_p(int(process._handle)),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        # FILETIME units are 100 ns.
        return (kernel.value + user.value) / 10_000_000.0
    except Exception:
        return None


def verify_prime95_started(process):
    """Verify that the exact Prime95 process launched by this script is alive and doing CPU work."""
    if process is None:
        return False

    if process.poll() is not None:
        log(f"ERROR: Prime95 exited during startup with code {process.returncode}.")
        return False

    running_pids = get_prime95_pids()
    if process.pid not in running_pids:
        log(f"ERROR: Launched Prime95 PID {process.pid} is not present in the process list.")
        return False

    log(f"Fresh Prime95 process verified (PID {process.pid}).")

    cpu_before = get_process_cpu_seconds(process)
    time.sleep(3)

    if process.poll() is not None:
        log(f"ERROR: Prime95 exited during workload verification with code {process.returncode}.")
        return False

    cpu_after = get_process_cpu_seconds(process)
    if cpu_before is not None and cpu_after is not None:
        cpu_delta = cpu_after - cpu_before
        if cpu_delta < 0.25:
            log(
                f"ERROR: Prime95 PID {process.pid} used only {cpu_delta:.2f} CPU seconds "
                "during the 3-second workload check. The torture workload does not appear active."
            )
            return False
        log(
            f"Prime95 workload activity verified: {cpu_delta:.2f} CPU seconds "
            "used during the 3-second check."
        )
    else:
        log("WARNING: Could not read Prime95 CPU time; PID/liveness verification passed.")

    return True


def start_prime95():
    if not os.path.exists(PRIME95_EXE):
        log(f"ERROR: Prime95 not found at {PRIME95_EXE}")
        return None

    log("Launching Prime95...")

    try:
        return subprocess.Popen(
            [PRIME95_EXE, "-t"],
            cwd=PRIME95_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        log(f"ERROR: Could not launch Prime95: {error}")
        return None


def monitor_prime95_output(process, output_lines):
    if process.stdout is None:
        return

    try:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                output_lines.append(line)
    except (OSError, ValueError):
        pass



def parse_self_test_line(line):
    """Return (fft_k, thread_number, thread_count) for a Prime95 self-test line."""
    match = re.search(
        r"\bSelf-test\s+(\d+(?:\.\d+)?)\s*K\s+"
        r"\(thread\s+(\d+)\s+of\s+(\d+)\)\s+passed!",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1)), int(match.group(2)), int(match.group(3))


def monitor_results_file(stop_event, runtime_lines, transitions, run_start):
    """Tail results.txt and stop on the selected full-cycle or diagnostic condition."""
    position = 0
    if os.path.exists(RESULTS_TXT):
        try:
            position = os.path.getsize(RESULTS_TXT)
        except OSError:
            position = 0

    # Each Prime95 worker advances independently. Track workers separately so
    # overlapping old/new FFT lines cannot create a false ladder wrap.
    workers = {}

    while not stop_event.is_set():
        if not os.path.exists(RESULTS_TXT):
            time.sleep(0.5)
            continue

        try:
            size = os.path.getsize(RESULTS_TXT)
            if size < position:
                position = 0
            with open(RESULTS_TXT, "r", encoding="utf-8", errors="replace") as file:
                file.seek(position)
                new_text = file.read()
                position = file.tell()
        except OSError:
            time.sleep(0.5)
            continue

        if new_text:
            for line in new_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                runtime_lines.append(line)

                parsed = parse_self_test_line(line)
                if parsed is None:
                    continue
                fft_size, thread_no, thread_count = parsed

                state = workers.setdefault(thread_no, {
                    "start_fft": fft_size,
                    "last_fft": None,
                    "distinct": set(),
                    "reached_upper": False,
                    "moved": False,
                    "wrapped": False,
                    "thread_count": thread_count,
                })

                if fft_size == state["last_fft"]:
                    continue

                elapsed = time.monotonic() - run_start
                state["last_fft"] = fft_size
                state["distinct"].add(fft_size)
                if fft_size != state["start_fft"]:
                    state["moved"] = True
                if fft_size >= (MAX_TORTURE_FFT * 0.90):
                    state["reached_upper"] = True
                if (state["reached_upper"] and state["moved"] and
                        fft_size == state["start_fft"]):
                    state["wrapped"] = True

                transitions.append({
                    "elapsed_seconds": elapsed,
                    "fft_k": fft_size,
                    "thread": thread_no,
                    "line": line,
                })
                log(
                    f"Runtime FFT observed: thread {thread_no}/{thread_count} | "
                    f"{fft_size:g}K | elapsed: {format_elapsed(elapsed)}"
                )

                expected_workers = max(st["thread_count"] for st in workers.values())
                have_all_workers = all(i in workers for i in range(1, expected_workers + 1))

                if RUN_MODE == "diagnostic":
                    # Short test: prove both workers can leave their initial FFT
                    # and reach a new FFT level. No full ladder is required.
                    if have_all_workers and all(
                        workers[i]["moved"] for i in range(1, expected_workers + 1)
                    ):
                        log("DIAGNOSTIC PASS: all workers reached a new FFT level.")
                        stop_event.set()
                        return
                else:
                    # Full run: do not stop until every worker independently
                    # reaches the upper ladder and returns to its starting FFT.
                    if have_all_workers and all(
                        workers[i]["wrapped"] for i in range(1, expected_workers + 1)
                    ):
                        log("FULL CYCLE COMPLETE: all workers wrapped to the starting FFT.")
                        stop_event.set()
                        return

        time.sleep(0.5)


def ladder_cycle_complete(transitions):
    """Legacy helper retained for compatibility; worker-aware logic is used above."""
    return False


def format_elapsed(seconds):
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def write_fft_transition_log(transitions):
    with open(FFT_TRANSITION_FILE, "w", encoding="utf-8") as file:
        file.write("Prime95 FFT Transition Log\n")
        file.write("==========================\n\n")
        file.write(f"Configured range: {MIN_TORTURE_FFT}K-{MAX_TORTURE_FFT}K\n")
        file.write(f"TortureTime: {TORTURE_TIME_MINUTES} minute(s) per FFT size\n")
        file.write(f"Run mode: {RUN_MODE}\n")
        file.write(f"Safety cap: {RUN_DURATION_SECONDS} seconds\n\n")

        if not transitions:
            file.write("No completed FFT stage was observed in results.txt during this run.\n")
            return

        file.write("Elapsed    FFT Size    Time Until Next Observed FFT\n")
        file.write("---------  ----------  ----------------------------\n")

        for index, item in enumerate(transitions):
            if index + 1 < len(transitions):
                dwell = transitions[index + 1]["elapsed_seconds"] - item["elapsed_seconds"]
                dwell_text = format_elapsed(dwell)
            else:
                dwell_text = "not observed"

            file.write(
                f"{format_elapsed(item['elapsed_seconds']):<9}  "
                f"{item['fft_k']:>7g}K    {dwell_text}\n"
            )

        file.write("\nObserved Prime95 result lines:\n")
        for item in transitions:
            file.write(item["line"] + "\n")

    log(f"FFT transition log saved to: {FFT_TRANSITION_FILE}")


def extract_fft_sizes(lines):
    """Return FFT sizes in K found in Prime95 output or results.txt."""
    sizes = []

    patterns = [
        r"\bFFT\s+length\s+(\d+(?:\.\d+)?)\s*K\b",
        r"\bSelf-test\s+(\d+(?:\.\d+)?)\s*K\b",
    ]

    for line in lines:
        for pattern in patterns:
            for value in re.findall(pattern, line, flags=re.IGNORECASE):
                try:
                    sizes.append(float(value))
                except ValueError:
                    pass

    return sizes


def validate_runtime_fft(output_lines, runtime_lines, results_before_mtime):
    """
    Validate any runtime FFT sizes Prime95 exposes.

    prime.txt is always validated separately. Runtime FFT text is an
    additional sanity check requested by the assignment. On Windows,
    Prime95 may display worker text only in its GUI rather than stdout.
    """
    sources = []
    lines = list(output_lines)

    if lines:
        sources.append("Prime95 process output")

    if runtime_lines:
        lines.extend(runtime_lines)
        sources.append("results.txt live runtime output")

    # results.txt is archived before each run, so any current file belongs
    # only to this run. Read it once at final validation to catch lines written
    # just before Prime95 shutdown that the tailing thread may not have consumed.
    if os.path.exists(RESULTS_TXT):
        try:
            with open(RESULTS_TXT, "r", encoding="utf-8", errors="replace") as file:
                result_lines = file.readlines()
            lines.extend(result_lines)
            sources.append("fresh results.txt from this run")
        except OSError:
            pass

    fft_sizes = extract_fft_sizes(lines)
    unique_sizes = sorted(set(fft_sizes))

    if unique_sizes:
        invalid = [
            size for size in unique_sizes
            if not (MIN_TORTURE_FFT <= size <= MAX_TORTURE_FFT)
        ]

        if invalid:
            status = "FAILED"
            detail = (
                "Observed FFT size(s) outside configured Small FFT range: "
                + ", ".join(f"{size:g}K" for size in invalid)
            )
        else:
            status = "PASSED"
            detail = (
                "Observed runtime FFT size(s): "
                + ", ".join(f"{size:g}K" for size in unique_sizes)
                + f" (all within {MIN_TORTURE_FFT}K-{MAX_TORTURE_FFT}K)"
            )
    else:
        status = "NOT OBSERVED"
        detail = (
            "No parseable runtime FFT result was observed. This diagnostic runs for "
            "7 minutes so at least one 6-minute FFT stage should normally finish. "
            "The Small FFT prime.txt configuration was still verified before launch."
        )

    with open(FFT_VALIDATION_FILE, "w", encoding="utf-8") as file:
        file.write("Prime95 FFT Validation\n")
        file.write("======================\n\n")
        file.write(f"Configured Small FFT range: {MIN_TORTURE_FFT}K-{MAX_TORTURE_FFT}K\n")
        file.write(f"Prime95 time per FFT size: {TORTURE_TIME_MINUTES} minute(s)\n")
        file.write(f"Python total experiment duration: {RUN_DURATION_SECONDS} second(s)\n")
        file.write("prime.txt configuration: PASSED\n")
        file.write(f"Runtime FFT observation: {status}\n")
        file.write(f"Details: {detail}\n")
        if sources:
            file.write("Text source(s): " + ", ".join(sources) + "\n")

    log(f"Runtime FFT validation: {status}")
    log(detail)
    log(f"FFT validation saved to: {FFT_VALIDATION_FILE}")

    return status, unique_sizes, detail


def stop_prime95(process):
    if process is None or process.poll() is not None:
        return

    log("Requesting Prime95 normal shutdown.")

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        WM_CLOSE = 0x0010
        CALLBACK = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        user32.EnumWindows.argtypes = [CALLBACK, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_bool

        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32

        target_pid = process.pid
        windows = []

        def callback(hwnd, _):
            pid = ctypes.c_uint32()

            user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(pid),
            )

            if pid.value == target_pid:
                windows.append(hwnd)

            return True

        user32.EnumWindows(CALLBACK(callback), 0)

        for hwnd in windows:
            user32.PostMessageW(
                hwnd,
                WM_CLOSE,
                0,
                0,
            )

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("Prime95 did not close normally; force killing.")
            process.kill()
            process.wait()

    except Exception as error:
        log(f"WARNING: Prime95 normal shutdown failed: {error}")

        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass

    log(f"Prime95 exited with code {process.returncode}.")


# ============================================================
# MAIN
# ============================================================

# ============================================================
# WINDOWS ELEVATION
# ============================================================

def is_process_elevated():
    """Return True when this Python process is running elevated."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_self_as_admin():
    """Relaunch this Python script through the Windows UAC prompt."""
    shell32 = ctypes.windll.shell32

    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p

    parameters = subprocess.list2cmdline(sys.argv)

    result = shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        parameters,
        os.getcwd(),
        1,
    )

    if result is None or result <= 32:
        log(
            f"ERROR: Could not relaunch as Administrator "
            f"(ShellExecuteW result: {result})."
        )
        return False

    return True


def main():
    global RUN_DURATION_SECONDS, TORTURE_TIME_MINUTES, RUN_MODE

    parser = argparse.ArgumentParser(description="Prime95 + HWiNFO telemetry orchestrator")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help=(
            "Run a short transition test: use 1 minute per FFT size and stop "
            "after every Prime95 worker reaches a different FFT size."
        ),
    )
    args = parser.parse_args()

    if args.diagnostic:
        RUN_MODE = "diagnostic"
        TORTURE_TIME_MINUTES = 1
        RUN_DURATION_SECONDS = 600  # 10-minute safety cap

    log("==============================================")
    log(" Prime95 + HWiNFO Telemetry")
    log("==============================================")
    if RUN_MODE == "diagnostic":
        log("MODE: DIAGNOSTIC - 1 minute per FFT size.")
        log("Diagnostic stop condition: both workers must advance to a new FFT size.")
        log("Diagnostic safety cap: 10 minutes.")
    else:
        log("MODE: FULL - stop after both workers complete one FFT ladder cycle.")

    hwinfo_process = None
    started_hwinfo = False
    prime95_process = None

    try:
        if not os.path.exists(PRIME95_EXE):
            log(f"ERROR: Prime95 not found at {PRIME95_EXE}")
            return 1

        existing_prime95 = get_prime95_pids()
        if existing_prime95:
            log(
                "ERROR: Prime95 is already running "
                f"(PID(s): {', '.join(str(pid) for pid in existing_prime95)})."
            )
            log("Close the existing Prime95 instance before starting this automated test.")
            log("==============================================")
            log(" STATUS: RUN NOT STARTED")
            log("==============================================")
            return 1

        log("No existing Prime95 process detected. Clean start confirmed.")

        # HWiNFO is started before Prime95 so its sensors are already active.
        hwinfo_process, started_hwinfo = start_hwinfo()

        # Do not start Prime95 unless HWiNFO telemetry is actually available.
        if read_hwinfo_sensors() is None:
            log("ERROR: HWiNFO telemetry is not available. Prime95 will not be started.")
            log("==============================================")
            log(" STATUS: RUN NOT STARTED")
            log("==============================================")
            return 1

        # Archive any old results so runtime validation and ladder detection
        # are based only on evidence generated by this specific run.
        archived_results = prepare_fresh_results_file()
        if archived_results is False:
            log("==============================================")
            log(" STATUS: RUN NOT STARTED")
            log("==============================================")
            return 1
        results_before_mtime = None

        configure_prime95()

        if not validate_prime95_config():
            log("ERROR: Prime95 configuration validation failed.")
            return 1

        log(
            f"Prime95 Small FFT configuration verified "
            f"({MIN_TORTURE_FFT}K-{MAX_TORTURE_FFT}K)."
        )

        prime95_process = start_prime95()

        if prime95_process is None:
            return 1

        output_lines = []
        runtime_fft_lines = []
        fft_transitions = []
        results_stop_event = threading.Event()
        run_start = time.monotonic()

        output_thread = threading.Thread(
            target=monitor_prime95_output,
            args=(prime95_process, output_lines),
            daemon=True,
        )
        output_thread.start()

        results_thread = threading.Thread(
            target=monitor_results_file,
            args=(results_stop_event, runtime_fft_lines, fft_transitions, run_start),
            daemon=True,
        )
        results_thread.start()

        log("Waiting 5 seconds for Prime95 to initialize...")
        time.sleep(5)

        if not verify_prime95_started(prime95_process):
            stop_prime95(prime95_process)
            return 1

        if collect_sample() is None:
            log("ERROR: Required HWiNFO sensors are not available.")
            stop_prime95(prime95_process)
            return 1

        log("Required HWiNFO sensors verified.")

        telemetry_ok, interrupted = collect_telemetry(results_stop_event)

        # Whether the ladder completed, the safety cap expired, or Ctrl+C was used,
        # always shut down Prime95 cleanly and preserve the collected data.
        stop_prime95(prime95_process)
        output_thread.join(timeout=3)
        results_stop_event.set()
        results_thread.join(timeout=2)
        write_fft_transition_log(fft_transitions)

        fft_status, fft_sizes, fft_detail = validate_runtime_fft(
            output_lines,
            runtime_fft_lines,
            results_before_mtime,
        )

        # A directly observed out-of-range FFT is a real validation failure.
        # "NOT OBSERVED" is reported transparently because Windows Prime95
        # may keep worker FFT text in its GUI instead of stdout.
        fft_ok = fft_status != "FAILED"

        if interrupted:
            log("==============================================")
            log(" STATUS: RUN INTERRUPTED - DATA SAVED")
            log("==============================================")
            return 130

        if telemetry_ok and fft_ok:
            log("==============================================")
            log(" STATUS: RUN COMPLETED")
            log("==============================================")
            return 0

        log("==============================================")
        log(" STATUS: RUN FAILED / NOT VERIFIED")
        log("==============================================")
        return 1

    except KeyboardInterrupt:
        log("Keyboard interrupt received.")
        stop_prime95(prime95_process)
        return 1

    except Exception as error:
        log(f"ERROR: Unexpected failure: {error}")
        stop_prime95(prime95_process)
        return 1

    finally:
        # Only close HWiNFO if this script launched it.
        if started_hwinfo:
            stop_process(hwinfo_process, "HWiNFO")


if __name__ == "__main__":
    sys.exit(main())