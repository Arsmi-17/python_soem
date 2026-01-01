#!/usr/bin/env python3
"""
EtherCAT Motor Control - Main Program
Based on semicon_slider.c

Controls:
    i - Enable all drives
    o - Disable all drives
    u - Reset all faults

    h - Home (0 m)
    1 - Move to +0.5 m
    2 - Move to -0.5 m
    3 - Move to +1.0 m
    4 - Move to -1.0 m

    t - Run template sequence

    p - Print positions
    l - Print status
    q - Quit
"""

import sys
import time
import threading


def force_stay_awake():
    """
    Force Windows to keep this process awake and prevent throttling.
    Called FIRST before anything else to ensure background operation works.
    """
    if sys.platform != 'win32':
        return

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        winmm = ctypes.windll.winmm

        # Set multimedia timer to 1ms resolution (system-wide)
        winmm.timeBeginPeriod(1)

        # CRITICAL: Prevent Windows from throttling when backgrounded
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED | ES_DISPLAY_REQUIRED
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ES_AWAYMODE_REQUIRED = 0x00000040
        kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED | ES_DISPLAY_REQUIRED
        )

        # Set process priority to REALTIME (requires admin) or HIGH
        current_process = kernel32.GetCurrentProcess()
        result = kernel32.SetPriorityClass(current_process, 0x100)  # REALTIME_PRIORITY_CLASS
        if not result:
            kernel32.SetPriorityClass(current_process, 0x80)  # HIGH_PRIORITY_CLASS

        # Disable priority boost (prevent Windows from lowering priority)
        kernel32.SetProcessPriorityBoost(current_process, True)

        print("[MAIN] Force stay-awake: ENABLED")
        print("[MAIN] Process priority: REALTIME/HIGH")
        print("[MAIN] Background throttling: DISABLED")
    except Exception as e:
        print(f"[MAIN] Stay-awake setup warning: {e}")


# Force stay awake IMMEDIATELY at module load
force_stay_awake()

# Keyboard input
try:
    import msvcrt
    def get_key():
        return msvcrt.getch().decode('utf-8', errors='ignore')
    
    def key_available():
        return msvcrt.kbhit()
    
    IS_WINDOWS = True
except ImportError:
    import tty
    import termios
    import select
    
    def get_key():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    
    def key_available():
        return select.select([sys.stdin], [], [], 0)[0] != []
    
    IS_WINDOWS = False

import pysoem
from ethercat_controller import EtherCATController
from movement_controller import MovementController


# Global stop flag for template
stop_template = False


def check_stop():
    """Check if 'q' was pressed to stop template"""
    global stop_template
    if key_available():
        k = get_key()
        if k == 'q':
            stop_template = True
            return True
    return stop_template


def list_adapters():
    """List network adapters"""
    print("\nAvailable adapters:")
    print("-" * 60)
    adapters = pysoem.find_adapters()
    for i, a in enumerate(adapters):
        print(f"  {i}: {a.desc}")
    print("-" * 60)
    return adapters


def main():
    global stop_template
    
    print("="*60)
    print("EtherCAT Motor Control")
    print("="*60)
    
    # Select adapter
    adapters = list_adapters()
    if not adapters:
        print("No adapters found!")
        return
    
    print("\nSelect adapter (Enter=0): ", end="", flush=True)
    try:
        choice = input().strip()
        idx = int(choice) if choice else 0
        interface = adapters[idx].name
    except (ValueError, IndexError):
        interface = adapters[0].name
    
    print(f"\nUsing: {interface}")
    
    # Initialize
    ec = EtherCATController(interface)
    if not ec.connect():
        print("Failed to connect!")
        return
    
    mv = MovementController(ec)
    if not mv.initialize():
        print("Failed to initialize movement controller!")
        ec.disconnect()
        return
    
    # Setup patterns and template
    mv.setup_default_patterns()
    mv.setup_template()
    
    # Print controls
    print("\n" + "="*60)
    print("CONTROLS:")
    print("  i - Enable all    o - Disable all    u - Reset faults")
    print("  h - Home (0m)")
    print("  1 - +0.5m         2 - -0.5m")
    print("  3 - +1.0m         4 - -1.0m")
    print("  t - Run template  T - Show template")
    print("  r - Reload config c - Show config")
    print("  p - Positions     l - Status         q - Quit")
    print("="*60)
    print("\nReady! Press a key...\n")
    
    # Main loop
    running = True
    while running:
        key = get_key()
        
        if key == 'q':
            print("\nQuitting...")
            running = False
        
        elif key == 'i':
            ec.enable_all()
        
        elif key == 'o':
            ec.disable_all()
        
        elif key == 'u':
            ec.reset_all()
        
        elif key == 'h':
            mv.home_all()
        
        elif key == '1':
            mv.move_all_to_meters([0.5])
        
        elif key == '2':
            mv.move_all_to_meters([-0.5])
        
        elif key == '3':
            mv.move_all_to_meters([1.0])
        
        elif key == '4':
            mv.move_all_to_meters([-1.0])
        
        elif key == 't':
            print("\n>>> Starting template (press 'q' to stop) <<<")
            stop_template = False
            mv.run_template(stop_check=check_stop)
            print("\n>>> Template finished <<<\n")
        
        elif key == 'T':
            mv.print_template()
        
        elif key == 'r':
            mv.reload_config()
        
        elif key == 'c':
            mv.print_config()
        
        elif key == 'p':
            mv.print_all_positions()
        
        elif key == 'l':
            ec.print_status()
    
    # Cleanup
    ec.disable_all()
    ec.disconnect()
    print("Done!")


if __name__ == "__main__":
    main()