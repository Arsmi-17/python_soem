#!/usr/bin/env python3
"""
EtherCAT Motor Control - Launcher

This script starts the web server for motor control.
Open http://localhost:8000 in your browser.
"""

import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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

        print("[LAUNCHER] Force stay-awake: ENABLED")
        print("[LAUNCHER] Process priority: REALTIME/HIGH")
        print("[LAUNCHER] Background throttling: DISABLED")
    except Exception as e:
        print(f"[LAUNCHER] Stay-awake setup warning: {e}")


# Force stay awake IMMEDIATELY at module load
force_stay_awake()


def check_dependencies():
    """Check if required packages are installed"""
    missing = []
    
    try:
        import pysoem
    except ImportError:
        missing.append('pysoem')
    
    try:
        import fastapi
    except ImportError:
        missing.append('fastapi')
    
    try:
        import uvicorn
    except ImportError:
        missing.append('uvicorn')
    
    if missing:
        print("="*60)
        print("Missing dependencies!")
        print("="*60)
        print(f"\nPlease install: {', '.join(missing)}")
        print("\nRun:")
        print("  pip install -r requirements.txt")
        print("\nOr:")
        print(f"  pip install {' '.join(missing)}")
        print("="*60)
        return False
    
    return True


def main():
    print("="*60)
    print("EtherCAT Motor Control - Web UI")
    print("="*60)
    
    if not check_dependencies():
        sys.exit(1)
    
    print("\nStarting server...")
    print("\n" + "="*60)
    print("  Open in browser: http://localhost:8000")
    print("="*60)
    print("\nKeyboard shortcuts (in browser):")
    print("  i - Enable    o - Disable    u - Reset")
    print("  h - Home      1 - +0.5m      2 - -0.5m")
    print("  t - Template  r - Reload config")
    print("\nPress Ctrl+C to stop")
    print("="*60 + "\n")
    
    import uvicorn
    from web_server import app
    
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()