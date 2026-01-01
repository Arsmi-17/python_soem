#!/usr/bin/env python3
"""
Run motor control as a Windows Service or detached process.
This prevents Windows from throttling when terminal is backgrounded.

Usage:
    python run_as_service.py          # Run detached from terminal
    python run_as_service.py --service # Install as Windows service (requires pywin32)
"""

import sys
import os
import subprocess
import ctypes

def is_admin():
    """Check if running as administrator"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_detached():
    """
    Launch the web server as a completely detached process.
    This process will NOT be affected by terminal window state.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable
    web_script = os.path.join(script_dir, "run_web.py")

    # CREATE_NEW_PROCESS_GROUP (0x200) - New process group, not tied to terminal
    # DETACHED_PROCESS (0x8) - No console window
    # CREATE_NO_WINDOW (0x08000000) - Truly windowless
    creation_flags = 0x00000008 | 0x00000200 | 0x08000000

    # Start the process completely detached
    process = subprocess.Popen(
        [python_exe, web_script],
        cwd=script_dir,
        creationflags=creation_flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True
    )

    print(f"Motor control started as detached process (PID: {process.pid})")
    print(f"Web UI: http://localhost:8000")
    print(f"\nThis terminal can now be closed safely.")
    print(f"To stop: taskkill /PID {process.pid} /F")

    # Save PID to file for easy stopping
    pid_file = os.path.join(script_dir, "motor_service.pid")
    with open(pid_file, 'w') as f:
        f.write(str(process.pid))
    print(f"PID saved to: {pid_file}")

def stop_detached():
    """Stop the detached process using saved PID"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pid_file = os.path.join(script_dir, "motor_service.pid")

    if os.path.exists(pid_file):
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())

        try:
            os.kill(pid, 9)  # SIGKILL
            print(f"Stopped process {pid}")
            os.remove(pid_file)
        except ProcessLookupError:
            print(f"Process {pid} not running")
            os.remove(pid_file)
        except Exception as e:
            print(f"Error stopping process: {e}")
            # Try taskkill as fallback
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
    else:
        print("No PID file found. Try: taskkill /IM python.exe /F")

def main():
    if not is_admin():
        print("WARNING: Not running as Administrator.")
        print("For best performance, run as Administrator.")
        print("")

    if len(sys.argv) > 1:
        if sys.argv[1] == '--stop':
            stop_detached()
            return
        elif sys.argv[1] == '--service':
            print("Windows Service installation requires pywin32.")
            print("Install with: pip install pywin32")
            print("For now, use the detached process mode (default).")
            return

    run_detached()

if __name__ == "__main__":
    main()
