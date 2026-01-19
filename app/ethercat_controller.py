#!/usr/bin/env python3
"""
EtherCAT Controller Module - CSP/PP Mode Support
Based on working simple_move_home_updated.py

Key CSP fixes:
1. When NOT moving: send back ACTUAL position to prevent drift (Err1B0)
2. When moving: smoothly interpolate towards target
3. sync_position_to_actual() must be called before enable and after fault reset
"""

import pysoem
import struct
import time
import threading
import json
import os


def load_config():
    """Load configuration from json/config.json"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'json', 'config.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        print(f"Loaded config from: {config_path}")
        return config
    except Exception as e:
        print(f"Config load error: {e}")
        return None


class EtherCATController:
    """EtherCAT controller with CSP and PP mode support"""
    
    # Modes
    MODE_PP = 1   # Profile Position
    MODE_PV = 3   # Profile Velocity
    MODE_CSP = 8  # Cyclic Synchronous Position
    
    # Control Words
    CONTROL_SHUTDOWN = 0x0006
    CONTROL_SWITCH_ON = 0x0007
    CONTROL_ENABLE_OP = 0x000F
    CONTROL_DISABLE = 0x0000
    CONTROL_FAULT_RESET = 0x0080
    CONTROL_NEW_SETPOINT = 0x0010   # Bit 4: New setpoint (rising edge triggers)
    CONTROL_CHANGE_SET_IMMEDIATELY = 0x0020  # Bit 5: Change set immediately (interrupt current motion)
    CONTROL_HALT = 0x0100           # Bit 8: Halt motion in PP mode
    CONTROL_QUICK_STOP = 0x0002     # Bit 2: Quick stop (active low - 0 = quick stop)
    
    # Status bits
    STATUS_FAULT = 0x0008
    STATUS_TARGET_REACHED = 0x0400
    STATUS_ERROR_CODE_OFFSET = 8  # Offset for error code in PDO

    # PDO offsets
    STATUS_OFFSET = 2
    POSITION_OFFSET = 4
    
    # CSP parameters
    CYCLE_TIME_US = 1000  # 1ms
    DEFAULT_CSP_VELOCITY = 100  # Default velocity limit for CSP (units/ms) - matches multi_motor_csp.py
    
    def __init__(self, interface=None):
        self.master = pysoem.Master()
        self.interface = interface
        self.slaves_count = 0
        self.connected = False
        
        # Load config
        self._config = load_config()
        if self._config:
            cfg = self._config.get('config', {})
            if not interface:
                self.interface = cfg.get('network_interface')
            mode_str = cfg.get('mode', 'CSP').upper()
            self.mode = self.MODE_CSP if mode_str == 'CSP' else self.MODE_PP
            self.ACTUAL_STEPS_PER_METER = cfg.get('actual_steps_per_meter', 792914)
            self.RAW_STEPS_PER_METER = cfg.get('raw_steps_per_meter', 202985985)
            
            spd = self._config.get('speed', {})
            self._velocity = spd.get('velocity', 80000)
            self._accel = spd.get('acceleration', 6000)
            self._decel = spd.get('deceleration', 6000)
        else:
            self.mode = self.MODE_CSP
            self._velocity = 80000
            self._accel = 6000
            self._decel = 6000
            self.ACTUAL_STEPS_PER_METER = 792914
            self.RAW_STEPS_PER_METER = 202985985
        
        self.SCALE_FACTOR = self.ACTUAL_STEPS_PER_METER / self.RAW_STEPS_PER_METER
        
        print(f"Mode: {'CSP' if self.mode == self.MODE_CSP else 'PP'}")

        # PDO thread
        self._pdo_thread = None
        self._pdo_running = False
        self._pdo_lock = threading.Lock()

        # Cached input data
        self._cached_input = {}

        # Output data per slave
        self._control_word = {}
        self._target_position = {}

        # CSP trajectory control per slave (each slave has its own lock like multi_motor_csp.py)
        self._trajectory_active = {}
        self._trajectory_target = {}
        self._trajectory_locks = {}  # Per-slave trajectory locks (like multi_motor_csp.py SlaveData)

        # Per-slave speed settings for CSP mode
        self._slave_csp_velocity = {}  # CSP velocity per slave

        # Per-slave PP mode speed settings
        self._slave_velocity = {}
        self._slave_accel = {}
        self._slave_decel = {}

        # Per-slave PV mode target velocity (for PDO-based velocity control)
        self._target_velocity = {}  # Target velocity per slave for PV mode

        # Slave connection monitoring
        self._last_slave_count = 0
        self._slaves_changed_callback = None
        self._communication_error_callback = None  # Callback for Er81b and communication errors
    
    def connect(self):
        """Initialize EtherCAT connection"""
        try:
            # Find interface
            if not self.interface or 'YOUR-ADAPTER' in str(self.interface):
                adapters = pysoem.find_adapters()
                print("\nAvailable adapters:")
                for i, a in enumerate(adapters):
                    print(f"  {i}: {a.name} - {a.desc}")
                if adapters:
                    self.interface = adapters[0].name
                else:
                    print("No adapters found!")
                    return False

            print(f"\nUsing interface: {self.interface}")

            # Open master
            self.master.open(self.interface)

            # Find slaves
            self.slaves_count = self.master.config_init(usetable=False)
            if self.slaves_count <= 0:
                print("No slaves found on this interface!")
                self.master.close()

                # Show available interfaces
                adapters = pysoem.find_adapters()
                if not adapters:
                    print("No network adapters found!")
                    return False

                print("\n" + "="*60)
                print("Available Network Interfaces:")
                print("="*60)
                for i, a in enumerate(adapters):
                    print(f"  [{i}] {a.desc}")
                    print(f"      Name: {a.name}")
                print("="*60)

                # Check if running interactively (has terminal access)
                import sys
                if not sys.stdin.isatty():
                    print("\nRunning in non-interactive mode.")
                    print("Please update 'network_interface' in json/config.json")
                    print("with one of the interface names listed above.")
                    return False

                # Interactive mode - let user choose
                while True:
                    try:
                        choice = input("\nEnter interface number (or 'q' to quit): ").strip()
                        if choice.lower() == 'q':
                            print("Exiting...")
                            return False

                        idx = int(choice)
                        if 0 <= idx < len(adapters):
                            self.interface = adapters[idx].name
                            print(f"\nSelected: {adapters[idx].desc}")
                            print(f"Trying interface: {self.interface}")

                            # Try connecting with selected interface
                            self.master = pysoem.Master()
                            self.master.open(self.interface)
                            self.slaves_count = self.master.config_init(usetable=False)

                            if self.slaves_count > 0:
                                print(f"Found {self.slaves_count} slave(s)!")
                                break
                            else:
                                print(f"No slaves found on {adapters[idx].desc}")
                                self.master.close()
                                print("\nTry another interface...")
                        else:
                            print(f"Invalid choice. Enter 0-{len(adapters)-1}")
                    except ValueError:
                        print("Please enter a valid number")
                    except EOFError:
                        print("\nNo interactive input available.")
                        print("Please update 'network_interface' in json/config.json")
                        return False
                    except Exception as e:
                        print(f"Error: {e}")
                        return False
            
            print(f"Found {self.slaves_count} slave(s)")
            
            # Configure each slave
            for i in range(self.slaves_count):
                slave = self.master.slaves[i]
                print(f"\nConfiguring slave {i}: {slave.name}")
                
                # Initialize state
                self._control_word[i] = 0
                self._target_position[i] = 0
                self._target_velocity[i] = 0  # For PV mode
                self._cached_input[i] = bytes()
                self._trajectory_active[i] = False
                self._trajectory_target[i] = 0
                self._trajectory_locks[i] = threading.Lock()  # Per-slave lock like multi_motor_csp.py

                # Initialize per-slave speed settings
                self._slave_csp_velocity[i] = self.DEFAULT_CSP_VELOCITY
                self._slave_velocity[i] = self._velocity
                self._slave_accel[i] = self._accel
                self._slave_decel[i] = self._decel
                
                # Set mode
                slave.sdo_write(0x6060, 0x00, bytes([self.mode]))
                print(f"  Mode: {'CSP' if self.mode == self.MODE_CSP else 'PP'} ({self.mode})")
                
                # CSP sync compensation parameters
                if self.mode == self.MODE_CSP:
                    try:
                        slave.sdo_write(0x2025, 0x00, (50).to_bytes(2, 'little'))
                        print(f"  Sync comp time 1: 50 (5.0us)")
                    except:
                        pass
                    try:
                        slave.sdo_write(0x2026, 0x00, (200).to_bytes(2, 'little'))
                        print(f"  Sync comp time 2: 200 (20.0us)")
                    except:
                        pass

                    # Try to increase watchdog timeout (Pr0.20 / 0x2020 on Panasonic)
                    # Er81b occurs when PDO is not received within watchdog time
                    try:
                        # 0x2020 = Pr0.20 (Communication timeout)
                        slave.sdo_write(0x2020, 0x00, (100).to_bytes(2, 'little'))
                        print(f"  Communication timeout (0x2020): 100")
                    except:
                        pass

                    # Try SM watchdog (0x10F1 - standard EtherCAT)
                    try:
                        slave.sdo_write(0x10F1, 0x01, (2500).to_bytes(2, 'little'))
                        slave.sdo_write(0x10F1, 0x02, (1000).to_bytes(2, 'little'))
                        print(f"  SM Watchdog configured: divider=2500, time=1000")
                    except:
                        pass

                # PP/PV mode watchdog - more relaxed since drive handles trajectory
                if self.mode in [self.MODE_PP, self.MODE_PV]:
                    try:
                        # Increase communication timeout for PP/PV modes
                        slave.sdo_write(0x2020, 0x00, (500).to_bytes(2, 'little'))  # 50ms or 500ms depending on drive
                        print(f"  PP/PV Communication timeout (0x2020): 500 (extended)")
                    except:
                        pass

                    # Try SM watchdog with longer timeout
                    try:
                        slave.sdo_write(0x10F1, 0x01, (2500).to_bytes(2, 'little'))  # Watchdog divider
                        slave.sdo_write(0x10F1, 0x02, (5000).to_bytes(2, 'little'))  # Watchdog time 500ms
                        print(f"  PP/PV SM Watchdog: divider=2500, time=5000 (extended)")
                    except:
                        pass

                # Velocity/acceleration
                slave.sdo_write(0x6081, 0x00, self._velocity.to_bytes(4, 'little', signed=False))
                slave.sdo_write(0x607F, 0x00, self._velocity.to_bytes(4, 'little', signed=False))
                slave.sdo_write(0x6083, 0x00, self._accel.to_bytes(4, 'little', signed=False))
                slave.sdo_write(0x6084, 0x00, self._decel.to_bytes(4, 'little', signed=False))
                print(f"  Velocity: {self._velocity}, Accel: {self._accel}")
            
            # Map PDO
            self.master.config_map()

            for i in range(self.slaves_count):
                slave = self.master.slaves[i]
                print(f"Slave {i}: Output={len(slave.output)}B, Input={len(slave.input)}B")

            # Give slaves time to stabilize before state transition
            print("\nWaiting for slaves to stabilize...")
            time.sleep(0.5)

            # Transition to SAFE_OP with retry
            print("Transitioning to SAFE_OP...")
            self.master.state = pysoem.SAFEOP_STATE
            self.master.write_state()

            # Check each slave's state and report errors
            for retry in range(3):
                try:
                    self.master.state_check(pysoem.SAFEOP_STATE, 50000)
                    break
                except Exception as e:
                    print(f"  SAFE_OP transition attempt {retry + 1} failed: {e}")
                    # Check individual slave states
                    for i in range(self.slaves_count):
                        slave = self.master.slaves[i]
                        state = slave.state
                        al_status = slave.al_status
                        if al_status != 0:
                            error_name = self.get_error_name(al_status)
                            print(f"  Slave {i} ({slave.name}): State={state}, AL Status=0x{al_status:04X} ({error_name})")
                    if retry < 2:
                        print(f"  Retrying in 1 second...")
                        time.sleep(1.0)
                        self.master.write_state()
                    else:
                        raise Exception(f"Failed to reach SAFE_OP state after 3 attempts")
            
            # Initial PDO exchanges
            for _ in range(10):
                self.master.send_processdata()
                self.master.receive_processdata(2000)
                time.sleep(0.001)
            
            # Transition to OPERATIONAL
            self.master.state = pysoem.OP_STATE
            self.master.write_state()
            for _ in range(50):
                self.master.send_processdata()
                self.master.receive_processdata(2000)
                time.sleep(0.001)
            self.master.state_check(pysoem.OP_STATE, 50000)
            
            print("\nAll slaves OPERATIONAL")
            self.connected = True
            
            # Start PDO thread
            self._start_pdo_thread()
            
            time.sleep(0.1)
            
            # CRITICAL for CSP: Sync positions to actual
            for i in range(self.slaves_count):
                self.sync_position_to_actual(i)
            
            return True
            
        except Exception as e:
            print(f"Connect error: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _start_pdo_thread(self):
        """Start continuous PDO thread with high priority"""
        self._pdo_running = True
        self._pdo_thread = threading.Thread(target=self._pdo_loop, daemon=True)
        self._pdo_thread.start()
        
        # Try to set thread priority (Linux)
        try:
            import os
            # Set nice value to -10 (higher priority, requires root)
            os.nice(-10)
        except:
            pass
        
        # Try to set real-time priority (Linux, requires root)
        try:
            import ctypes
            SCHED_RR = 2
            class sched_param(ctypes.Structure):
                _fields_ = [('sched_priority', ctypes.c_int)]
            
            libc = ctypes.CDLL('libc.so.6', use_errno=True)
            param = sched_param(50)  # Priority 50 (1-99)
            # This will likely fail without root, but worth trying
            # libc.sched_setscheduler(0, SCHED_RR, ctypes.byref(param))
        except:
            pass
        
        print("PDO thread started (high priority requested)")
    
    def set_slaves_changed_callback(self, callback):
        """Set callback for when slave count changes (disconnect/reconnect)"""
        self._slaves_changed_callback = callback

    def set_communication_error_callback(self, callback):
        """Set callback for communication errors (Er81b, WKC mismatch, timing critical)"""
        self._communication_error_callback = callback

    def _pdo_loop(self):
        """
        PDO loop - runs at 1ms cycle (EXACTLY like multi_motor_csp.py)

        CRITICAL for CSP smooth movement:
        - When trajectory_active=True: interpolate towards target, keep sending target when reached
        - When trajectory_active=False: track actual position to stay in sync

        IMPORTANT: This loop MUST run continuously to prevent watchdog timeout (Er81b)

        NOTE: On Windows, we use multiple techniques to ensure reliable timing in background:
        1. timeBeginPeriod(1) - Request 1ms system timer resolution
        2. SetThreadPriority - Elevate thread priority to TIME_CRITICAL
        3. SetProcessPriorityClass - Elevate process priority to HIGH
        4. Pure spin-wait for maximum timing precision (no sleep during critical timing)

        AUTO-RECOVERY: If communication errors (Er81b) are detected, this loop will
        signal for automatic recovery attempt.
        """
        consecutive_wkc_failures = 0
        winmm = None
        kernel32 = None
        use_perf_counter = False
        perf_freq = 0

        # Windows high-priority setup for background execution
        try:
            import ctypes

            # Get handles
            kernel32 = ctypes.windll.kernel32
            winmm = ctypes.windll.winmm

            # Set multimedia timer resolution to 1ms
            winmm.timeBeginPeriod(1)
            print("[PDO] Windows multimedia timer set to 1ms")

            # CRITICAL: Prevent Windows from throttling this thread
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            # This tells Windows "don't sleep, don't throttle, I'm doing real-time work"
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
            print("[PDO] Thread execution state set to prevent throttling")

            # Set thread priority to THREAD_PRIORITY_TIME_CRITICAL (15)
            # This is the highest priority level
            current_thread = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(current_thread, 15)  # THREAD_PRIORITY_TIME_CRITICAL
            print("[PDO] Thread priority elevated to TIME_CRITICAL (15)")

            # Pin PDO thread to CPU core 1 (different from main process on core 0)
            # This ensures the PDO thread has dedicated CPU time
            try:
                current_thread_handle = kernel32.GetCurrentThread()
                # SetThreadAffinityMask to core 1 (bit 1 = value 2)
                # If only one core, this will fail silently
                kernel32.SetThreadAffinityMask(current_thread_handle, 2)  # Core 1
                print("[PDO] Thread pinned to CPU core 1")
            except:
                pass

            # Setup performance counter for precise timing
            freq = ctypes.c_int64()
            kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
            perf_freq = freq.value
            use_perf_counter = True
            print(f"[PDO] Using high-resolution performance counter ({perf_freq} Hz)")

        except Exception as e:
            print(f"[PDO] Windows priority setup warning: {e}")

        # Mode-specific timing settings
        # CSP mode needs tight 1ms timing, PP/PV modes can be more relaxed
        if self.mode == self.MODE_CSP:
            cycle_time_sec = self.CYCLE_TIME_US / 1000000.0  # 1ms for CSP
            max_allowed_cycle_ms = 5.0  # Warn at 5ms
            watchdog_threshold_ms = 8.0  # Critical at 8ms
            print(f"[PDO] CSP mode: Using 1ms cycle time, strict timing")
        else:
            # PP and PV modes - drive handles trajectory, we just need status updates
            cycle_time_sec = 0.005  # 5ms cycle is fine for PP/PV
            max_allowed_cycle_ms = 50.0  # Very relaxed warning threshold - PP/PV handles timing internally
            watchdog_threshold_ms = 100.0  # Very relaxed critical threshold - only trigger on real issues
            print(f"[PDO] PP/PV mode: Using 5ms cycle time, very relaxed timing (50ms warn, 100ms critical)")

        late_cycle_count = 0
        last_late_warning = 0
        consecutive_late_cycles = 0  # Track consecutive late cycles for recovery trigger

        try:
            last_cycle_end = time.perf_counter()

            while self._pdo_running:
                # Get start time for this cycle using performance counter
                if use_perf_counter:
                    start_count = ctypes.c_int64()
                    kernel32.QueryPerformanceCounter(ctypes.byref(start_count))
                    cycle_start = start_count.value
                else:
                    cycle_start = time.perf_counter()

                # Check if we're running late (cycle took too long)
                cycle_gap_ms = (time.perf_counter() - last_cycle_end) * 1000
                if cycle_gap_ms > max_allowed_cycle_ms:
                    late_cycle_count += 1
                    consecutive_late_cycles += 1
                    now = time.time()

                    # Warning frequency depends on mode (less spam for PP/PV)
                    warn_interval = 10.0 if self.mode == self.MODE_CSP else 30.0

                    # Only warn periodically to avoid spam
                    if now - last_late_warning > warn_interval:
                        print(f"[PDO WARNING] Timing issue: {late_cycle_count} late cycles, last gap: {cycle_gap_ms:.1f}ms")
                        last_late_warning = now

                    # Critical: If gap is dangerously close to watchdog timeout
                    if cycle_gap_ms > watchdog_threshold_ms:
                        # Only print critical for CSP mode, PP/PV can tolerate more
                        if self.mode == self.MODE_CSP:
                            print(f"[PDO CRITICAL] Gap {cycle_gap_ms:.1f}ms approaching watchdog timeout!")

                    # Only trigger recovery after many consecutive late cycles
                    # PP/PV modes need much more tolerance since timing is less critical
                    recovery_threshold = 20 if self.mode == self.MODE_CSP else 200
                    if consecutive_late_cycles >= recovery_threshold and cycle_gap_ms > watchdog_threshold_ms and self._communication_error_callback:
                        print(f"[PDO CRITICAL] {consecutive_late_cycles} consecutive late cycles - triggering recovery!")
                        consecutive_late_cycles = 0
                        threading.Thread(
                            target=self._communication_error_callback,
                            args=(f"Timing critical: {cycle_gap_ms:.1f}ms gap",),
                            daemon=True
                        ).start()
                else:
                    consecutive_late_cycles = 0  # Reset on good cycle

                # === PDO Processing ===
                wkc = 0
                try:
                    with self._pdo_lock:
                        # Send/receive for all slaves
                        self.master.send_processdata()
                        wkc = self.master.receive_processdata(2000)

                        # Check working counter for slave disconnect detection
                        if wkc < self.slaves_count:
                            consecutive_wkc_failures += 1
                            # Use different thresholds for CSP vs PP/PV
                            # PP/PV modes are more tolerant of occasional WKC issues
                            wkc_error_threshold = 200 if self.mode == self.MODE_CSP else 500
                            wkc_disconnect_threshold = 1000 if self.mode == self.MODE_CSP else 2000

                            if consecutive_wkc_failures > wkc_error_threshold:
                                # Check if this is a communication error (Er81b)
                                if self._communication_error_callback and consecutive_wkc_failures == wkc_error_threshold + 1:
                                    print(f"[PDO ERROR] WKC mismatch: {wkc}/{self.slaves_count} - possible Er81b (after {wkc_error_threshold} failures)")
                                    threading.Thread(
                                        target=self._communication_error_callback,
                                        args=(f"WKC mismatch: {wkc}/{self.slaves_count}",),
                                        daemon=True
                                    ).start()

                                if consecutive_wkc_failures > wkc_disconnect_threshold and self._slaves_changed_callback:
                                    consecutive_wkc_failures = 0
                                    threading.Thread(
                                        target=self._slaves_changed_callback,
                                        args=(wkc, self.slaves_count),
                                        daemon=True
                                    ).start()
                        else:
                            consecutive_wkc_failures = 0

                        # Process each slave
                        for i in range(self.slaves_count):
                            slave = self.master.slaves[i]
                            out_len = len(slave.output)

                            # Cache input
                            self._cached_input[i] = bytes(slave.input)

                            # Mode-specific position handling
                            if self.mode == self.MODE_CSP:
                                # CSP mode: Master generates trajectory (like multi_motor_csp.py)
                                actual_pos_scaled = int(self._get_pos_scaled_internal(i))

                                # Smooth position update towards target OR track actual
                                with self._trajectory_locks[i]:
                                    if self._trajectory_active.get(i, False):
                                        position_error = self._trajectory_target.get(i, 0) - self._target_position.get(i, 0)

                                        if abs(position_error) > self.DEFAULT_CSP_VELOCITY:
                                            step = self.DEFAULT_CSP_VELOCITY if position_error > 0 else -self.DEFAULT_CSP_VELOCITY
                                            self._target_position[i] = self._target_position.get(i, 0) + step
                                        else:
                                            self._target_position[i] = self._trajectory_target.get(i, 0)
                                    else:
                                        # No trajectory - track actual to stay in sync
                                        self._target_position[i] = actual_pos_scaled
                            # else: PP mode - target position is set by move_to_position()
                            #       Drive generates trajectory, we just send target position as-is

                            # Pack output data (same for all modes - PDO uses target position)
                            # Note: PV mode velocity is set via SDO (0x60FF), not PDO
                            out_data = bytearray(out_len)
                            struct.pack_into('<H', out_data, 0, self._control_word.get(i, 0))
                            struct.pack_into('<i', out_data, 2, self._target_position.get(i, 0))
                            if out_len >= 7:
                                out_data[6] = self.mode  # Mode byte = actual mode (1 for PP, 3 for PV, 8 for CSP)
                            slave.output = bytes(out_data)

                except Exception as e:
                    # Communication error during PDO exchange
                    print(f"[PDO EXCEPTION] {e}")
                    if self._communication_error_callback:
                        threading.Thread(
                            target=self._communication_error_callback,
                            args=(str(e),),
                            daemon=True
                        ).start()

                # === Precise timing wait ===
                # Use hybrid approach: short sleep + spin-wait for final precision
                # The short sleep (0.5ms) helps reduce CPU usage while spin-wait ensures precision
                if use_perf_counter:
                    target_count = cycle_start + int(perf_freq * cycle_time_sec)
                    # Calculate when to stop sleeping and start spinning (0.3ms before target)
                    spin_start = target_count - int(perf_freq * 0.0003)

                    while True:
                        now_count = ctypes.c_int64()
                        kernel32.QueryPerformanceCounter(ctypes.byref(now_count))
                        if now_count.value >= target_count:
                            break
                        # Sleep in small chunks until we're close to target
                        if now_count.value < spin_start:
                            # Very short sleep - 0.1ms chunks
                            # Windows Sleep(0) yields to other threads but returns quickly
                            kernel32.Sleep(0)  # Yield without giving up too much time
                        # else: pure spin for final 0.3ms
                else:
                    # Fallback: simple sleep (less precise)
                    elapsed = time.perf_counter() - cycle_start
                    remaining = cycle_time_sec - elapsed
                    if remaining > 0:
                        time.sleep(remaining)

                # Track cycle end time for late detection
                last_cycle_end = time.perf_counter()

        finally:
            # Restore Windows timer settings (but not process priority - that's managed at process level)
            try:
                if winmm:
                    winmm.timeEndPeriod(1)
                    print("[PDO] Windows timer resolution restored")
            except:
                pass
    
    def _get_pos_scaled_internal(self, idx):
        """Get scaled position from cached input (internal use)"""
        inp = self._cached_input.get(idx, bytes())
        if len(inp) >= self.POSITION_OFFSET + 4:
            raw = struct.unpack_from('<i', inp, self.POSITION_OFFSET)[0]
            return int(raw * self.SCALE_FACTOR)
        return 0
    
    def sync_position_to_actual(self, idx):
        """
        CRITICAL for CSP: Sync output target to actual drive position
        Must be called before enabling or after fault clear
        (Matches multi_motor_csp.py sync_position_to_actual)
        """
        pos_scaled = self._get_pos_scaled_internal(idx)

        with self._pdo_lock:
            self._target_position[idx] = pos_scaled

        with self._trajectory_locks[idx]:
            self._trajectory_target[idx] = pos_scaled
            self._trajectory_active[idx] = False

        print(f"  Slave {idx} position synced to: {pos_scaled}")
        return pos_scaled
    
    # ========== Read functions ==========
    
    def read_status(self, idx):
        """Read status word"""
        if idx >= self.slaves_count:
            return 0
        inp = self._cached_input.get(idx, bytes())
        if len(inp) >= self.STATUS_OFFSET + 2:
            return struct.unpack_from('<H', inp, self.STATUS_OFFSET)[0]
        return 0
    
    def read_position(self, idx):
        """Read actual position (scaled)"""
        return self._get_pos_scaled_internal(idx)
    
    def read_position_raw(self, idx):
        """Read actual position (raw)"""
        inp = self._cached_input.get(idx, bytes())
        if len(inp) >= self.POSITION_OFFSET + 4:
            return struct.unpack_from('<i', inp, self.POSITION_OFFSET)[0]
        return 0
    
    def read_position_meters(self, idx):
        """Read position in meters"""
        return self._get_pos_scaled_internal(idx) / self.ACTUAL_STEPS_PER_METER
    
    def is_enabled(self, idx):
        """Check if drive is enabled"""
        return (self.read_status(idx) & 0x006F) == 0x0027
    
    # def has_fault(self, idx):
    #     """Check if drive has fault"""
    #     return bool(self.read_status(idx) & self.STATUS_FAULT)
    
    def is_target_reached(self, idx):
        """Check if target reached"""
        return bool(self.read_status(idx) & self.STATUS_TARGET_REACHED)
    
    # ========== Control functions ==========
    
    def reset_fault(self, idx):
        """Reset fault"""
        print(f"Resetting fault on slave {idx}...")
        with self._pdo_lock:
            self._control_word[idx] = self.CONTROL_FAULT_RESET
        time.sleep(0.05)
        with self._pdo_lock:
            self._control_word[idx] = 0
        time.sleep(0.05)
        
        # CRITICAL for CSP: Sync position after fault reset
        self.sync_position_to_actual(idx)
    
    def enable_drive(self, idx):
        """Enable drive"""
        print(f"Enabling slave {idx}...")
        
        status = self.read_status(idx)
        print(f"  Initial: 0x{status:04X}")
        
        # Check fault
        if status & self.STATUS_FAULT:
            print(f"  Fault detected, resetting...")
            self.reset_fault(idx)
            time.sleep(0.1)
        
        # CRITICAL for CSP: Sync position before enable
        self.sync_position_to_actual(idx)
        
        # State machine with retries
        max_retries = 3
        for retry in range(max_retries):
            # Shutdown
            with self._pdo_lock:
                self._control_word[idx] = self.CONTROL_SHUTDOWN
            time.sleep(0.02)
            
            # Switch On
            with self._pdo_lock:
                self._control_word[idx] = self.CONTROL_SWITCH_ON
            time.sleep(0.02)
            
            # Enable Operation
            with self._pdo_lock:
                self._control_word[idx] = self.CONTROL_ENABLE_OP
            time.sleep(0.02)
            
            status = self.read_status(idx)
            print(f"  Status: 0x{status:04X}")
            
            if (status & 0x006F) == 0x0027:
                print(f"  Slave {idx} ENABLED!")
                return True
            
            if status & self.STATUS_FAULT:
                print(f"  Fault during enable, resetting...")
                self.reset_fault(idx)
                time.sleep(0.05)
            
            print(f"  Retry {retry+1}/{max_retries}")
        
        print(f"  Slave {idx} enable FAILED!")
        return False
    
    def disable_drive(self, idx):
        """Disable drive"""
        print(f"Disabling slave {idx}...")

        # Stop any trajectory (use per-slave lock)
        with self._trajectory_locks[idx]:
            self._trajectory_active[idx] = False

        with self._pdo_lock:
            self._control_word[idx] = self.CONTROL_DISABLE
        time.sleep(0.02)
    
    def read_error_code(self, idx):
        """Read error code from drive (SDO 0x603F - same as physical display)"""
        if idx >= self.slaves_count:
            return 0
        
        try:
            slave = self.master.slaves[idx]
            # Read from SDO 0x603F (Error code register)
            error_data = slave.sdo_read(0x603F, 0x00)
            error_code = int.from_bytes(error_data, byteorder='little', signed=False)
            return error_code
        except Exception as e:
            # Fallback: try to read from PDO if SDO fails
            inp = self._cached_input.get(idx, bytes())
            if len(inp) >= 8 + 2:  # Assuming error code at offset 8
                return struct.unpack_from('<H', inp, 8)[0]
            return 0

    def get_error_name(self, error_code):
        """Convert 0x603F error code to drive display format (Er### as shown on Leadshine EL7-EC)

        Leadshine EL7-EC drives show errors as Er### on their display.
        The 0x603F SDO returns IEC 61800 codes which map to display codes.
        
        Based on Table 8.2 from EL7-EC_Series_AC_Servo_Drive_User_Manual_V1_00.pdf
        """
        if error_code == 0:
            return None

        # Leadshine EL7-EC error code mapping (0x603F → display code)
        # From Table 8.2 "Alarm and 603F correspondence"
        error_map = {
            # Current/Power errors (0x0A - 0x0E)
            0x3150: 'Er0A0',   # Phase A circuit current detection error
            0x3151: 'Er0A1',   # Phase B circuit current detection error
            0x3153: 'Er0A3',   # Motor power cable not connected
            0x3206: 'Er0b1',   # Control power supply voltage too high
            0x3211: 'Er0C0',   # DC bus overvoltage
            0x3221: 'Er0d0',   # DC bus undervoltage
            0x3130: 'Er0d1',   # Single phasing of main power supply
            0x3222: 'Er0d2',   # No main power supply detected
            0x2211: 'Er0E0',   # Overcurrent
            0x2212: 'Er0E1',   # IPM overcurrent
            0x2218: 'Er0E2',   # Power output to motor shorted to ground
            0x2230: 'Er0E4',   # Phase overcurrent
            
            # Thermal/Load errors (0x0F - 0x12)
            0x4210: 'Er0f0',   # Driver overheated
            0x8311: 'Er100',   # Motor overloaded
            0x8310: 'Er101',   # Driver overloaded
            0x8301: 'Er102',   # Motor rotor blocked
            0x7701: 'Er120',   # Regenerative resistor overvoltage
            0x7702: 'Er121',   # Holding brake error
            0x7703: 'Er122',   # Regenerative resistor value too low
            
            # Encoder errors (0x15 - 0x17)
            0x7321: 'Er150',   # Encoder disconnected
            0x7322: 'Er151',   # Encoder communication error
            0x7323: 'Er152',   # Encoder initial position error
            0x7325: 'Er153',   # Multiturn encoder error (IMPORTANT: requires Pr0.15 to clear)
            0x7326: 'Er155',   # Encoder data overflow
            0x7327: 'Er156',   # Encoder overheated
            0x7328: 'Er157',   # Encoder count error
            0x7324: 'Er170',   # Encoder data error
            
            # Motion errors (0x18 - 0x1C)
            0x8611: 'Er180',   # Excessive position deviation
            0x8401: 'Er190',   # Motor vibration too strong
            0x8402: 'Er1A0',   # Overspeed
            0x8403: 'Er1A1',   # Velocity out of control
            0x8612: 'Er1b0',   # Bus input signal dithering
            0x8503: 'Er1b1',   # Incorrect electronic gear ratio
            0x8313: 'Er1c0',   # STO failed
            
            # I/O errors (0x21)
            0x6321: 'Er210',   # I/O input interface assignment error
            0x6322: 'Er211',   # I/O input interface function assignment error
            0x6323: 'Er212',   # I/O output interface function assignment error
            
            # EEPROM errors (0x24)
            0x5530: 'Er240',   # EEPROM parameters initialization error
            0x5531: 'Er241',   # EEPROM hardware error
            0x5532: 'Er242',   # Error saving alarm history record
            0x5533: 'Er243',   # Error saving vendor parameters
            0x5534: 'Er244',   # Error saving communication parameters
            0x5535: 'Er245',   # Error saving parameter 402
            0x5536: 'Er246',   # Data saving error during power-off
            
            # Position/Other errors (0x26 - 0x28)
            0x7329: 'Er260',   # Positive/Negative limit triggered
            0x7201: 'Er280',   # Output pulse frequency too high
            
            # Special errors (0x5 - 0x7)
            0x5441: 'Er570',   # Forced alarm input valid (Quick Stop)
            0x7122: 'Er5f0',   # Motor model detection error
            0x1100: 'Er5f1',   # Driver power module detection error
            0x6204: 'Er600',   # Main/velocity loop interrupted timeout
            0x7001: 'Er700',   # Encryption error
            
            # EtherCAT sync errors (0x73)
            0x873A: 'Er73A',   # SyncManager2 lost
            0x873B: 'Er73b',   # SYNC0 lost
            0x873C: 'Er73c',   # Excessive Distributed Clock error
            
            # EtherCAT communication errors (0x80 - 0x8)
            0x8201: 'Er801',   # Unknown communication error
            0x5510: 'Er802',   # Memory overflow
            0x5511: 'Er803',   # RAM out of bound
            0x6202: 'Er805',   # FOE firmware upgrade failed
            0x6201: 'Er806',   # ESI file mismatch
            0xA001: 'Er811',   # Invalid EtherCAT transition request
            0xA002: 'Er812',   # Unknown EtherCAT transition request
            0x8213: 'Er813',   # Protection request from boot state
            0x6203: 'Er814',   # Invalid boot configuration
            0x8215: 'Er815',   # Invalid mailbox configuration (boot)
            0x8216: 'Er816',   # Invalid mailbox configuration (pre-op)
            0x8217: 'Er817',   # Invalid SyncManager configuration
            0x8211: 'Er818',   # No valid input data
            0x8212: 'Er819',   # No valid output data
            0xFF02: 'Er81A',   # Synchronization error
            0x821B: 'Er81b',   # SyncManager2 watchdog timeout
            0x821C: 'Er81C',   # Invalid SyncManager type
            0x821D: 'Er81d',   # Invalid output configuration
            0x821E: 'Er81E',   # Invalid input configuration
            0x8224: 'Er824',   # Invalid input mapping
            0x8225: 'Er825',   # Invalid output mapping
            0x8728: 'Er828',   # Sync mode not supported
            0x872C: 'Er82c',   # Fatal sync error (DC watchdog)
            0x872D: 'Er82d',   # No sync error
            0x872E: 'Er82E',   # Sync cycle too short
            0x8730: 'Er830',   # Invalid DC sync settings
            0x8732: 'Er832',   # DC PLL failure
            0x8735: 'Er835',   # DC cycle time invalid
            0x8736: 'Er836',   # Invalid DC sync cycle time
            0x5550: 'Er850',   # EEPROM inaccessible
            0x5551: 'Er851',   # EEPROM error
            0x5552: 'Er852',   # Hardware not ready
        }

        # Return mapped display code or generate from hex
        if error_code in error_map:
            return error_map[error_code]
        else:
            # Generate Er format for unknown codes
            return f"Er{error_code:03X}"

    def read_error_code_sdo(self, idx):
        """
        Read error code directly from SDO 0x603F
        This matches what's displayed on the physical servo driver
        
        Common Panasonic servo error codes:
        - Er102 (0x066): Overspeed
        - Er18B (0x18B): Position deviation excess
        - Er16 (0x010): Overload
        - Er13 (0x00D): Main circuit overvoltage
        """
        if idx >= self.slaves_count:
            return 0
        
        try:
            slave = self.master.slaves[idx]
            error_data = slave.sdo_read(0x603F, 0x00)
            error_code = int.from_bytes(error_data, byteorder='little', signed=False)
            
            if error_code != 0:
                print(f"  [Slave {idx}] Error Code: 0x{error_code:04X} ({error_code}) = {self.get_error_name(error_code)}")
            
            return error_code
        except Exception as e:
            print(f"  [Slave {idx}] Failed to read error code: {e}")
            return 0

    def has_fault(self, idx):
        """Check if drive has fault (uses cached PDO status - fast, no SDO)"""
        status = self.read_status(idx)
        return bool(status & self.STATUS_FAULT)


    def stop_motion(self, idx):
        """Stop motion IMMEDIATELY - NO deceleration, stays enabled, handles all modes"""
        print(f"  IMMEDIATE STOP slave {idx}...")

        # Stop trajectory FIRST (use per-slave lock) - for CSP mode
        with self._trajectory_locks[idx]:
            self._trajectory_active[idx] = False

        # Sync target position to actual position IMMEDIATELY
        actual_pos = self._get_pos_scaled_internal(idx)
        with self._pdo_lock:
            self._target_position[idx] = actual_pos

        # Mode-specific immediate stop
        if self.mode == self.MODE_PV:
            # PV mode: Set velocity to 0 immediately
            self.set_target_velocity(idx, 0)
        elif self.mode == self.MODE_PP:
            # PP mode: Set MAXIMUM deceleration for instant stop, then use HALT
            try:
                slave = self.master.slaves[idx]
                # Set deceleration to maximum (0x7FFFFFFF = max int32)
                slave.sdo_write(0x6084, 0x00, (0x7FFFFFFF).to_bytes(4, 'little', signed=False))
            except:
                pass

            # Use HALT bit to stop
            with self._pdo_lock:
                self._control_word[idx] = self.CONTROL_ENABLE_OP | self.CONTROL_HALT

            time.sleep(0.002)

            # Update target to current position
            actual_pos = self._get_pos_scaled_internal(idx)
            with self._pdo_lock:
                self._target_position[idx] = actual_pos
                # Clear HALT and send new setpoint at current position
                self._control_word[idx] = self.CONTROL_ENABLE_OP | self.CONTROL_NEW_SETPOINT

            time.sleep(0.002)
            with self._pdo_lock:
                self._control_word[idx] = self.CONTROL_ENABLE_OP

            # Restore original deceleration
            try:
                slave.sdo_write(0x6084, 0x00, self._decel.to_bytes(4, 'little', signed=False))
            except:
                pass
        # CSP mode: target already synced above, PDO loop will hold position

        print(f"  Slave {idx} STOPPED at position: {actual_pos}")

    def stop_all(self):
        """Stop all slaves IMMEDIATELY - NO deceleration, stays enabled"""
        print("\n[STOP ALL] IMMEDIATE STOP all motion...")

        # Stop all trajectories first (use per-slave locks) - for CSP mode
        for i in range(self.slaves_count):
            with self._trajectory_locks[i]:
                self._trajectory_active[i] = False

        # Sync all positions to actual IMMEDIATELY
        for i in range(self.slaves_count):
            actual_pos = self._get_pos_scaled_internal(i)
            with self._pdo_lock:
                self._target_position[i] = actual_pos

        # Mode-specific immediate stop
        if self.mode == self.MODE_PV:
            # PV mode: Set velocity to 0 for all slaves via SDO
            for i in range(self.slaves_count):
                self._target_velocity[i] = 0
                try:
                    slave = self.master.slaves[i]
                    slave.sdo_write(0x60FF, 0x00, (0).to_bytes(4, 'little', signed=True))
                except:
                    pass
        elif self.mode == self.MODE_PP:
            # PP mode: Set MAXIMUM deceleration for instant stop on all slaves
            for i in range(self.slaves_count):
                try:
                    slave = self.master.slaves[i]
                    slave.sdo_write(0x6084, 0x00, (0x7FFFFFFF).to_bytes(4, 'little', signed=False))
                except:
                    pass

            # Use HALT bit for all slaves
            with self._pdo_lock:
                for i in range(self.slaves_count):
                    self._control_word[i] = self.CONTROL_ENABLE_OP | self.CONTROL_HALT

            time.sleep(0.002)

            # Update all targets to current positions
            for i in range(self.slaves_count):
                actual_pos = self._get_pos_scaled_internal(i)
                with self._pdo_lock:
                    self._target_position[i] = actual_pos

            # Clear HALT and send new setpoints at current positions
            with self._pdo_lock:
                for i in range(self.slaves_count):
                    self._control_word[i] = self.CONTROL_ENABLE_OP | self.CONTROL_NEW_SETPOINT

            time.sleep(0.002)
            with self._pdo_lock:
                for i in range(self.slaves_count):
                    self._control_word[i] = self.CONTROL_ENABLE_OP

            # Restore original deceleration for all slaves
            for i in range(self.slaves_count):
                try:
                    slave = self.master.slaves[i]
                    slave.sdo_write(0x6084, 0x00, self._decel.to_bytes(4, 'little', signed=False))
                except:
                    pass
        # CSP mode: targets already synced above, PDO loop will hold positions

        print("[STOP ALL] All slaves STOPPED immediately (still enabled)")

    def move_to_position(self, idx, position_scaled, trigger_immediate=True):
        """
        Move to position (scaled units)
        PP mode: use new setpoint (drive handles trajectory)
        CSP mode: set trajectory target (master handles trajectory)

        Args:
            idx: Slave index
            position_scaled: Target position in scaled units
            trigger_immediate: If True (default), trigger motion immediately.
                               If False, just set target without triggering (for batch moves)
        """
        target = int(position_scaled)
        current = self._get_pos_scaled_internal(idx)
        distance = abs(target - current)

        # Get per-slave velocity settings
        slave_velocity = self._slave_velocity.get(idx, self._velocity)
        slave_csp_velocity = self._slave_csp_velocity.get(idx, self.DEFAULT_CSP_VELOCITY)

        if self.mode == self.MODE_PP:
            # PP mode - drive generates trajectory
            slave_accel = self._slave_accel.get(idx, self._accel)
            slave_decel = self._slave_decel.get(idx, self._decel)

            print(f"\n  [PP Mode] Slave {idx} move command:")
            print(f"        Current: {current}")
            print(f"        Target: {target}")
            print(f"        Distance: {distance}")
            print(f"        Velocity: {slave_velocity} units/s, Accel: {slave_accel}, Decel: {slave_decel}")

            with self._pdo_lock:
                self._target_position[idx] = target
                if trigger_immediate:
                    # Set NEW_SETPOINT + CHANGE_SET_IMMEDIATELY bits
                    # CHANGE_SET_IMMEDIATELY allows smooth transition to new target
                    self._control_word[idx] = self.CONTROL_ENABLE_OP | self.CONTROL_NEW_SETPOINT | self.CONTROL_CHANGE_SET_IMMEDIATELY
                    print(f"        Control word set: 0x{self._control_word[idx]:04X}")

            if trigger_immediate:
                # Wait for PDO cycle to send the command (PP mode runs at 5ms cycle)
                # Need at least 2 PDO cycles for reliable transmission
                time.sleep(0.015)
                # Clear NEW_SETPOINT bit - drive should have latched the target
                with self._pdo_lock:
                    self._control_word[idx] = self.CONTROL_ENABLE_OP
                    print(f"        Control word cleared: 0x{self._control_word[idx]:04X}")
        else:
            # CSP mode - master generates trajectory
            estimated_time = distance / (slave_csp_velocity * 1000) if slave_csp_velocity > 0 else 0

            print(f"\n  [CSP Mode] Slave {idx} move command:")
            print(f"        Current: {current}")
            print(f"        Target: {target}")
            print(f"        Distance: {distance}")
            print(f"        CSP velocity: {slave_csp_velocity} units/ms ({slave_csp_velocity * 1000} units/s)")
            print(f"        Estimated time: {estimated_time:.2f}s")

            with self._trajectory_locks[idx]:
                self._trajectory_target[idx] = target
                if trigger_immediate:
                    self._trajectory_active[idx] = True

    def trigger_move(self, slave_indices):
        """
        Trigger simultaneous movement for multiple slaves.
        Call this after setting targets with move_to_position(..., trigger_immediate=False)
        """
        if self.mode == self.MODE_PP:
            # PP mode - trigger new setpoint for all slaves at once
            # Set NEW_SETPOINT + CHANGE_SET_IMMEDIATELY bits for smooth motion
            with self._pdo_lock:
                for idx in slave_indices:
                    if idx < self.slaves_count:
                        self._control_word[idx] = self.CONTROL_ENABLE_OP | self.CONTROL_NEW_SETPOINT | self.CONTROL_CHANGE_SET_IMMEDIATELY

            # Wait for PDO cycle to send the command (PP mode runs at 5ms cycle)
            # Need at least 2-3 PDO cycles for reliable transmission
            time.sleep(0.015)

            # Clear NEW_SETPOINT bit but keep enabled
            with self._pdo_lock:
                for idx in slave_indices:
                    if idx < self.slaves_count:
                        self._control_word[idx] = self.CONTROL_ENABLE_OP
        else:
            # CSP mode - activate all trajectories at once (use per-slave locks)
            for idx in slave_indices:
                if idx < self.slaves_count:
                    with self._trajectory_locks[idx]:
                        self._trajectory_active[idx] = True

        print(f"  [TRIGGER] Started simultaneous move for slaves: {slave_indices}")

    def calculate_move_order(self, slave_positions, return_details=False):
        """
        Calculate safe move order for multiple slaves to avoid collisions.
        Uses topological sort based on current/target positions and movement directions.

        Based on the algorithm from semicon_slider.c:
        1. Determine if motors are spreading or converging (variance comparison)
        2. Detect potential collision paths between motors
        3. Create dependency graph with edges
        4. Use topological sort to find safe move order

        Args:
            slave_positions: List of tuples [(slave_idx, target_meters), ...]
            return_details: If True, return detailed log info for UI display

        Returns:
            tuple: (ordered_list, is_spreading) or (ordered_list, is_spreading, details) if return_details=True
                - ordered_list: List of slave indices in safe move order
                - is_spreading: True if motors are spreading apart, False if converging
                - details: Dict with calculation details (if return_details=True)
        """
        details = {'log': []}

        if not slave_positions or len(slave_positions) <= 1:
            result = ([sp[0] for sp in slave_positions], False)
            if return_details:
                details['log'].append("Single or no slaves - no ordering needed")
                return result + (details,)
            return result

        num_motors = len(slave_positions)

        # Get current and target positions in meters
        current_meters = []
        target_meters = []
        slave_indices = []

        details['log'].append(f"=== Move Order Calculation for {num_motors} slaves ===")

        for slave_idx, target in slave_positions:
            current = self.read_position_meters(slave_idx)
            current_meters.append(current)
            target_meters.append(target)
            slave_indices.append(slave_idx)
            details['log'].append(f"Slave {slave_idx+1}: current={current:.4f}m -> target={target:.4f}m")

        # Calculate mean positions
        current_mean = sum(current_meters) / num_motors
        target_mean = sum(target_meters) / num_motors

        # Calculate variance to determine spreading vs converging
        current_var = sum((c - current_mean) ** 2 for c in current_meters) / num_motors
        target_var = sum((t - target_mean) ** 2 for t in target_meters) / num_motors

        is_spreading = target_var > current_var + 1e-6

        details['log'].append(f"")
        details['log'].append(f"Current mean: {current_mean:.4f}m, variance: {current_var:.6f}")
        details['log'].append(f"Target mean: {target_mean:.4f}m, variance: {target_var:.6f}")
        details['log'].append(f"Movement type: {'SPREADING (apart)' if is_spreading else 'CONVERGING (together)'}")

        # Determine direction for each motor: 'L' (left/negative), 'R' (right/positive), '0' (no move)
        directions = []
        details['log'].append(f"")
        details['log'].append("Direction analysis:")
        for i in range(num_motors):
            delta = target_meters[i] - current_meters[i]
            if delta < -0.0001:  # Moving left (negative direction)
                directions.append('L')
                details['log'].append(f"  Slave {slave_indices[i]+1}: LEFT (delta={delta:.4f}m)")
            elif delta > 0.0001:  # Moving right (positive direction)
                directions.append('R')
                details['log'].append(f"  Slave {slave_indices[i]+1}: RIGHT (delta={delta:.4f}m)")
            else:
                directions.append('0')
                details['log'].append(f"  Slave {slave_indices[i]+1}: STATIONARY")

        # Calculate distance from target mean for each motor
        dist_from_mean = [abs(target_meters[i] - target_mean) for i in range(num_motors)]

        # Build dependency graph using adjacency list
        successors = [[] for _ in range(num_motors)]
        indegree = [0] * num_motors
        dependencies = []

        for i in range(num_motors):
            for j in range(num_motors):
                if i == j or directions[i] == '0' or directions[j] == '0':
                    continue

                # Check if motors are at nearly the same position
                if abs(current_meters[i] - current_meters[j]) < 0.001:
                    dist_i = dist_from_mean[i]
                    dist_j = dist_from_mean[j]

                    if is_spreading:
                        # Spreading: prioritize motor going farther from center
                        if dist_i > dist_j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (farther from center)")
                        elif dist_j > dist_i:
                            successors[j].append(i)
                            indegree[i] += 1
                            dependencies.append(f"Slave {slave_indices[j]+1} before {slave_indices[i]+1} (farther from center)")
                        # Equal distances: higher index for 'R', lower for 'L'
                        elif directions[i] == 'R' and directions[j] == 'R' and i > j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (higher index, both R)")
                        elif directions[i] == 'L' and directions[j] == 'L' and i < j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (lower index, both L)")
                    else:
                        # Converging: prioritize motor going closer to center
                        if dist_i < dist_j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (closer to center)")
                        elif dist_j < dist_i:
                            successors[j].append(i)
                            indegree[i] += 1
                            dependencies.append(f"Slave {slave_indices[j]+1} before {slave_indices[i]+1} (closer to center)")
                        # Equal distances: use index priority
                        elif directions[i] == 'R' and directions[j] == 'R' and i > j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (higher index)")
                        elif directions[i] == 'L' and directions[j] == 'L' and i < j:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (lower index)")
                else:
                    # Check for potential collision based on movement paths
                    # Motor i moving right, j is in its path to target
                    if (directions[i] == 'R' and
                        current_meters[i] < current_meters[j] and
                        target_meters[i] >= current_meters[j]):
                        if is_spreading:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (collision path R)")
                        else:
                            successors[j].append(i)
                            indegree[i] += 1
                            dependencies.append(f"Slave {slave_indices[j]+1} before {slave_indices[i]+1} (collision path R)")
                    # Motor i moving left, j is in its path to target
                    elif (directions[i] == 'L' and
                          current_meters[i] > current_meters[j] and
                          target_meters[i] <= current_meters[j]):
                        if is_spreading:
                            successors[i].append(j)
                            indegree[j] += 1
                            dependencies.append(f"Slave {slave_indices[i]+1} before {slave_indices[j]+1} (collision path L)")
                        else:
                            successors[j].append(i)
                            indegree[i] += 1
                            dependencies.append(f"Slave {slave_indices[j]+1} before {slave_indices[i]+1} (collision path L)")

        if dependencies:
            details['log'].append(f"")
            details['log'].append("Dependencies found:")
            for dep in dependencies:
                details['log'].append(f"  {dep}")
        else:
            details['log'].append(f"")
            details['log'].append("No dependencies - all slaves can move freely")

        # Topological sort using Kahn's algorithm
        # For spreading: RIGHT direction (moving outward positive) goes first with higher index priority
        #                LEFT direction (moving outward negative) goes second with lower index priority
        # For converging: The opposite - inner positions first
        queue = []

        # Separate motors by direction for proper ordering
        right_motors = []  # Moving in positive direction
        left_motors = []   # Moving in negative direction

        for i in range(num_motors):
            if indegree[i] == 0 and directions[i] != '0':
                if directions[i] == 'R':
                    right_motors.append(i)
                else:  # 'L'
                    left_motors.append(i)

        # Sort by distance from target mean (farther first for spreading, closer first for converging)
        if is_spreading:
            # Spreading: farther from center first
            # RIGHT motors: higher index first (they go to positive outer positions)
            right_motors.sort(key=lambda i: (-dist_from_mean[i], -i))
            # LEFT motors: lower index first (they go to negative outer positions)
            left_motors.sort(key=lambda i: (-dist_from_mean[i], i))
            # RIGHT goes before LEFT for spreading
            queue = right_motors + left_motors
        else:
            # Converging: closer to center first
            # Sort by distance (closer first), then by index
            right_motors.sort(key=lambda i: (dist_from_mean[i], -i))
            left_motors.sort(key=lambda i: (dist_from_mean[i], i))
            # For converging, inner positions should clear first
            queue = right_motors + left_motors

        details['log'].append(f"")
        details['log'].append(f"Initial queue (after direction sorting):")
        details['log'].append(f"  RIGHT motors: {[slave_indices[i]+1 for i in right_motors]}")
        details['log'].append(f"  LEFT motors: {[slave_indices[i]+1 for i in left_motors]}")
        details['log'].append(f"  Combined queue: {[slave_indices[i]+1 for i in queue]}")

        order = []
        topo_log = []
        while queue:
            u = queue.pop(0)
            order.append(u)
            topo_log.append(f"Pick slave {slave_indices[u]+1} (indegree=0, dir={directions[u]})")
            for v in successors[u]:
                indegree[v] -= 1
                if indegree[v] == 0:
                    # Insert maintaining order: find correct position based on direction
                    inserted = False
                    for idx, q_item in enumerate(queue):
                        # Compare by distance first, then by index based on direction
                        if is_spreading:
                            # Farther goes first; same distance: R before L, then by index
                            if dist_from_mean[v] > dist_from_mean[q_item]:
                                queue.insert(idx, v)
                                inserted = True
                                break
                            elif dist_from_mean[v] == dist_from_mean[q_item]:
                                if directions[v] == 'R' and directions[q_item] == 'L':
                                    queue.insert(idx, v)
                                    inserted = True
                                    break
                                elif directions[v] == directions[q_item]:
                                    if directions[v] == 'R' and v > q_item:
                                        queue.insert(idx, v)
                                        inserted = True
                                        break
                                    elif directions[v] == 'L' and v < q_item:
                                        queue.insert(idx, v)
                                        inserted = True
                                        break
                    if not inserted:
                        queue.append(v)
                    topo_log.append(f"  -> Slave {slave_indices[v]+1} now has indegree=0")

        # Append non-moving motors at the end
        for i in range(num_motors):
            if directions[i] == '0' and i not in order:
                order.append(i)
                topo_log.append(f"Append stationary slave {slave_indices[i]+1}")

        details['log'].append(f"")
        details['log'].append("Topological sort:")
        for log_line in topo_log:
            details['log'].append(f"  {log_line}")

        # Convert local indices back to slave indices
        ordered_slaves = [slave_indices[i] for i in order]

        # If topological sort failed (cycle detected), return original order
        if len(ordered_slaves) != num_motors:
            print(f"  [MOVE ORDER] Warning: cycle detected, using original order")
            details['log'].append(f"")
            details['log'].append("WARNING: Cycle detected, using original order!")
            result = ([sp[0] for sp in slave_positions], is_spreading)
            if return_details:
                return result + (details,)
            return result

        # Build final order display (1-indexed for UI)
        final_order_str = " -> ".join([str(s+1) for s in ordered_slaves])
        details['log'].append(f"")
        details['log'].append(f"=== FINAL ORDER: {final_order_str} ===")

        # Store additional info
        details['slave_info'] = []
        for i, slave_idx in enumerate(ordered_slaves):
            local_idx = slave_indices.index(slave_idx)
            details['slave_info'].append({
                'slave_idx': slave_idx,
                'order': i + 1,
                'current': current_meters[local_idx],
                'target': target_meters[local_idx],
                'direction': directions[local_idx]
            })

        print(f"  [MOVE ORDER] {'Spreading' if is_spreading else 'Converging'}: {ordered_slaves}")

        if return_details:
            return (ordered_slaves, is_spreading, details)
        return (ordered_slaves, is_spreading)

    def move_to_meters(self, idx, meters, trigger_immediate=True):
        """Move to position in meters"""
        target_scaled = int(meters * self.ACTUAL_STEPS_PER_METER)
        print(f"Moving slave {idx} to {meters:.4f}m (scaled={target_scaled})")
        self.move_to_position(idx, target_scaled, trigger_immediate)

    def move_multiple_to_meters(self, slave_positions, simultaneous=True, slave_delay_ms=10, move_order=None, stop_check=None):
        """
        Move multiple slaves to their target positions.

        Args:
            slave_positions: List of tuples [(slave_idx, meters), ...]
                            or dict {slave_idx: meters, ...}
            simultaneous: If True, trigger all slaves at once (default).
                         If False, trigger each slave with a delay between them.
            slave_delay_ms: Delay in milliseconds between triggering each slave
                           when simultaneous=False (default: 10ms)
            move_order: Optional list of slave indices in the order they should move.
                       Used for staggered movement to ensure correct order.
            stop_check: Optional callable that returns True if stop was requested.
                       Used to interrupt staggered movement when stop is clicked.
        """
        if isinstance(slave_positions, dict):
            slave_positions = list(slave_positions.items())

        if not slave_positions:
            return []

        # Create a lookup dict for positions
        pos_lookup = {idx: meters for idx, meters in slave_positions}
        moving_slaves = []

        if simultaneous:
            # Original behavior: Set all targets without triggering, then trigger all at once
            for slave_idx, meters in slave_positions:
                if slave_idx < self.slaves_count:
                    self.move_to_meters(slave_idx, meters, trigger_immediate=False)
                    moving_slaves.append(slave_idx)

            # Trigger all at once
            if moving_slaves:
                self.trigger_move(moving_slaves)
        else:
            # Staggered movement: trigger each slave with delay IN MOVE ORDER
            delay_sec = slave_delay_ms / 1000.0
            print(f"    [STAGGERED MODE] slave_delay_ms={slave_delay_ms}, delay_sec={delay_sec}")

            # Use move_order if provided, otherwise use original order
            if move_order:
                # Start with slaves in move_order that are in slave_positions
                ordered_slaves = [idx for idx in move_order if idx in pos_lookup]
                # Add any remaining slaves from slave_positions that weren't in move_order
                remaining_slaves = [idx for idx in pos_lookup.keys() if idx not in ordered_slaves]
                ordered_slaves.extend(remaining_slaves)
            else:
                ordered_slaves = [idx for idx, _ in slave_positions]

            print(f"    [STAGGERED] Moving in order: {[s+1 for s in ordered_slaves]} with {slave_delay_ms}ms delay")

            for i, slave_idx in enumerate(ordered_slaves):
                # Check for stop before starting next slave
                if stop_check and stop_check():
                    print(f"    [STAGGERED] Stop requested - stopping all moving slaves")
                    for s in moving_slaves:
                        self.stop_motion(s)
                    return moving_slaves

                if slave_idx < self.slaves_count and slave_idx in pos_lookup:
                    meters = pos_lookup[slave_idx]
                    # Set target and trigger immediately for this slave
                    print(f"    [STAGGERED] Triggering slave {slave_idx+1} -> {meters:.4f}m")
                    self.move_to_meters(slave_idx, meters, trigger_immediate=True)
                    moving_slaves.append(slave_idx)

                    # Add delay before next slave (except for the last one)
                    if i < len(ordered_slaves) - 1:
                        # Split delay into small chunks to check stop flag
                        delay_remaining = delay_sec
                        while delay_remaining > 0:
                            if stop_check and stop_check():
                                print(f"    [STAGGERED] Stop requested during delay - stopping all moving slaves")
                                for s in moving_slaves:
                                    self.stop_motion(s)
                                return moving_slaves
                            sleep_time = min(0.01, delay_remaining)  # Check every 10ms
                            time.sleep(sleep_time)
                            delay_remaining -= sleep_time

        return moving_slaves
    
    def move_home(self, idx):
        """Move to home position (0)"""
        print(f"Moving slave {idx} to HOME")
        self.move_to_position(idx, 0)
    
    def set_home_position(self, idx):
        """
        Set current position as home (zero position)

        Per manual section 6.12:
        1. If Pr0.15 is already 9, first set to a valid mode (0, 1, 2, or 3)
        2. Then set Pr0.15 = 9 to clear multiturn position
        3. Pr0.15 will automatically revert to encoder mode after clearing

        SDO Address: 0x2015 (Pr0.15 - Absolute Encoder Mode)
        """
        print(f"\n" + "="*50)
        print(f"HOMING - Set Current Position as Zero (Slave {idx})")
        print("="*50)

        if idx >= self.slaves_count:
            print(f"  Error: Invalid slave index {idx}")
            return False

        slave = self.master.slaves[idx]

        # Stop trajectory (use per-slave lock)
        with self._trajectory_locks[idx]:
            self._trajectory_active[idx] = False
        
        # Read current position
        current_pos = self._get_pos_scaled_internal(idx)
        current_meters = current_pos / self.ACTUAL_STEPS_PER_METER
        print(f"  Current position before homing:")
        print(f"    Scaled: {current_pos:.0f}")
        print(f"    Meters: {current_meters:.4f} m")
        
        # Must disable first
        was_enabled = self.is_enabled(idx)
        if was_enabled:
            print("\n  Disabling axis (required for homing)...")
            self.disable_drive(idx)
            time.sleep(0.3)
        
        try:
            # Read current pr0.15 value (SDO 0x2015)
            print("\n  Reading current Pr0.15 (Absolute Encoder mode)...")
            pr015_data = slave.sdo_read(0x2015, 0x00)
            current_pr015 = int.from_bytes(pr015_data, byteorder='little', signed=True)
            print(f"    Current Pr0.15 = {current_pr015}")
            
            # If already at 9, reset it first
            if current_pr015 == 9:
                print(f"\n  Pr0.15 is stuck at 9 - resetting to mode 0 first...")
                slave.sdo_write(0x2015, 0x00, (0).to_bytes(4, 'little', signed=True))
                time.sleep(0.5)
                
                # Verify reset
                verify_data = slave.sdo_read(0x2015, 0x00)
                verify_value = int.from_bytes(verify_data, byteorder='little', signed=True)
                print(f"    Reset Pr0.15 = {verify_value}")
                
                if verify_value != 0:
                    print(f"    ERROR: Failed to reset Pr0.15")
                    return False
            
            # Set pr0.15 = 9 to clear multiturn position
            print(f"\n  Setting Pr0.15 = 9 (Clear multiturn and set home)...")
            slave.sdo_write(0x2015, 0x00, (9).to_bytes(4, 'little', signed=True))
            time.sleep(0.2)
            
            # Verify write
            verify_data = slave.sdo_read(0x2015, 0x00)
            verify_value = int.from_bytes(verify_data, byteorder='little', signed=True)
            print(f"    Wrote Pr0.15 = {verify_value}")
            
            if verify_value != 9:
                print(f"    ERROR: Failed to write Pr0.15 = 9")
                return False
            
            # Wait for drive to process (check for up to 5 seconds)
            print(f"\n  Waiting for drive to process homing command...")
            success = False
            for i in range(10):
                time.sleep(0.5)
                check_data = slave.sdo_read(0x2015, 0x00)
                check_value = int.from_bytes(check_data, byteorder='little', signed=True)
                print(f"    Check {i+1}: Pr0.15 = {check_value}")
                
                if check_value != 9:
                    success = True
                    print(f"  ✓ Homing complete! Pr0.15 reverted to {check_value}")
                    break
            
            if not success:
                print(f"  WARNING: Pr0.15 still at 9 after timeout")
                print(f"           This may indicate Er153 - check drive display")
            
        except Exception as e:
            print(f"  Error during homing: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # Sync position and re-enable
        time.sleep(0.2)
        self.sync_position_to_actual(idx)
        
        # Check new position
        new_pos = self._get_pos_scaled_internal(idx)
        new_meters = new_pos / self.ACTUAL_STEPS_PER_METER
        print(f"\n  Position after homing:")
        print(f"    Scaled: {new_pos:.0f}")
        print(f"    Meters: {new_meters:.4f} m")
        
        if was_enabled:
            print("\n  Re-enabling axis...")
            self.enable_drive(idx)
        
        print("="*50)
        return True
    
    # ========== Batch operations ==========
    
    def enable_all(self):
        """Enable all drives"""
        print("\n" + "="*60)
        print("ENABLING ALL DRIVES")
        print("="*60)
        success = True
        for i in range(self.slaves_count):
            if not self.enable_drive(i):
                success = False
        return success
    
    def disable_all(self):
        """Disable all drives"""
        print("\n" + "="*60)
        print("DISABLING ALL DRIVES")
        print("="*60)
        for i in range(self.slaves_count):
            self.disable_drive(i)
    
    def reset_all(self):
        """Reset all faults"""
        print("\n" + "="*60)
        print("RESETTING ALL FAULTS")
        print("="*60)
        for i in range(self.slaves_count):
            self.reset_fault(i)
    
    def home_all(self):
        """Move all to home simultaneously"""
        print("\n" + "="*60)
        print("MOVING ALL TO HOME")
        print("="*60)
        # Set all targets without triggering
        for i in range(self.slaves_count):
            print(f"Moving slave {i} to HOME")
            self.move_to_position(i, 0, trigger_immediate=False)

        # Trigger all at once
        all_slaves = list(range(self.slaves_count))
        self.trigger_move(all_slaves)
    
    def configure_speed(self, idx, velocity, accel=None, decel=None):
        """Configure motion parameters via SDO (for PP mode) - per slave"""
        if idx >= self.slaves_count:
            return False
        if accel is None:
            accel = velocity // 2
        if decel is None:
            decel = accel

        # Store per-slave values
        self._slave_velocity[idx] = velocity
        self._slave_accel[idx] = accel
        self._slave_decel[idx] = decel

        slave = self.master.slaves[idx]
        try:
            # Write to drive SDOs
            slave.sdo_write(0x6081, 0x00, velocity.to_bytes(4, 'little', signed=False))  # Profile velocity
            slave.sdo_write(0x607F, 0x00, velocity.to_bytes(4, 'little', signed=False))  # Max velocity
            slave.sdo_write(0x6083, 0x00, accel.to_bytes(4, 'little', signed=False))     # Acceleration
            slave.sdo_write(0x6084, 0x00, decel.to_bytes(4, 'little', signed=False))     # Deceleration

            print(f"  [SDO] Slave {idx} speed configured:")
            print(f"        Profile Velocity (0x6081): {velocity} units/s")
            print(f"        Max Velocity (0x607F): {velocity} units/s")
            print(f"        Acceleration (0x6083): {accel} units/s²")
            print(f"        Deceleration (0x6084): {decel} units/s²")
            return True
        except Exception as e:
            print(f"configure_speed error: {e}")
            return False
    
    def set_csp_velocity(self, velocity, slave_idx=None):
        """
        Set CSP max position change per ms
        If slave_idx is None, sets for all slaves
        If slave_idx is specified, sets only for that slave
        """
        if slave_idx is not None:
            # Set for specific slave
            old_vel = self._slave_csp_velocity.get(slave_idx, self.DEFAULT_CSP_VELOCITY)
            self._slave_csp_velocity[slave_idx] = velocity
            print(f"  [CSP] Slave {slave_idx} velocity: {old_vel} -> {velocity} units/ms ({velocity * 1000} units/s)")
        else:
            # Set for all slaves
            for i in range(self.slaves_count):
                old_vel = self._slave_csp_velocity.get(i, self.DEFAULT_CSP_VELOCITY)
                self._slave_csp_velocity[i] = velocity
            print(f"  [CSP] All slaves velocity set to: {velocity} units/ms ({velocity * 1000} units/s)")
    
    def set_target_velocity(self, idx, velocity):
        """
        Set target velocity for PV (Profile Velocity) mode
        Positive = forward, Negative = backward, 0 = stop

        Uses SDO to write target velocity (0x60FF)
        """
        if idx >= self.slaves_count:
            return False

        self._target_velocity[idx] = int(velocity)

        slave = self.master.slaves[idx]
        try:
            # Write target velocity to 0x60FF via SDO
            slave.sdo_write(0x60FF, 0x00, int(velocity).to_bytes(4, 'little', signed=True))
            return True
        except Exception as e:
            print(f"  [PV] Error setting velocity for slave {idx}: {e}")
            return False
    
    def velocity_forward(self, idx, speed=None):
        """Move forward in PV mode"""
        if speed is None:
            speed = self._velocity
        self.set_target_velocity(idx, speed)

    def velocity_backward(self, idx, speed=None):
        """Move backward in PV mode"""
        if speed is None:
            speed = self._velocity
        self.set_target_velocity(idx, -speed)

    def velocity_stop(self, idx):
        """Stop in PV mode - stays enabled"""
        self.set_target_velocity(idx, 0)

    def clear_alarm_controlword(self, idx):
        """
        Clear alarm by writing 0x80 (fault reset) to controlword 0x6040
        This works for most alarms except over-voltage and over-current
        """
        if idx >= self.slaves_count:
            return False

        print(f"\n[Method 1] Slave {idx}: Clearing alarm via controlword (0x6040 = 0x80)...")
        slave = self.master.slaves[idx]
        try:
            slave.sdo_write(0x6040, 0x00, (0x80).to_bytes(2, 'little'))
            print(f"  Fault reset command sent")
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"  SDO write error: {e}")
            return False

    def clear_alarm_history(self, idx):
        """
        Clear historical alarm records by writing 1 to 0x2093
        This clears all sub-indexes of 0x3FFE
        """
        if idx >= self.slaves_count:
            return False

        print(f"\n[Method 2] Slave {idx}: Clearing alarm history (0x2093 = 1)...")
        slave = self.master.slaves[idx]
        try:
            slave.sdo_write(0x2093, 0x00, (1).to_bytes(1, 'little'))
            print(f"  Alarm history cleared")
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"  SDO write error: {e}")
            return False

    def clear_multiturn_error(self, idx, reset_position=True):
        """
        Clear Er153 (Multiturn encoder error) via Pr0.15 (SDO 0x2015)
        
        This is a Type 2 alarm that CANNOT be cleared by normal fault reset.
        It requires writing to Pr0.15 (Absolute Encoder settings).
        
        From EL7-EC Manual Section 6.12.3:
        - Er153 occurs when:
          1) Absolute encoder used for first time (needs homing)
          2) Battery voltage < 3.2V (replace battery and restart)
          3) Battery voltage < 2.5V or power was cut off (needs homing)
        
        Pr0.15 values:
        - 5: Clear multiturn alarm, activate multiturn absolute function
             (preserves position if data is valid)
        - 9: Clear multiturn position, reset alarm, activate function
             (clears position data - REQUIRES HOMING AFTER)
        
        Args:
            idx: Slave index
            reset_position: If True, use Pr0.15=9 (clears position, requires homing)
                           If False, use Pr0.15=5 (preserves position if valid)
        
        Returns:
            True if error cleared, False otherwise
        """
        if idx >= self.slaves_count:
            return False

        slave = self.master.slaves[idx]
        value = 9 if reset_position else 5
        
        print(f"\n[Method 3] Slave {idx}: Clearing Er153 via Pr0.15 (0x2015)...")
        print(f"  Setting Pr0.15 = {value} ({'reset position + clear alarm' if reset_position else 'preserve position + clear alarm'})")
        
        try:
            # Read current Pr0.15 value
            current_val = int.from_bytes(slave.sdo_read(0x2015, 0x00), 'little')
            print(f"  Current Pr0.15 value: {current_val}")
            
            # Write new value to clear alarm
            slave.sdo_write(0x2015, 0x00, value.to_bytes(2, 'little'))
            print(f"  Wrote Pr0.15 = {value}, waiting for drive to process...")
            
            # Drive needs time to process (manual says if remains at 5/9 after 3s, alarm not cleared)
            time.sleep(3.5)
            
            # Read back to verify
            new_val = int.from_bytes(slave.sdo_read(0x2015, 0x00), 'little')
            print(f"  Pr0.15 after clear attempt: {new_val}")
            
            # If drive processed the command, Pr0.15 should change to operational mode (1, 2, or 3)
            # If it stays at 5 or 9, the alarm was not cleared successfully
            if new_val in [1, 2, 3]:
                print(f"  ✓ Er153 cleared successfully (Pr0.15 switched to mode {new_val})")
                if reset_position:
                    print(f"  ⚠ WARNING: Position data was cleared. Axis MUST be homed before use!")
                return True
            elif new_val == value:
                print(f"  ✗ Er153 NOT cleared (Pr0.15 still at {value})")
                print(f"    Possible causes:")
                print(f"    - Battery voltage too low (<2.5V)")
                print(f"    - Encoder hardware issue")
                print(f"    - Need to cycle power after battery replacement")
                return False
            else:
                print(f"  Pr0.15 changed to unexpected value: {new_val}")
                return False
                
        except Exception as e:
            print(f"  SDO error: {e}")
            return False

    def clear_error(self, idx, method='all'):
        """
        Clear error/alarm on slave

        Methods:
        - 'controlword': Reset via controlword (0x6040 = 0x80)
        - 'history': Clear alarm history (0x2093 = 1)
        - 'multiturn': Clear Er153 via Pr0.15 (0x2015)
        - 'all': Try all methods in sequence

        Returns: (success: bool, message: str)
        """
        if idx >= self.slaves_count:
            return False, "Invalid slave index"

        print(f"\n{'='*50}")
        print(f"CLEARING ERROR - Slave {idx}")
        print(f"{'='*50}")

        # Read initial error code
        initial_error = self.read_error_code(idx)
        error_name = self.get_error_name(initial_error) if initial_error else "No error"
        print(f"  Initial error: {error_name} (0x{initial_error:04X})")

        if initial_error == 0:
            return True, "No error to clear"

        success = False
        requires_homing = False

        # Check if this is Er153/Er154 (Multiturn encoder error) - SDO 0x7325
        # This is a Type 2 alarm that requires special clearing via Pr0.15
        is_multiturn_error = (initial_error == 0x7325)
        
        if is_multiturn_error:
            print(f"\n  ⚠ Detected Er153/Er154 (Multiturn encoder error)")
            print(f"  This is a Type 2 alarm requiring special Pr0.15 procedure")
            
            # Try to clear via Pr0.15
            if self.clear_multiturn_error(idx, reset_position=True):
                success = True
                requires_homing = True
                print(f"  ✓ Er153 cleared via Pr0.15")
            else:
                print(f"  ✗ Er153 could not be cleared via Pr0.15")
                print(f"    Check battery voltage and encoder connection")
        else:
            # Standard error clearing for Type 1 alarms
            if method in ['controlword', 'all']:
                self.clear_alarm_controlword(idx)
                time.sleep(0.3)

                # Check if error cleared
                error = self.read_error_code(idx)
                if error == 0:
                    success = True
                    print(f"  ✓ Error cleared via controlword")

            if not success and method in ['history', 'all']:
                self.clear_alarm_history(idx)
                time.sleep(0.3)

                # Check if error cleared
                error = self.read_error_code(idx)
                if error == 0:
                    success = True
                    print(f"  ✓ Error cleared via alarm history")

        # Final status
        final_error = self.read_error_code(idx)
        final_name = self.get_error_name(final_error) if final_error else "No error"
        print(f"  Final error: {final_name} (0x{final_error:04X})")

        # Sync position after clearing error (important for CSP mode)
        if success or final_error == 0:
            self.sync_position_to_actual(idx)
            if requires_homing:
                return True, "Error cleared successfully. ⚠ HOMING REQUIRED before operation!"
            else:
                return True, "Error cleared successfully"
        else:
            if is_multiturn_error:
                return False, f"Er153 still present. Check: 1) Battery voltage 2) Encoder connection 3) Power cycle may be needed"
            else:
                return False, f"Error still present: {final_name}"

    def set_home_all(self):
        """Set home position for all slaves sequentially"""
        print("\n" + "="*60)
        print("SETTING HOME FOR ALL SLAVES")
        print("="*60)

        results = []
        for i in range(self.slaves_count):
            print(f"\n--- Slave {i} ---")
            success = self.set_home_position(i)
            results.append((i, success))
            time.sleep(0.5)  # Give drive time between operations

        success_count = sum(1 for _, s in results if s)
        print(f"\n{'='*60}")
        print(f"HOME SET COMPLETE: {success_count}/{len(results)} slaves")
        print("="*60)

        return results
    
    def set_mode(self, mode):
        """Change operating mode (PP=1, PV=3, CSP=8)"""
        mode_names = {1: 'PP', 3: 'PV', 8: 'CSP'}
        mode_name = mode_names.get(mode, f'Unknown({mode})')
        print(f"\nChanging mode to {mode_name}...")
        
        # Disable all first
        for i in range(self.slaves_count):
            if self.is_enabled(i):
                self.disable_drive(i)
        
        time.sleep(0.1)
        
        # Set new mode
        self.mode = mode
        for i in range(self.slaves_count):
            slave = self.master.slaves[i]
            slave.sdo_write(0x6060, 0x00, bytes([mode]))
            
            # For PV mode, set initial velocity to 0
            if mode == self.MODE_PV:
                self.set_target_velocity(i, 0)
            else:
                self.sync_position_to_actual(i)
        
        print(f"  Mode set to {mode_name}")
    
    def print_status(self):
        """Print status of all drives"""
        print("\n" + "="*60)
        print("STATUS")
        print("="*60)
        for i in range(self.slaves_count):
            status = self.read_status(i)
            pos_m = self.read_position_meters(i)
            enabled = self.is_enabled(i)
            fault = self.has_fault(i)
            print(f"Slave {i}: 0x{status:04X} pos={pos_m:.4f}m en={enabled} fault={fault}")
    
    def disconnect(self):
        """Disconnect"""
        print("\nDisconnecting...")

        # Stop trajectories (use per-slave locks)
        for i in range(self.slaves_count):
            with self._trajectory_locks[i]:
                self._trajectory_active[i] = False

        # Disable drives WHILE PDO is still running
        # This ensures the disable command is actually sent to the drives
        print("Sending disable to drives...")
        for i in range(self.slaves_count):
            self._control_word[i] = 0x0000  # Disable

        # Wait for PDO to send the disable command (several cycles)
        time.sleep(0.3)

        # Now stop PDO
        self._pdo_running = False
        if self._pdo_thread:
            self._pdo_thread.join(timeout=1.0)

        # Restore Windows timer resolution if set
        try:
            import ctypes
            winmm = ctypes.windll.winmm
            winmm.timeEndPeriod(1)
            print("[PDO] Windows timer resolution restored")
        except:
            pass

        # Transition to INIT state
        try:
            self.master.state = pysoem.INIT_STATE
            self.master.write_state()
            time.sleep(0.1)
            self.master.close()
        except Exception as e:
            print(f"Disconnect warning: {e}")

        self.connected = False
        print("Disconnected")