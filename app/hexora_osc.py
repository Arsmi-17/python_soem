#!/usr/bin/env python3
"""
Hexora OSC Handler - Controls motor movement via OSC for Hexora installation

OSC Addresses:
    /hexora_open  - Move configured slaves from pos1 to pos2 (opening motion)
    /hexora_close - Move configured slaves from pos2 to pos1 (closing motion)

Requirements:
    - Only works in PV (Profile Velocity) mode (mode = 3)
    - Uses smooth accel-decel movement similar to multi-slave loop test
    - Configurable slaves, positions, and speed in the handler

Usage:
    from hexora_osc import HexoraOSC
    hexora = HexoraOSC(motor_process)
    hexora.start(ip='0.0.0.0', port=8001)
"""

import socket
import struct
import threading
import time


class HexoraOSC:
    """
    Hexora OSC Handler for motor control.
    Listens for /hexora_open and /hexora_close OSC messages.
    Only processes commands when in PV (velocity) mode.
    """

    # Mode constants
    MODE_PP = 1
    MODE_PV = 3
    MODE_CSP = 8

    def __init__(self, motor_process=None, ethercat_controller=None, on_log=None):
        """
        Initialize Hexora OSC Handler.

        Args:
            motor_process: MotorProcess instance for getting status
            ethercat_controller: EtherCAT controller instance for direct motor control
            on_log: Optional callback function for logging (receives type and message)
        """
        self.motor_process = motor_process
        self.ec = ethercat_controller
        self.on_log = on_log
        self._running = False
        self._thread = None
        self._socket = None
        self.ip = '0.0.0.0'
        self.port = 8001

        # Movement state
        self._movement_active = False
        self._movement_thread = None
        self._stop_requested = False

        # =====================================================
        # HEXORA CONFIGURATION - Edit these values as needed
        # =====================================================

        # Slaves to control (0-indexed, e.g., [0, 1] for slaves 1 and 2)
        self.slaves = [0, 1]

        # Position settings (in meters)
        self.pos1 = 0.0      # Closed/Home position
        self.pos2 = 0.028      # Open position

        # Speed settings
        self.speed = 1000        # Maximum speed (units/s)
        self.acc_dec_speed = 300 # Acceleration/deceleration speed (start/end speed)

        # Movement thresholds
        self.decel_distance = 0.008    # Start decelerating when this close to target (8mm)
        self.min_distance_cruise = 0.01 # If distance < this, use slow speed only

        # Ramping settings for smooth accel/decel
        self.ramp_steps = 30           # Number of steps for ramping
        self.ramp_interval = 0.03      # Seconds between speed updates (30ms)

        # =====================================================

    def configure(self, slaves=None, pos1=None, pos2=None, speed=None, acc_dec_speed=None):
        """
        Configure Hexora movement parameters.

        Args:
            slaves: List of slave indices (0-indexed)
            pos1: Closed/Home position in meters
            pos2: Open position in meters
            speed: Maximum movement speed
            acc_dec_speed: Acceleration/deceleration speed
        """
        if slaves is not None:
            self.slaves = slaves
        if pos1 is not None:
            self.pos1 = pos1
        if pos2 is not None:
            self.pos2 = pos2
        if speed is not None:
            self.speed = speed
        if acc_dec_speed is not None:
            self.acc_dec_speed = acc_dec_speed

        self._log('info', f"Config: slaves={[s+1 for s in self.slaves]}, pos1={self.pos1}m, pos2={self.pos2}m, speed={self.speed}, acc_dec={self.acc_dec_speed}")

    def _log(self, log_type, message):
        """Log a message."""
        print(f"[Hexora OSC] [{log_type.upper()}] {message}")
        if self.on_log:
            self.on_log(log_type, message)

    def _get_current_mode(self):
        """Get current operation mode from motor process."""
        if self.motor_process:
            status = self.motor_process.get_status()
            return status.get('mode', 0)
        return 0

    def _is_pv_mode(self):
        """Check if currently in PV (Profile Velocity) mode."""
        mode = self._get_current_mode()
        return mode == self.MODE_PV

    def _parse_osc_message(self, data):
        """Parse incoming OSC message."""
        try:
            null_idx = data.find(b'\x00')
            if null_idx == -1:
                return None, None

            address = data[:null_idx].decode('utf-8')

            idx = null_idx + 1
            while idx % 4 != 0:
                idx += 1

            if idx >= len(data) or data[idx:idx+1] != b',':
                return address, []

            type_tag_end = data.find(b'\x00', idx)
            type_tag = data[idx+1:type_tag_end].decode('utf-8')

            idx = type_tag_end + 1
            while idx % 4 != 0:
                idx += 1

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
            self._log('error', f"Parse error: {e}")
            return None, None

    def _move_slaves_to_target(self, target_pos):
        """
        Move all configured slaves to target position with smooth accel-decel.
        Uses velocity mode with gradual speed ramping.

        Returns: (success, reason)
        """
        if not self.ec:
            self._log('error', "No EtherCAT controller available")
            return False, 'no_controller'

        slave_list = self.slaves
        vel_speed = self.speed
        vel_acc_dec = self.acc_dec_speed

        self._log('info', f"Moving slaves {[s+1 for s in slave_list]} to {target_pos}m")
        self._log('info', f"Speed: max={vel_speed}, acc_dec={vel_acc_dec}")

        # Calculate ramp step
        ramp_step = max(30, (vel_speed - vel_acc_dec) // self.ramp_steps)

        # Track each slave's state
        slave_directions = {}    # 1 = forward, -1 = backward
        slave_phase = {}         # 'accel', 'cruise', 'decel', 'slow_only'
        slave_current_speed = {} # Current actual speed (for gradual ramping)
        slaves_moving = set()

        # Initialize each slave
        for s in slave_list:
            try:
                current_pos = self.ec.read_position_meters(s)
                total_distance = abs(target_pos - current_pos)

                if total_distance < 0.0001:
                    self._log('info', f"Slave {s+1} already at target {target_pos}m")
                    continue

                # Determine direction
                if target_pos > current_pos:
                    slave_directions[s] = 1  # Forward
                else:
                    slave_directions[s] = -1  # Backward

                # Choose initial phase based on total distance
                if total_distance < self.min_distance_cruise:
                    slave_phase[s] = 'slow_only'
                    slave_current_speed[s] = vel_acc_dec
                    self._log('info', f"Slave {s+1}: short distance ({total_distance:.4f}m), slow_only")
                else:
                    slave_phase[s] = 'accel'
                    slave_current_speed[s] = vel_acc_dec
                    self._log('info', f"Slave {s+1}: distance={total_distance:.4f}m, starting accel")

                # Start movement at initial speed
                if slave_directions[s] == 1:
                    self.ec.velocity_forward(s, slave_current_speed[s])
                else:
                    self.ec.velocity_backward(s, slave_current_speed[s])
                slaves_moving.add(s)

            except Exception as e:
                self._log('error', f"Error initializing slave {s+1}: {e}")

        if not slaves_moving:
            return True, 'already_at_target'

        last_ramp_time = time.time()
        move_start_time = time.time()

        while slaves_moving and self._movement_active:
            if self._stop_requested:
                for s in slave_list:
                    try:
                        self.ec.velocity_stop(s)
                    except:
                        pass
                return False, 'stopped'

            current_time = time.time()
            time_since_ramp = current_time - last_ramp_time
            do_ramp = time_since_ramp >= self.ramp_interval

            for s in list(slaves_moving):
                try:
                    # Check for faults
                    if self.ec.has_fault(s):
                        time.sleep(0.02)
                        if self.ec.has_fault(s):
                            error_code = self.ec.read_error_code(s)
                            if error_code != 0:
                                for ss in slave_list:
                                    try:
                                        self.ec.velocity_stop(ss)
                                    except:
                                        pass
                                return False, 'fault'

                    current_pos = self.ec.read_position_meters(s)
                    direction = slave_directions[s]
                    phase = slave_phase[s]
                    curr_speed = slave_current_speed[s]

                    distance_to_target = abs(target_pos - current_pos)

                    # Check if reached target
                    reached = False
                    if direction == 1 and current_pos >= target_pos:
                        reached = True
                    elif direction == -1 and current_pos <= target_pos:
                        reached = True

                    if reached:
                        self.ec.velocity_stop(s)
                        slaves_moving.discard(s)
                        elapsed = time.time() - move_start_time
                        self._log('info', f"Slave {s+1} reached {current_pos:.4f}m in {elapsed:.3f}s")
                        continue

                    # Phase state machine with gradual ramping
                    if phase == 'accel':
                        if distance_to_target <= self.decel_distance:
                            slave_phase[s] = 'decel'
                            self._log('info', f"Slave {s+1}: accel->decel at {curr_speed}")
                        elif do_ramp and curr_speed < vel_speed:
                            new_speed = min(curr_speed + ramp_step, vel_speed)
                            slave_current_speed[s] = new_speed
                            if direction == 1:
                                self.ec.velocity_forward(s, new_speed)
                            else:
                                self.ec.velocity_backward(s, new_speed)
                            if new_speed >= vel_speed:
                                slave_phase[s] = 'cruise'

                    elif phase == 'cruise':
                        if distance_to_target <= self.decel_distance:
                            slave_phase[s] = 'decel'
                            self._log('info', f"Slave {s+1}: cruise->decel at {curr_speed}")

                    elif phase == 'decel':
                        if do_ramp and curr_speed > vel_acc_dec:
                            new_speed = max(curr_speed - ramp_step, vel_acc_dec)
                            slave_current_speed[s] = new_speed
                            if direction == 1:
                                self.ec.velocity_forward(s, new_speed)
                            else:
                                self.ec.velocity_backward(s, new_speed)

                    # slow_only phase maintains constant speed

                except Exception as e:
                    self._log('error', f"Movement error for slave {s+1}: {e}")
                    slaves_moving.discard(s)

            if do_ramp:
                last_ramp_time = current_time

            time.sleep(0.005)

        # Safety stop all
        for s in slave_list:
            try:
                self.ec.velocity_stop(s)
            except:
                pass

        return True, 'ok'

    def _handle_hexora_open(self, address, args):
        """
        Handle /hexora_open command.
        Move slaves from pos1 to pos2 (opening motion).
        Only works in PV mode.
        """
        self._log('recv', f"/hexora_open {args}")

        # Check if in PV mode
        if not self._is_pv_mode():
            mode = self._get_current_mode()
            mode_names = {1: 'PP', 3: 'PV', 8: 'CSP'}
            self._log('warn', f"Ignoring - not in PV mode (current: {mode_names.get(mode, mode)})")
            return

        # Check if movement already active
        if self._movement_active:
            self._log('warn', "Movement already in progress, ignoring")
            return

        self._log('info', f"Opening: {self.pos1}m -> {self.pos2}m")

        # Start movement in thread
        self._movement_active = True
        self._stop_requested = False
        self._movement_thread = threading.Thread(
            target=self._execute_movement,
            args=(self.pos2, 'open'),
            daemon=True
        )
        self._movement_thread.start()

    def _handle_hexora_close(self, address, args):
        """
        Handle /hexora_close command.
        Move slaves from pos2 to pos1 (closing motion).
        Only works in PV mode.
        """
        self._log('recv', f"/hexora_close {args}")

        # Check if in PV mode
        if not self._is_pv_mode():
            mode = self._get_current_mode()
            mode_names = {1: 'PP', 3: 'PV', 8: 'CSP'}
            self._log('warn', f"Ignoring - not in PV mode (current: {mode_names.get(mode, mode)})")
            return

        # Check if movement already active
        if self._movement_active:
            self._log('warn', "Movement already in progress, ignoring")
            return

        self._log('info', f"Closing: {self.pos2}m -> {self.pos1}m")

        # Start movement in thread
        self._movement_active = True
        self._stop_requested = False
        self._movement_thread = threading.Thread(
            target=self._execute_movement,
            args=(self.pos1, 'close'),
            daemon=True
        )
        self._movement_thread.start()

    def _execute_movement(self, target_pos, action_name):
        """Execute movement in background thread."""
        try:
            success, reason = self._move_slaves_to_target(target_pos)
            if success:
                self._log('info', f"{action_name.capitalize()} completed successfully")
            else:
                self._log('warn', f"{action_name.capitalize()} ended: {reason}")
        except Exception as e:
            self._log('error', f"Movement error: {e}")
        finally:
            self._movement_active = False

    def stop_movement(self):
        """Stop any active movement."""
        self._stop_requested = True
        if self._movement_thread and self._movement_thread.is_alive():
            self._movement_thread.join(timeout=1.0)

    def _receiver_loop(self):
        """Main receiver loop."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(0.5)

        try:
            self._socket.bind((self.ip, self.port))
        except Exception as e:
            self._log('error', f"Failed to bind to {self.ip}:{self.port} - {e}")
            return

        self._log('info', f"Listening on {self.ip}:{self.port}")
        self._log('info', f"Slaves: {[s+1 for s in self.slaves]}, Positions: {self.pos1}m <-> {self.pos2}m")
        self._log('info', f"Speed: max={self.speed}, acc_dec={self.acc_dec_speed}")
        self._log('info', "Addresses: /hexora_open, /hexora_close (PV mode only)")

        while self._running:
            try:
                data, addr = self._socket.recvfrom(4096)

                address, args = self._parse_osc_message(data)
                if address is None:
                    continue

                args_str = ' '.join(str(a) for a in args) if args else ''
                self._log('recv', f"Received: {address} [{args_str}]")

                # Handle Hexora addresses
                if address == '/hexora_open':
                    self._handle_hexora_open(address, args)
                elif address == '/hexora_close':
                    self._handle_hexora_close(address, args)
                else:
                    self._log('info', f"Unhandled address: {address}")

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    self._log('error', f"Receiver error: {e}")

        self._socket.close()
        self._log('info', "Hexora OSC stopped")

    def start(self, ip='0.0.0.0', port=8001):
        """
        Start the Hexora OSC handler.

        Args:
            ip: IP address to bind to (default: '0.0.0.0' for all interfaces)
            port: Port to listen on (default: 8001)
        """
        if self._running:
            self._log('warn', "Hexora OSC already running")
            return

        self.ip = ip
        self.port = port
        self._running = True

        self._thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the Hexora OSC handler."""
        if not self._running:
            return

        self._running = False
        self.stop_movement()

        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def is_running(self):
        """Check if handler is running."""
        return self._running


# =============================================================================
# Standalone Testing
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Hexora OSC Handler')
    parser.add_argument('--ip', default='0.0.0.0', help='IP to bind (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8001, help='Port to listen (default: 8001)')
    args = parser.parse_args()

    print("=" * 60)
    print("Hexora OSC Handler - Standalone Test Mode")
    print("=" * 60)
    print(f"Listening on {args.ip}:{args.port}")
    print()
    print("OSC Addresses (PV mode only):")
    print("  /hexora_open   - Move pos1 -> pos2 (opening)")
    print("  /hexora_close  - Move pos2 -> pos1 (closing)")
    print()
    print("NOTE: Without motor_process, commands will be received but not executed")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)

    # Create handler without motor process (test mode)
    handler = HexoraOSC()
    handler.start(ip=args.ip, port=args.port)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        handler.stop()
        print("Done")
