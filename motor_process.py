#!/usr/bin/env python3
"""
Motor Process - Runs EtherCAT in separate process
Communicates with UI via multiprocessing Queue and shared memory
"""

import multiprocessing as mp
from multiprocessing import Process, Queue, Value, Array
import time
import ctypes
import socket
import threading

from ethercat_controller import EtherCATController


class MotorProcess:
    """Motor control process with IPC"""
    
    # Commands
    CMD_ENABLE = 'enable'
    CMD_DISABLE = 'disable'
    CMD_RESET = 'reset'
    CMD_MOVE = 'move'
    CMD_HOME = 'home'
    CMD_SET_HOME = 'set_home'
    CMD_SET_HOME_ALL = 'set_home_all'
    CMD_SET_MODE = 'set_mode'
    CMD_TEMPLATE = 'template'
    CMD_TEMPLATE_LOOP = 'template_loop'
    CMD_STOP = 'stop'
    CMD_RELOAD = 'reload'
    CMD_STATUS = 'status'
    CMD_RESCAN = 'rescan'  # Rescan slaves after disconnect/reconnect
    CMD_CLEAR_ERROR = 'clear_error'  # Clear error on specific slave
    CMD_UDP_CONNECT = 'udp_connect'  # Start UDP receiver
    CMD_UDP_DISCONNECT = 'udp_disconnect'  # Stop UDP receiver
    CMD_CHANGE_INTERFACE = 'change_interface'  # Change network interface
    CMD_GET_ADAPTERS = 'get_adapters'  # Get available network adapters
    CMD_RECOVER = 'recover'  # Manual recovery after communication error
    CMD_QUIT = 'quit'

    
    def __init__(self, interface=None):
        self.interface = interface
        
        # IPC - Queues for commands and responses
        self.cmd_queue = Queue()
        self.resp_queue = Queue()
        
        # Shared memory for real-time status
        # [0-7]: positions (up to 8 slaves), [8-15]: status words, [16]: velocity, [17]: accel, [18]: decel, [19]: csp_vel, [20-27]: error codes
        # Total: 28 doubles for 8 slaves + 4 config values
        self.shared_data = Array(ctypes.c_double, 32)
        self.shared_state = Value(ctypes.c_int, 0)  # 0=disconnected, 1=connected, 2=enabled
        self.shared_moving = Value(ctypes.c_int, 0)
        self.shared_num_slaves = Value(ctypes.c_int, 0)
        self.shared_mode = Value(ctypes.c_int, 8)  # 1=PP, 8=CSP
        self.shared_stop = Value(ctypes.c_int, 0)  # CRITICAL: Stop flag - checked directly, not queued
        
        # Shared interface name (use Array of chars)
        self.shared_interface = Array(ctypes.c_char, 256)

        # UDP receiver state
        self.shared_udp_connected = Value(ctypes.c_int, 0)  # 0=disconnected, 1=connected
        self.shared_udp_ip = Array(ctypes.c_char, 64)
        self.shared_udp_port = Value(ctypes.c_int, 9000)

        # Event tracking for UI notifications
        self.shared_event_type = Value(ctypes.c_int, 0)  # 0=none, 1=slave_change, 2=error, 3=info
        self.shared_event_slave = Value(ctypes.c_int, -1)  # Which slave (-1 = all/general)
        self.shared_event_code = Value(ctypes.c_int, 0)  # Error code or event-specific code
        self.shared_event_msg = Array(ctypes.c_char, 256)  # Event message
        self.shared_event_counter = Value(ctypes.c_int, 0)  # Incremented on each new event

        # Process handle
        self.process = None
        self._running = Value(ctypes.c_bool, False)

    def start(self):
        """Start motor process"""
        if self.process and self.process.is_alive():
            print("Motor process already running")
            return
        
        self._running.value = True
        self.process = Process(
            target=self._run_process,
            args=(
                self.interface,
                self.cmd_queue,
                self.resp_queue,
                self.shared_data,
                self.shared_state,
                self.shared_moving,
                self.shared_num_slaves,
                self.shared_mode,
                self.shared_interface,
                self.shared_stop,
                self._running,
                self.shared_udp_connected,
                self.shared_udp_ip,
                self.shared_udp_port,
                self.shared_event_type,
                self.shared_event_slave,
                self.shared_event_code,
                self.shared_event_msg,
                self.shared_event_counter
            )
        )
        self.process.start()
        print(f"Motor process started (PID: {self.process.pid})")
    
    def stop(self):
        """Stop motor process"""
        if self.process and self.process.is_alive():
            self._running.value = False
            self.cmd_queue.put({'cmd': self.CMD_QUIT})
            self.process.join(timeout=3.0)
            if self.process.is_alive():
                self.process.terminate()
        print("Motor process stopped")
    
    def emergency_stop(self):
        """Emergency stop - sets flag immediately, bypassing queue"""
        print("\n[EMERGENCY STOP] Setting stop flag NOW!")
        self.shared_stop.value = 1
        # Also send command to queue as backup
        self.send_command(self.CMD_STOP)

    def rescan_slaves(self):
        """Rescan for slaves after disconnect/reconnect"""
        print("\n[RESCAN REQUEST] Requesting slave rescan...")
        self.send_command(self.CMD_RESCAN)
    
    def send_command(self, cmd, data=None):
        """Send command to motor process"""
        # STOP command sets flag immediately AND goes to queue
        if cmd == self.CMD_STOP:
            self.shared_stop.value = 1
            print(f"[STOP FLAG SET] shared_stop = {self.shared_stop.value}")
        
        self.cmd_queue.put({'cmd': cmd, 'data': data})
        print(f"[CMD SENT] {cmd} -> queue")
    
    def get_response(self, timeout=0.1):
        """Get response from motor process"""
        try:
            return self.resp_queue.get(timeout=timeout)
        except:
            return None
    
    def get_status(self):
        """Get real-time status from shared memory"""
        num_slaves = self.shared_num_slaves.value
        positions = []
        status_words = []
        error_codes = []

        for i in range(min(num_slaves, 8)):
            positions.append(self.shared_data[i])           # [0-7]: positions
            status_words.append(int(self.shared_data[8 + i]))   # [8-15]: status words
            error_codes.append(int(self.shared_data[20 + i]))   # [20-27]: error codes

        # Get interface name
        try:
            interface = self.shared_interface.value.decode('utf-8')
        except:
            interface = "Unknown"

        # Get UDP state
        try:
            udp_ip = self.shared_udp_ip.value.decode('utf-8')
        except:
            udp_ip = "127.0.0.1"

        # Get event info
        event = None
        if self.shared_event_type.value != 0:
            try:
                event_msg = self.shared_event_msg.value.decode('utf-8')
            except:
                event_msg = ""
            event = {
                'type': self.shared_event_type.value,  # 1=slave_change, 2=error, 3=info
                'slave': self.shared_event_slave.value,
                'code': self.shared_event_code.value,
                'message': event_msg,
                'counter': self.shared_event_counter.value
            }

        return {
            'state': self.shared_state.value,
            'moving': bool(self.shared_moving.value),
            'num_slaves': num_slaves,
            'positions': positions,
            'status_words': status_words,
            'error_codes': error_codes,
            'mode': self.shared_mode.value,
            'interface': interface,
            'velocity': int(self.shared_data[16]),      # [16]: velocity
            'acceleration': int(self.shared_data[17]),  # [17]: accel
            'deceleration': int(self.shared_data[18]),  # [18]: decel
            'csp_velocity': int(self.shared_data[19]),  # [19]: csp_vel
            'udp_connected': bool(self.shared_udp_connected.value),
            'udp_ip': udp_ip,
            'udp_port': self.shared_udp_port.value,
            'event': event
        }

    def clear_event(self):
        """Clear the current event after UI has processed it"""
        self.shared_event_type.value = 0

    @staticmethod
    def _run_process(interface, cmd_queue, resp_queue, shared_data, shared_state,
                     shared_moving, shared_num_slaves, shared_mode, shared_interface, shared_stop, running,
                     shared_udp_connected, shared_udp_ip, shared_udp_port,
                     shared_event_type, shared_event_slave, shared_event_code, shared_event_msg, shared_event_counter):
        """Main process loop"""
        import pysoem

        # =============================================================
        # CRITICAL: Set process priority IMMEDIATELY at process start
        # This prevents Windows from throttling when terminal is in background
        # =============================================================
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            winmm = ctypes.windll.winmm

            # Set multimedia timer to 1ms resolution (system-wide)
            winmm.timeBeginPeriod(1)

            # Set process priority to REALTIME_PRIORITY_CLASS (0x100)
            # This is necessary for consistent 1ms timing even in background
            # Note: Requires admin privileges for full effect
            current_process = kernel32.GetCurrentProcess()

            # Try REALTIME first, fall back to HIGH if it fails
            result = kernel32.SetPriorityClass(current_process, 0x100)  # REALTIME_PRIORITY_CLASS
            if result:
                print("[MOTOR PROCESS] Process priority set to REALTIME_PRIORITY_CLASS")
            else:
                # Fallback to HIGH_PRIORITY_CLASS
                result = kernel32.SetPriorityClass(current_process, 0x80)  # HIGH_PRIORITY_CLASS
                if result:
                    print("[MOTOR PROCESS] Process priority set to HIGH_PRIORITY_CLASS (REALTIME failed)")
                else:
                    print("[MOTOR PROCESS] Warning: Failed to set process priority")

            # Disable priority boost (prevents Windows from lowering priority)
            kernel32.SetProcessPriorityBoost(current_process, True)  # True = disable boost
            print("[MOTOR PROCESS] Priority boost disabled")

            # CRITICAL: Prevent Windows from throttling when terminal is backgrounded
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
            print("[MOTOR PROCESS] Execution state set to prevent background throttling")

            # Set process affinity to a single CPU core for more consistent timing
            # This prevents Windows from moving the process between cores
            try:
                # Get current affinity mask
                process_affinity = ctypes.c_ulonglong()
                system_affinity = ctypes.c_ulonglong()
                kernel32.GetProcessAffinityMask(current_process,
                                                ctypes.byref(process_affinity),
                                                ctypes.byref(system_affinity))

                # Find first available core and pin to it
                if system_affinity.value > 0:
                    # Use core 0 (or first available)
                    single_core = 1  # Core 0
                    kernel32.SetProcessAffinityMask(current_process, single_core)
                    print(f"[MOTOR PROCESS] Pinned to CPU core 0 for consistent timing")
            except Exception as e:
                print(f"[MOTOR PROCESS] CPU affinity warning: {e}")

        except Exception as e:
            print(f"[MOTOR PROCESS] Priority setup warning: {e}")

        ec = None
        last_stop_check = 0
        slaves_changed_flag = [False]  # Use list for mutability in nested function
        communication_error_flag = [False]  # Flag for Er81b/communication errors
        recovery_in_progress = [False]  # Prevent multiple recovery attempts

        # UDP receiver state
        udp_thread = None
        udp_shutdown = [False]  # Mutable flag for thread shutdown

        def send_response(success, message, data=None):
            resp_queue.put({
                'success': success,
                'message': message,
                'data': data
            })

        def send_event(event_type, slave_idx, error_code, message):
            """
            Send event to UI via shared memory
            event_type: 1=slave_change, 2=error, 3=info
            """
            shared_event_type.value = event_type
            shared_event_slave.value = slave_idx
            shared_event_code.value = error_code
            try:
                shared_event_msg.value = message.encode('utf-8')[:255]
            except:
                shared_event_msg.value = b''
            shared_event_counter.value += 1
            print(f"[EVENT] Type={event_type}, Slave={slave_idx}, Code={error_code}, Msg={message}")

        def on_slaves_changed(wkc, expected):
            """Callback when slave count changes (disconnect/reconnect detected)"""
            print(f"\n[SLAVES CHANGED] WKC={wkc}, expected={expected}")
            print("[SLAVES CHANGED] Stopping all motion and flagging for rescan...")
            slaves_changed_flag[0] = True
            shared_stop.value = 1
            shared_moving.value = 0

            # Send event to UI
            send_event(1, -1, wkc, f"Slave disconnect! WKC={wkc}/{expected}")

            send_response(False, f"SLAVE DISCONNECT DETECTED! WKC={wkc}, expected={expected}. Motion stopped.", {
                'slave_change': True,
                'wkc': wkc,
                'expected': expected
            })

        def on_communication_error(error_msg):
            """
            Callback when communication error detected (Er81b, WKC mismatch, timing critical)
            This triggers auto-recovery attempt
            """
            nonlocal ec

            if recovery_in_progress[0]:
                print(f"[COMM ERROR] Recovery already in progress, ignoring: {error_msg}")
                return

            print(f"\n[COMMUNICATION ERROR] {error_msg}")
            print("[COMMUNICATION ERROR] Flagging for auto-recovery...")

            communication_error_flag[0] = True
            shared_stop.value = 1
            shared_moving.value = 0

            # Send event to UI with recovery option
            send_event(2, -1, 0x81B, f"Communication error: {error_msg}")

            send_response(False, f"COMMUNICATION ERROR: {error_msg}. Auto-recovery will be attempted.", {
                'communication_error': True,
                'error_msg': error_msg,
                'auto_recovery': True
            })

        # Shared UDP log queue for UI display
        udp_log_queue = []
        udp_log_lock = threading.Lock()

        def add_udp_log(msg_type, message, positions=None):
            """Add log entry for UDP activity"""
            with udp_log_lock:
                entry = {
                    'type': msg_type,  # 'recv', 'move', 'error', 'info'
                    'message': message,
                    'positions': positions,
                    'time': time.time()
                }
                udp_log_queue.append(entry)
                # Keep only last 50 entries
                if len(udp_log_queue) > 50:
                    udp_log_queue.pop(0)

                # Send to UI via response queue
                send_response(True, f"[UDP] {message}", {'udp_log': entry})

        def udp_listener(ip, port, shutdown_flag, controller):
            """
            Listen for UDP commands (EXACTLY like multi_motor_csp.py)
            Format: "slave_idx,position;slave_idx,position" e.g., "0,0.14;1,0.00"
            """
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)

            try:
                sock.bind((ip, port))
            except Exception as e:
                print(f"[UDP] Failed to bind: {e}")
                send_response(False, f"UDP bind failed: {e}")
                return

            print(f"[UDP] Listener started on {ip}:{port}")
            shared_udp_connected.value = 1
            shared_udp_ip.value = ip.encode('utf-8')[:63]
            shared_udp_port.value = port

            while not shutdown_flag[0] and running.value:
                try:
                    data, addr = sock.recvfrom(1024)
                    message = data.decode().strip().rstrip('\x00')

                    # Parse message: "0,0.14;1,0.00"
                    commands = message.split(';')

                    for cmd in commands:
                        cmd = cmd.strip()
                        if not cmd:
                            continue

                        parts = cmd.split(',')
                        if len(parts) == 2:
                            try:
                                slave_idx = int(parts[0])
                                target_meters = float(parts[1])

                                if slave_idx < 0 or slave_idx >= controller.slaves_count:
                                    print(f"  UDP: Slave {slave_idx} not found")
                                    continue

                                if controller.is_enabled(slave_idx):
                                    target_scaled = int(target_meters * controller.ACTUAL_STEPS_PER_METER)

                                    # Set trajectory (like multi_motor_csp.py move_to_csp)
                                    with controller._pdo_lock:
                                        controller._control_word[slave_idx] = 0x000F

                                    # Use per-slave trajectory lock (like multi_motor_csp.py)
                                    with controller._trajectory_locks[slave_idx]:
                                        controller._trajectory_target[slave_idx] = target_scaled
                                        controller._trajectory_active[slave_idx] = True

                                    print(f"  UDP: Slave {slave_idx} -> {target_meters:.4f} m")
                                else:
                                    print(f"  UDP: Slave {slave_idx} not enabled!")

                            except ValueError as e:
                                print(f"  UDP: Invalid data: {cmd} ({e})")
                        else:
                            print(f"  UDP: Expected 'slave,position', got: {cmd}")

                except socket.timeout:
                    continue
                except Exception as e:
                    if not shutdown_flag[0]:
                        print(f"  UDP error: {e}")

            sock.close()
            shared_udp_connected.value = 0
            print("[UDP] Listener stopped")
        
        def update_shared_status():
            """Update shared memory with current status"""
            if ec:
                for i in range(min(ec.slaves_count, 8)):
                    shared_data[i] = ec.read_position_meters(i)        # [0-7]: positions
                    shared_data[8 + i] = float(ec.read_status(i))      # [8-15]: status words
                    shared_data[20 + i] = float(ec.read_error_code(i)) # [20-27]: error codes
                shared_mode.value = ec.mode
                # Update speed values [16-19]
                shared_data[16] = float(ec._velocity)
                shared_data[17] = float(ec._accel)
                shared_data[18] = float(ec._decel)
                # For CSP, show the first slave's velocity as reference (or default)
                csp_vel = ec._slave_csp_velocity.get(0, ec.DEFAULT_CSP_VELOCITY)
                shared_data[19] = float(csp_vel)

        def check_priority_commands():
            """Check for high-priority commands (reset, disable, stop) during template execution"""
            try:
                while True:
                    try:
                        cmd_data = cmd_queue.get_nowait()
                        cmd = cmd_data.get('cmd')
                        
                        if cmd == MotorProcess.CMD_STOP:
                            print("\n[PRIORITY CMD] STOP received during template!")
                            shared_stop.value = 1
                            shared_moving.value = 0
                            for i in range(ec.slaves_count):
                                ec.stop_motion(i)
                            send_response(True, "EMERGENCY STOP")
                            return True
                        
                        elif cmd == MotorProcess.CMD_RESET:
                            print("\n[PRIORITY CMD] RESET received during template!")
                            ec.reset_all()
                            send_response(True, "Faults reset")
                        
                        elif cmd == MotorProcess.CMD_DISABLE:
                            print("\n[PRIORITY CMD] DISABLE received!")
                            ec.disable_all()
                            shared_state.value = 1
                            shared_stop.value = 1
                            shared_moving.value = 0
                            send_response(True, "Drives disabled")
                            return True
                        
                        elif cmd == MotorProcess.CMD_ENABLE:
                            print("\n[PRIORITY CMD] ENABLE received!")
                            if ec.enable_all():
                                shared_state.value = 2
                                send_response(True, "Drives enabled")
                            else:
                                send_response(False, "Failed to enable")
                                
                    except:
                        break
            except Exception as e:
                print(f"[check_priority_commands] Error: {e}")
            return False

        def wait_for_target_reached(slave_indices, timeout=30.0):
            """Wait for all specified slaves to reach their targets"""
            start_time = time.time()
            check_interval = 0.1

            while time.time() - start_time < timeout:
                # Check for slave disconnect
                if slaves_changed_flag[0]:
                    print("    [SLAVE CHANGE] Slave disconnect detected during wait")
                    return False

                # Check for priority commands
                if check_priority_commands():
                    return False

                # Check stop flag
                if shared_stop.value == 1:
                    print("    [STOPPED] Motion interrupted during wait")
                    return False

                # Update positions in shared memory
                update_shared_status()
                
                # Check for faults
                for idx in slave_indices:
                    if idx < ec.slaves_count:
                        if ec.has_fault(idx):
                            error_code = ec.read_error_code(idx)
                            error_name = ec.get_error_name(error_code)
                            print(f"    [FAULT] Slave {idx} fault detected: {error_name} (0x{error_code:04X})")
                            # Send error event to UI
                            send_event(2, idx, error_code, f"Slave {idx}: {error_name}")
                            shared_stop.value = 1
                            return False
                
                # Check if all slaves reached target
                all_reached = True
                for idx in slave_indices:
                    if idx < ec.slaves_count:
                        if ec.mode == ec.MODE_PP:
                            if not ec.is_target_reached(idx):
                                all_reached = False
                                break
                        # In CSP mode, check if trajectory is complete (use per-slave lock)
                        elif ec.mode == ec.MODE_CSP:
                            with ec._trajectory_locks[idx]:
                                if ec._trajectory_active.get(idx, False):
                                    all_reached = False
                                    break
                
                if all_reached:
                    # Verify all slaves actually at target (position check)
                    position_ok = True
                    for idx in slave_indices:
                        if idx < ec.slaves_count:
                            actual_pos = ec.read_position_meters(idx)
                            # Get the target that was sent for this slave
                            target_pos = ec._target_position.get(idx, 0) / ec.ACTUAL_STEPS_PER_METER
                            error_m = abs(actual_pos - target_pos)
                            if error_m > 0.002:  # 2mm tolerance
                                position_ok = False
                                print(f"    [WARNING] Slave {idx} position error: {error_m*1000:.2f}mm")
                                break
                    
                    if position_ok:
                        return True
                    else:
                        # Continue waiting if position not accurate enough
                        all_reached = False
                
                time.sleep(check_interval)
            
            print(f"    [WARNING] Timeout waiting for targets (after {timeout}s)")
            # Check for faults one more time
            for idx in slave_indices:
                if idx < ec.slaves_count and ec.has_fault(idx):
                    error_code = ec.read_error_code(idx)
                    error_name = ec.get_error_name(error_code)
                    print(f"    [FAULT] Slave {idx} has fault: {error_name}")
                    # Send error event to UI
                    send_event(2, idx, error_code, f"Slave {idx}: {error_name}")
                    shared_stop.value = 1
                    return False
            
            return True  # Continue anyway if no faults
        
        try:
            # Create controller
            ec = EtherCATController(interface)

            if not ec.connect():
                send_response(False, "Failed to connect to EtherCAT")
                shared_state.value = 0
                return

            # Register slave change callback for disconnect detection
            ec.set_slaves_changed_callback(on_slaves_changed)

            # Register communication error callback for Er81b auto-recovery
            ec.set_communication_error_callback(on_communication_error)

            shared_state.value = 1  # Connected
            shared_num_slaves.value = ec.slaves_count
            shared_mode.value = ec.mode

            # Store interface name
            if ec.interface:
                shared_interface.value = ec.interface.encode('utf-8')[:255]

            send_response(True, f"Connected with {ec.slaves_count} slave(s), Mode: {'CSP' if ec.mode == 8 else 'PP'}")
            
            # Main loop
            last_status_update = 0

            while running.value:
                # Update shared status every 10ms
                now = time.time()
                if now - last_status_update > 0.01:
                    update_shared_status()
                    last_status_update = now

                # Check for slave disconnect/reconnect
                if slaves_changed_flag[0]:
                    print("\n[MAIN LOOP] Slave change detected - stopping all motion")
                    slaves_changed_flag[0] = False
                    shared_moving.value = 0
                    shared_state.value = 1  # Mark as connected but not enabled
                    # Stop all motion
                    for i in range(ec.slaves_count):
                        ec.stop_motion(i)

                # ============================================================
                # AUTO-RECOVERY for communication errors (Er81b)
                # ============================================================
                if communication_error_flag[0] and not recovery_in_progress[0]:
                    print("\n[AUTO-RECOVERY] Communication error detected - attempting recovery...")
                    communication_error_flag[0] = False
                    recovery_in_progress[0] = True
                    shared_moving.value = 0

                    try:
                        # Step 1: Stop PDO loop and disconnect
                        print("[AUTO-RECOVERY] Step 1: Stopping PDO and disconnecting...")
                        ec._pdo_running = False
                        if ec._pdo_thread:
                            ec._pdo_thread.join(timeout=2.0)
                        time.sleep(0.5)

                        # Step 2: Close master connection
                        print("[AUTO-RECOVERY] Step 2: Closing master connection...")
                        try:
                            ec.master.state = pysoem.INIT_STATE
                            ec.master.write_state()
                            ec.master.close()
                        except:
                            pass
                        time.sleep(1.0)

                        # Step 3: Reconnect
                        print("[AUTO-RECOVERY] Step 3: Reconnecting...")
                        ec = EtherCATController(interface)
                        if ec.connect():
                            # Re-register callbacks
                            ec.set_slaves_changed_callback(on_slaves_changed)
                            ec.set_communication_error_callback(on_communication_error)

                            shared_state.value = 1  # Connected but not enabled
                            shared_num_slaves.value = ec.slaves_count
                            shared_mode.value = ec.mode
                            if ec.interface:
                                shared_interface.value = ec.interface.encode('utf-8')[:255]

                            print("[AUTO-RECOVERY] SUCCESS! Reconnected to EtherCAT")
                            send_event(3, -1, 0, "Auto-recovery successful! Reconnected.")
                            send_response(True, f"Auto-recovery successful! Reconnected with {ec.slaves_count} slave(s).", {
                                'recovery_success': True,
                                'num_slaves': ec.slaves_count
                            })
                        else:
                            print("[AUTO-RECOVERY] FAILED! Could not reconnect")
                            shared_state.value = 0
                            shared_num_slaves.value = 0
                            send_event(2, -1, 0x81B, "Auto-recovery failed! Manual restart required.")
                            send_response(False, "Auto-recovery FAILED! Please restart the program.", {
                                'recovery_success': False
                            })

                    except Exception as e:
                        print(f"[AUTO-RECOVERY] Exception during recovery: {e}")
                        import traceback
                        traceback.print_exc()
                        send_event(2, -1, 0x81B, f"Auto-recovery exception: {e}")
                        send_response(False, f"Auto-recovery exception: {e}", {
                            'recovery_success': False
                        })

                    finally:
                        recovery_in_progress[0] = False
                        shared_stop.value = 0  # Clear stop flag after recovery attempt

                # Check STOP flag every cycle (HIGH PRIORITY)
                if shared_stop.value == 1 and (now - last_stop_check > 0.1):
                    print("\n[STOP FLAG DETECTED] Executing emergency stop!")
                    last_stop_check = now
                    shared_moving.value = 0

                    # Stop all trajectories and sync positions
                    for i in range(ec.slaves_count):
                        ec.stop_motion(i)

                    print("[STOP COMPLETE] All motion stopped")

                    # Reset stop flag after handling
                    shared_stop.value = 0

                # Check for commands
                try:
                    cmd_data = cmd_queue.get(timeout=0.01)
                    cmd = cmd_data.get('cmd')
                    data = cmd_data.get('data')
                    
                    print(f"[PROCESSING CMD] {cmd}")

                    if cmd == MotorProcess.CMD_QUIT:
                        break
                    
                    elif cmd == MotorProcess.CMD_ENABLE:
                        # Check for faults before enabling
                        fault_slaves = []
                        for i in range(ec.slaves_count):
                            if ec.has_fault(i):
                                error_code = ec.read_error_code(i)
                                error_name = ec.get_error_name(error_code)
                                fault_slaves.append(f"Slave {i}: {error_name}")

                        if fault_slaves:
                            send_response(False, f"Cannot enable - faults detected: {', '.join(fault_slaves)}. Clear errors first.")
                        elif ec.enable_all():
                            shared_state.value = 2
                            send_response(True, "Drives enabled")
                        else:
                            send_response(False, "Failed to enable drives")
                    
                    elif cmd == MotorProcess.CMD_DISABLE:
                        ec.disable_all()
                        shared_state.value = 1
                        send_response(True, "Drives disabled")
                    
                    elif cmd == MotorProcess.CMD_RESET:
                        ec.reset_all()
                        send_response(True, "Faults reset")
                    
                    elif cmd == MotorProcess.CMD_STOP:
                        print("\n[STOP COMMAND] Emergency stop from queue!")
                        shared_stop.value = 1
                        shared_moving.value = 0
                        
                        # Stop all trajectories and sync positions
                        for i in range(ec.slaves_count):
                            ec.stop_motion(i)
                        
                        send_response(True, "EMERGENCY STOP - All motion stopped")
                    
                    elif cmd == MotorProcess.CMD_MOVE:
                        if data:
                            positions = data.get('positions', [])
                            slave = data.get('slave', None)
                            
                            shared_moving.value = 1
                            
                            if slave is not None and slave != '':
                                # Move specific slave
                                slave_idx = int(slave)
                                if slave_idx < ec.slaves_count and len(positions) > 0:
                                    ec.move_to_meters(slave_idx, positions[0])
                                    print(f"Moving Slave {slave_idx} to {positions[0]}m")
                            else:
                                # Move all slaves (for template use)
                                for i, pos in enumerate(positions):
                                    if i < ec.slaves_count:
                                        ec.move_to_meters(i, pos)
                            
                            time.sleep(0.5)
                            shared_moving.value = 0
                            send_response(True, f"Move command sent")
                    
                    elif cmd == MotorProcess.CMD_HOME:
                        shared_moving.value = 1
                        ec.home_all()
                        time.sleep(0.5)
                        shared_moving.value = 0
                        send_response(True, "Moving to home")
                    
                    elif cmd == MotorProcess.CMD_SET_HOME:
                        if data:
                            slave = data.get('slave', 'all')
                        else:
                            slave = 'all'
                        
                        if slave == 'all':
                            for i in range(ec.slaves_count):
                                ec.set_home_position(i)
                            send_response(True, "Home position set for all slaves")
                        else:
                            ec.set_home_position(int(slave))
                            send_response(True, f"Home position set for slave {slave}")
                    
                    elif cmd == MotorProcess.CMD_SET_MODE:
                        if data:
                            # Check for faults before changing mode
                            fault_slaves = []
                            for i in range(ec.slaves_count):
                                if ec.has_fault(i):
                                    error_code = ec.read_error_code(i)
                                    error_name = ec.get_error_name(error_code)
                                    fault_slaves.append(f"Slave {i}: {error_name}")

                            if fault_slaves:
                                send_response(False, f"Cannot change mode - faults detected: {', '.join(fault_slaves)}. Clear errors first.")
                            else:
                                mode = data.get('mode', 8)
                                ec.set_mode(mode)
                                shared_mode.value = mode
                                mode_names = {1: 'PP', 3: 'PV', 8: 'CSP'}
                                send_response(True, f"Mode set to {mode_names.get(mode, mode)}")
                    
                    elif cmd == 'velocity_forward':
                        if data:
                            slave = data.get('slave', 'all')
                            speed = data.get('speed', ec._velocity)
                            if slave == 'all':
                                for i in range(ec.slaves_count):
                                    ec.velocity_forward(i, speed)
                            else:
                                ec.velocity_forward(int(slave), speed)
                            send_response(True, f"Velocity forward: {speed}")
                    
                    elif cmd == 'velocity_backward':
                        if data:
                            slave = data.get('slave', 'all')
                            speed = data.get('speed', ec._velocity)
                            if slave == 'all':
                                for i in range(ec.slaves_count):
                                    ec.velocity_backward(i, speed)
                            else:
                                ec.velocity_backward(int(slave), speed)
                            send_response(True, f"Velocity backward: {speed}")
                    
                    elif cmd == 'velocity_stop':
                        if data:
                            slave = data.get('slave', 'all')
                        else:
                            slave = 'all'
                        if slave == 'all':
                            for i in range(ec.slaves_count):
                                ec.velocity_stop(i)
                        else:
                            ec.velocity_stop(int(slave))
                        send_response(True, "Velocity stopped")
                    
                    elif cmd == 'set_speed':
                        if data:
                            mode = data.get('mode', 'PP')
                            
                            if mode == 'CSP':
                                # CSP mode - only set max step
                                csp_vel = data.get('csp_velocity', 800)
                                ec.set_csp_velocity(csp_vel)
                                send_response(True, f"CSP Speed: max_step={csp_vel} units/ms")
                            else:
                                # PP or PV mode - set SDO parameters
                                velocity = data.get('velocity', 80000)
                                accel = data.get('acceleration', 6000)
                                decel = data.get('deceleration', 6000)
                                
                                print(f"Applying {mode} speed: vel={velocity}, accel={accel}, decel={decel}")
                                for i in range(ec.slaves_count):
                                    ec.configure_speed(i, velocity, accel, decel)
                                
                                send_response(True, f"{mode} Speed: vel={velocity}, accel={accel}, decel={decel}")
                    
                    elif cmd == 'set_steps_config':
                        if data:
                            actual_steps = data.get('actual_steps_per_meter', 792914)
                            raw_steps = data.get('raw_steps_per_meter', 202985985)
                            
                            ec.ACTUAL_STEPS_PER_METER = actual_steps
                            ec.RAW_STEPS_PER_METER = raw_steps
                            
                            print(f"[Steps Config] Actual: {actual_steps}, Raw: {raw_steps}")
                            send_response(True, f"Steps config applied: Actual={actual_steps}, Raw={raw_steps}")
                    
                    elif cmd == MotorProcess.CMD_STATUS:
                        send_response(True, "Status", {
                            'state': shared_state.value,
                            'num_slaves': ec.slaves_count,
                            'mode': ec.mode,
                            'interface': ec.interface,
                            'positions': [ec.read_position_meters(i) for i in range(ec.slaves_count)]
                        })

                    elif cmd == MotorProcess.CMD_RESCAN:
                        # Rescan slaves after disconnect/reconnect
                        print("\n[RESCAN] Attempting to rescan slaves...")
                        try:
                            # Disconnect and reconnect
                            ec.disconnect()
                            time.sleep(0.5)

                            ec = EtherCATController(interface)
                            if ec.connect():
                                ec.set_slaves_changed_callback(on_slaves_changed)
                                ec.set_communication_error_callback(on_communication_error)
                                shared_state.value = 1
                                shared_num_slaves.value = ec.slaves_count
                                shared_mode.value = ec.mode
                                if ec.interface:
                                    shared_interface.value = ec.interface.encode('utf-8')[:255]
                                send_response(True, f"Rescan complete: {ec.slaves_count} slave(s) found")
                            else:
                                shared_state.value = 0
                                shared_num_slaves.value = 0
                                send_response(False, "Rescan failed - no slaves found")
                        except Exception as e:
                            print(f"[RESCAN] Error: {e}")
                            send_response(False, f"Rescan error: {e}")

                    elif cmd == MotorProcess.CMD_RECOVER:
                        # Manual recovery - triggered by user from UI
                        print("\n[MANUAL RECOVERY] User requested recovery...")

                        if recovery_in_progress[0]:
                            send_response(False, "Recovery already in progress")
                            continue

                        recovery_in_progress[0] = True
                        shared_moving.value = 0

                        try:
                            # Step 1: Stop PDO loop and disconnect
                            print("[MANUAL RECOVERY] Step 1: Stopping PDO and disconnecting...")
                            ec._pdo_running = False
                            if ec._pdo_thread:
                                ec._pdo_thread.join(timeout=2.0)
                            time.sleep(0.5)

                            # Step 2: Close master connection
                            print("[MANUAL RECOVERY] Step 2: Closing master connection...")
                            try:
                                ec.master.state = pysoem.INIT_STATE
                                ec.master.write_state()
                                ec.master.close()
                            except:
                                pass
                            time.sleep(1.0)

                            # Step 3: Reconnect
                            print("[MANUAL RECOVERY] Step 3: Reconnecting...")
                            ec = EtherCATController(interface)
                            if ec.connect():
                                # Re-register callbacks
                                ec.set_slaves_changed_callback(on_slaves_changed)
                                ec.set_communication_error_callback(on_communication_error)

                                shared_state.value = 1  # Connected but not enabled
                                shared_num_slaves.value = ec.slaves_count
                                shared_mode.value = ec.mode
                                if ec.interface:
                                    shared_interface.value = ec.interface.encode('utf-8')[:255]

                                print("[MANUAL RECOVERY] SUCCESS! Reconnected to EtherCAT")
                                send_event(3, -1, 0, "Recovery successful! Reconnected.")
                                send_response(True, f"Recovery successful! Reconnected with {ec.slaves_count} slave(s).", {
                                    'recovery_success': True,
                                    'num_slaves': ec.slaves_count
                                })
                            else:
                                print("[MANUAL RECOVERY] FAILED! Could not reconnect")
                                shared_state.value = 0
                                shared_num_slaves.value = 0
                                send_event(2, -1, 0x81B, "Recovery failed! Check connections.")
                                send_response(False, "Recovery FAILED! Check connections and try again.", {
                                    'recovery_success': False
                                })

                        except Exception as e:
                            print(f"[MANUAL RECOVERY] Exception: {e}")
                            import traceback
                            traceback.print_exc()
                            send_response(False, f"Recovery exception: {e}", {
                                'recovery_success': False
                            })

                        finally:
                            recovery_in_progress[0] = False
                            communication_error_flag[0] = False
                            shared_stop.value = 0

                    elif cmd == MotorProcess.CMD_CLEAR_ERROR:
                        # Clear error on specific slave
                        if data:
                            slave_idx = data.get('slave', 0)
                            method = data.get('method', 'all')
                        else:
                            slave_idx = 0
                            method = 'all'

                        if isinstance(slave_idx, str):
                            slave_idx = int(slave_idx)

                        print(f"\n[CLEAR ERROR] Slave {slave_idx}, method: {method}")
                        success, msg = ec.clear_error(slave_idx, method)
                        send_response(success, msg)

                    elif cmd == MotorProcess.CMD_SET_HOME_ALL:
                        # Set home for all slaves
                        print("\n[SET HOME ALL] Setting home for all slaves...")
                        results = ec.set_home_all()
                        success_count = sum(1 for _, s in results if s)
                        send_response(True, f"Home set for {success_count}/{len(results)} slaves")

                    elif cmd == MotorProcess.CMD_UDP_CONNECT:
                        # Start UDP receiver
                        if data:
                            udp_ip = data.get('ip', '127.0.0.1')
                            udp_port = data.get('port', 9000)
                        else:
                            udp_ip = '127.0.0.1'
                            udp_port = 9000

                        if isinstance(udp_port, str):
                            udp_port = int(udp_port)

                        # Stop existing UDP thread if any
                        if udp_thread and udp_thread.is_alive():
                            udp_shutdown[0] = True
                            udp_thread.join(timeout=1.0)

                        # Start new UDP listener
                        udp_shutdown[0] = False
                        udp_thread = threading.Thread(
                            target=udp_listener,
                            args=(udp_ip, udp_port, udp_shutdown, ec),
                            daemon=True
                        )
                        udp_thread.start()

                    elif cmd == MotorProcess.CMD_UDP_DISCONNECT:
                        # Stop UDP receiver
                        if udp_thread and udp_thread.is_alive():
                            print("\n[UDP] Stopping UDP listener...")
                            udp_shutdown[0] = True
                            udp_thread.join(timeout=1.0)
                            shared_udp_connected.value = 0
                            send_response(True, "UDP disconnected")
                        else:
                            send_response(True, "UDP was not connected")

                    elif cmd == MotorProcess.CMD_GET_ADAPTERS:
                        # Get list of available network adapters
                        print("\n[GET ADAPTERS] Listing network adapters...")
                        adapters = []
                        for a in pysoem.find_adapters():
                            adapters.append({
                                'name': a.name,
                                'desc': a.desc
                            })
                        send_response(True, f"Found {len(adapters)} adapters", {'adapters': adapters})

                    elif cmd == MotorProcess.CMD_CHANGE_INTERFACE:
                        # Change network interface
                        if data:
                            new_interface = data.get('interface')
                        else:
                            send_response(False, "No interface specified")
                            continue

                        print(f"\n[CHANGE INTERFACE] Changing to: {new_interface}")

                        # Stop UDP if running
                        if udp_thread and udp_thread.is_alive():
                            udp_shutdown[0] = True
                            udp_thread.join(timeout=1.0)

                        # Disconnect current controller
                        if ec:
                            ec.disconnect()
                            time.sleep(0.5)

                        # Connect with new interface
                        interface = new_interface
                        ec = EtherCATController(interface)
                        if ec.connect():
                            ec.set_slaves_changed_callback(on_slaves_changed)
                            ec.set_communication_error_callback(on_communication_error)
                            shared_state.value = 1
                            shared_num_slaves.value = ec.slaves_count
                            shared_mode.value = ec.mode
                            if ec.interface:
                                shared_interface.value = ec.interface.encode('utf-8')[:255]
                            send_response(True, f"Connected to {new_interface}: {ec.slaves_count} slave(s)")
                        else:
                            shared_state.value = 0
                            shared_num_slaves.value = 0
                            send_response(False, f"Failed to connect to {new_interface}")
                    
                    elif cmd == 'load_config':
                        if data:
                            filename = data.get('filename', 'config.json')
                        else:
                            filename = 'config.json'

                        import json
                        import os

                        # Try multiple paths (json folder first)
                        base_dir = os.path.dirname(os.path.abspath(__file__))
                        json_dir = os.path.join(base_dir, 'json')
                        possible_paths = [
                            os.path.join(json_dir, filename),
                            os.path.join(json_dir, os.path.basename(filename)),
                            filename,
                            os.path.join(os.getcwd(), filename),
                            os.path.join(base_dir, filename),
                        ]
                        
                        config_path = None
                        for path in possible_paths:
                            if path and os.path.exists(path):
                                config_path = path
                                break
                        
                        print(f"[load_config] Looking for: {filename}")
                        print(f"[load_config] Found at: {config_path}")
                        
                        if not config_path:
                            send_response(False, f"File not found: {filename}")
                            continue
                        
                        try:
                            with open(config_path, 'r') as f:
                                config = json.load(f)
                            
                            template = config.get('template', {})
                            print(f"[load_config] Template: {template.get('name', 'No name')}, Steps: {len(template.get('steps', []))}")
                            send_response(True, f"Loaded: {filename}", {
                                'template': template,
                                'config': config
                            })
                        except Exception as e:
                            print(f"[load_config] Error: {e}")
                            send_response(False, f"Error loading {filename}: {e}")
                    
                    elif cmd in [MotorProcess.CMD_TEMPLATE, MotorProcess.CMD_TEMPLATE_LOOP]:
                        import json
                        import os

                        # Check if config is provided from UI
                        config = None
                        config_filename = None

                        if data and isinstance(data, dict):
                            # Config passed directly from UI
                            if 'config' in data:
                                config = data.get('config')
                                print(f"[Template] Using config from UI")
                            elif 'template' in data:
                                # Just template data passed
                                config = data
                                print(f"[Template] Using template data from UI")
                            elif 'filename' in data:
                                # Filename specified - will load below
                                config_filename = data.get('filename')
                                print(f"[Template] Will load config from file: {config_filename}")

                        # If no config from UI, load from file
                        if not config:
                            config_path = None
                            base_dir = os.path.dirname(os.path.abspath(__file__))
                            json_dir = os.path.join(base_dir, 'json')

                            # If filename was specified, try that first
                            if config_filename:
                                possible_paths = [
                                    os.path.join(json_dir, config_filename),
                                    os.path.join(json_dir, os.path.basename(config_filename)),
                                    config_filename,
                                    os.path.join(os.getcwd(), config_filename),
                                ]
                            else:
                                # Default search order (json folder first)
                                possible_paths = [
                                    os.path.join(json_dir, 'config_both.json'),
                                    os.path.join(json_dir, 'config.json'),
                                    'config_both.json',
                                    'config.json',
                                    os.path.join(os.getcwd(), 'config_both.json'),
                                    os.path.join(os.getcwd(), 'config.json'),
                                ]

                            for path in possible_paths:
                                if os.path.exists(path):
                                    config_path = path
                                    break

                            if not config_path:
                                send_response(False, f"Config file not found: {config_filename or 'config_both.json/config.json'}")
                                continue

                            try:
                                with open(config_path, 'r') as f:
                                    config = json.load(f)
                                print(f"[Template] Loaded config from: {config_path}")
                            except Exception as e:
                                send_response(False, f"Failed to load {config_path}: {e}")
                                continue

                        template = config.get('template', {})
                        steps = template.get('steps', [])
                        positions = config.get('positions', {})
                        
                        # Get slave assignments
                        slaves_config = config.get('slaves', {})
                        movement_slaves = slaves_config.get('movement_slaves', [])
                        rotation_slaves = slaves_config.get('rotation_slaves', [])

                        # Get speed settings
                        speed_config = config.get('speed', {})
                        movement_speed = speed_config.get('movement_speed', {})
                        rotation_speed = speed_config.get('rotation_speed', {})

                        # Pre-configure speeds for ALL slaves before template starts
                        print(f"\n  Configuring speeds for {ec.slaves_count} slaves...")

                        for slave_idx in range(ec.slaves_count):
                            # Determine speed based on slave role
                            if slave_idx in movement_slaves:
                                base_speed = movement_speed
                                role = "movement"
                            elif slave_idx in rotation_slaves:
                                base_speed = rotation_speed
                                role = "rotation"
                            else:
                                # Default to movement speed if not explicitly assigned
                                base_speed = movement_speed
                                role = "unassigned"

                            # Get speed values from config
                            velocity = base_speed.get('velocity', 80000)
                            accel = base_speed.get('acceleration', 6000)
                            decel = base_speed.get('deceleration', 6000)
                            csp_vel = base_speed.get('csp_velocity', base_speed.get('csp_max_step', 800))

                            print(f"  Slave {slave_idx} ({role}):")

                            if ec.mode == ec.MODE_CSP:
                                ec.set_csp_velocity(csp_vel, slave_idx)
                                print(f"    CSP velocity: {csp_vel} units/ms ({csp_vel * 1000} units/s)")
                            else:
                                # PP mode - configure SDO speeds for this slave
                                ec.configure_speed(slave_idx, velocity, accel, decel)
                                print(f"    PP velocity: {velocity}, accel: {accel}, decel: {decel}")

                        if not steps:
                            send_response(False, "No template steps defined")
                            continue
                        
                        # Determine loop behavior
                        loop_mode = (cmd == MotorProcess.CMD_TEMPLATE_LOOP)
                        loop = loop_mode or template.get('loop', False)
                        loop_count = template.get('loop_count', 1)
                        
                        # DON'T clear stop flag here - if user already pressed stop, respect it
                        if shared_stop.value == 1:
                            print("[Template] Stop flag already set - not starting template")
                            send_response(False, "Template not started - stop was requested")
                            continue

                        print(f"\n[Template] Starting: {template.get('name', 'Unnamed')}")
                        print(f"  Operation mode: {template.get('operation_mode', 'both')}")
                        print(f"  Movement slaves: {movement_slaves}")
                        print(f"  Rotation slaves: {rotation_slaves}")
                        print(f"  Steps: {len(steps)}, Loop: {loop}, Count: {loop_count if not loop_mode else 'Infinite'}")

                        if movement_speed:
                            print(f"  Movement speed: vel={movement_speed.get('velocity')}, accel={movement_speed.get('acceleration')}")
                        if rotation_speed:
                            print(f"  Rotation speed: vel={rotation_speed.get('velocity')}, accel={rotation_speed.get('acceleration')}")

                        # Track template start time
                        template_start_time = time.time()

                        # Send template start notification (triggers UI to reset timings)
                        send_response(True, f"Running template: {len(steps)} steps (Loop: {loop_mode or loop})", {
                            'template_start': {
                                'name': template.get('name', 'Unnamed'),
                                'steps': len(steps),
                                'loop': loop_mode or loop
                            }
                        })
                        shared_moving.value = 1
                        
                        try:
                            iterations = 999999 if loop_mode else (loop_count if loop else 1)
                            for iteration in range(iterations):
                                # Check stop flag at iteration level
                                if shared_stop.value == 1:
                                    print("\n  [STOPPED] Template interrupted by user")
                                    break
                                
                                if loop or loop_mode:
                                    print(f"\n  === Iteration {iteration + 1} ===")
                                    
                                for step_idx, step in enumerate(steps):
                                    # Check stop flag before each step
                                    if shared_stop.value == 1:
                                        print("\n  [STOPPED] Template interrupted by user")
                                        break

                                    # Check for faults before executing step
                                    fault_detected = False
                                    for i in range(ec.slaves_count):
                                        if ec.has_fault(i):
                                            error_code = ec.read_error_code(i)
                                            error_name = ec.get_error_name(error_code)
                                            print(f"\n  [FAULT BEFORE STEP] Slave {i}: {error_name}")
                                            # Send error event to UI
                                            send_event(2, i, error_code, f"Slave {i}: {error_name}")
                                            shared_stop.value = 1
                                            fault_detected = True
                                            break

                                    if fault_detected:
                                        break
                                    
                                    step_type = step.get('type', 'all')
                                    delay = step.get('delay', 1.0)
                                    name = step.get('name', f'Step {step_idx + 1}')
                                    
                                    # Get position reference
                                    pos_ref = step.get('position')
                                    pos_rot_ref = step.get('position_rotation')
                                    
                                    # Resolve position from positions dict
                                    pos_values = positions.get(pos_ref, []) if pos_ref else []
                                    pos_rot_values = positions.get(pos_rot_ref, []) if pos_rot_ref else []
                                    
                                    print(f"\n  [{step_idx + 1}/{len(steps)}] {name} (type: {step_type})")

                                    # Track step start time
                                    step_start_time = time.time()

                                    # Collect all slave positions for SIMULTANEOUS movement
                                    slave_positions = []  # List of (slave_idx, position)
                                    movement_slave_positions = []  # Only movement slaves for move order calc
                                    moving_slaves = []

                                    # Handle 'home' type - move all slaves to home positions
                                    if step_type == 'home':
                                        # Collect movement slaves home positions
                                        home_m = positions.get('home_pos_M', [0] * len(movement_slaves))
                                        for i, slave_idx in enumerate(movement_slaves):
                                            if slave_idx < ec.slaves_count:
                                                pos = home_m[i] if i < len(home_m) else 0
                                                print(f"    Movement Slave {slave_idx} -> {pos} (home)")
                                                slave_positions.append((slave_idx, pos))
                                                movement_slave_positions.append((slave_idx, pos))
                                                moving_slaves.append(slave_idx)

                                        # Collect rotation slaves home positions
                                        home_r = positions.get('home_pos_R', [0] * len(rotation_slaves))
                                        for i, slave_idx in enumerate(rotation_slaves):
                                            if slave_idx < ec.slaves_count:
                                                pos = home_r[i] if i < len(home_r) else 0
                                                print(f"    Rotation Slave {slave_idx} -> {pos} (home)")
                                                slave_positions.append((slave_idx, pos))
                                                moving_slaves.append(slave_idx)

                                    # Handle movement positions
                                    elif step_type in ['movement', 'all'] and pos_values:
                                        for i, pos in enumerate(pos_values):
                                            if i < len(movement_slaves):
                                                slave_idx = movement_slaves[i]
                                                if slave_idx < ec.slaves_count:
                                                    print(f"    Movement Slave {slave_idx} -> {pos}")
                                                    slave_positions.append((slave_idx, pos))
                                                    movement_slave_positions.append((slave_idx, pos))
                                                    moving_slaves.append(slave_idx)

                                    # Handle rotation positions (can be combined with movement in 'all' type)
                                    if step_type in ['rotation', 'all']:
                                        rot_values = pos_rot_values if pos_rot_values else (pos_values if step_type == 'rotation' else [])
                                        for i, pos in enumerate(rot_values):
                                            if i < len(rotation_slaves):
                                                slave_idx = rotation_slaves[i]
                                                if slave_idx < ec.slaves_count:
                                                    print(f"    Rotation Slave {slave_idx} -> {pos}")
                                                    slave_positions.append((slave_idx, pos))
                                                    if slave_idx not in moving_slaves:
                                                        moving_slaves.append(slave_idx)

                                    # Calculate move order for movement slaves only
                                    move_order = []
                                    is_spreading = False
                                    if movement_slave_positions and len(movement_slave_positions) > 1:
                                        move_order, is_spreading = ec.calculate_move_order(movement_slave_positions)
                                        print(f"    Move order: {move_order} ({'Spreading' if is_spreading else 'Converging'})")
                                    elif movement_slave_positions:
                                        move_order = [movement_slave_positions[0][0]]

                                    # Send step start notification with move order
                                    send_response(True, f"Executing: {name}", {
                                        'template_step': {
                                            'index': step_idx + 1,
                                            'total': len(steps),
                                            'name': name,
                                            'type': step_type,
                                            'event': 'start',
                                            'move_order': move_order,
                                            'is_spreading': is_spreading
                                        }
                                    })

                                    # Execute SIMULTANEOUS movement for all collected positions
                                    if slave_positions:
                                        print(f"    Starting SIMULTANEOUS move for {len(slave_positions)} slaves...")
                                        ec.move_multiple_to_meters(slave_positions)

                                        # Wait for all slaves to reach target
                                        print(f"    Waiting for slaves {moving_slaves} to reach target...")
                                        success = wait_for_target_reached(moving_slaves, timeout=30.0)

                                        if not success:
                                            print(f"    [FAILED] Movement failed or interrupted")
                                            shared_stop.value = 1
                                            break

                                        print(f"    All slaves reached target")

                                    
                                    # Apply delay AFTER reaching target
                                    if delay > 0:
                                        print(f"    Delay: {delay}s")
                                        delay_steps = int(delay * 10)
                                        for _ in range(delay_steps):
                                            # Check for priority commands
                                            if check_priority_commands():
                                                shared_stop.value = 1
                                                break
                                            if shared_stop.value == 1:
                                                break
                                            # Update positions during delay
                                            update_shared_status()
                                            time.sleep(0.1)

                                    # Calculate step time and send complete notification
                                    step_time_taken = time.time() - step_start_time
                                    print(f"    Step completed in {step_time_taken:.1f}s")

                                    # Send step complete notification
                                    send_response(True, f"Completed: {name} ({step_time_taken:.1f}s)", {
                                        'template_step': {
                                            'index': step_idx + 1,
                                            'total': len(steps),
                                            'name': name,
                                            'type': step_type,
                                            'event': 'complete',
                                            'time_taken': step_time_taken
                                        }
                                    })

                                    if shared_stop.value == 1:
                                        break
                                
                                if shared_stop.value == 1:
                                    break
                                    
                                if not loop and not loop_mode:
                                    break
                                    
                        except Exception as e:
                            print(f"Template error: {e}")
                            import traceback
                            traceback.print_exc()
                            shared_stop.value = 1

                        shared_moving.value = 0

                        # Clear any queued commands that arrived during template execution
                        # (except for priority commands which were already processed)
                        cleared_count = 0
                        while True:
                            try:
                                discarded = cmd_queue.get_nowait()
                                cleared_count += 1
                                print(f"[Template] Discarded queued command: {discarded.get('cmd')}")
                            except:
                                break
                        if cleared_count > 0:
                            print(f"[Template] Cleared {cleared_count} queued commands")

                        # Calculate total template time
                        template_total_time = time.time() - template_start_time

                        if shared_stop.value == 1:
                            # Check if stopped due to fault
                            fault_msg = None
                            for i in range(ec.slaves_count):
                                if ec.has_fault(i):
                                    error_code = ec.read_error_code(i)
                                    error_name = ec.get_error_name(error_code)
                                    fault_msg = f"Template stopped - Slave {i} fault: {error_name}"
                                    # Send error event to UI (if not already sent)
                                    send_event(2, i, error_code, f"Slave {i}: {error_name}")
                                    break

                            shared_stop.value = 0  # Reset stop flag
                            send_response(True, fault_msg if fault_msg else "Template stopped by user")
                        else:
                            # Send template complete with total time
                            print(f"\n[Template] Complete in {template_total_time:.1f}s")
                            send_response(True, f"Template complete ({template_total_time:.1f}s)", {
                                'template_complete': {
                                    'total_time': template_total_time
                                }
                            })
                
                except:
                    pass  # Queue empty, continue
                
                time.sleep(0.001)
        
        except Exception as e:
            send_response(False, f"Error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            if ec:
                ec.disconnect()
            shared_state.value = 0


if __name__ == "__main__":
    # Test
    mp = MotorProcess()
    mp.start()
    
    try:
        while True:
            status = mp.get_status()
            print(f"State: {status['state']}, Positions: {status['positions']}")
            time.sleep(1)
    except KeyboardInterrupt:
        mp.stop()