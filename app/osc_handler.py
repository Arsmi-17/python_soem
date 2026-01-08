#!/usr/bin/env python3
"""
OSC Handler - Binds OSC addresses with motor control actions
Receives OSC messages and triggers corresponding motor movements

Usage:
    1. Import and use with motor_process:
       from osc_handler import OSCHandler
       handler = OSCHandler(motor_process)
       handler.start(ip='0.0.0.0', port=8000)

    2. Or run standalone for testing:
       python osc_handler.py --ip 0.0.0.0 --port 8000

OSC Address Bindings:
    /start [value]  - Move slave0 to target position based on value mapping
                      Broadcasts /movement during motion, sends /reached when done
    /move [slave] [position] - Move specific slave to position
    /home - Move all slaves to home (0m)
    /stop - Emergency stop all motion
    /enable - Enable all drives
    /disable - Disable all drives

OSC Output Messages:
    /movement [slave_id] [position] - Sent continuously while moving
    /reached [value] - Sent when target position reached

Configuration:
    Settings are loaded from osc/rotorscope_config.json
"""

import json
import os
import socket
import struct
import threading
import time


def load_osc_config():
    """Load OSC configuration from rotorscope_config.json"""
    config_path = os.path.join(os.path.dirname(__file__), 'osc', 'rotorscope_config.json')
    default_config = {
        "speed": {"velocity": 80000, "acceleration": 40000, "deceleration": 40000},
        "start_value_map": {"0": 0, "1": 0.1, "2": 0.5, "3": 1.0, "4": 1.5, "5": 2.0, "6": 3.0},
        "position_tolerance": 0.002,
        "broadcast_interval": 0.05
    }
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            print(f"[OSC Handler] Loaded config from {config_path}")
            return config
    except Exception as e:
        print(f"[OSC Handler] Could not load config: {e}, using defaults")
        return default_config


class OSCHandler:
    """
    OSC Handler that binds OSC addresses with motor control actions.
    Configuration is loaded from osc/rotorscope_config.json
    """

    # Load config at class level
    _config = load_osc_config()

    # Value to target position mapping (convert string keys to int)
    START_VALUE_MAP = {int(k): v for k, v in _config.get('start_value_map', {}).items()}

    # Speed settings from config
    _speed = _config.get('speed', {})
    MOVE_SPEED = _speed.get('velocity', 80000)
    MOVE_ACCEL = _speed.get('acceleration', MOVE_SPEED // 2)
    MOVE_DECEL = _speed.get('deceleration', MOVE_SPEED // 2)

    # Position tolerance and broadcast interval
    POSITION_TOLERANCE = _config.get('position_tolerance', 0.002)
    BROADCAST_INTERVAL = _config.get('broadcast_interval', 0.05)

    @classmethod
    def reload_config(cls):
        """Reload configuration from rotorscope_config.json"""
        cls._config = load_osc_config()
        cls.START_VALUE_MAP = {int(k): v for k, v in cls._config.get('start_value_map', {}).items()}
        cls._speed = cls._config.get('speed', {})
        cls.MOVE_SPEED = cls._speed.get('velocity', 80000)
        cls.MOVE_ACCEL = cls._speed.get('acceleration', cls.MOVE_SPEED // 2)
        cls.MOVE_DECEL = cls._speed.get('deceleration', cls.MOVE_SPEED // 2)
        cls.POSITION_TOLERANCE = cls._config.get('position_tolerance', 0.002)
        cls.BROADCAST_INTERVAL = cls._config.get('broadcast_interval', 0.05)
        print(f"[OSC Handler] Config reloaded. Value map: {cls.START_VALUE_MAP}")

    def __init__(self, motor_process=None, on_log=None):
        """
        Initialize OSC Handler.

        Args:
            motor_process: MotorProcess instance for sending commands
            on_log: Optional callback function for logging (receives type and message)
        """
        self.motor_process = motor_process
        self.on_log = on_log
        self._running = False
        self._thread = None
        self._socket = None
        self.ip = '0.0.0.0'
        self.port = 8000

        # OSC sender for /movement and /reached
        self._send_socket = None
        self._send_ip = '127.0.0.1'
        self._send_port = 8000

        # Movement tracking
        self._movement_active = False
        self._movement_slave = 0
        self._movement_target = 0.0
        self._movement_value = 0
        self._movement_thread = None

        # Address handlers registry
        self._handlers = {}
        self._register_default_handlers()

    def _log(self, log_type, message):
        """Log a message."""
        print(f"[OSC Handler] [{log_type}] {message}")
        if self.on_log:
            self.on_log(log_type, message)

    def _register_default_handlers(self):
        """Register default OSC address handlers."""
        self.register_handler('/start', self._handle_start)
        self.register_handler('/move', self._handle_move)
        self.register_handler('/home', self._handle_home)
        self.register_handler('/stop', self._handle_stop)
        self.register_handler('/enable', self._handle_enable)
        self.register_handler('/disable', self._handle_disable)
        self.register_handler('/reset', self._handle_reset)
        self.register_handler('/reload', self._handle_reload)

    def register_handler(self, address, handler):
        """
        Register a handler for an OSC address.

        Args:
            address: OSC address string (e.g., '/start')
            handler: Function that takes (address, args) as parameters
        """
        self._handlers[address] = handler
        self._log('info', f"Registered handler for {address}")

    def set_value_map(self, value_map):
        """
        Update the value to position mapping for /start command.

        Args:
            value_map: Dictionary mapping integer values to float positions
        """
        self.START_VALUE_MAP = value_map
        self._log('info', f"Updated value map: {value_map}")

    def set_speed(self, velocity, accel=None, decel=None):
        """
        Set movement speed settings.

        Args:
            velocity: Movement velocity
            accel: Acceleration (defaults to velocity/2)
            decel: Deceleration (defaults to velocity/2)
        """
        self.MOVE_SPEED = velocity
        self.MOVE_ACCEL = accel if accel is not None else velocity // 2
        self.MOVE_DECEL = decel if decel is not None else velocity // 2
        self._log('info', f"Speed: vel={self.MOVE_SPEED}, accel={self.MOVE_ACCEL}, decel={self.MOVE_DECEL}")

    def _build_osc_message(self, address, *args):
        """Build an OSC message."""
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
        arg_bytes = b''
        for arg in args:
            if isinstance(arg, float):
                arg_bytes += struct.pack('>f', arg)
            elif isinstance(arg, int):
                arg_bytes += struct.pack('>i', arg)
            else:
                s = str(arg).encode('utf-8') + b'\x00'
                while len(s) % 4 != 0:
                    s += b'\x00'
                arg_bytes += s

        return addr_bytes + type_bytes + arg_bytes

    def _osc_send(self, address, *args):
        """Send an OSC message."""
        if self._send_socket is None:
            return

        try:
            message = self._build_osc_message(address, *args)
            self._send_socket.sendto(message, (self._send_ip, self._send_port))

            args_str = ' '.join(str(a) for a in args) if args else ''
            self._log('send', f"{address} {args_str}".strip())
        except Exception as e:
            self._log('error', f"Send error: {e}")

    def _parse_osc_message(self, data):
        """
        Parse incoming OSC message.

        Returns:
            (address, args) tuple or (None, None) if invalid
        """
        try:
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
            self._log('error', f"Parse error: {e}")
            return None, None

    def _send_command(self, cmd, data=None):
        """Send command to motor process if available."""
        if self.motor_process:
            self.motor_process.send_command(cmd, data)
            return True
        else:
            self._log('warn', f"No motor process - command not sent: {cmd}")
            return False

    def _movement_broadcaster(self):
        """
        Thread function that broadcasts /movement messages while slave is moving.
        When target is reached, sends /reached message.
        """
        slave_idx = self._movement_slave
        target_pos = self._movement_target
        start_value = self._movement_value

        self._log('info', f"Movement broadcaster started: slave {slave_idx} -> {target_pos}m (value={start_value})")

        last_sent_pos = None

        while self._movement_active and self._running:
            try:
                if not self.motor_process:
                    break

                # Get current position from motor process status
                status = self.motor_process.get_status()
                if status['num_slaves'] > slave_idx:
                    current_pos = status['positions'][slave_idx]

                    # Send /movement if position changed
                    if last_sent_pos is None or abs(current_pos - last_sent_pos) > 0.0001:
                        self._osc_send("/movement", slave_idx, float(current_pos))
                        last_sent_pos = current_pos

                    # Check if target reached (using config tolerance)
                    if abs(current_pos - target_pos) <= self.POSITION_TOLERANCE:
                        self._log('info', f"Target reached: {current_pos:.4f}m")

                        # Send final position
                        self._osc_send("/movement", slave_idx, float(target_pos))

                        # Send /reached
                        self._osc_send("/reached", start_value)

                        self._movement_active = False
                        break

                time.sleep(self.BROADCAST_INTERVAL)

            except Exception as e:
                self._log('error', f"Movement broadcaster error: {e}")
                break

        self._log('info', "Movement broadcaster stopped")

    # =========================================================================
    # OSC Address Handlers
    # =========================================================================

    def _handle_start(self, address, args):
        """
        Handle /start [value] command.
        Maps value to target position and moves slave0.
        Broadcasts /movement during motion and /reached when complete.

        Value mapping (configurable via set_value_map):
            1 -> 0.1m
            2 -> 0.5m
            3 -> 1.0m
            4 -> 1.5m
            5 -> 2.0m
        """
        if not args:
            self._log('warn', f"/start received without value")
            return

        value = int(args[0])
        target_position = self.START_VALUE_MAP.get(value)

        if target_position is None:
            self._log('warn', f"/start received unknown value: {value}")
            self._log('info', f"Valid values: {list(self.START_VALUE_MAP.keys())}")
            return

        self._log('recv', f"/start [{value}] -> Moving slave0 to {target_position}m")
        self._log('info', f"Speed: vel={self.MOVE_SPEED}, accel={self.MOVE_ACCEL}, decel={self.MOVE_DECEL}")

        # Stop any existing movement broadcaster
        self._movement_active = False
        if self._movement_thread and self._movement_thread.is_alive():
            self._movement_thread.join(timeout=0.5)

        # Apply speed settings
        self._send_command('set_speed', {
            'mode': 'PP',
            'velocity': self.MOVE_SPEED,
            'acceleration': self.MOVE_ACCEL,
            'deceleration': self.MOVE_DECEL
        })

        # Set movement tracking
        self._movement_slave = 0
        self._movement_target = target_position
        self._movement_value = value
        self._movement_active = True

        # Send move command
        self._send_command('move', {
            'positions': [target_position],
            'slave': 0
        })

        # Start movement broadcaster
        self._movement_thread = threading.Thread(
            target=self._movement_broadcaster,
            daemon=True
        )
        self._movement_thread.start()

    def _handle_move(self, address, args):
        """
        Handle /move [slave] [position] command.
        Moves specified slave to the given position in meters.
        """
        if len(args) < 2:
            self._log('warn', f"/move requires 2 arguments: slave, position")
            return

        slave = int(args[0])
        position = float(args[1])

        self._log('recv', f"/move slave{slave} to {position}m")

        self._send_command('move', {
            'positions': [position],
            'slave': slave
        })

    def _handle_home(self, address, args):
        """Handle /home command - move all slaves to home (0m)."""
        self._log('recv', f"/home -> Moving all to home")
        self._send_command('move_all_home')

    def _handle_stop(self, address, args):
        """Handle /stop command - emergency stop."""
        self._log('recv', f"/stop -> Emergency stop")

        # Stop movement broadcaster
        self._movement_active = False

        if self.motor_process:
            self.motor_process.emergency_stop()
        else:
            self._send_command('stop')

    def _handle_enable(self, address, args):
        """Handle /enable command - enable all drives."""
        self._log('recv', f"/enable -> Enabling drives")
        self._send_command('enable')

    def _handle_disable(self, address, args):
        """Handle /disable command - disable all drives."""
        self._log('recv', f"/disable -> Disabling drives")
        self._send_command('disable')

    def _handle_reset(self, address, args):
        """Handle /reset command - reset faults."""
        self._log('recv', f"/reset -> Resetting faults")
        self._send_command('reset')

    def _handle_reload(self, address, args):
        """Handle /reload command - reload config from rotorscope_config.json."""
        self._log('recv', f"/reload -> Reloading configuration")
        OSCHandler.reload_config()
        self._log('info', f"New value map: {self.START_VALUE_MAP}")

    # =========================================================================
    # Server Methods
    # =========================================================================

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

        self._log('info', f"OSC Handler listening on {self.ip}:{self.port}")
        self._log('info', f"Value mapping: {self.START_VALUE_MAP}")
        self._log('info', f"Speed: vel={self.MOVE_SPEED}, accel={self.MOVE_ACCEL}, decel={self.MOVE_DECEL}")

        while self._running:
            try:
                data, addr = self._socket.recvfrom(4096)

                address, args = self._parse_osc_message(data)
                if address is None:
                    continue

                # Log received message
                args_str = ' '.join(str(a) for a in args) if args else ''
                self._log('recv', f"Received: {address} [{args_str}]")

                # Find and call handler
                handler = self._handlers.get(address)
                if handler:
                    try:
                        handler(address, args)
                    except Exception as e:
                        self._log('error', f"Handler error for {address}: {e}")
                else:
                    self._log('warn', f"No handler for address: {address}")

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    self._log('error', f"Receiver error: {e}")

        self._socket.close()
        self._log('info', "OSC Handler stopped")

    def start(self, ip='0.0.0.0', port=8000, send_ip='127.0.0.1', send_port=None):
        """
        Start the OSC handler.

        Args:
            ip: IP address to bind to (default: '0.0.0.0' for all interfaces)
            port: Port to listen on (default: 8000)
            send_ip: IP to send OSC messages to (default: '127.0.0.1')
            send_port: Port to send OSC messages to (default: same as receive port)
        """
        if self._running:
            self._log('warn', "OSC Handler already running")
            return

        # Reload config on start to pick up any changes
        OSCHandler.reload_config()

        self.ip = ip
        self.port = port
        self._send_ip = send_ip
        self._send_port = send_port if send_port is not None else port
        self._running = True

        # Create send socket
        self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the OSC handler."""
        if not self._running:
            return

        self._running = False
        self._movement_active = False

        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._send_socket:
            self._send_socket.close()
            self._send_socket = None

    def is_running(self):
        """Check if handler is running."""
        return self._running


# =============================================================================
# Standalone Testing
# =============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='OSC Handler for Motor Control')
    parser.add_argument('--ip', default='0.0.0.0', help='IP to bind (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8000, help='Port to listen (default: 8000)')
    parser.add_argument('--send-ip', default='127.0.0.1', help='IP to send OSC to (default: 127.0.0.1)')
    args = parser.parse_args()

    print("=" * 60)
    print("OSC Handler - Standalone Test Mode")
    print("=" * 60)
    print(f"Config: osc/rotorscope_config.json")
    print(f"Listening on {args.ip}:{args.port}")
    print(f"Sending to {args.send_ip}:{args.port}")
    print()
    print("Value Mapping for /start:")
    for val, pos in sorted(OSCHandler.START_VALUE_MAP.items()):
        print(f"  {val} -> {pos}m")
    print()
    print(f"Speed Settings:")
    print(f"  Velocity: {OSCHandler.MOVE_SPEED}")
    print(f"  Acceleration: {OSCHandler.MOVE_ACCEL}")
    print(f"  Deceleration: {OSCHandler.MOVE_DECEL}")
    print()
    print(f"Movement Settings:")
    print(f"  Position Tolerance: {OSCHandler.POSITION_TOLERANCE}m")
    print(f"  Broadcast Interval: {OSCHandler.BROADCAST_INTERVAL}s")
    print()
    print("Supported Commands:")
    print("  /start [0-5]           - Move slave0 to mapped position")
    print("                           Sends /movement during motion")
    print("                           Sends /reached when complete")
    print("  /move [slave] [pos]    - Move slave to position")
    print("  /home                  - Move all to home")
    print("  /stop                  - Emergency stop")
    print("  /enable                - Enable drives")
    print("  /disable               - Disable drives")
    print("  /reset                 - Reset faults")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)

    # Create handler without motor process (test mode)
    handler = OSCHandler()
    handler.start(ip=args.ip, port=args.port, send_ip=args.send_ip)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        handler.stop()
        print("Done")
