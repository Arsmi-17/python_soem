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
import os
import glob

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
    CMD_OSC_CONNECT = 'osc_connect'  # Start OSC sender/receiver
    CMD_OSC_DISCONNECT = 'osc_disconnect'  # Stop OSC
    CMD_LIST_CONFIGS = 'list_configs'  # List JSON config files
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

        # OSC state (for PP mode)
        self.shared_osc_connected = Value(ctypes.c_int, 0)  # 0=disconnected, 1=connected
        self.shared_osc_mode = Value(ctypes.c_int, 0)  # 0=off, 1=receive, 2=send, 3=both
        self.shared_osc_ip = Array(ctypes.c_char, 64)
        self.shared_osc_port = Value(ctypes.c_int, 8000)

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
                self.shared_event_counter,
                self.shared_osc_connected,
                self.shared_osc_mode,
                self.shared_osc_ip,
                self.shared_osc_port
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

        # Get OSC state
        try:
            osc_ip = self.shared_osc_ip.value.decode('utf-8')
        except:
            osc_ip = "0.0.0.0"

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
            'osc_connected': bool(self.shared_osc_connected.value),
            'osc_mode': self.shared_osc_mode.value,
            'osc_ip': osc_ip,
            'osc_port': self.shared_osc_port.value,
            'event': event
        }

    def clear_event(self):
        """Clear the current event after UI has processed it"""
        self.shared_event_type.value = 0

    @staticmethod
    def _run_process(interface, cmd_queue, resp_queue, shared_data, shared_state,
                     shared_moving, shared_num_slaves, shared_mode, shared_interface, shared_stop, running,
                     shared_udp_connected, shared_udp_ip, shared_udp_port,
                     shared_event_type, shared_event_slave, shared_event_code, shared_event_msg, shared_event_counter,
                     shared_osc_connected, shared_osc_mode, shared_osc_ip, shared_osc_port):
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

        # Cooldown for communication error callbacks to prevent spam
        last_comm_error_time = [0]
        comm_error_cooldown = 5.0  # 5 seconds cooldown between error callbacks

        def on_communication_error(error_msg):
            """
            Callback when communication error detected (Er81b, WKC mismatch, timing critical)
            This triggers auto-recovery attempt
            """
            nonlocal ec

            if recovery_in_progress[0]:
                print(f"[COMM ERROR] Recovery already in progress, ignoring: {error_msg}")
                return

            # Check cooldown to prevent rapid-fire error callbacks
            current_time = time.time()
            if current_time - last_comm_error_time[0] < comm_error_cooldown:
                print(f"[COMM ERROR] Ignoring (cooldown active): {error_msg}")
                return

            last_comm_error_time[0] = current_time

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

        # =====================================================
        # OSC (Open Sound Control) for PP Mode
        # =====================================================
        # OSC modes: 0=off, 1=receive only, 2=send only, 3=both
        OSC_MODE_OFF = 0
        OSC_MODE_RECEIVE = 1
        OSC_MODE_SEND = 2
        OSC_MODE_BOTH = 3

        osc_send_socket = [None]  # Use list for mutable reference in nested functions
        osc_target_ip = ['']
        osc_target_port = [8000]
        osc_recv_thread = [None]
        osc_recv_shutdown = [False]

        def add_osc_log(log_type, message):
            """Send OSC log entry to UI. log_type: 'send', 'recv', 'info'"""
            entry = {
                'type': log_type,
                'message': message,
                'time': time.time()
            }
            send_response(True, f"[OSC] {message}", {'osc_log': entry})

        def osc_send(address, *args):
            """
            Send OSC message via UDP broadcast.
            Simple OSC implementation without external library.
            Address format: /path/to/handler
            Args: floats or ints
            """
            if shared_osc_mode.value not in [OSC_MODE_SEND, OSC_MODE_BOTH]:
                return
            if osc_send_socket[0] is None:
                return

            try:
                # Build OSC message manually
                # OSC format: address (null-padded to 4 bytes), typetag string, arguments

                # Pad address to multiple of 4 bytes
                addr_bytes = address.encode('utf-8') + b'\x00'
                while len(addr_bytes) % 4 != 0:
                    addr_bytes += b'\x00'

                # Build type tag string
                type_tag = ','
                for arg in args:
                    if isinstance(arg, float):
                        type_tag += 'f'
                    elif isinstance(arg, int):
                        type_tag += 'i'
                    else:
                        type_tag += 's'

                type_bytes = type_tag.encode('utf-8') + b'\x00'
                while len(type_bytes) % 4 != 0:
                    type_bytes += b'\x00'

                # Build argument data
                import struct
                arg_bytes = b''
                for arg in args:
                    if isinstance(arg, float):
                        arg_bytes += struct.pack('>f', arg)
                    elif isinstance(arg, int):
                        arg_bytes += struct.pack('>i', arg)
                    else:
                        # String: null-terminated and padded
                        s = str(arg).encode('utf-8') + b'\x00'
                        while len(s) % 4 != 0:
                            s += b'\x00'
                        arg_bytes += s

                # Combine all parts
                osc_message = addr_bytes + type_bytes + arg_bytes

                # Send to target IP
                osc_send_socket[0].sendto(osc_message, (osc_target_ip[0], osc_target_port[0]))

                # Log the sent message
                args_str = ' '.join(str(a) for a in args) if args else ''
                add_osc_log('send', f"{address} {args_str}".strip())

            except Exception as e:
                print(f"[OSC] Send error: {e}")

        def osc_send_slave_move(slave_idx, position_meters):
            """Send OSC message when slave moves: /slave_move/<slave_id>/<position>"""
            osc_send(f"/slave_move/{slave_idx}/{position_meters:.4f}")

        def osc_send_movement(slave_idx, position_meters):
            """Send OSC message when slave is moving: /movement/<slave_id> with position value"""
            osc_send(f"/movement/{slave_idx}", float(position_meters))

        def osc_send_template_step(step_index):
            """Send OSC message when template step starts: /template_step with step_no as argument"""
            osc_send("/template_step", step_index)

        def osc_send_template_complete():
            """Send OSC message when template completes: /template_complete"""
            osc_send("/template_complete")

        def parse_osc_message(data):
            """
            Parse incoming OSC message.
            Returns (address, args) tuple or (None, None) if invalid.
            """
            try:
                import struct

                # Find address (null-terminated, padded to 4 bytes)
                null_idx = data.find(b'\x00')
                if null_idx == -1:
                    return None, None

                address = data[:null_idx].decode('utf-8')

                # Skip to next 4-byte boundary
                idx = null_idx + 1
                while idx % 4 != 0:
                    idx += 1

                # Parse type tag
                if idx >= len(data) or data[idx:idx+1] != b',':
                    return address, []

                type_tag_end = data.find(b'\x00', idx)
                type_tag = data[idx+1:type_tag_end].decode('utf-8')  # Skip the comma

                # Skip to next 4-byte boundary
                idx = type_tag_end + 1
                while idx % 4 != 0:
                    idx += 1

                # Parse arguments
                args = []
                for t in type_tag:
                    if t == 'f':
                        val = struct.unpack('>f', data[idx:idx+4])[0]
                        args.append(val)
                        idx += 4
                    elif t == 'i':
                        val = struct.unpack('>i', data[idx:idx+4])[0]
                        args.append(val)
                        idx += 4
                    elif t == 's':
                        str_end = data.find(b'\x00', idx)
                        val = data[idx:str_end].decode('utf-8')
                        args.append(val)
                        idx = str_end + 1
                        while idx % 4 != 0:
                            idx += 1

                return address, args

            except Exception as e:
                print(f"[OSC] Parse error: {e}")
                return None, None

        # Load OSC config from JSON file
        def load_osc_config():
            """Load OSC configuration from rotorscope_config.json"""
            import os as os_module  # Local import to avoid scope issues
            config_path = os_module.path.join(os_module.path.dirname(__file__), 'osc', 'rotorscope_config.json')
            default_config = {
                "speed": {"velocity": 80000, "acceleration": 40000, "deceleration": 40000},
                "start_value_map": {"0": 0, "1": 0.1, "2": 0.5, "3": 1.0, "4": 1.5, "5": 2.0, "6": 3.0},
                "position_tolerance": 0.002,
                "broadcast_interval": 0.05
            }
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    print(f"[OSC] Loaded config from {config_path}")
                    return config
            except Exception as e:
                print(f"[OSC] Could not load config: {e}, using defaults")
                return default_config

        # Load OSC config
        osc_config = load_osc_config()

        # OSC value to position mapping for /start command (convert string keys to int)
        OSC_START_VALUE_MAP = {int(k): v for k, v in osc_config.get('start_value_map', {}).items()}

        # OSC movement speed settings from config
        osc_speed = osc_config.get('speed', {})
        OSC_MOVE_SPEED = osc_speed.get('velocity', 80000)
        OSC_MOVE_ACCEL = osc_speed.get('acceleration', OSC_MOVE_SPEED // 2)
        OSC_MOVE_DECEL = osc_speed.get('deceleration', OSC_MOVE_SPEED // 2)

        # Position tolerance and broadcast interval from config
        OSC_POSITION_TOLERANCE = osc_config.get('position_tolerance', 0.002)
        OSC_BROADCAST_INTERVAL = osc_config.get('broadcast_interval', 0.05)

        # Track active OSC movement for position broadcasting
        osc_movement_active = [False]  # Is there an active /start movement?
        osc_movement_slave = [0]  # Which slave is moving
        osc_movement_target = [0.0]  # Target position
        osc_movement_value = [0]  # Original /start value (for /reached message)
        osc_movement_thread = [None]  # Thread for position broadcasting

        def osc_movement_broadcaster(controller):
            """
            Thread function that broadcasts /movement messages while slave is moving.
            When target is reached, sends /reached message.
            """
            slave_idx = osc_movement_slave[0]
            target_pos = osc_movement_target[0]
            start_value = osc_movement_value[0]

            print(f"[OSC] Movement broadcaster started for slave {slave_idx} -> {target_pos}m (value={start_value})")

            last_sent_pos = None

            while osc_movement_active[0] and running.value:
                try:
                    # Get current position
                    current_pos = controller.read_position_meters(slave_idx)

                    # Send /movement message (slave_id, current_position)
                    # Only send if position changed significantly (0.1mm threshold)
                    if last_sent_pos is None or abs(current_pos - last_sent_pos) > 0.0001:
                        osc_send("/movement", slave_idx, float(current_pos))
                        last_sent_pos = current_pos

                    # Check if target reached (using config tolerance)
                    if abs(current_pos - target_pos) <= OSC_POSITION_TOLERANCE:
                        print(f"[OSC] Target reached! Position: {current_pos:.4f}m, Target: {target_pos:.4f}m")

                        # Send final /movement at exact target
                        osc_send("/movement", slave_idx, float(target_pos))

                        # Send /reached message with original value
                        osc_send("/reached", start_value)
                        add_osc_log('send', f"/reached {start_value}")
                        print(f"[OSC] Sent /reached {start_value}")

                        osc_movement_active[0] = False
                        break

                    time.sleep(OSC_BROADCAST_INTERVAL)

                except Exception as e:
                    print(f"[OSC] Movement broadcaster error: {e}")
                    break

            print(f"[OSC] Movement broadcaster stopped")

        def osc_receiver(ip, port, shutdown_flag, controller):
            """
            Listen for incoming OSC messages.
            Supported commands:
            - /start [value]                           - Move slave0 to mapped position (1->0.1, 2->0.5, 3->1.0, 4->1.5, 5->2.0)
            - /move [slave] [position]                 - Move slave to position in meters
            - /slave_move/<slave_id> <position_float>  - Move slave to position (legacy format)
            - /home                                    - Move all slaves to home (0m)
            - /template_run                            - Run current template
            - /template_stop                           - Stop template
            - /stop                                    - Emergency stop
            - /enable                                  - Enable all drives
            - /disable                                 - Disable all drives
            - /reset                                   - Reset faults
            """
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)

            try:
                sock.bind((ip, port))
            except Exception as e:
                print(f"[OSC] Receiver failed to bind: {e}")
                send_response(False, f"OSC bind failed: {e}")
                return

            print(f"[OSC] Receiver started on {ip}:{port}")
            print(f"[OSC] Value mapping for /start: {OSC_START_VALUE_MAP}")

            while not shutdown_flag[0] and running.value:
                try:
                    data, addr = sock.recvfrom(4096)

                    address, args = parse_osc_message(data)
                    if address is None:
                        continue

                    # Log received message
                    args_str = ' '.join(str(a) for a in args) if args else ''
                    add_osc_log('recv', f"{address} [{args_str}]".strip())
                    print(f"[OSC] Received: {address} {args}")

                    # =========================================================
                    # /start [value] - Move slave0 to mapped position
                    # =========================================================
                    if address == '/start':
                        if args and len(args) > 0:
                            value = int(args[0])
                            target_position = OSC_START_VALUE_MAP.get(value)

                            if target_position is not None:
                                print(f"[OSC] /start [{value}] -> Moving slave0 to {target_position}m")
                                print(f"[OSC] Speed: vel={OSC_MOVE_SPEED}, accel={OSC_MOVE_ACCEL}, decel={OSC_MOVE_DECEL}")
                                add_osc_log('info', f"/start [{value}] -> slave0 to {target_position}m")

                                if controller.slaves_count > 0:
                                    if controller.is_enabled(0):
                                        # Stop any existing movement broadcaster
                                        osc_movement_active[0] = False
                                        if osc_movement_thread[0] and osc_movement_thread[0].is_alive():
                                            osc_movement_thread[0].join(timeout=0.5)

                                        # Apply speed settings (vel, accel=vel/2, decel=vel/2)
                                        controller.configure_speed(0, OSC_MOVE_SPEED, OSC_MOVE_ACCEL, OSC_MOVE_DECEL)

                                        # Set movement tracking variables
                                        osc_movement_slave[0] = 0
                                        osc_movement_target[0] = target_position
                                        osc_movement_value[0] = value
                                        osc_movement_active[0] = True

                                        # Start movement
                                        controller.move_to_meters(0, target_position)

                                        # Start position broadcaster thread
                                        osc_movement_thread[0] = threading.Thread(
                                            target=osc_movement_broadcaster,
                                            args=(controller,),
                                            daemon=True
                                        )
                                        osc_movement_thread[0].start()
                                    else:
                                        print(f"[OSC] Slave 0 not enabled")
                                        add_osc_log('warn', "Slave 0 not enabled")
                                else:
                                    print(f"[OSC] No slaves available")
                                    add_osc_log('warn', "No slaves available")
                            else:
                                print(f"[OSC] /start unknown value: {value}. Valid: {list(OSC_START_VALUE_MAP.keys())}")
                                add_osc_log('warn', f"Unknown value: {value}")
                        else:
                            print(f"[OSC] /start requires a value argument")
                            add_osc_log('warn', "/start requires a value")

                    # =========================================================
                    # /move [slave] [position] - Move slave to position
                    # =========================================================
                    elif address == '/move':
                        if len(args) >= 2:
                            slave_idx = int(args[0])
                            position = float(args[1])

                            print(f"[OSC] /move slave{slave_idx} to {position}m")
                            add_osc_log('info', f"/move slave{slave_idx} to {position}m")

                            if slave_idx >= 0 and slave_idx < controller.slaves_count:
                                if controller.is_enabled(slave_idx):
                                    controller.move_to_meters(slave_idx, position)
                                else:
                                    print(f"[OSC] Slave {slave_idx} not enabled")
                            else:
                                print(f"[OSC] Invalid slave index: {slave_idx}")
                        else:
                            print(f"[OSC] /move requires 2 arguments: slave, position")

                    # =========================================================
                    # /home - Move all slaves to home (0m)
                    # =========================================================
                    elif address == '/home':
                        print("[OSC] /home -> Moving all to home (0m)")
                        add_osc_log('info', "/home -> All slaves to 0m")
                        for i in range(controller.slaves_count):
                            if controller.is_enabled(i):
                                controller.move_to_meters(i, 0.0)

                    # =========================================================
                    # /slave_move/<slave_id> [position] - Legacy format
                    # =========================================================
                    elif address.startswith('/slave_move/'):
                        # Format: /slave_move/<slave_id> with float arg for position
                        parts = address.split('/')
                        if len(parts) >= 3:
                            try:
                                slave_idx = int(parts[2])
                                if args and len(args) > 0:
                                    position = float(args[0])
                                elif len(parts) >= 4:
                                    position = float(parts[3])
                                else:
                                    continue

                                if slave_idx < 0 or slave_idx >= controller.slaves_count:
                                    print(f"[OSC] Invalid slave: {slave_idx}")
                                    continue

                                if controller.is_enabled(slave_idx):
                                    controller.move_to_meters(slave_idx, position)
                                    print(f"[OSC] Moving slave {slave_idx} to {position:.4f}m")
                                else:
                                    print(f"[OSC] Slave {slave_idx} not enabled")

                            except (ValueError, IndexError) as e:
                                print(f"[OSC] Parse error for slave_move: {e}")

                    elif address == '/template_run':
                        print("[OSC] Triggering template run")
                        cmd_queue.put({'cmd': MotorProcess.CMD_TEMPLATE})

                    elif address == '/template_stop' or address == '/stop':
                        print("[OSC] Triggering stop")
                        shared_stop.value = 1

                    elif address == '/enable':
                        print("[OSC] Triggering enable")
                        cmd_queue.put({'cmd': MotorProcess.CMD_ENABLE})

                    elif address == '/disable':
                        print("[OSC] Triggering disable")
                        cmd_queue.put({'cmd': MotorProcess.CMD_DISABLE})

                    elif address == '/reset':
                        print("[OSC] Triggering reset")
                        cmd_queue.put({'cmd': MotorProcess.CMD_RESET})

                except socket.timeout:
                    continue
                except Exception as e:
                    if not shutdown_flag[0]:
                        print(f"[OSC] Receiver error: {e}")

            sock.close()
            print("[OSC] Receiver stopped")

        def start_osc(mode, send_ip, send_port, recv_ip, recv_port):
            """Start OSC sender and/or receiver based on mode with separate ports"""
            nonlocal osc_send_socket, osc_target_ip, osc_target_port, osc_recv_thread, osc_recv_shutdown

            # Stop existing OSC if any
            stop_osc()

            osc_target_ip[0] = send_ip
            osc_target_port[0] = send_port
            shared_osc_mode.value = mode
            shared_osc_ip.value = recv_ip.encode('utf-8')[:63]
            shared_osc_port.value = recv_port

            # Start sender (direct UDP to target IP, not broadcast)
            if mode in [OSC_MODE_SEND, OSC_MODE_BOTH]:
                try:
                    osc_send_socket[0] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    # Only enable broadcast if sending to broadcast address
                    if send_ip in ['255.255.255.255', '<broadcast>']:
                        osc_send_socket[0].setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    print(f"[OSC] Sender started (sending to {send_ip}:{send_port})")
                except Exception as e:
                    print(f"[OSC] Failed to create send socket: {e}")
                    osc_send_socket[0] = None

            # Start receiver
            if mode in [OSC_MODE_RECEIVE, OSC_MODE_BOTH]:
                osc_recv_shutdown[0] = False
                osc_recv_thread[0] = threading.Thread(
                    target=osc_receiver,
                    args=(recv_ip, recv_port, osc_recv_shutdown, ec),
                    daemon=True
                )
                osc_recv_thread[0].start()

            shared_osc_connected.value = 1
            mode_str = {OSC_MODE_RECEIVE: 'Receive', OSC_MODE_SEND: 'Send', OSC_MODE_BOTH: 'Both'}[mode]
            send_response(True, f"OSC started ({mode_str}) - Send: {send_ip}:{send_port}, Recv: {recv_ip}:{recv_port}")

        def stop_osc():
            """Stop OSC sender and receiver"""
            nonlocal osc_send_socket, osc_recv_thread, osc_recv_shutdown

            # Stop receiver
            if osc_recv_thread[0] and osc_recv_thread[0].is_alive():
                osc_recv_shutdown[0] = True
                osc_recv_thread[0].join(timeout=1.0)
                osc_recv_thread[0] = None

            # Close sender socket
            if osc_send_socket[0]:
                try:
                    osc_send_socket[0].close()
                except:
                    pass
                osc_send_socket[0] = None

            shared_osc_connected.value = 0
            shared_osc_mode.value = OSC_MODE_OFF
            print("[OSC] Stopped")

        def update_shared_status(send_osc_movement=False):
            """Update shared memory with current status (uses fast PDO reads, no SDO)

            Args:
                send_osc_movement: If True and OSC is enabled, send movement OSC messages for all slaves
            """
            if ec:
                for i in range(min(ec.slaves_count, 8)):
                    pos_meters = ec.read_position_meters(i)
                    shared_data[i] = pos_meters        # [0-7]: positions (cached PDO)
                    status_word = ec.read_status(i)
                    shared_data[8 + i] = float(status_word)      # [8-15]: status words (cached PDO)

                    # Only read error code via SDO when fault is detected (to avoid communication errors)
                    # Fault bit is bit 3 (0x0008) of status word
                    if status_word & 0x0008:
                        try:
                            error_code = ec.read_error_code(i)
                            shared_data[20 + i] = float(error_code)  # [20-27]: error codes
                        except:
                            pass  # Keep previous error code if read fails

                    # Send OSC movement message if enabled and requested
                    if send_osc_movement and shared_osc_connected.value == 1:
                        osc_send_movement(i, pos_meters)

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
            last_warning_time = {}  # Track warning time per slave
            position_error_count = {}  # Track consecutive position errors per slave
            max_position_error_m = 0.5  # 500mm - if error exceeds this, something is very wrong

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

                # Update positions in shared memory and send OSC movement messages
                update_shared_status(send_osc_movement=True)
                
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

                            if error_m > max_position_error_m:
                                # Critical error - position way off, likely mechanical issue or wrong target
                                print(f"    [CRITICAL] Slave {idx} position error: {error_m*1000:.2f}mm exceeds {max_position_error_m*1000:.0f}mm limit!")
                                print(f"               Actual: {actual_pos:.4f}m, Target: {target_pos:.4f}m")
                                position_error_count[idx] = position_error_count.get(idx, 0) + 1

                                # After 10 consecutive critical errors, abort
                                if position_error_count.get(idx, 0) >= 10:
                                    print(f"    [ABORT] Slave {idx} has persistent position error - aborting template")
                                    send_event(2, idx, 0xFFFF, f"Slave {idx}: Position error {error_m*1000:.0f}mm")
                                    shared_stop.value = 1
                                    return False
                                position_ok = False
                                break
                            elif error_m > 0.002:  # 2mm tolerance
                                position_ok = False
                                # Only warn once per second per slave to avoid spam
                                now = time.time()
                                if now - last_warning_time.get(idx, 0) > 1.0:
                                    print(f"    [WARNING] Slave {idx} position error: {error_m*1000:.2f}mm (target: {target_pos:.4f}m)")
                                    last_warning_time[idx] = now
                                break
                            else:
                                # Position OK - reset error counter
                                position_error_count[idx] = 0

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
                    # Send OSC movement messages when motor is moving
                    update_shared_status(send_osc_movement=(shared_moving.value == 1))
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
                                'slaves_found': ec.slaves_count
                            })
                        else:
                            print("[AUTO-RECOVERY] FAILED! Could not reconnect")
                            shared_state.value = 0
                            shared_num_slaves.value = 0
                            send_event(2, -1, 0x81B, "Auto-recovery failed! Manual restart required.")
                            send_response(False, "Auto-recovery FAILED! Please restart the program.", {
                                'recovery_success': False,
                                'error_msg': 'Could not reconnect to EtherCAT'
                            })

                    except Exception as e:
                        print(f"[AUTO-RECOVERY] Exception during recovery: {e}")
                        import traceback
                        traceback.print_exc()
                        send_event(2, -1, 0x81B, f"Auto-recovery exception: {e}")
                        send_response(False, f"Auto-recovery exception: {e}", {
                            'recovery_success': False,
                            'error_msg': str(e)
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
                        print(f"[MOVE] Received move command with data: {data}")
                        if data:
                            positions = data.get('positions', [])
                            slave = data.get('slave', None)
                            print(f"[MOVE] Positions: {positions}, Slave: {slave} (type: {type(slave)})")

                            shared_moving.value = 1

                            if slave is not None and slave != '':
                                # Move specific slave
                                slave_idx = int(slave)
                                print(f"[MOVE] Moving specific slave {slave_idx} to position {positions[0] if positions else 'N/A'}")
                                if slave_idx < ec.slaves_count and len(positions) > 0:
                                    ec.move_to_meters(slave_idx, positions[0])
                                    print(f"[MOVE] Command executed for Slave {slave_idx} to {positions[0]}m")
                            else:
                                # Move all slaves (for template use)
                                for i, pos in enumerate(positions):
                                    if i < ec.slaves_count:
                                        ec.move_to_meters(i, pos)
                            
                            time.sleep(0.5)
                            shared_moving.value = 0
                            send_response(True, f"Move command sent")

                    elif cmd == 'move_all_home':
                        # Move all slaves to home (0m) position
                        print(f"[MOVE ALL HOME] Moving {ec.slaves_count} slaves to 0m")
                        shared_moving.value = 1
                        # Use move_multiple_to_meters for proper simultaneous triggering
                        slave_positions = [(i, 0.0) for i in range(ec.slaves_count)]
                        ec.move_multiple_to_meters(slave_positions, simultaneous=True)
                        time.sleep(0.1)
                        shared_moving.value = 0
                        send_response(True, "Moving all slaves to home (0m)")

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
                            shared_moving.value = 1  # Mark as moving for OSC
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
                            shared_moving.value = 1  # Mark as moving for OSC
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
                        shared_moving.value = 0  # Mark as stopped
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
                                    'slaves_found': ec.slaves_count
                                })
                            else:
                                print("[MANUAL RECOVERY] FAILED! Could not reconnect")
                                shared_state.value = 0
                                shared_num_slaves.value = 0
                                send_event(2, -1, 0x81B, "Recovery failed! Check connections.")
                                send_response(False, "Recovery FAILED! Check connections and try again.", {
                                    'recovery_success': False,
                                    'error_msg': 'Could not reconnect to EtherCAT'
                                })

                        except Exception as e:
                            print(f"[MANUAL RECOVERY] Exception: {e}")
                            import traceback
                            traceback.print_exc()
                            send_response(False, f"Recovery exception: {e}", {
                                'recovery_success': False,
                                'error_msg': str(e)
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

                    elif cmd == MotorProcess.CMD_OSC_CONNECT:
                        # Start OSC sender/receiver with separate ports
                        if data:
                            osc_send_ip = data.get('send_ip', '127.0.0.1')
                            osc_send_port = data.get('send_port', 9000)
                            osc_recv_ip = data.get('recv_ip', '0.0.0.0')
                            osc_recv_port = data.get('recv_port', data.get('port', 8000))
                            osc_mode = data.get('mode', OSC_MODE_BOTH)  # 1=recv, 2=send, 3=both
                        else:
                            osc_send_ip = '127.0.0.1'
                            osc_send_port = 9000
                            osc_recv_ip = '0.0.0.0'
                            osc_recv_port = 8000
                            osc_mode = OSC_MODE_BOTH

                        if isinstance(osc_send_port, str):
                            osc_send_port = int(osc_send_port)
                        if isinstance(osc_recv_port, str):
                            osc_recv_port = int(osc_recv_port)
                        if isinstance(osc_mode, str):
                            osc_mode = int(osc_mode)

                        print(f"\n[OSC] Starting OSC (mode={osc_mode}, send={osc_send_ip}:{osc_send_port}, recv={osc_recv_ip}:{osc_recv_port})")
                        start_osc(osc_mode, osc_send_ip, osc_send_port, osc_recv_ip, osc_recv_port)

                    elif cmd == MotorProcess.CMD_OSC_DISCONNECT:
                        # Stop OSC
                        print("\n[OSC] Stopping OSC...")
                        stop_osc()
                        send_response(True, "OSC disconnected")

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

                    elif cmd == MotorProcess.CMD_LIST_CONFIGS:
                        # List JSON config files in json folder
                        import sys
                        print("\n[LIST CONFIGS] Listing config files...", flush=True)
                        json_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'json')
                        print(f"[LIST CONFIGS] Looking in: {json_folder}", flush=True)
                        config_files = []
                        try:
                            if os.path.exists(json_folder):
                                for f in sorted(glob.glob(os.path.join(json_folder, '*.json'))):
                                    config_files.append(os.path.basename(f))
                                print(f"[LIST CONFIGS] Found files: {config_files}", flush=True)
                            else:
                                print(f"[LIST CONFIGS] Folder does not exist: {json_folder}", flush=True)
                        except Exception as e:
                            print(f"[LIST CONFIGS] Error listing files: {e}", flush=True)
                        send_response(True, f"Found {len(config_files)} config files", {'config_files': config_files})
                        print(f"[LIST CONFIGS] Response sent with {len(config_files)} files", flush=True)

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
                    
                    elif cmd == 'loop_test_start':
                        # Loop test: move slave back and forth between pos1 and pos2
                        if not data:
                            send_response(False, "No loop test data provided")
                            continue

                        slave = data.get('slave')
                        pos1 = data.get('pos1', 0)
                        pos2 = data.get('pos2', 0.1)
                        cycles = data.get('cycles', 5)  # 0 = infinite
                        speed = data.get('speed', ec._velocity)  # Use UI speed or default
                        start_delay = data.get('start_delay', 0)  # Delay before moving (seconds)
                        stop_delay = data.get('stop_delay', 0)  # Delay after reaching position (seconds)

                        if slave is None:
                            send_response(False, "No slave specified for loop test")
                            continue

                        slave = int(slave)
                        if slave >= ec.slaves_count:
                            send_response(False, f"Invalid slave index: {slave}")
                            continue

                        print(f"\n[LOOP TEST] Starting: Slave {slave}, Pos1={pos1}m, Pos2={pos2}m, Cycles={cycles}, Speed={speed}, StartDelay={start_delay}s, StopDelay={stop_delay}s")

                        # Clear stop flag for this operation
                        shared_stop.value = 0
                        shared_moving.value = 1

                        # Send immediate response
                        send_response(True, f"Loop test started", {
                            'loop_test': {
                                'status': 'started',
                                'slave': slave,
                                'pos1': pos1,
                                'pos2': pos2,
                                'cycles': cycles,
                                'current_cycle': 0
                            }
                        })

                        # Run loop test in a thread to not block command processing
                        def run_loop_test(slave_idx, p1, p2, num_cycles, vel_speed, delay_start, delay_stop):
                            try:
                                max_cycles = 999999 if num_cycles == 0 else num_cycles

                                # Helper function to wait with stop check (keeps PDO alive)
                                def delay_with_stop_check(delay_seconds):
                                    if delay_seconds <= 0:
                                        return True
                                    steps = int(delay_seconds * 100)  # Check every 10ms to keep PDO responsive
                                    for _ in range(steps):
                                        if shared_stop.value == 1:
                                            return False
                                        if ec.has_fault(slave_idx):
                                            return False
                                        update_shared_status(send_osc_movement=True)  # Keep PDO loop alive and send OSC
                                        time.sleep(0.01)
                                    return True

                                # Set max acceleration/deceleration for instant stop
                                ec.set_max_accel_decel(slave_idx)

                                # Small buffer for position check (0.5mm)
                                pos_buffer = 0.0005

                                # Determine overall direction based on pos1 vs pos2
                                go_forward = (p2 > p1)

                                # Always move to pos1 first (homing)
                                current_pos = ec.read_position_meters(slave_idx)
                                print(f"  [LOOP TEST] Homing to pos1 ({p1}m), current: {current_pos:.4f}m")
                                send_response(True, f"Homing to start position", {
                                    'loop_test': {
                                        'status': 'homing',
                                        'current_cycle': 0,
                                        'direction': 'to_pos1'
                                    }
                                })

                                # Move to pos1 based on current position
                                if current_pos < p1:
                                    ec.velocity_forward(slave_idx, vel_speed)
                                    while ec.read_position_meters(slave_idx) < (p1 - pos_buffer):
                                        if shared_stop.value == 1:
                                            ec.velocity_stop(slave_idx)
                                            shared_moving.value = 0
                                            send_response(True, "Loop test stopped during homing", {
                                                'loop_test': {'status': 'stopped', 'current_cycle': 0}
                                            })
                                            return
                                        if ec.has_fault(slave_idx):
                                            ec.velocity_stop(slave_idx)
                                            shared_moving.value = 0
                                            send_response(False, f"Fault during homing", {
                                                'loop_test': {'status': 'fault', 'current_cycle': 0}
                                            })
                                            return
                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)
                                elif current_pos > p1:
                                    ec.velocity_backward(slave_idx, vel_speed)
                                    while ec.read_position_meters(slave_idx) > (p1 + pos_buffer):
                                        if shared_stop.value == 1:
                                            ec.velocity_stop(slave_idx)
                                            shared_moving.value = 0
                                            send_response(True, "Loop test stopped during homing", {
                                                'loop_test': {'status': 'stopped', 'current_cycle': 0}
                                            })
                                            return
                                        if ec.has_fault(slave_idx):
                                            ec.velocity_stop(slave_idx)
                                            shared_moving.value = 0
                                            send_response(False, f"Fault during homing", {
                                                'loop_test': {'status': 'fault', 'current_cycle': 0}
                                            })
                                            return
                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)

                                ec.velocity_stop(slave_idx)
                                print(f"  [LOOP TEST] Reached start position {p1}m")
                                time.sleep(0.2)  # Brief pause before starting cycles

                                fault_detected = False
                                for cycle in range(max_cycles):
                                    if shared_stop.value == 1:
                                        print(f"  [LOOP TEST] Stopped by user at cycle {cycle + 1}")
                                        break

                                    # Check for faults
                                    if ec.has_fault(slave_idx):
                                        error_code = ec.read_error_code(slave_idx)
                                        error_name = ec.get_error_name(error_code)
                                        print(f"  [LOOP TEST] Fault detected: {error_name}")
                                        send_event(2, slave_idx, error_code, f"Slave {slave_idx}: {error_name}")
                                        fault_detected = True
                                        break

                                    # Start delay: wait before moving forward
                                    if delay_start > 0:
                                        print(f"  [LOOP TEST] Cycle {cycle + 1}: Waiting {delay_start}s before start")
                                        send_response(True, f"Cycle {cycle + 1}: Waiting {delay_start}s", {
                                            'loop_test': {
                                                'status': 'start_delay',
                                                'current_cycle': cycle + 1,
                                                'delay': delay_start
                                            }
                                        })
                                        if not delay_with_stop_check(delay_start):
                                            break

                                    # Move to pos2
                                    print(f"  [LOOP TEST] Cycle {cycle + 1}: Moving to {p2}m")
                                    send_response(True, f"Cycle {cycle + 1}: Moving forward", {
                                        'loop_test': {
                                            'status': 'moving_forward',
                                            'current_cycle': cycle + 1,
                                            'direction': 'forward'
                                        }
                                    })

                                    # Move based on go_forward direction
                                    if go_forward:
                                        ec.velocity_forward(slave_idx, vel_speed)
                                        while ec.read_position_meters(slave_idx) < (p2 - pos_buffer):
                                            if shared_stop.value == 1:
                                                break
                                            if ec.has_fault(slave_idx):
                                                time.sleep(0.02)
                                                if ec.has_fault(slave_idx) and ec.read_error_code(slave_idx) != 0:
                                                    fault_detected = True
                                                    break
                                            update_shared_status(send_osc_movement=True)
                                            time.sleep(0.01)
                                    else:
                                        ec.velocity_backward(slave_idx, vel_speed)
                                        while ec.read_position_meters(slave_idx) > (p2 + pos_buffer):
                                            if shared_stop.value == 1:
                                                break
                                            if ec.has_fault(slave_idx):
                                                time.sleep(0.02)
                                                if ec.has_fault(slave_idx) and ec.read_error_code(slave_idx) != 0:
                                                    fault_detected = True
                                                    break
                                            update_shared_status(send_osc_movement=True)
                                            time.sleep(0.01)

                                    ec.velocity_stop(slave_idx)

                                    if shared_stop.value == 1 or fault_detected:
                                        break

                                    # Stop delay: wait after reaching pos2 before moving backward
                                    if delay_stop > 0:
                                        print(f"  [LOOP TEST] Cycle {cycle + 1}: Waiting {delay_stop}s at pos2")
                                        send_response(True, f"Cycle {cycle + 1}: Waiting {delay_stop}s", {
                                            'loop_test': {
                                                'status': 'stop_delay',
                                                'current_cycle': cycle + 1,
                                                'delay': delay_stop
                                            }
                                        })
                                        if not delay_with_stop_check(delay_stop):
                                            break
                                    else:
                                        time.sleep(0.1)  # Small pause at target

                                    # Move backward: pos2 -> pos1
                                    print(f"  [LOOP TEST] Cycle {cycle + 1}: Moving to {p1}m")
                                    send_response(True, f"Cycle {cycle + 1}: Moving backward", {
                                        'loop_test': {
                                            'status': 'moving_backward',
                                            'current_cycle': cycle + 1,
                                            'direction': 'backward'
                                        }
                                    })

                                    # Move based on go_forward direction (opposite for return)
                                    if go_forward:
                                        ec.velocity_backward(slave_idx, vel_speed)
                                        while ec.read_position_meters(slave_idx) > (p1 + pos_buffer):
                                            if shared_stop.value == 1:
                                                break
                                            if ec.has_fault(slave_idx):
                                                time.sleep(0.02)
                                                if ec.has_fault(slave_idx) and ec.read_error_code(slave_idx) != 0:
                                                    fault_detected = True
                                                    break
                                            update_shared_status(send_osc_movement=True)
                                            time.sleep(0.01)
                                    else:
                                        ec.velocity_forward(slave_idx, vel_speed)
                                        while ec.read_position_meters(slave_idx) < (p1 - pos_buffer):
                                            if shared_stop.value == 1:
                                                break
                                            if ec.has_fault(slave_idx):
                                                time.sleep(0.02)
                                                if ec.has_fault(slave_idx) and ec.read_error_code(slave_idx) != 0:
                                                    fault_detected = True
                                                    break
                                            update_shared_status(send_osc_movement=True)
                                            time.sleep(0.01)

                                    ec.velocity_stop(slave_idx)

                                    if shared_stop.value == 1 or fault_detected:
                                        break

                                    time.sleep(0.1)  # Small pause at target

                                    print(f"  [LOOP TEST] Cycle {cycle + 1} complete")

                                # Loop test finished
                                ec.velocity_stop(slave_idx)
                                shared_moving.value = 0

                                if shared_stop.value == 1:
                                    send_response(True, "Loop test stopped", {
                                        'loop_test': {'status': 'stopped', 'current_cycle': cycle + 1}
                                    })
                                elif fault_detected:
                                    send_response(False, f"Loop test stopped due to fault at cycle {cycle + 1}", {
                                        'loop_test': {'status': 'fault', 'current_cycle': cycle + 1}
                                    })
                                else:
                                    send_response(True, f"Loop test completed: {cycle + 1} cycles", {
                                        'loop_test': {'status': 'completed', 'total_cycles': cycle + 1}
                                    })

                            except Exception as e:
                                print(f"  [LOOP TEST] Error: {e}")
                                ec.velocity_stop(slave_idx)
                                shared_moving.value = 0
                                send_response(False, f"Loop test error: {e}")

                        # Start the loop test thread with speed and delay parameters
                        loop_test_thread = threading.Thread(
                            target=run_loop_test,
                            args=(slave, pos1, pos2, cycles, speed, start_delay, stop_delay),
                            daemon=True
                        )
                        loop_test_thread.start()

                    elif cmd == 'loop_test_stop':
                        # Stop loop test by setting stop flag
                        print("[LOOP TEST] Stop requested")
                        shared_stop.value = 1
                        # Also stop velocity immediately
                        for i in range(ec.slaves_count):
                            ec.velocity_stop(i)
                        send_response(True, "Loop test stop requested")

                    elif cmd == 'multi_loop_test_start':
                        # Multi-slave loop test: move multiple slaves back and forth simultaneously
                        if not data:
                            send_response(False, "No multi-loop test data provided")
                            continue

                        slaves = data.get('slaves', [])
                        pos1 = data.get('pos1', 0)
                        pos2 = data.get('pos2', 0.1)
                        cycles = data.get('cycles', 5)  # 0 = infinite
                        speed = data.get('speed', ec._velocity)
                        start_delay = data.get('start_delay', 0)
                        stop_delay = data.get('stop_delay', 0)

                        if not slaves:
                            send_response(False, "No slaves specified for multi-loop test")
                            continue

                        # Validate slave indices
                        valid_slaves = [s for s in slaves if s < ec.slaves_count]
                        if not valid_slaves:
                            send_response(False, "No valid slaves specified")
                            continue

                        print(f"\n[MULTI-LOOP TEST] Starting: Slaves {[s+1 for s in valid_slaves]}, Pos1={pos1}m, Pos2={pos2}m, Cycles={cycles}, Speed={speed}")

                        # Clear stop flag
                        shared_stop.value = 0
                        shared_moving.value = 1

                        # Send immediate response
                        send_response(True, f"Multi-loop test started", {
                            'multi_loop_test': {
                                'status': 'started',
                                'slaves': valid_slaves,
                                'pos1': pos1,
                                'pos2': pos2,
                                'cycles': cycles
                            }
                        })

                        def run_multi_loop_test(slave_list, p1, p2, num_cycles, vel_speed, delay_start, delay_stop):
                            try:
                                max_cycles = 999999 if num_cycles == 0 else num_cycles

                                # Helper function to update all slave statuses
                                def send_all_status(status_dict, cycle, delay_duration=None):
                                    data = {
                                        'multi_loop_test': {
                                            'status': 'all_status',
                                            'slave_statuses': status_dict,
                                            'current_cycle': cycle
                                        }
                                    }
                                    if delay_duration is not None:
                                        data['multi_loop_test']['delay_duration'] = delay_duration
                                    send_response(True, f"Cycle {cycle}", data)

                                # Helper function to wait with stop check
                                def delay_with_stop_check(delay_seconds):
                                    if delay_seconds <= 0:
                                        return True
                                    steps = int(delay_seconds * 100)
                                    for _ in range(steps):
                                        if shared_stop.value == 1:
                                            return False
                                        # Check for faults in any slave
                                        for s in slave_list:
                                            if ec.has_fault(s):
                                                return False
                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)
                                    return True

                                # Helper function to move all slaves to target and wait
                                def move_all_to_target(target_pos, status_type='moving'):
                                    """Move all slaves to target position, stop each when it reaches/passes target"""
                                    print(f"  [MULTI-LOOP] Moving all slaves to {target_pos}m")
                                    status_dict = {s: status_type for s in slave_list}
                                    send_all_status(status_dict, 0 if status_type == 'homing' else cycle + 1)

                                    # Determine direction for each slave and start movement
                                    slave_directions = {}  # 1 = forward (increasing), -1 = backward (decreasing)
                                    slaves_moving = set()

                                    for s in slave_list:
                                        current_pos = ec.read_position_meters(s)
                                        if target_pos > current_pos:
                                            ec.velocity_forward(s, vel_speed)
                                            slave_directions[s] = 1  # Moving forward (position increasing)
                                            slaves_moving.add(s)
                                        elif target_pos < current_pos:
                                            ec.velocity_backward(s, vel_speed)
                                            slave_directions[s] = -1  # Moving backward (position decreasing)
                                            slaves_moving.add(s)
                                        else:
                                            # Already at target
                                            print(f"  [MULTI-LOOP] Slave {s+1} already at target {target_pos}m")

                                    # If no slaves need to move, we're done
                                    if not slaves_moving:
                                        return True, 'ok'

                                    # Wait until all slaves reach or pass target
                                    while slaves_moving:
                                        if shared_stop.value == 1:
                                            for s in slave_list:
                                                ec.velocity_stop(s)
                                            return False, 'stopped'

                                        for s in list(slaves_moving):
                                            # Double-check fault to avoid false positives
                                            if ec.has_fault(s):
                                                time.sleep(0.02)
                                                if ec.has_fault(s):
                                                    error_code = ec.read_error_code(s)
                                                    if error_code != 0:
                                                        for ss in slave_list:
                                                            ec.velocity_stop(ss)
                                                        return False, 'fault'

                                            current_pos = ec.read_position_meters(s)
                                            direction = slave_directions[s]

                                            # Check if slave reached or passed target
                                            if direction == 1:  # Moving forward
                                                if current_pos >= target_pos:
                                                    ec.velocity_stop(s)
                                                    slaves_moving.discard(s)
                                                    print(f"  [MULTI-LOOP] Slave {s+1} reached {current_pos:.4f}m (target: {target_pos}m)")
                                            else:  # Moving backward
                                                if current_pos <= target_pos:
                                                    ec.velocity_stop(s)
                                                    slaves_moving.discard(s)
                                                    print(f"  [MULTI-LOOP] Slave {s+1} reached {current_pos:.4f}m (target: {target_pos}m)")

                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)

                                    # Safety stop all
                                    for s in slave_list:
                                        ec.velocity_stop(s)
                                    return True, 'ok'

                                # Set max acceleration/deceleration for all slaves (instant stop)
                                print(f"  [MULTI-LOOP] Setting max accel/decel for instant stop")
                                for s in slave_list:
                                    ec.set_max_accel_decel(s)

                                # ALWAYS move ALL slaves to pos1 first (homing)
                                print(f"  [MULTI-LOOP] Homing all slaves to pos1 ({p1}m)")
                                success, reason = move_all_to_target(p1, 'homing')
                                if not success:
                                    shared_moving.value = 0
                                    if reason == 'stopped':
                                        send_response(True, "Multi-loop test stopped during homing", {
                                            'multi_loop_test': {'status': 'stopped', 'current_cycle': 0}
                                        })
                                    else:
                                        send_response(False, "Fault during homing", {
                                            'multi_loop_test': {'status': 'fault', 'current_cycle': 0}
                                        })
                                    return

                                print(f"  [MULTI-LOOP] All slaves at start position {p1}m")
                                time.sleep(0.2)  # Brief pause after reaching start

                                fault_found = False
                                for cycle in range(max_cycles):
                                    if shared_stop.value == 1:
                                        print(f"  [MULTI-LOOP] Stopped by user at cycle {cycle + 1}")
                                        break

                                    # Check for faults
                                    for s in slave_list:
                                        if ec.has_fault(s):
                                            error_code = ec.read_error_code(s)
                                            error_name = ec.get_error_name(error_code)
                                            print(f"  [MULTI-LOOP] Slave {s+1} fault: {error_name}")
                                            fault_found = True
                                            break
                                    if fault_found:
                                        break

                                    # Start delay
                                    if delay_start > 0:
                                        print(f"  [MULTI-LOOP] Cycle {cycle + 1}: Waiting {delay_start}s before start")
                                        status_dict = {s: 'wait' for s in slave_list}
                                        send_all_status(status_dict, cycle + 1, delay_duration=delay_start)
                                        if not delay_with_stop_check(delay_start):
                                            break

                                    # Small buffer for position check (0.5mm) to handle load/momentum
                                    pos_buffer = 0.0005

                                    # All slaves move in same direction: pos1 -> pos2
                                    go_forward = (p2 > p1)

                                    # Move forward: all slaves to pos2
                                    print(f"  [MULTI-LOOP] Cycle {cycle + 1}: Moving all slaves to {p2}m")
                                    status_dict = {s: 'moving' for s in slave_list}
                                    send_all_status(status_dict, cycle + 1)

                                    # Start all slaves moving - send command to each slave
                                    for s in slave_list:
                                        if go_forward:
                                            ec.velocity_forward(s, vel_speed)
                                        else:
                                            ec.velocity_backward(s, vel_speed)
                                        time.sleep(0.005)  # Small delay between SDO writes

                                    # Wait until all reach pos2
                                    move_fault = False
                                    slaves_moving = set(slave_list)
                                    while slaves_moving:
                                        if shared_stop.value == 1:
                                            break
                                        for s in list(slaves_moving):
                                            # Double-check fault to avoid false positives
                                            if ec.has_fault(s):
                                                time.sleep(0.02)  # Small delay
                                                if ec.has_fault(s):  # Check again
                                                    error_code = ec.read_error_code(s)
                                                    if error_code != 0:  # Only if real error
                                                        move_fault = True
                                                        print(f"  [MULTI-LOOP] Fault on slave {s+1} during move to pos2: error 0x{error_code:04X}")
                                                        break
                                            current_pos = ec.read_position_meters(s)
                                            # Check with buffer - stop slightly before target
                                            if go_forward and current_pos >= (p2 - pos_buffer):
                                                ec.velocity_stop(s)
                                                slaves_moving.discard(s)
                                            elif not go_forward and current_pos <= (p2 + pos_buffer):
                                                ec.velocity_stop(s)
                                                slaves_moving.discard(s)
                                        if move_fault:
                                            break
                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)

                                    for s in slave_list:
                                        ec.velocity_stop(s)

                                    if shared_stop.value == 1 or move_fault:
                                        if move_fault:
                                            fault_found = True
                                        break

                                    # Stop delay
                                    if delay_stop > 0:
                                        print(f"  [MULTI-LOOP] Cycle {cycle + 1}: Waiting {delay_stop}s at pos2")
                                        status_dict = {s: 'wait' for s in slave_list}
                                        send_all_status(status_dict, cycle + 1, delay_duration=delay_stop)
                                        if not delay_with_stop_check(delay_stop):
                                            break
                                    else:
                                        time.sleep(0.1)

                                    # Move backward: all slaves to pos1
                                    print(f"  [MULTI-LOOP] Cycle {cycle + 1}: Moving all slaves to {p1}m")
                                    status_dict = {s: 'moving' for s in slave_list}
                                    send_all_status(status_dict, cycle + 1)

                                    # Start all slaves moving - send command to each slave
                                    for s in slave_list:
                                        if go_forward:
                                            ec.velocity_backward(s, vel_speed)
                                        else:
                                            ec.velocity_forward(s, vel_speed)
                                        time.sleep(0.005)  # Small delay between SDO writes

                                    # Wait until all reach pos1
                                    move_fault = False
                                    slaves_moving = set(slave_list)
                                    while slaves_moving:
                                        if shared_stop.value == 1:
                                            break
                                        for s in list(slaves_moving):
                                            # Double-check fault to avoid false positives
                                            if ec.has_fault(s):
                                                time.sleep(0.02)  # Small delay
                                                if ec.has_fault(s):  # Check again
                                                    error_code = ec.read_error_code(s)
                                                    if error_code != 0:  # Only if real error
                                                        move_fault = True
                                                        print(f"  [MULTI-LOOP] Fault on slave {s+1} during move to pos1: error 0x{error_code:04X}")
                                                        break
                                            current_pos = ec.read_position_meters(s)
                                            # Check with buffer - stop slightly before target
                                            if go_forward and current_pos <= (p1 + pos_buffer):
                                                ec.velocity_stop(s)
                                                slaves_moving.discard(s)
                                            elif not go_forward and current_pos >= (p1 - pos_buffer):
                                                ec.velocity_stop(s)
                                                slaves_moving.discard(s)
                                        if move_fault:
                                            break
                                        update_shared_status(send_osc_movement=True)
                                        time.sleep(0.01)

                                    for s in slave_list:
                                        ec.velocity_stop(s)

                                    if shared_stop.value == 1 or move_fault:
                                        if move_fault:
                                            fault_found = True
                                        break

                                    time.sleep(0.1)
                                    print(f"  [MULTI-LOOP] Cycle {cycle + 1} complete")

                                # Test finished
                                for s in slave_list:
                                    ec.velocity_stop(s)
                                shared_moving.value = 0

                                if shared_stop.value == 1:
                                    send_response(True, "Multi-loop test stopped", {
                                        'multi_loop_test': {'status': 'stopped', 'current_cycle': cycle + 1}
                                    })
                                elif fault_found:
                                    send_response(False, f"Multi-loop test stopped due to fault at cycle {cycle + 1}", {
                                        'multi_loop_test': {'status': 'fault', 'current_cycle': cycle + 1}
                                    })
                                else:
                                    send_response(True, f"Multi-loop test completed: {cycle + 1} cycles", {
                                        'multi_loop_test': {'status': 'completed', 'total_cycles': cycle + 1}
                                    })

                            except Exception as e:
                                print(f"  [MULTI-LOOP] Error: {e}")
                                for s in slave_list:
                                    ec.velocity_stop(s)
                                shared_moving.value = 0
                                send_response(False, f"Multi-loop test error: {e}")

                        # Start thread
                        multi_loop_thread = threading.Thread(
                            target=run_multi_loop_test,
                            args=(valid_slaves, pos1, pos2, cycles, speed, start_delay, stop_delay),
                            daemon=True
                        )
                        multi_loop_thread.start()

                    elif cmd == 'multi_loop_test_stop':
                        # Stop multi-loop test
                        print("[MULTI-LOOP] Stop requested")
                        shared_stop.value = 1
                        # Stop all slaves
                        for i in range(ec.slaves_count):
                            ec.velocity_stop(i)
                        send_response(True, "Multi-loop test stop requested")

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

                        # Global staggered movement settings (default: simultaneous)
                        is_global = template.get('is_global', True)  # If true, use global settings; if false, use per-step settings
                        global_is_simultaneous = template.get('is_simultaneous', True)
                        global_slave_delay_ms = template.get('slave_delay_ms', 10)

                        # Debug: Print template keys to verify parsing
                        print(f"  [DEBUG] Template keys: {list(template.keys())}")
                        print(f"  [DEBUG] is_global: {is_global}")
                        print(f"  [DEBUG] is_simultaneous from template: {template.get('is_simultaneous', 'NOT FOUND')}")
                        print(f"  [DEBUG] slave_delay_ms from template: {template.get('slave_delay_ms', 'NOT FOUND')}")

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
                        print(f"  Global settings: {is_global}" + (f", Simultaneous: {global_is_simultaneous}, Slave delay: {global_slave_delay_ms}ms" if is_global else " (using per-step settings)"))

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

                                    # Determine is_simultaneous and slave_delay_ms for this step
                                    if is_global:
                                        # Use global settings
                                        step_is_simultaneous = global_is_simultaneous
                                        step_slave_delay_ms = global_slave_delay_ms
                                    else:
                                        # Use per-step settings (fallback to global if not specified)
                                        step_is_simultaneous = step.get('is_simultaneous', global_is_simultaneous)
                                        step_slave_delay_ms = step.get('slave_delay_ms', global_slave_delay_ms)

                                    # Get move_order mode: "dynamic" (system calculated) or "define" (user provided)
                                    step_move_order_mode = step.get('move_order', 'dynamic')  # Default to dynamic
                                    step_move_order_list = step.get('move_order_list', [])  # User-defined order (0-indexed slave indices)

                                    # Get position reference
                                    pos_ref = step.get('position')
                                    pos_rot_ref = step.get('position_rotation')

                                    # Resolve position from positions dict
                                    pos_values = positions.get(pos_ref, []) if pos_ref else []
                                    pos_rot_values = positions.get(pos_rot_ref, []) if pos_rot_ref else []

                                    print(f"\n  [{step_idx + 1}/{len(steps)}] {name} (type: {step_type})")
                                    print(f"    [STEP CONFIG] is_global={is_global}, step_is_simultaneous={step_is_simultaneous}, step_slave_delay_ms={step_slave_delay_ms}")
                                    print(f"    [STEP CONFIG] move_order_mode={step_move_order_mode}, move_order_list={step_move_order_list}")

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

                                    # Calculate or use defined move order for movement slaves
                                    move_order = []
                                    is_spreading = False
                                    move_order_details = None
                                    move_order_source = 'none'

                                    if step_move_order_mode == 'define' and step_move_order_list:
                                        # Use user-defined move order
                                        move_order = step_move_order_list
                                        move_order_source = 'defined'
                                        print(f"    Move order (DEFINED): {[s+1 for s in move_order]}")
                                    elif movement_slave_positions and len(movement_slave_positions) > 1:
                                        # Calculate dynamic move order
                                        move_order, is_spreading, move_order_details = ec.calculate_move_order(movement_slave_positions, return_details=True)
                                        move_order_source = 'dynamic'
                                        print(f"    Move order (DYNAMIC): {[s+1 for s in move_order]} ({'Spreading' if is_spreading else 'Converging'})")
                                    elif movement_slave_positions:
                                        move_order = [movement_slave_positions[0][0]]
                                        move_order_source = 'single'

                                    # Send OSC notification BEFORE step starts (1-indexed)
                                    osc_send_template_step(step_idx + 1)

                                    # Send step start notification with move order and details
                                    step_data = {
                                        'template_step': {
                                            'index': step_idx + 1,
                                            'total': len(steps),
                                            'name': name,
                                            'type': step_type,
                                            'event': 'start',
                                            'move_order': move_order,
                                            'move_order_source': move_order_source,
                                            'is_spreading': is_spreading,
                                            'moving_slaves': moving_slaves,  # All slaves moving in this step
                                            'is_simultaneous': step_is_simultaneous,
                                            'slave_delay_ms': step_slave_delay_ms
                                        }
                                    }
                                    if move_order_details:
                                        step_data['template_step']['order_details'] = move_order_details
                                    send_response(True, f"Executing: {name}", step_data)

                                    # Execute movement for all collected positions
                                    if slave_positions:
                                        # Debug: Print movement mode before execution
                                        print(f"    [DEBUG] About to call move_multiple_to_meters:")
                                        print(f"           simultaneous={step_is_simultaneous} (type: {type(step_is_simultaneous)})")
                                        print(f"           slave_delay_ms={step_slave_delay_ms} (type: {type(step_slave_delay_ms)})")
                                        if step_is_simultaneous:
                                            print(f"    Starting SIMULTANEOUS move for {len(slave_positions)} slaves...")
                                        else:
                                            print(f"    Starting STAGGERED move for {len(slave_positions)} slaves (delay: {step_slave_delay_ms}ms)...")
                                            print(f"    Using move order: {[s+1 for s in move_order]}")

                                        # OSC notifications for each slave move (currently disabled)
                                        # for slave_idx, pos in slave_positions:
                                        #     osc_send_slave_move(slave_idx, pos)

                                        # Create stop check function for staggered movement
                                        def staggered_stop_check():
                                            # Check shared stop flag
                                            if shared_stop.value == 1:
                                                return True
                                            # Also check for priority commands (stop, disable)
                                            return check_priority_commands()

                                        # Pass move_order and stop_check for staggered movement
                                        ec.move_multiple_to_meters(slave_positions, simultaneous=step_is_simultaneous, slave_delay_ms=step_slave_delay_ms, move_order=move_order, stop_check=staggered_stop_check)

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
                                            # Update positions during delay (keep OSC movement messages flowing)
                                            update_shared_status(send_osc_movement=True)
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