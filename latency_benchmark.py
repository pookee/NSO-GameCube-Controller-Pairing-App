#!/usr/bin/env python3
"""
Latency Benchmark for NSO GameCube Controller

Measures and displays real-time input pipeline latency:
  - USB report interval (time between consecutive HID reads)
  - BLE notification interval (time between consecutive GATT notifications)
  - Processing time (parse + normalize + emulation update)
  - Effective polling rate (Hz)
  - Report drop count (if any)

Usage:
    python latency_benchmark.py              # auto-detect USB controller
    python latency_benchmark.py --ble        # scan and connect via BLE
    python latency_benchmark.py --duration 30  # run for 30 seconds
    python latency_benchmark.py --csv          # output CSV to file

Requires: hidapi (pip install hidapi), bleak (pip install bleak) for --ble
"""

import argparse
import collections
import os
import sys
import time

VENDOR_ID = 0x057e
PRODUCT_ID = 0x2073
IS_WINDOWS = sys.platform == 'win32'


def find_controller():
    """Find and open the NSO GameCube controller via HID."""
    import hid
    devices = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    if not devices:
        return None, None
    dev = hid.device()
    dev.open_path(devices[0]['path'])
    return dev, devices[0]


def translate_0x05(data):
    """Minimal translation for Windows 0x05 reports (just sticks + triggers)."""
    buf = [0] * 64
    b0 = data[5]
    b1 = data[6]
    b2 = data[7]
    b3 = 0
    if b0 & 0x04: b3 |= 0x01
    if b0 & 0x08: b3 |= 0x02
    if b0 & 0x01: b3 |= 0x04
    if b0 & 0x02: b3 |= 0x08
    if b0 & 0x40: b3 |= 0x10
    if b0 & 0x80: b3 |= 0x20
    if b1 & 0x02: b3 |= 0x40
    buf[3] = b3
    b4 = 0
    if b2 & 0x01: b4 |= 0x01
    if b2 & 0x04: b4 |= 0x02
    if b2 & 0x08: b4 |= 0x04
    if b2 & 0x02: b4 |= 0x08
    if b2 & 0x40: b4 |= 0x10
    if b2 & 0x80: b4 |= 0x20
    buf[4] = b4
    for i in range(6):
        buf[6 + i] = data[11 + i]
    if len(data) > 62:
        buf[13] = data[61]
        buf[14] = data[62]
    return buf


def simulate_processing(data):
    """Simulate the full processing pipeline to measure its cost."""
    left_x = data[6] | ((data[7] & 0x0F) << 8)
    left_y = ((data[7] >> 4) | (data[8] << 4))
    right_x = data[9] | ((data[10] & 0x0F) << 8)
    right_y = ((data[10] >> 4) | (data[11] << 4))

    cx, rx = 2048.0, 1800.0
    left_x_n = max(-1.0, min(1.0, (left_x - cx) / rx))
    left_y_n = max(-1.0, min(1.0, (left_y - cx) / rx))
    right_x_n = max(-1.0, min(1.0, (right_x - cx) / rx))
    right_y_n = max(-1.0, min(1.0, (right_y - cx) / rx))

    lt = data[13] if len(data) > 13 else 0
    rt = data[14] if len(data) > 14 else 0

    buttons = 0
    if len(data) > 3: buttons |= data[3]
    if len(data) > 4: buttons |= data[4] << 8

    _ = int(max(-32767, min(32767, left_x_n * 32767)))
    _ = int(max(-32767, min(32767, left_y_n * 32767)))
    _ = int(max(-32767, min(32767, right_x_n * 32767)))
    _ = int(max(-32767, min(32767, right_y_n * 32767)))

    return left_x_n, left_y_n, right_x_n, right_y_n, lt, rt, buttons


def percentile(sorted_list, p):
    """Compute the p-th percentile from a sorted list."""
    if not sorted_list:
        return 0.0
    k = (len(sorted_list) - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[f]
    return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])


def format_ms(us):
    """Format microseconds as milliseconds string."""
    return f"{us / 1000:.2f}"


def clear_line():
    """Clear current terminal line."""
    sys.stdout.write('\r' + ' ' * 120 + '\r')


def print_final_report(all_intervals, all_process_times, total_reports,
                       total_dropped, elapsed, csv_path, mode_label):
    """Print the final summary report (shared by USB and BLE modes)."""
    print()
    print()
    print("=" * 72)
    print("  RESULTS")
    print("=" * 72)
    print()
    print(f"  Mode:            {mode_label}")
    print(f"  Duration:        {elapsed:.1f} seconds")
    print(f"  Total reports:   {total_reports}")
    print(f"  Dropped (drain): {total_dropped}")
    if elapsed > 0:
        print(f"  Effective rate:  {total_reports / elapsed:.1f} Hz")
    print()

    if all_intervals:
        sorted_int = sorted(all_intervals)
        label = "BLE Notification" if "BLE" in mode_label else "USB Report"
        print(f"  {label} Interval (time between consecutive reports):")
        print(f"    Min:    {format_ms(sorted_int[0]):>8} ms")
        print(f"    Avg:    {format_ms(sum(sorted_int) // len(sorted_int)):>8} ms")
        print(f"    Median: {format_ms(percentile(sorted_int, 50)):>8} ms")
        print(f"    p95:    {format_ms(percentile(sorted_int, 95)):>8} ms")
        print(f"    p99:    {format_ms(percentile(sorted_int, 99)):>8} ms")
        print(f"    Max:    {format_ms(sorted_int[-1]):>8} ms")

        avg_ms = (sum(sorted_int) / len(sorted_int)) / 1000
        if avg_ms > 0:
            detected_hz = 1000 / avg_ms
            print()
            if "BLE" in mode_label:
                if avg_ms < 10:
                    print(f"  >> BLE connection interval: ~{avg_ms:.1f}ms ({detected_hz:.0f} Hz) — excellent")
                elif avg_ms < 20:
                    print(f"  >> BLE connection interval: ~{avg_ms:.1f}ms ({detected_hz:.0f} Hz) — good")
                elif avg_ms < 40:
                    print(f"  >> BLE connection interval: ~{avg_ms:.1f}ms ({detected_hz:.0f} Hz) — default")
                else:
                    print(f"  >> BLE connection interval: ~{avg_ms:.1f}ms ({detected_hz:.0f} Hz) — slow")
                    print("     Win10 limits BLE to ~30-60ms; Win11 can do ~7.5-15ms")
            else:
                if detected_hz > 800:
                    print(f"  >> USB polling rate: ~{detected_hz:.0f} Hz (overclocked)")
                elif detected_hz > 200:
                    print(f"  >> USB polling rate: ~{detected_hz:.0f} Hz (slightly above default)")
                else:
                    print(f"  >> USB polling rate: ~{detected_hz:.0f} Hz (default 125Hz = 8ms)")
                    print("     To reduce to ~1ms: see README 'Reducing Input Latency' section")
    print()

    if all_process_times:
        sorted_proc = sorted(all_process_times)
        print("  Python Processing Time (parse + normalize + scale):")
        print(f"    Min:    {format_ms(sorted_proc[0]):>8} ms")
        print(f"    Avg:    {format_ms(sum(sorted_proc) // len(sorted_proc)):>8} ms")
        print(f"    p99:    {format_ms(percentile(sorted_proc, 99)):>8} ms")
        print(f"    Max:    {format_ms(sorted_proc[-1]):>8} ms")
    print()

    if all_intervals:
        jitter_values = []
        for i in range(1, len(all_intervals)):
            jitter_values.append(abs(all_intervals[i] - all_intervals[i - 1]))
        if jitter_values:
            sorted_jitter = sorted(jitter_values)
            avg_jitter = sum(sorted_jitter) / len(sorted_jitter)
            print("  Jitter (variation between consecutive intervals):")
            print(f"    Avg:    {format_ms(avg_jitter):>8} ms")
            print(f"    p99:    {format_ms(percentile(sorted_jitter, 99)):>8} ms")
            print(f"    Max:    {format_ms(sorted_jitter[-1]):>8} ms")
            print()
            if avg_jitter / 1000 > 2.0:
                print("  >> WARNING: High jitter detected. Possible causes:")
                if "BLE" in mode_label:
                    print("     - BLE radio interference (Wi-Fi, other Bluetooth)")
                    print("     - Distance from adapter too far")
                    print("     - System under heavy load")
                else:
                    print("     - USB hub / extension cable adding latency")
                    print("     - System under heavy load (CPU/GPU)")
                    print("     - USB power management (disable USB selective suspend)")
            elif avg_jitter / 1000 > 0.5:
                print("  >> Jitter is moderate — acceptable for casual play")
            else:
                print("  >> Jitter is low — excellent for competitive play")

    print()
    print("=" * 72)

    if csv_path:
        print(f"\nDetailed data saved to: {csv_path}")
        print("Open in Excel/Google Sheets or plot with:")
        print(f"  python -c \"import pandas as pd; import matplotlib.pyplot as plt; "
              f"df=pd.read_csv('{csv_path}'); "
              f"df['interval_ms']=df.interval_us/1000; "
              f"df.interval_ms.plot(); plt.ylabel('ms'); plt.show()\"")


# ---------------------------------------------------------------------------
# USB benchmark
# ---------------------------------------------------------------------------

def run_benchmark(duration=None, csv_path=None):
    print("=" * 72)
    print("  NSO GameCube Controller — USB Latency Benchmark")
    print("=" * 72)
    print()
    print("Searching for controller (VID=057e PID=2073)...")

    device, info = find_controller()
    if not device:
        print("ERROR: No NSO GameCube controller found.")
        print("  - Make sure it's connected via USB")
        print("  - On Linux: check udev rules / run as root")
        sys.exit(1)

    product = info.get('product_string', 'Unknown')
    path = info.get('path', b'?')
    if isinstance(path, bytes):
        path = path.decode('utf-8', errors='replace')
    print(f"Found: {product}")
    print(f"  Path: {path}")
    print()

    csv_file = None
    if csv_path:
        csv_file = open(csv_path, 'w')
        csv_file.write("timestamp_us,interval_us,process_us,drain_count,buttons,lt,rt,lx,ly\n")
        print(f"CSV output: {csv_path}")

    print("Warming up (reading first reports)...")
    for _ in range(10):
        device.read(64, timeout_ms=100)

    print()
    if duration:
        print(f"Benchmarking for {duration} seconds... (press Ctrl+C to stop early)")
    else:
        print("Benchmarking... (press Ctrl+C to stop)")
    print()
    print("  Interval = time between USB reports (ideal: 8.00ms at 125Hz)")
    print("  Process  = time to parse + normalize + scale (Python overhead)")
    print()

    WINDOW = 500
    intervals_window = collections.deque(maxlen=WINDOW)
    process_times_window = collections.deque(maxlen=WINDOW)

    total_reports = 0
    total_dropped = 0
    start_time = time.perf_counter()
    last_read_time = None
    last_print_time = start_time
    print_interval = 1.0

    all_intervals = []
    all_process_times = []

    try:
        while True:
            now = time.perf_counter()
            if duration and (now - start_time) >= duration:
                break

            data = device.read(64, timeout_ms=16)
            t_read = time.perf_counter()

            if not data:
                continue

            drain_count = 0
            latest = data
            device.set_nonblocking(1)
            try:
                for _ in range(63):
                    more = device.read(64)
                    if more:
                        latest = more
                        drain_count += 1
                    else:
                        break
            finally:
                device.set_nonblocking(0)

            total_dropped += drain_count

            if IS_WINDOWS and len(latest) > 0 and latest[0] == 0x05:
                latest = translate_0x05(latest)
            elif IS_WINDOWS and len(latest) > 0:
                latest = latest[1:]

            t_process_start = time.perf_counter()
            lx, ly, rx, ry, lt, rt, buttons = simulate_processing(latest)
            t_process_end = time.perf_counter()

            interval_us = 0
            if last_read_time is not None:
                interval_us = int((t_read - last_read_time) * 1_000_000)
                intervals_window.append(interval_us)
                all_intervals.append(interval_us)
            last_read_time = t_read

            process_us = int((t_process_end - t_process_start) * 1_000_000)
            process_times_window.append(process_us)
            all_process_times.append(process_us)

            total_reports += 1

            if csv_file:
                ts = int((t_read - start_time) * 1_000_000)
                csv_file.write(f"{ts},{interval_us},{process_us},{drain_count},"
                               f"{buttons},{lt},{rt},{lx:.3f},{ly:.3f}\n")

            if (t_process_end - last_print_time) >= print_interval:
                last_print_time = t_process_end
                elapsed = t_process_end - start_time

                if intervals_window:
                    sorted_int = sorted(intervals_window)
                    int_min = sorted_int[0]
                    int_avg = sum(sorted_int) // len(sorted_int)
                    int_max = sorted_int[-1]
                    int_p50 = percentile(sorted_int, 50)
                    int_p99 = percentile(sorted_int, 99)
                    hz = 1_000_000 / int_avg if int_avg > 0 else 0
                else:
                    int_min = int_avg = int_max = 0
                    int_p50 = int_p99 = 0
                    hz = 0

                if process_times_window:
                    sorted_proc = sorted(process_times_window)
                    proc_avg = sum(sorted_proc) // len(sorted_proc)
                    proc_max = sorted_proc[-1]
                    proc_p99 = percentile(sorted_proc, 99)
                else:
                    proc_avg = proc_max = 0
                    proc_p99 = 0

                clear_line()
                sys.stdout.write(
                    f"  [{elapsed:6.1f}s] "
                    f"{total_reports:>6} reports | "
                    f"{hz:>6.1f} Hz | "
                    f"Interval: {format_ms(int_min)}/{format_ms(int_avg)}/"
                    f"{format_ms(int_max)} ms "
                    f"(p50={format_ms(int_p50)} p99={format_ms(int_p99)}) | "
                    f"Process: {format_ms(proc_avg)}/{format_ms(proc_max)} ms | "
                    f"Drops: {total_dropped}"
                )
                sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    finally:
        device.close()
        if csv_file:
            csv_file.close()

    elapsed = time.perf_counter() - start_time
    print_final_report(all_intervals, all_process_times, total_reports,
                       total_dropped, elapsed, csv_path, "USB HID")


# ---------------------------------------------------------------------------
# BLE benchmark
# ---------------------------------------------------------------------------

def run_ble_benchmark(duration=None, csv_path=None, address=None):
    """BLE latency benchmark using Bleak — measures GATT notification intervals."""
    import asyncio

    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("ERROR: bleak not installed.  pip install bleak")
        sys.exit(1)

    print("=" * 72)
    print("  NSO GameCube Controller — BLE Latency Benchmark")
    print("=" * 72)
    print()

    _HANDSHAKE_CMD = bytearray([
        0x02, 0x91, 0x01, 0x04,
        0x00, 0x08, 0x00, 0x00, 0x40, 0x7e, 0x00, 0x00, 0x00, 0x30, 0x01, 0x00
    ])
    _DEFAULT_REPORT_DATA = bytearray([
        0x03, 0x91, 0x00, 0x0d, 0x00, 0x08,
        0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
    ])
    _SET_INPUT_MODE = bytearray([
        0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x30
    ])

    NINTENDO_COMPANY_IDS = {0x037E, 0x0553}
    NINTENDO_NAMES = ('Pro Controller', 'Nintendo', 'Joy-Con', 'NSO', 'DeviceName')

    all_intervals = []
    all_process_times = []
    state = {
        'total_reports': 0,
        'last_notify_t': 0.0,
        'start_time': 0.0,
        'last_print_time': 0.0,
        'intervals_window': collections.deque(maxlen=500),
        'process_window': collections.deque(maxlen=500),
        'done': False,
    }

    csv_file = None
    if csv_path:
        csv_file = open(csv_path, 'w')
        csv_file.write("timestamp_us,interval_us,process_us,report_len\n")
        print(f"CSV output: {csv_path}")

    def on_notification(char, value: bytearray):
        if len(value) < 15:
            return
        t_now = time.perf_counter()

        t_proc_start = time.perf_counter()
        data = list(value)
        if len(data) >= 15:
            simulate_processing(data)
        t_proc_end = time.perf_counter()

        state['total_reports'] += 1
        process_us = int((t_proc_end - t_proc_start) * 1_000_000)
        state['process_window'].append(process_us)
        all_process_times.append(process_us)

        interval_us = 0
        if state['last_notify_t'] > 0:
            interval_us = int((t_now - state['last_notify_t']) * 1_000_000)
            state['intervals_window'].append(interval_us)
            all_intervals.append(interval_us)
        state['last_notify_t'] = t_now

        if csv_file:
            ts = int((t_now - state['start_time']) * 1_000_000)
            csv_file.write(f"{ts},{interval_us},{process_us},{len(value)}\n")

        if (t_proc_end - state['last_print_time']) >= 1.0:
            state['last_print_time'] = t_proc_end
            elapsed = t_proc_end - state['start_time']
            iw = state['intervals_window']
            pw = state['process_window']

            if iw:
                si = sorted(iw)
                i_min, i_avg, i_max = si[0], sum(si) // len(si), si[-1]
                i_p50 = percentile(si, 50)
                i_p99 = percentile(si, 99)
                hz = 1_000_000 / i_avg if i_avg > 0 else 0
            else:
                i_min = i_avg = i_max = 0
                i_p50 = i_p99 = 0
                hz = 0

            if pw:
                sp = sorted(pw)
                p_avg = sum(sp) // len(sp)
                p_max = sp[-1]
            else:
                p_avg = p_max = 0

            clear_line()
            sys.stdout.write(
                f"  [{elapsed:6.1f}s] "
                f"{state['total_reports']:>6} notifs | "
                f"{hz:>6.1f} Hz | "
                f"Interval: {format_ms(i_min)}/{format_ms(i_avg)}/"
                f"{format_ms(i_max)} ms "
                f"(p50={format_ms(i_p50)} p99={format_ms(i_p99)}) | "
                f"Process: {format_ms(p_avg)}/{format_ms(p_max)} ms"
            )
            sys.stdout.flush()

    async def _run():
        target = None

        if address:
            print(f"Connecting to {address}...")
            target = address
        else:
            print("Scanning for NSO GameCube controller (10s)...")
            devices = await BleakScanner.discover(timeout=10.0)
            for d in devices:
                name = d.name or ''
                md = getattr(d, 'metadata', {}) or {}
                mfr = md.get('manufacturer_data', {})
                is_nintendo = any(cid in mfr for cid in NINTENDO_COMPANY_IDS)
                is_named = any(n in name for n in NINTENDO_NAMES)
                if is_nintendo or is_named:
                    target = d.address
                    print(f"Found: {name or 'Unknown'} [{d.address}] RSSI={d.rssi}")
                    break

            if not target:
                print("ERROR: No Nintendo controller found via BLE.")
                print("  - Make sure the controller is in pairing mode")
                print("  - Check that Bluetooth is enabled")
                return

        client = BleakClient(target, timeout=15.0)

        try:
            await client.connect()
        except Exception as e:
            print(f"Connection failed: {e}")
            return

        if not client.is_connected:
            print("Connection failed (not connected)")
            return

        print(f"Connected to {target}")
        try:
            print(f"  MTU: {client.mtu_size}")
        except Exception:
            pass

        # Handshake + init (same sequence as bleak_backend)
        write_chars = []
        notify_chars = []
        for svc in client.services:
            for char in svc.characteristics:
                props = getattr(char, "properties", []) or []
                if "notify" in props or "indicate" in props:
                    notify_chars.append(char)
                if "write" in props or "write-without-response" in props:
                    write_chars.append(char)

        handshake_char = None
        for char in write_chars:
            try:
                await client.write_gatt_char(char.uuid, _HANDSHAKE_CMD)
                handshake_char = char
                break
            except Exception:
                try:
                    await client.write_gatt_char(char.uuid, bytearray([0x01, 0x01]))
                    handshake_char = char
                    break
                except Exception:
                    pass

        if not handshake_char:
            print("  Handshake failed — not a supported controller")
            await client.disconnect()
            return

        for char in notify_chars:
            try:
                await client.start_notify(char.uuid, on_notification)
            except Exception:
                pass

        # Find command channel (2nd WriteNoResp by handle)
        cmd_char = handshake_char
        for svc in client.services:
            wnr = sorted(
                [c for c in svc.characteristics
                 if "write-without-response" in (getattr(c, "properties", []) or [])],
                key=lambda c: c.handle)
            if len(wnr) >= 3:
                cmd_char = wnr[1]
                break

        for data in (_DEFAULT_REPORT_DATA,):
            try:
                await client.write_gatt_char(cmd_char, data, response=False)
            except Exception:
                pass

        try:
            await client.write_gatt_char(handshake_char.uuid, _SET_INPUT_MODE)
        except Exception:
            pass

        print("  Init complete — receiving notifications")
        print()
        if duration:
            print(f"Benchmarking for {duration} seconds... (press Ctrl+C to stop)")
        else:
            print("Benchmarking... (press Ctrl+C to stop)")
        print()
        print("  Interval = time between BLE GATT notifications")
        print("  Process  = time to parse + normalize (Python overhead)")
        print()

        state['start_time'] = time.perf_counter()
        state['last_print_time'] = state['start_time']

        try:
            if duration:
                await asyncio.sleep(duration)
            else:
                while True:
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file:
            csv_file.close()

    elapsed = time.perf_counter() - state['start_time'] if state['start_time'] else 0
    print_final_report(all_intervals, all_process_times, state['total_reports'],
                       0, elapsed, csv_path, "BLE (Bleak)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="NSO GameCube Controller — Latency Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python latency_benchmark.py                   # USB, run until Ctrl+C
  python latency_benchmark.py --duration 10     # USB, 10 seconds
  python latency_benchmark.py --ble             # BLE, auto-scan
  python latency_benchmark.py --ble --address XX:XX:XX:XX:XX:XX
  python latency_benchmark.py --csv report.csv  # save detailed data
        """)
    parser.add_argument('--duration', type=float, default=None,
                        help='benchmark duration in seconds (default: until Ctrl+C)')
    parser.add_argument('--csv', type=str, default=None,
                        help='save per-frame data to CSV file')
    parser.add_argument('--ble', action='store_true',
                        help='use BLE instead of USB (requires bleak)')
    parser.add_argument('--address', type=str, default=None,
                        help='BLE device address to connect to (skip scan)')
    args = parser.parse_args()

    if args.ble:
        run_ble_benchmark(duration=args.duration, csv_path=args.csv,
                          address=args.address)
    else:
        run_benchmark(duration=args.duration, csv_path=args.csv)


if __name__ == '__main__':
    main()
