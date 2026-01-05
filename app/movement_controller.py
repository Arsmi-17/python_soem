#!/usr/bin/env python3
"""
Movement Controller Module
Based on semicon_slider.c

Supports:
  - PP (Profile Position) mode: Drive generates trajectory
  - CSP (Cyclic Synchronous Position) mode: Master generates trajectory

Configuration loaded from config.json
"""

import time
import json
import os
import math


# ============================================================
# LOAD CONFIGURATION FROM JSON
# ============================================================

CONFIG_FILE = "config.json"

def load_config():
    """Load configuration from JSON file"""
    # Find config file (same directory as this script)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, CONFIG_FILE)
    
    if not os.path.exists(config_path):
        # Try current directory
        config_path = CONFIG_FILE
    
    if not os.path.exists(config_path):
        print(f"WARNING: {CONFIG_FILE} not found, using defaults")
        return None
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        print(f"Loaded config from: {config_path}")
        return config
    except Exception as e:
        print(f"ERROR loading config: {e}")
        return None


# Load config at module import
_CONFIG = load_config()


class MovementController:
    """Movement controller for servo drives - config from JSON"""
    
    # Default values (overridden by config.json)
    DEFAULT_RAW_STEPS_PER_METER = 202985985
    DEFAULT_ACTUAL_STEPS_PER_METER = 792914
    DEFAULT_VELOCITY = 80000
    DEFAULT_ACCEL = 6000
    DEFAULT_DELAY = 0.5
    
    def __init__(self, ec_controller):
        self.ec = ec_controller
        self.slaves_count = 0
        self._speed = {}
        
        # Load from config
        self._load_config()
    
    def _load_config(self):
        """Load settings from config"""
        global _CONFIG
        
        if _CONFIG is None:
            _CONFIG = load_config()
        
        if _CONFIG:
            # Config section
            cfg = _CONFIG.get('config', {})
            self.RAW_STEPS_PER_METER = cfg.get('raw_steps_per_meter', self.DEFAULT_RAW_STEPS_PER_METER)
            self.ACTUAL_STEPS_PER_METER = cfg.get('actual_steps_per_meter', self.DEFAULT_ACTUAL_STEPS_PER_METER)
            self.DEFAULT_DELAY = cfg.get('default_delay', 0.5)
            self._num_slaves_config = cfg.get('num_slaves', 1)
            
            # Mode
            mode_str = cfg.get('mode', 'CSP').upper()
            self._mode = 8 if mode_str == 'CSP' else 1
            
            # Speed section
            spd = _CONFIG.get('speed', {})
            self._velocity = spd.get('velocity', self.DEFAULT_VELOCITY)
            self._accel = spd.get('acceleration', self.DEFAULT_ACCEL)
            self._decel = spd.get('deceleration', self.DEFAULT_ACCEL)
            
            # Positions
            self._positions = _CONFIG.get('positions', {'HOME': [0.0]})
            
            # Template
            self._template = _CONFIG.get('template', {
                'name': 'DEFAULT',
                'loop': False,
                'loop_count': 1,
                'sequence': [['HOME']]
            })
        else:
            # Use defaults
            self.RAW_STEPS_PER_METER = self.DEFAULT_RAW_STEPS_PER_METER
            self.ACTUAL_STEPS_PER_METER = self.DEFAULT_ACTUAL_STEPS_PER_METER
            self._velocity = self.DEFAULT_VELOCITY
            self._accel = self.DEFAULT_ACCEL
            self._decel = self.DEFAULT_ACCEL
            self._num_slaves_config = 1
            self._mode = 8  # CSP default
            self._positions = {'HOME': [0.0]}
            self._template = {'name': 'DEFAULT', 'loop': False, 'loop_count': 1, 'sequence': [['HOME']]}
        
        # Calculate scale factor
        self.SCALE_FACTOR = self.ACTUAL_STEPS_PER_METER / self.RAW_STEPS_PER_METER
        
        # CSP trajectory parameters (in scaled units per second)
        self._csp_max_velocity = self._velocity  # steps/s
        self._csp_acceleration = self._accel     # steps/s²
        self._csp_cycle_time = 0.001             # 1ms PDO cycle
    
    def reload_config(self):
        """Reload config from JSON file"""
        global _CONFIG
        _CONFIG = load_config()
        self._load_config()
        print("Config reloaded!")
        self.print_config()
    
    def print_config(self):
        """Print current configuration"""
        print(f"\n{'='*60}")
        print("CONFIGURATION")
        print(f"{'='*60}")
        print(f"  Mode: {'CSP' if self._mode == 8 else 'PP'}")
        print(f"  Num slaves (config): {self._num_slaves_config}")
        print(f"  Num slaves (actual): {self.slaves_count}")
        print(f"  ACTUAL_STEPS_PER_METER: {self.ACTUAL_STEPS_PER_METER}")
        print(f"  RAW_STEPS_PER_METER: {self.RAW_STEPS_PER_METER}")
        print(f"  SCALE_FACTOR: {self.SCALE_FACTOR}")
        print(f"\nSpeed:")
        print(f"  Velocity: {self._velocity}")
        print(f"  Acceleration: {self._accel}")
        print(f"  Deceleration: {self._decel}")
        print(f"\nPositions:")
        for name, pos in self._positions.items():
            print(f"  {name}: {pos}")
        print(f"\nTemplate: {self._template.get('name', 'N/A')}")
        print(f"{'='*60}")
    
    def initialize(self):
        """Initialize after EtherCAT connection"""
        if not self.ec.connected:
            print("ERROR: Not connected!")
            return False
        
        self.slaves_count = self.ec.slaves_count
        
        # Apply speed from config to all slaves
        for i in range(self.slaves_count):
            self._speed[i] = {
                'velocity': self._velocity,
                'accel': self._accel,
                'decel': self._decel
            }
            self.ec.configure_speed(i, self._velocity, self._accel, self._decel)
        
        print(f"\nMovement controller initialized")
        print(f"  Slaves: {self.slaves_count}")
        print(f"  Mode: {'CSP' if self._mode == 8 else 'PP'}")
        print(f"  ACTUAL_STEPS_PER_METER: {self.ACTUAL_STEPS_PER_METER}")
        print(f"  Velocity: {self._velocity}")
        return True
    
    def enable_and_home(self):
        """Enable all drives and immediately home (required for CSP mode)"""
        print(f"\n{'='*60}")
        print(f"ENABLE AND HOME ({'CSP' if self._mode == 8 else 'PP'} mode)")
        print(f"{'='*60}\n")
        
        # Enable all drives
        # if not self.ec.enable_all():
        #     print("WARNING: Not all drives enabled")
        self.ec.enable_all()
        
        # For CSP mode, immediately set target to home (0) and keep it there
        if self._mode == 8:  # CSP
            print("\nCSP Mode: Setting all targets to HOME (0)...")
            for i in range(self.slaves_count):
                # Set target to 0 (home) - this will be sent every cycle by PDO loop
                self.ec.csp_set_target(i, 0)
                print(f"  Slave {i}: target = 0 (HOME)")
            
            print("\nCSP Mode: Targets set. Motors will move to home.")
            print("The PDO loop is continuously sending position 0.")
        
        return True
    
    # ========== Position Conversion ==========
    
    def raw_to_scaled(self, raw):
        """Convert raw position to scaled"""
        return raw * self.SCALE_FACTOR
    
    def scaled_to_meters(self, scaled):
        """Convert scaled to meters"""
        return scaled / self.ACTUAL_STEPS_PER_METER
    
    def meters_to_scaled(self, meters):
        """Convert meters to scaled (for target position)"""
        return meters * self.ACTUAL_STEPS_PER_METER
    
    def get_position_scaled(self, idx):
        """Get current position in scaled steps"""
        raw = self.ec.read_position_raw(idx)
        return self.raw_to_scaled(raw)
    
    def get_position_meters(self, idx):
        """Get current position in meters"""
        scaled = self.get_position_scaled(idx)
        return self.scaled_to_meters(scaled)
    
    # ========== Speed ==========
    
    def set_speed(self, idx, velocity, accel=None, decel=None):
        """Set speed for a slave"""
        if accel is None:
            accel = velocity // 2
        if decel is None:
            decel = accel
        
        self._speed[idx] = {'velocity': velocity, 'accel': accel, 'decel': decel}
        self.ec.configure_speed(idx, velocity, accel, decel)
    
    def set_all_speeds(self, velocity, accel=None, decel=None):
        """Set speed for all slaves"""
        for i in range(self.slaves_count):
            self.set_speed(i, velocity, accel, decel)
    
    def _apply_speed(self, idx):
        """Apply stored speed"""
        s = self._speed.get(idx, {})
        self.ec.configure_speed(
            idx,
            s.get('velocity', self.DEFAULT_VELOCITY),
            s.get('accel', self.DEFAULT_ACCEL),
            s.get('decel', self.DEFAULT_ACCEL)
        )
    
    # ========== Print ==========
    
    def print_position(self, idx):
        """Print position of a slave"""
        raw = self.ec.read_position_raw(idx)
        scaled = self.raw_to_scaled(raw)
        meters = self.scaled_to_meters(scaled)
        
        print("=======================================")
        print(f"Slave {idx}")
        print(f"  Raw      : {raw}")
        print(f"  Scaled   : {scaled:.2f}")
        print(f"  Meters   : {meters:.4f} m")
        print("=======================================")
    
    def print_all_positions(self):
        """Print all positions"""
        for i in range(self.slaves_count):
            self.print_position(i)
    
    # ========== Wait for Target ==========
    
    def _generate_csp_trajectory(self, start_pos, end_pos):
        """
        Generate trapezoidal velocity profile trajectory for CSP mode
        Returns list of positions for each cycle
        """
        distance = end_pos - start_pos
        if abs(distance) < 1:
            return [end_pos]
        
        direction = 1 if distance > 0 else -1
        distance = abs(distance)
        
        max_vel = self._csp_max_velocity
        accel = self._csp_acceleration
        dt = self._csp_cycle_time
        
        # Time to accelerate to max velocity
        t_accel = max_vel / accel
        # Distance during acceleration
        d_accel = 0.5 * accel * t_accel * t_accel
        
        if 2 * d_accel >= distance:
            # Triangle profile (can't reach max velocity)
            t_accel = math.sqrt(distance / accel)
            d_accel = distance / 2
            t_cruise = 0
        else:
            # Trapezoidal profile
            d_cruise = distance - 2 * d_accel
            t_cruise = d_cruise / max_vel
        
        # Generate trajectory points
        trajectory = []
        t = 0
        pos = start_pos
        vel = 0
        
        # Acceleration phase
        while vel < max_vel and pos * direction < (start_pos + d_accel * direction) * direction:
            vel += accel * dt
            if vel > max_vel:
                vel = max_vel
            pos += vel * dt * direction
            trajectory.append(int(pos))
            t += dt
        
        # Cruise phase
        while pos * direction < (start_pos + (d_accel + (distance - 2 * d_accel)) * direction) * direction:
            pos += max_vel * dt * direction
            trajectory.append(int(pos))
            t += dt
        
        # Deceleration phase
        while vel > 0:
            vel -= accel * dt
            if vel < 0:
                vel = 0
            pos += vel * dt * direction
            trajectory.append(int(pos))
            t += dt
        
        # Ensure we end at target
        trajectory.append(int(end_pos))
        
        return trajectory
    
    def move_csp(self, idx, target_scaled, timeout=10000):
        """
        CSP mode movement with trajectory generation
        Master generates trajectory and sends position each cycle
        """
        start_pos = self.get_position_scaled(idx)
        target_m = self.scaled_to_meters(target_scaled)
        
        print(f"  CSP move: {self.scaled_to_meters(start_pos):.4f}m -> {target_m:.4f}m")
        
        # Generate trajectory
        trajectory = self._generate_csp_trajectory(start_pos, target_scaled)
        print(f"  Trajectory points: {len(trajectory)}")
        
        # Start CSP movement (stop tracking actual position)
        self.ec.csp_start_move(idx)
        
        try:
            # Execute trajectory
            for i, pos in enumerate(trajectory):
                self.ec.csp_set_target(idx, pos)
                time.sleep(self._csp_cycle_time)
                
                # Print progress every 100 points
                if i % 100 == 0:
                    current = self.get_position_meters(idx)
                    print(f"  [{i:5d}/{len(trajectory)}] pos={current:.4f}m")
            
            # Wait for settling
            time.sleep(0.1)
            
        finally:
            # Stop CSP movement (resume tracking actual position)
            self.ec.csp_stop_move(idx)
        
        # Check final position
        final = self.get_position_meters(idx)
        error = abs(final - target_m)
        
        if error < 0.001:  # 1mm tolerance
            print(f"  CSP move complete: {final:.4f}m (error: {error*1000:.2f}mm)")
            return True
        else:
            print(f"  CSP move warning: {final:.4f}m (error: {error*1000:.2f}mm)")
            return True  # Still return true, just with warning
    
    def wait_for_target(self, idx, target_scaled, timeout=10000):
        """Wait for target reached using status bit"""
        target_m = self.scaled_to_meters(target_scaled)
        
        retries = 0
        while retries < timeout:
            status = self.ec.read_status(idx)
            scaled = self.get_position_scaled(idx)
            meters = self.scaled_to_meters(scaled)
            
            if retries % 100 == 0:
                tr = "YES" if self.ec.is_target_reached(idx) else "no"
                print(f"  [{retries:5d}] {meters:8.4f}m -> {target_m:8.4f}m | "
                      f"st=0x{status:04X} | TgtRchd={tr}")
            
            if self.ec.is_target_reached(idx):
                print(f"\n  Slave {idx} TARGET REACHED!")
                time.sleep(0.5)
                return True
            
            if self.ec.has_fault(idx):
                print(f"\n  Slave {idx} FAULT!")
                return False
            
            time.sleep(0.01)
            retries += 1
        
        print(f"\n  Slave {idx} TIMEOUT!")
        return False
    
    def wait_all_targets(self, targets, timeout=10000):
        """Wait for all slaves to reach targets"""
        pending = set(targets.keys())
        retries = 0
        
        while pending and retries < timeout:
            if retries % 100 == 0:
                print(f"\n--- Iteration {retries} ---")
                for idx in sorted(targets.keys()):
                    meters = self.get_position_meters(idx)
                    target_m = self.scaled_to_meters(targets[idx])
                    tr = "YES" if self.ec.is_target_reached(idx) else "no"
                    status = "DONE" if idx not in pending else "moving"
                    print(f"  Slave {idx}: {meters:8.4f}m -> {target_m:8.4f}m | TgtRchd={tr} [{status}]")
            
            for idx in list(pending):
                if self.ec.is_target_reached(idx):
                    pending.remove(idx)
                    print(f"\n  Slave {idx} TARGET REACHED!")
            
            time.sleep(0.01)
            retries += 1
        
        if pending:
            print(f"\n  Slaves {list(pending)} TIMEOUT!")
            return False
        return True
    
    # ========== Movement ==========
    
    def move_to_meters(self, idx, meters, wait=True):
        """Move single slave to position in meters"""
        if idx >= self.slaves_count:
            print(f"ERROR: Slave {idx} does not exist!")
            return False
        
        if not self.ec.is_enabled(idx):
            print(f"ERROR: Slave {idx} not enabled!")
            return False
        
        target_scaled = self.meters_to_scaled(meters)
        
        print(f"\n{'='*50}")
        print(f"Moving slave {idx} to {meters:.4f} m")
        print(f"  Target (scaled): {target_scaled:.0f}")
        print(f"  Mode: {'CSP' if self._mode == 8 else 'PP'}")
        print(f"{'='*50}")
        
        self.print_position(idx)
        
        self._apply_speed(idx)
        
        if self._mode == 8:  # CSP mode
            result = self.move_csp(idx, target_scaled)
        else:  # PP mode
            self.ec.move_to_position(idx, target_scaled)
            s = self.ec.read_status(idx)
            print(f"  Status after move cmd: 0x{s:04X} [{self.ec.decode_status(s)}]")
            
            if wait:
                result = self.wait_for_target(idx, target_scaled)
            else:
                result = True
        
        self.print_position(idx)
        return result
    
    def move_all_to_meters(self, positions, wait=True, sequential=False):
        """Move multiple slaves to positions"""
        if isinstance(positions, list):
            positions = {i: p for i, p in enumerate(positions) if i < self.slaves_count}
        
        positions = {k: v for k, v in positions.items() if k < self.slaves_count}
        
        if not positions:
            print("ERROR: No valid positions!")
            return False
        
        # Convert to scaled
        targets = {idx: self.meters_to_scaled(m) for idx, m in positions.items()}
        
        print(f"\n{'='*60}")
        print(f"Moving {len(positions)} slaves {'SEQUENTIALLY' if sequential else 'IN PARALLEL'}")
        print(f"{'='*60}")
        
        for idx, m in sorted(positions.items()):
            print(f"  Slave {idx}: target = {m:.4f} m ({targets[idx]:.0f} scaled)")
        
        print("\nCurrent positions:")
        for idx in sorted(positions.keys()):
            self.print_position(idx)
        
        if sequential:
            for idx in sorted(positions.keys()):
                if not self.ec.is_enabled(idx):
                    print(f"\n  Slave {idx} not enabled, skipping")
                    continue
                
                print(f"\nMoving slave {idx}...")
                self._apply_speed(idx)
                self.ec.move_to_position(idx, targets[idx])
                
                if wait:
                    self.wait_for_target(idx, targets[idx])
        else:
            # Start all moves
            for idx in sorted(positions.keys()):
                if not self.ec.is_enabled(idx):
                    print(f"\n  Slave {idx} not enabled, skipping")
                    continue
                
                print(f"\nStarting slave {idx}...")
                self._apply_speed(idx)
                self.ec.move_to_position(idx, targets[idx])
                time.sleep(0.03)
            
            # Wait for all
            if wait:
                self.wait_all_targets(targets)
        
        print(f"\n{'='*60}")
        print("COMPLETE")
        print(f"{'='*60}")
        
        print("\nFinal positions:")
        for idx in sorted(positions.keys()):
            self.print_position(idx)
        
        return True
    
    # ========== Patterns ==========
    
    def home_all(self, wait=True):
        """Home all slaves (move to 0)"""
        positions = {i: 0.0 for i in range(self.slaves_count)}
        return self.move_all_to_meters(positions, wait=wait)
    
    def define_pattern(self, name, positions):
        """Define a movement pattern"""
        if not hasattr(self, '_patterns'):
            self._patterns = {}
        self._patterns[name] = positions
        print(f"Pattern '{name}' defined: {positions}")
    
    def run_pattern(self, name, wait=True):
        """Run a defined pattern"""
        if not hasattr(self, '_patterns') or name not in self._patterns:
            print(f"ERROR: Pattern '{name}' not defined!")
            return False
        
        print(f"\n>>> Running pattern: {name}")
        return self.move_all_to_meters(self._patterns[name], wait=wait)
    
    def setup_default_patterns(self):
        """Setup default patterns from config"""
        for name, pos in self._positions.items():
            self.define_pattern(name, pos)
    
    # ========== TEMPLATE SYSTEM ==========
    
    def setup_template(self):
        """Template is already loaded from config.json"""
        print(f"\nTemplate '{self._template.get('name', 'N/A')}' loaded from config:")
        print(f"  Loop: {self._template.get('loop', False)}")
        print(f"  Loop count: {self._template.get('loop_count', 1)}")
        print(f"  Steps: {len(self._template.get('sequence', []))}")
        for i, step in enumerate(self._template.get('sequence', [])):
            pos_name = step[0] if step else 'N/A'
            delay = step[1] if len(step) > 1 else self.DEFAULT_DELAY
            positions = self._positions.get(pos_name, ['???'])
            print(f"    {i+1}. {pos_name} -> {positions}, delay={delay}s")
    
    def run_template(self, stop_check=None):
        """
        Run the template sequence from config.json
        
        Args:
            stop_check: Optional function that returns True to stop
        """
        template = self._template
        
        print(f"\n{'='*60}")
        print(f"RUNNING TEMPLATE: {template.get('name', 'N/A')}")
        print(f"{'='*60}")
        
        loop_count = 0
        max_loops = template.get('loop_count', 1)
        infinite_loop = template.get('loop', False)
        sequence = template.get('sequence', [])
        
        if not sequence:
            print("ERROR: No sequence defined in template!")
            return False
        
        try:
            while True:
                loop_count += 1
                
                if infinite_loop:
                    print(f"\n--- Loop {loop_count} (infinite, press 'q' to stop) ---")
                else:
                    print(f"\n--- Loop {loop_count}/{max_loops} ---")
                
                # Execute each step
                for step_num, step in enumerate(sequence):
                    pos_name = step[0] if step else None
                    delay = step[1] if len(step) > 1 else self.DEFAULT_DELAY
                    
                    if not pos_name:
                        continue
                    
                    # Get positions from config
                    positions = self._positions.get(pos_name)
                    if positions is None:
                        print(f"  ERROR: Position '{pos_name}' not defined in config!")
                        continue
                    
                    print(f"\n[Step {step_num + 1}/{len(sequence)}] {pos_name} -> {positions}")
                    
                    # Check for stop request
                    if stop_check and stop_check():
                        print("\n>>> STOPPED BY USER <<<")
                        return True
                    
                    # Move all slaves
                    self.move_all_to_meters(positions, wait=True)
                    
                    # Delay
                    if delay > 0:
                        print(f"  Waiting {delay}s...")
                        time.sleep(delay)
                
                # Check loop condition
                if not infinite_loop and loop_count >= max_loops:
                    break
                
                # Check for stop request between loops
                if stop_check and stop_check():
                    print("\n>>> STOPPED BY USER <<<")
                    return True
        
        except KeyboardInterrupt:
            print("\n>>> INTERRUPTED <<<")
            return False
        
        print(f"\n{'='*60}")
        print(f"TEMPLATE COMPLETE: {loop_count} loop(s)")
        print(f"{'='*60}")
        
        return True
    
    def print_template(self):
        """Print current template from config"""
        template = self._template
        print(f"\n{'='*60}")
        print(f"TEMPLATE: {template.get('name', 'N/A')}")
        print(f"{'='*60}")
        print(f"  Loop: {template.get('loop', False)}")
        print(f"  Loop count: {template.get('loop_count', 1)}")
        print(f"  Default delay: {self.DEFAULT_DELAY}s")
        print(f"\n  POSITIONS (from config.json):")
        for name, pos in self._positions.items():
            print(f"    {name}: {pos}")
        print(f"\n  SEQUENCE:")
        for i, step in enumerate(template.get('sequence', [])):
            pos_name = step[0] if step else 'N/A'
            delay = step[1] if len(step) > 1 else self.DEFAULT_DELAY
            positions = self._positions.get(pos_name, ['???'])
            print(f"    {i+1}. {pos_name} -> {positions}, delay={delay}s")
        print(f"{'='*60}")