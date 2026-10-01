#!/usr/bin/env python3
"""HUD stats for the RP5 on ROCKNIX, read from sysfs, procfs and the EmulationStation API.
Every value is None when it cant be read, never 0 or an empty string.
"""

import json
import subprocess
import urllib.request
import urllib.error
import os
import sys


def read_file(path: str) -> str | None:
    """Reads one file, None on any error."""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return None


def find_sysfs_by_name(base_path: str, name_value: str, sub_file: str = 'type') -> str | None:
    """Finds a sysfs folder by a name field, like find_sysfs_by_name('/sys/class/thermal',
    'battery', 'type') for the thermal_zone* with type == 'battery'.
    """
    try:
        entries = os.listdir(base_path)
    except Exception:
        return None

    for entry in entries:
        type_path = os.path.join(base_path, entry, sub_file)
        type_val = read_file(type_path)
        if type_val == name_value:
            return os.path.join(base_path, entry)
    return None


def find_hwmon_by_name(name_value: str) -> str | None:
    """Find an hwmon device by its name field."""
    return find_sysfs_by_name('/sys/class/hwmon', name_value, 'name')


def find_thermal_by_type(type_value: str) -> str | None:
    """Find a thermal zone by its type field."""
    return find_sysfs_by_name('/sys/class/thermal', type_value, 'type')


def read_temperature_c(thermal_path: str) -> float | None:
    """Temperature from a thermal zone, m°C to °C."""
    val = read_file(os.path.join(thermal_path, 'temp'))
    if val is None:
        return None
    try:
        return float(val) / 1000.0
    except Exception:
        return None


def read_battery_stats() -> dict:
    """Read battery capacity, status, voltage, current, temperature."""
    result = {
        'battery_percent': None,
        'battery_status': None,
        'battery_voltage_v': None,
        'battery_current_a': None,
        'battery_power_w': None,
        'battery_temp_c': None,
        'charger_online': None,
    }

    batt_base = '/sys/class/power_supply/battery'

    cap = read_file(os.path.join(batt_base, 'capacity'))
    if cap is not None:
        try:
            result['battery_percent'] = int(cap)
        except Exception:
            pass

    status = read_file(os.path.join(batt_base, 'status'))
    if status is not None:
        result['battery_status'] = status

    volt_uv = read_file(os.path.join(batt_base, 'voltage_now'))
    if volt_uv is not None:
        try:
            result['battery_voltage_v'] = float(volt_uv) / 1e6
        except Exception:
            pass

    curr_ua = read_file(os.path.join(batt_base, 'current_now'))
    if curr_ua is not None:
        try:
            result['battery_current_a'] = float(curr_ua) / 1e6
        except Exception:
            pass

    if result['battery_voltage_v'] is not None and result['battery_current_a'] is not None:
        result['battery_power_w'] = result['battery_voltage_v'] * result['battery_current_a']

    # the battery reports tenths of a degree
    temp_01c = read_file(os.path.join(batt_base, 'temp'))
    if temp_01c is not None:
        try:
            result['battery_temp_c'] = float(temp_01c) / 10.0
        except Exception:
            pass

    charger = read_file('/sys/class/power_supply/pm8150b-charger/online')
    if charger is not None:
        try:
            result['charger_online'] = bool(int(charger))
        except Exception:
            pass

    return result


def read_cpu_stats() -> dict:
    """Read CPU cluster frequencies and governors."""
    result = {
        'cpu_clusters': None,
        'gpu_mhz': None,
        'gpu_load_percent': None,
    }

    clusters = []
    for policy_num in [0, 4, 7]:
        policy_path = f'/sys/devices/system/cpu/cpufreq/policy{policy_num}'

        cpus_line = read_file(os.path.join(policy_path, 'related_cpus'))
        if cpus_line is None:
            continue

        try:
            cpus = [int(x) for x in cpus_line.split()]
        except Exception:
            continue

        # kHz to MHz
        cur_khz = read_file(os.path.join(policy_path, 'scaling_cur_freq'))
        cur_mhz = None
        if cur_khz is not None:
            try:
                cur_mhz = int(cur_khz) // 1000
            except Exception:
                pass

        max_khz = read_file(os.path.join(policy_path, 'cpuinfo_max_freq'))
        max_mhz = None
        if max_khz is not None:
            try:
                max_mhz = int(max_khz) // 1000
            except Exception:
                pass

        governor = read_file(os.path.join(policy_path, 'scaling_governor'))

        clusters.append({
            'cpus': cpus,
            'cur_mhz': cur_mhz,
            'max_mhz': max_mhz,
            'governor': governor,
        })

    if clusters:
        result['cpu_clusters'] = clusters

    gpu_devfreq = '/sys/class/devfreq/3d00000.gpu'
    gpu_cur = read_file(os.path.join(gpu_devfreq, 'cur_freq'))
    if gpu_cur is not None:
        try:
            result['gpu_mhz'] = int(gpu_cur) // 1_000_000  # Hz to MHz
        except Exception:
            pass

    # GPU load is the busy time change between this call and the last one (GpuLoadSampler keeps
    # the state). None on the first call and whenever the fdinfo totals cant be read, never 0.
    result['gpu_load_percent'] = _GPU_LOAD.sample()

    return result


def parse_drm_fdinfo(text: str):
    """Parses one /proc/<pid>/fdinfo/<fd> text into (driver, client_id, engine_gpu_ns), any missing
    field is None.
    """
    driver = client = ns = None
    for line in text.split('\n'):
        key, _, val = line.partition(':')
        val = val.strip()
        if key == 'drm-driver':
            driver = val
        elif key == 'drm-client-id':
            client = val
        elif key == 'drm-engine-gpu':
            parts = val.split()
            if parts:
                try:
                    ns = int(parts[0])
                except ValueError:
                    ns = None
    return driver, client, ns


def dedupe_gpu_totals(records) -> dict | None:
    """records: (driver, client_id, engine_ns) from every DRM fd.

    One DRM client shows up once per fd that points at it (sway alone had three fds for one
    client, each with the same 26 s total), so summing per fd counts it three times. This keeps
    one value per drm-client-id, the largest in case two reads straddle an update. None if no msm
    client could be read.
    """
    totals = {}
    for driver, client, ns in records:
        if driver != 'msm' or client is None or ns is None:
            continue
        totals[client] = max(totals.get(client, 0), ns)
    return totals if totals else None


def read_gpu_engine_totals() -> dict | None:
    """drm-engine-gpu busy nanoseconds per drm-client-id across all processes. Only fds linking into
    /dev/dri get read.
    """
    records = []
    try:
        pids = [p for p in os.listdir('/proc') if p.isdigit()]
    except Exception:
        return None
    for pid in pids:
        fd_dir = '/proc/%s/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        for fd in fds:
            try:
                if not os.readlink('%s/%s' % (fd_dir, fd)).startswith('/dev/dri/'):
                    continue
            except Exception:
                continue
            text = read_file('/proc/%s/fdinfo/%s' % (pid, fd))
            if text is not None:
                records.append(parse_drm_fdinfo(text))
    return dedupe_gpu_totals(records)


class GpuLoadSampler:
    """GPU load % from the change in the de-duplicated drm-engine-gpu totals between calls. The HUD
    sheet calls sample() every second, so the last totals are kept here.
    """

    def __init__(self, reader=None, clock=None):
        import time as _time
        self._reader = reader or read_gpu_engine_totals
        self._clock = clock or _time.monotonic
        self._prev = None           # (t, totals)

    def sample(self):
        return self.update(self._clock(), self._reader())

    def update(self, now, totals):
        if totals is None:
            self._prev = None
            return None
        prev, self._prev = self._prev, (now, totals)
        if prev is None:
            return None             # no baseline yet
        dt_ns = (now - prev[0]) * 1e9
        if dt_ns <= 0:
            return None
        busy = 0
        for client, ns in totals.items():
            old = prev[1].get(client)
            # A client that showed up since the last call is skipped instead of counted from zero, since
            # its total may be older than the interval (an fd passed between processes, or missed by a scan)
            # and counting it would show a fake spike.
            if old is not None and ns >= old:
                busy += ns - old
        pct = busy / dt_ns * 100.0
        return round(min(100.0, max(0.0, pct)), 1)


_GPU_LOAD = GpuLoadSampler()


def read_temperatures() -> dict:
    """Read all available temperatures."""
    result = {}

    thermal_types = [
        'battery',
        'cpu0-thermal', 'cpu1-thermal', 'cpu2-thermal', 'cpu3-thermal',
        'cpu4-thermal', 'cpu5-thermal', 'cpu6-thermal', 'cpu7-thermal',
        'cpu4-top-thermal', 'cpu5-top-thermal', 'cpu6-top-thermal', 'cpu7-top-thermal',
        'cpu4-bottom-thermal', 'cpu5-bottom-thermal', 'cpu6-bottom-thermal', 'cpu7-bottom-thermal',
        'cpu4-5-top-thermal', 'cpu4-5-bottom-thermal',
        'cpu4-6-top-thermal', 'cpu4-6-bottom-thermal',
        'cpu4-7-top-thermal', 'cpu4-7-bottom-thermal',
        'cluster0-thermal', 'cluster1-thermal',
        'gpu-top-thermal', 'gpu-bottom-thermal',
        'mem-thermal', 'video-thermal', 'wlan-thermal',
        'skin-msm-thermal',
        'aoss0-thermal', 'aoss1-thermal',
        'q6-hvx-thermal', 'camera-thermal', 'compute-thermal', 'npu-thermal',
        'xo-thermal', 'wifi-thermal', 'conn-thermal',
        'pm8150l-pcb-thermal',
    ]

    for tz_type in thermal_types:
        tz_path = find_thermal_by_type(tz_type)
        if tz_path is not None:
            temp = read_temperature_c(tz_path)
            if temp is not None:
                result[tz_type] = temp

    # pm8150 variants too
    pm8150_variants = [
        'pm8150-thermal',
        'pm8150b-thermal',
        'pm8150l-thermal',
        'pm8150l-pcb-thermal',
    ]
    for tz_type in pm8150_variants:
        tz_path = find_thermal_by_type(tz_type)
        if tz_path is not None:
            temp = read_temperature_c(tz_path)
            if temp is not None:
                result[tz_type] = temp

    return result if result else None


def read_fan_stats() -> dict:
    """Read fan RPM and PWM."""
    result = {
        'fan_rpm': None,
        'fan_pwm': None,
    }

    pwmfan = find_hwmon_by_name('pwmfan')
    if pwmfan is not None:
        rpm = read_file(os.path.join(pwmfan, 'fan1_input'))
        if rpm is not None:
            try:
                result['fan_rpm'] = int(rpm)
            except Exception:
                pass

        pwm = read_file(os.path.join(pwmfan, 'pwm1'))
        if pwm is not None:
            try:
                result['fan_pwm'] = int(pwm)
            except Exception:
                pass

    return result


def read_memory_stats() -> dict:
    """Read RAM and swap from /proc/meminfo."""
    result = {
        'ram_total_mb': None,
        'ram_available_mb': None,
    }

    meminfo = read_file('/proc/meminfo')
    if meminfo is None:
        return result

    lines = meminfo.split('\n')
    mem_dict = {}
    for line in lines:
        parts = line.split(':')
        if len(parts) == 2:
            key = parts[0].strip()
            val_str = parts[1].strip().split()[0]
            try:
                mem_dict[key] = int(val_str)
            except Exception:
                pass

    if 'MemTotal' in mem_dict:
        result['ram_total_mb'] = mem_dict['MemTotal'] // 1024
    if 'MemAvailable' in mem_dict:
        result['ram_available_mb'] = mem_dict['MemAvailable'] // 1024

    return result


def read_storage_stats() -> dict:
    """Read storage from df."""
    result = {
        'storage_total_gb': None,
        'storage_free_gb': None,
    }

    try:
        output = subprocess.check_output(['df', '/storage'], text=True, timeout=5)
        lines = output.strip().split('\n')
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                total_kb = int(parts[1])
                free_kb = int(parts[3])
                # KB to GiB (1 GiB = 1048576 KB)
                result['storage_total_gb'] = total_kb / 1_048_576
                result['storage_free_gb'] = free_kb / 1_048_576
    except Exception:
        pass

    return result


def read_network_stats() -> dict:
    """Read WiFi SSID, signal, and IP address."""
    result = {
        'wifi_ssid': None,
        'wifi_signal_dbm': None,
        'ip_address': None,
    }

    # Wi-Fi from `iw dev wlan0 link`
    try:
        output = subprocess.check_output(['iw', 'dev', 'wlan0', 'link'], text=True, timeout=5)
        lines = output.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('SSID:'):
                result['wifi_ssid'] = line[5:].strip()
            elif line.startswith('signal:'):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        result['wifi_signal_dbm'] = int(parts[1])
                    except Exception:
                        pass
    except Exception:
        pass

    # IP from `ip -4 -o addr show wlan0`
    try:
        output = subprocess.check_output(['ip', '-4', '-o', 'addr', 'show', 'wlan0'], text=True, timeout=5)
        lines = output.strip().split('\n')
        if lines:
            parts = lines[0].split()
            for i, part in enumerate(parts):
                if '/' in part:
                    result['ip_address'] = part.split('/')[0]
                    break
    except Exception:
        pass

    return result


def read_uptime() -> int | None:
    """Read uptime in seconds."""
    uptime = read_file('/proc/uptime')
    if uptime is None:
        return None
    try:
        return int(float(uptime.split()[0]))
    except Exception:
        return None


def read_running_game() -> str | None:
    """Asks EmulationStation's API what game is running."""
    try:
        req = urllib.request.Request('http://127.0.0.1:1234/runningGame')
        with urllib.request.urlopen(req, timeout=1) as response:
            data = response.read().decode('utf-8')
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and 'msg' in parsed:
                    if 'NO GAME RUNNING' in parsed['msg']:
                        return None
                    return parsed.get('msg')
                return None
            except json.JSONDecodeError:
                if 'NO GAME RUNNING' in data:
                    return None
                return None
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        return None


def sample() -> dict:
    """All HUD stats as a dict with every key, a value is None when it cant be read."""
    result = {}

    battery_stats = read_battery_stats()
    result.update(battery_stats)

    cpu_gpu_stats = read_cpu_stats()
    result.update(cpu_gpu_stats)

    temps = read_temperatures()
    result['temps_c'] = temps

    fan_stats = read_fan_stats()
    result.update(fan_stats)

    memory_stats = read_memory_stats()
    result.update(memory_stats)

    storage_stats = read_storage_stats()
    result.update(storage_stats)

    network_stats = read_network_stats()
    result.update(network_stats)

    result['uptime_s'] = read_uptime()

    result['running_game'] = read_running_game()

    return result


def main():
    """CLI: print sample() as JSON."""
    if '--json' in sys.argv:
        # gpu_load_percent needs a baseline, so prime it, wait, then sample
        _GPU_LOAD.sample()
        import time as _time
        _time.sleep(0.5)
        data = sample()
        print(json.dumps(data, indent=2))
    else:
        print("Usage: python3 hud.py --json")


if __name__ == '__main__':
    main()
