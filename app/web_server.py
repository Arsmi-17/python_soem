#!/usr/bin/env python3
"""
Web Server - FastAPI with WebSocket for real-time motor control UI
"""

import asyncio
import json
import os
import sys
from typing import List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

from motor_process import MotorProcess


def force_stay_awake():
    """
    Force Windows to keep this process awake and prevent throttling.
    Must be called at process start before any other operations.
    """
    if sys.platform != 'win32':
        return

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        winmm = ctypes.windll.winmm

        # Set multimedia timer to 1ms resolution
        winmm.timeBeginPeriod(1)

        # Prevent Windows from throttling - ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)

        # Set process priority to HIGH
        current_process = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(current_process, 0x80)  # HIGH_PRIORITY_CLASS

        # Disable priority boost
        kernel32.SetProcessPriorityBoost(current_process, True)

        print("[WEB SERVER] Force stay-awake enabled: HIGH priority, no throttling")
    except Exception as e:
        print(f"[WEB SERVER] Stay-awake warning: {e}")


# Force stay awake immediately on module load
force_stay_awake()


# Global motor process instance
motor: MotorProcess = None

# WebSocket connections
connected_clients: List[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global motor

    # Startup
    print("Starting motor process...")
    motor = MotorProcess()
    motor.start()
    
    # Wait for connection
    await asyncio.sleep(2)
    
    # Start status broadcast task
    asyncio.create_task(broadcast_status())
    
    yield
    
    # Shutdown
    print("Stopping motor process...")
    if motor:
        motor.stop()


app = FastAPI(title="EtherCAT Motor Control", lifespan=lifespan)


# ============================================================
# WebSocket Status Broadcast
# ============================================================

async def broadcast_status():
    """Broadcast status to all connected WebSocket clients"""
    while True:
        try:
            if motor and connected_clients:
                try:
                    status = motor.get_status()
                    
                    # Check for any faults in status and add fault flag
                    has_fault = False
                    fault_slaves = []
                    for i, sw in enumerate(status.get('status_words', [])):
                        if sw & 0x0008:  # Fault bit
                            has_fault = True
                            fault_slaves.append(i)
                    status['has_fault'] = has_fault
                    status['fault_slaves'] = fault_slaves
                    
                    message = json.dumps({
                        'type': 'status',
                        'data': status
                    })
                    
                    # Send to all connected clients (use list copy)
                    disconnected = []
                    for client in list(connected_clients):
                        try:
                            await client.send_text(message)
                        except Exception as e:
                            disconnected.append(client)
                    
                    # Remove disconnected clients
                    for client in disconnected:
                        if client in connected_clients:
                            connected_clients.remove(client)
                    
                    # Check for template updates (non-blocking)
                    # Note: Only broadcast async responses (template steps, UDP/OSC logs)
                    # Direct command responses are handled in websocket_endpoint
                    try:
                        resp = motor.get_response(timeout=0.001)
                        if resp and connected_clients:
                            # Only broadcast if it's an async event (has template_step, udp_log, osc_log, etc.)
                            resp_data = resp.get('data', {}) if resp.get('data') else {}
                            is_async_event = (
                                resp_data.get('template_step') or
                                resp_data.get('template_start') or
                                resp_data.get('template_complete') or
                                resp_data.get('udp_log') or
                                resp_data.get('osc_log') or
                                resp_data.get('slave_change') or
                                resp_data.get('communication_error') or
                                resp_data.get('recovery_success') is not None or
                                resp_data.get('config_files') or
                                resp_data.get('config') or
                                resp_data.get('template') or
                                resp_data.get('loop_test')
                            )
                            if is_async_event:
                                print(f"[broadcast] Broadcasting async event: {resp.get('message', 'unknown')}")
                                resp_message = json.dumps({
                                    'type': 'response',
                                    'data': resp
                                })
                                for client in list(connected_clients):
                                    try:
                                        await client.send_text(resp_message)
                                    except:
                                        pass
                            else:
                                # Non-async event consumed - this is the bug!
                                print(f"[broadcast] WARNING: Consumed non-async response: {resp.get('message', 'unknown')}")
                    except:
                        pass
                        
                except Exception as e:
                    print(f"[broadcast] Status error: {e}")
        except Exception as e:
            print(f"[broadcast] Loop error: {e}")
        
        await asyncio.sleep(0.05)  # 50ms update rate


# ============================================================
# WebSocket Endpoint
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"WebSocket client connected ({len(connected_clients)} total)")
    
    try:
        while True:
            # Receive commands from client
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            cmd = msg.get('cmd')
            cmd_data = msg.get('data')
            
            # Process command
            if cmd == 'enable':
                motor.send_command(MotorProcess.CMD_ENABLE)
            elif cmd == 'disable':
                motor.send_command(MotorProcess.CMD_DISABLE)
            elif cmd == 'reset':
                motor.send_command(MotorProcess.CMD_RESET)
            elif cmd == 'home':
                motor.send_command(MotorProcess.CMD_HOME)
            elif cmd == 'set_home':
                motor.send_command(MotorProcess.CMD_SET_HOME, cmd_data)
            elif cmd == 'set_mode':
                motor.send_command(MotorProcess.CMD_SET_MODE, cmd_data)
            elif cmd == 'set_speed':
                motor.send_command('set_speed', cmd_data)
            elif cmd == 'set_steps_config':
                motor.send_command('set_steps_config', cmd_data)
            elif cmd == 'velocity_forward':
                motor.send_command('velocity_forward', cmd_data)
            elif cmd == 'velocity_backward':
                motor.send_command('velocity_backward', cmd_data)
            elif cmd == 'velocity_stop':
                motor.send_command('velocity_stop', cmd_data)
            elif cmd == 'move':
                motor.send_command(MotorProcess.CMD_MOVE, cmd_data)
            elif cmd == 'move_all_home':
                motor.send_command('move_all_home', cmd_data)
            elif cmd == 'stop':
                motor.emergency_stop()  # Use emergency_stop for immediate action
            elif cmd == 'template':
                # Pass config data from UI if provided
                motor.send_command(MotorProcess.CMD_TEMPLATE, cmd_data)
            elif cmd == 'template_loop':
                # Pass config data from UI if provided
                motor.send_command(MotorProcess.CMD_TEMPLATE_LOOP, cmd_data)
            elif cmd == 'load_config':
                print(f"[web_server] load_config command received: {cmd_data}")
                # Clear any pending responses first
                while True:
                    try:
                        old_resp = motor.resp_queue.get_nowait()
                        print(f"[web_server] Cleared old response: {old_resp.get('message', '')}")
                    except:
                        break
                motor.send_command('load_config', cmd_data)
            elif cmd == 'reload':
                motor.send_command(MotorProcess.CMD_RELOAD)
            elif cmd == 'status':
                motor.send_command(MotorProcess.CMD_STATUS)
            elif cmd == 'clear_error':
                motor.send_command(MotorProcess.CMD_CLEAR_ERROR, cmd_data)
            elif cmd == 'set_home_all':
                motor.send_command(MotorProcess.CMD_SET_HOME_ALL)
            elif cmd == 'udp_connect':
                motor.send_command(MotorProcess.CMD_UDP_CONNECT, cmd_data)
            elif cmd == 'udp_disconnect':
                motor.send_command(MotorProcess.CMD_UDP_DISCONNECT)
            elif cmd == 'osc_connect':
                motor.send_command(MotorProcess.CMD_OSC_CONNECT, cmd_data)
            elif cmd == 'osc_disconnect':
                motor.send_command(MotorProcess.CMD_OSC_DISCONNECT)
            elif cmd == 'get_adapters':
                motor.send_command(MotorProcess.CMD_GET_ADAPTERS)
            elif cmd == 'list_configs':
                motor.send_command(MotorProcess.CMD_LIST_CONFIGS)
            elif cmd == 'change_interface':
                motor.send_command(MotorProcess.CMD_CHANGE_INTERFACE, cmd_data)
            elif cmd == 'rescan':
                motor.send_command(MotorProcess.CMD_RESCAN)
            elif cmd == 'recover':
                motor.send_command(MotorProcess.CMD_RECOVER)
            elif cmd == 'loop_test_start':
                motor.send_command('loop_test_start', cmd_data)
            elif cmd == 'loop_test_stop':
                motor.send_command('loop_test_stop', cmd_data)
            elif cmd == 'clear_event':
                motor.clear_event()
            elif cmd == 'quit':
                # Terminate program - disable drives first to avoid error 81b
                print("\n[QUIT] Terminate command received - shutting down...")

                # First stop any motion immediately
                motor.emergency_stop()
                await asyncio.sleep(0.2)

                # Then disable all drives gracefully
                print("[QUIT] Sending disable command...")
                motor.send_command(MotorProcess.CMD_DISABLE)

                # Wait for disable response with longer timeout
                try:
                    resp = motor.get_response(timeout=2.0)
                    if resp:
                        print(f"[QUIT] Disable response: {resp.get('message', 'OK')}")
                except:
                    pass

                # Wait for PDO cycle to complete disable sequence
                print("[QUIT] Waiting for drives to fully disable...")
                await asyncio.sleep(1.0)

                # Send response before stopping
                try:
                    await websocket.send_text(json.dumps({
                        'type': 'response',
                        'data': {'success': True, 'message': 'Program terminated'}
                    }))
                except:
                    pass

                await asyncio.sleep(0.1)

                # Stop motor process (this stops PDO thread)
                print("[QUIT] Stopping motor process...")
                motor.stop()

                print("[QUIT] Exiting...")
                os._exit(0)  # Force exit the entire process

            # Check for response (increase timeout for file operations and loop test)
            timeout = 2.0 if cmd in ['load_config', 'list_configs', 'loop_test_start'] else 0.5
            resp = motor.get_response(timeout=timeout)
            if resp:
                print(f"[web_server] Response for {cmd}: {resp.get('message', 'no message')}")
                # Debug: print config_files if present
                if resp.get('data') and resp['data'].get('config_files'):
                    print(f"[web_server] Config files in response: {resp['data']['config_files']}")
                await websocket.send_text(json.dumps({
                    'type': 'response',
                    'data': resp
                }))
            else:
                print(f"[web_server] No response for {cmd} (timeout={timeout}s)")
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        print(f"WebSocket client disconnected ({len(connected_clients)} total)")


# ============================================================
# REST API Endpoints
# ============================================================

@app.get("/api/status")
async def get_status():
    """Get current status"""
    if not motor:
        raise HTTPException(status_code=503, detail="Motor not initialized")
    return motor.get_status()


@app.post("/api/enable")
async def enable():
    """Enable all drives"""
    motor.send_command(MotorProcess.CMD_ENABLE)
    return {"message": "Enable command sent"}


@app.post("/api/disable")
async def disable():
    """Disable all drives"""
    motor.send_command(MotorProcess.CMD_DISABLE)
    return {"message": "Disable command sent"}


@app.post("/api/reset")
async def reset():
    """Reset faults"""
    motor.send_command(MotorProcess.CMD_RESET)
    return {"message": "Reset command sent"}


@app.post("/api/home")
async def home():
    """Home all drives"""
    motor.send_command(MotorProcess.CMD_HOME)
    return {"message": "Home command sent"}


@app.post("/api/move")
async def move(positions: List[float]):
    """Move to positions"""
    motor.send_command(MotorProcess.CMD_MOVE, {'positions': positions})
    return {"message": f"Move command sent: {positions}"}


@app.post("/api/template")
async def run_template():
    """Run template (single execution)"""
    motor.send_command(MotorProcess.CMD_TEMPLATE)
    return {"message": "Template command sent"}


@app.post("/api/template_loop")
async def run_template_loop():
    """Run template in loop mode"""
    motor.send_command(MotorProcess.CMD_TEMPLATE_LOOP)
    return {"message": "Template loop command sent"}


@app.post("/api/reload")
async def reload_config():
    """Reload config.json"""
    motor.send_command(MotorProcess.CMD_RELOAD)
    return {"message": "Reload command sent"}


@app.get("/api/config")
async def get_config():
    """Get config.json from json folder"""
    config_path = os.path.join(os.path.dirname(__file__), 'json', 'config.json')
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except:
        return {}


@app.get("/api/adapters")
async def get_adapters():
    """List available network adapters"""
    import pysoem
    adapters = []
    for a in pysoem.find_adapters():
        adapters.append({
            'name': a.name,
            'desc': a.desc
        })
    return {'adapters': adapters}


@app.get("/api/configs")
async def list_configs():
    """List available JSON config files in json folder"""
    import glob
    json_folder = os.path.join(os.path.dirname(__file__), 'json')
    config_files = []
    if os.path.exists(json_folder):
        for f in sorted(glob.glob(os.path.join(json_folder, '*.json'))):
            config_files.append(os.path.basename(f))
    print(f"[API] Found config files: {config_files}")
    return {'config_files': config_files}


@app.post("/api/config")
async def save_config(config: dict):
    """Save config.json to json folder"""
    config_path = os.path.join(os.path.dirname(__file__), 'json', 'config.json')
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        motor.send_command(MotorProcess.CMD_RELOAD)
        return {"message": "Config saved and reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Serve UI
# ============================================================

@app.get("/")
async def serve_ui():
    """Serve main UI page (new UI by default)"""
    ui_path = os.path.join(os.path.dirname(__file__), 'newui.html')
    if os.path.exists(ui_path):
        return FileResponse(ui_path)
    else:
        return HTMLResponse(content="<h1>UI file not found</h1>", status_code=404)


@app.get("/newui.html")
async def serve_new_ui():
    """Serve new UI page"""
    ui_path = os.path.join(os.path.dirname(__file__), 'newui.html')
    if os.path.exists(ui_path):
        return FileResponse(ui_path)
    else:
        return HTMLResponse(content="<h1>New UI file not found</h1>", status_code=404)


@app.get("/ui.html")
async def serve_classic_ui():
    """Serve classic UI page"""
    ui_path = os.path.join(os.path.dirname(__file__), 'ui.html')
    if os.path.exists(ui_path):
        return FileResponse(ui_path)
    else:
        return HTMLResponse(content="<h1>UI file not found</h1>", status_code=404)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("="*60)
    print("EtherCAT Motor Control - Web Server")
    print("="*60)
    print("\nOpen browser: http://localhost:8000")
    print("="*60)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)