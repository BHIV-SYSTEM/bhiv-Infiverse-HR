#!/usr/bin/env python3
"""
BHIV HR Platform - Backend Services Launcher (No Docker Required)
==================================================================
This script starts all backend services locally without Docker.

Services:
  - Gateway:   Port 8000 (Main API)
  - Agent:     Port 9000 (AI Matching)
  - LangGraph: Port 9001 (Workflow Automation)

Usage:
  python run_services.py           # Start all services
  python run_services.py gateway   # Start only gateway
  python run_services.py agent     # Start only agent
  python run_services.py langgraph # Start only langgraph
  python run_services.py --stop    # Stop anything left on ports 8000/9000/9001
"""

import subprocess
import sys
import os
import time
import signal
from pathlib import Path

# Fix Windows console encoding for emojis (line-buffered so output shows live)
if sys.platform == 'win32':
    try:
        # Line-buffered UTF-8 wrapper: every print appears immediately instead
        # of sitting in an 8KB block buffer (which looked like a hang).
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except:
        # Fallback: create safe print function that removes emojis
        def safe_print(*args, **kwargs):
            """Print function that handles encoding errors on Windows."""
            try:
                print(*args, **kwargs)
            except UnicodeEncodeError:
                # Remove emojis and print
                text = ' '.join(str(arg) for arg in args)
                # Remove common emoji patterns
                import re
                text = re.sub(r'[^\x00-\x7F]+', '', text)  # Remove non-ASCII
                print(text, **kwargs)
        
        # Replace print with safe_print
        import builtins
        builtins.print = safe_print

# Service configurations
SERVICES = {
    "gateway": {
        "name": "BHIV HR Gateway",
        "port": 8000,
        "dir": "services/gateway",
        "command": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"],
        "health_url": "http://localhost:8000/health"
    },
    "agent": {
        "name": "BHIV HR Agent",
        "port": 9000,
        "dir": "services/agent",
        "command": ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--reload"],
        "health_url": "http://localhost:9000/health"
    },
    "langgraph": {
        "name": "BHIV HR LangGraph",
        "port": 9001,
        "dir": "services/langgraph",
        "command": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001", "--reload"],
        "health_url": "http://localhost:9001/health"
    }
}

# Environment variables for all services. Secrets must come from .env or process environment;
# load_env_file() overwrites these from backend/.env when present. Do not hardcode secrets here.
COMMON_ENV = {
    "DATABASE_URL": os.getenv("DATABASE_URL", ""),
    "MONGODB_URI": os.getenv("MONGODB_URI", os.getenv("DATABASE_URL", "")),
    "MONGODB_DB_NAME": os.getenv("MONGODB_DB_NAME", "bhiv_hr"),
    "API_KEY_SECRET": os.getenv("API_KEY_SECRET", ""),
    "JWT_SECRET_KEY": os.getenv("JWT_SECRET_KEY", ""),
    "CANDIDATE_JWT_SECRET_KEY": os.getenv("CANDIDATE_JWT_SECRET_KEY", ""),
    "GATEWAY_SECRET_KEY": os.getenv("GATEWAY_SECRET_KEY", ""),
    "GATEWAY_SERVICE_URL": os.getenv("GATEWAY_SERVICE_URL", "http://localhost:8000"),
    "AGENT_SERVICE_URL": os.getenv("AGENT_SERVICE_URL", "http://localhost:9000"),
    "LANGGRAPH_SERVICE_URL": os.getenv("LANGGRAPH_SERVICE_URL", "http://localhost:9001"),
    "ENVIRONMENT": os.getenv("ENVIRONMENT", "development"),
    "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
    "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
    "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-pro"),
    "HF_TOKEN": os.getenv("HF_TOKEN", ""),
    "HF_HUB_DISABLE_SYMLINKS_WARNING": os.getenv("HF_HUB_DISABLE_SYMLINKS_WARNING", "1"),
    "HF_HUB_DISABLE_TELEMETRY": os.getenv("HF_HUB_DISABLE_TELEMETRY", "1"),
    "HF_HUB_DISABLE_PROGRESS_BARS": os.getenv("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
    "TRANSFORMERS_VERBOSITY": os.getenv("TRANSFORMERS_VERBOSITY", "error"),
    "TOKENIZERS_PARALLELISM": os.getenv("TOKENIZERS_PARALLELISM", "false"),
    # Windows: force UTF-8 for child pipes/consoles so emoji in log lines
    # (e.g. print("❌ ...")) doesn't crash services with UnicodeEncodeError
    # ('charmap' codec can't encode character '\u274C').
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
}

# Track running processes
processes = []


def get_backend_dir():
    """Get the backend directory path."""
    return Path(__file__).parent.resolve()


def load_env_file():
    """Load environment variables from .env file if exists."""
    env_path = get_backend_dir() / ".env"
    if env_path.exists():
        print(f"[INFO] Loading environment from {env_path}")
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if value and not value.startswith("<"):
                        COMMON_ENV[key] = value
    return COMMON_ENV


def check_port(port):
    """Check if a port is available."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) != 0


def start_service(name, service_config, env_vars):
    """Start a single service."""
    backend_dir = get_backend_dir()
    service_dir = backend_dir / service_config["dir"]
    
    if not service_dir.exists():
        print(f"[ERROR] Service directory not found: {service_dir}")
        return None

    port = service_config["port"]
    if not check_port(port):
        owner = _port_owner(port)
        hint = f" (PID {owner} — run: python run_services.py --stop)" if owner else ""
        print(f"[WARNING] Port {port} is already in use. {service_config['name']} may already be running{hint}.")
        return None
    
    print(f"\n[STARTING] Starting {service_config['name']} on port {port}...")
    print(f"   Directory: {service_dir}")
    print(f"   Command: {' '.join(service_config['command'])}")
    
    # Merge environment variables
    service_env = os.environ.copy()
    service_env.update(env_vars)
    
    # Start the process
    try:
        process = subprocess.Popen(
            service_config["command"],
            cwd=service_dir,
            env=service_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        )
        print(f"   [OK] Started with PID: {process.pid}")
        return process
    except Exception as e:
        print(f"   [ERROR] Failed to start: {e}")
        return None


def stream_output(process, name):
    """Stream output from a process."""
    try:
        for line in iter(process.stdout.readline, ''):
            if line:
                print(f"[{name}] {line.rstrip()}")
    except:
        pass


def _port_owner(port):
    """PID listening on a local TCP port, or None if the port is free."""
    try:
        if os.name == 'nt':
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, errors="replace"
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING":
                    if parts[1].rsplit(":", 1)[-1] == str(port):
                        return int(parts[4])
        else:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, errors="replace"
            ).stdout
            pids = [int(p) for p in out.split() if p.strip().isdigit()]
            return pids[0] if pids else None
    except Exception:
        return None
    return None


def _kill_process_tree(pid):
    """Kill a service and its children.

    `uvicorn --reload` spawns a worker; killing only the master leaves an
    orphan still holding the port, which is why the next run reported
    "Port 8000 is already in use".
    """
    if os.name == 'nt':
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _uvicorn_masters_for(port):
    """PIDs of `uvicorn --reload` processes started for ``port`` (Windows).

    The reload master is the *parent* of the process that owns the listening
    socket, so killing only the socket owner leaves a master that respawns a
    worker and re-grabs the port — the next run then reports "port already in
    use" again. Match on the service's own `--port` argument so unrelated
    python processes are never touched.
    """
    if os.name != 'nt':
        return []
    try:
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
            f"| Where-Object {{ $_.CommandLine -like '*uvicorn*--port {port}*' }} "
            "| ForEach-Object { $_.ProcessId }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, errors="replace"
        ).stdout
        return [int(t) for t in out.split() if t.strip().isdigit()]
    except Exception:
        return []


def stop_ports():
    """Free the ports this launcher owns (explicit: `python run_services.py --stop`)."""
    print("\n[STOP] Freeing launcher ports...")
    for cfg in SERVICES.values():
        port = cfg["port"]
        owner = _port_owner(port)
        masters = _uvicorn_masters_for(port)
        if owner is None and not masters:
            print(f"   [OK] :{port} already free")
            continue
        print(f"   [..] :{port} held by PID {owner or masters[0]} — stopping {cfg['name']} (whole tree + reload master)")
        for _attempt in range(3):
            pids = []
            owner = _port_owner(port)
            if owner:
                pids.append(owner)
            pids.extend(p for p in _uvicorn_masters_for(port) if p not in pids)
            if not pids:
                break
            for pid in pids:
                _kill_process_tree(pid)
            time.sleep(1)
        still = _port_owner(port)
        print(f"   [OK] :{port} freed" if still is None else f"   [WARNING] :{port} still busy (PID {still})")


def stop_all_services():
    """Stop all running services."""
    print("\n\n[STOPPING] Stopping all services...")
    for process, name in processes:
        try:
            if os.name == 'nt':
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=5)
            print(f"   [OK] Stopped {name}")
        except Exception:
            print(f"   [WARNING] Force killing {name} (tree)")
            try:
                _kill_process_tree(process.pid)
            except Exception:
                pass
    processes.clear()


def signal_handler(sig, frame):
    """Handle Ctrl+C."""
    stop_all_services()
    sys.exit(0)


def check_health(url, timeout=30):
    """Check service health."""
    import urllib.request
    import urllib.error
    
    print(f"   [CHECK] Checking health: {url}")
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            response = urllib.request.urlopen(url, timeout=5)
            if response.status == 200:
                print(f"   [OK] Service is healthy!")
                return True
        except:
            time.sleep(1)
    
    print(f"   [WARNING] Health check timeout after {timeout}s")
    return False


def main():
    """Main entry point."""
    print("=" * 60)
    print("BHIV HR Platform - Backend Services Launcher")
    print("=" * 60)
    print(f"Backend Directory: {get_backend_dir()}")
    
    # Parse arguments
    if sys.argv[1:2] and sys.argv[1].lower() in ("--stop", "stop"):
        stop_ports()
        sys.exit(0)

    services_to_start = sys.argv[1:] if len(sys.argv) > 1 else list(SERVICES.keys())
    
    # Validate service names
    for svc in services_to_start:
        if svc not in SERVICES:
            print(f"[ERROR] Unknown service: {svc}")
            print(f"   Available: {', '.join(SERVICES.keys())}")
            sys.exit(1)
    
    # Load environment
    env_vars = load_env_file()
    db_uri = env_vars.get("DATABASE_URL") or env_vars.get("MONGODB_URI") or ""
    db_name = env_vars.get("MONGODB_DB_NAME", "bhiv_hr")
    if db_uri:
        db_label = "MongoDB Atlas" if db_uri.startswith("mongodb+srv://") else "MongoDB"
        print(f"\n[INFO] Database: {db_label} ({db_name})")
    else:
        print("\n[INFO] Database: not configured — services use their in-memory "
              "fallbacks (set DATABASE_URL in backend/.env for MongoDB persistence)")
    print(f"[INFO] Environment: {env_vars.get('ENVIRONMENT', 'development')}")
    
    # Register signal handler
    signal.signal(signal.SIGINT, signal_handler)
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, signal_handler)
    
    # Start services
    print(f"\n[INFO] Starting services: {', '.join(services_to_start)}")
    
    for svc_name in services_to_start:
        config = SERVICES[svc_name]
        process = start_service(svc_name, config, env_vars)
        if process:
            processes.append((process, config["name"]))
    
    if not processes:
        print("\n[ERROR] No services started!")
        sys.exit(1)
    
    # Wait a bit then check health
    print("\n[INFO] Waiting for services to initialize...")
    time.sleep(3)
    
    for svc_name in services_to_start:
        config = SERVICES[svc_name]
        check_health(config["health_url"], timeout=30)
    
    # Print summary
    print("\n" + "=" * 60)
    print("Services Running:")
    for svc_name in services_to_start:
        config = SERVICES[svc_name]
        print(f"   - {config['name']}: http://localhost:{config['port']}")
    print("=" * 60)
    print("\n[INFO] Press Ctrl+C to stop all services\n")
    
    # Keep running and stream output
    import threading
    for process, name in processes:
        thread = threading.Thread(target=stream_output, args=(process, name))
        thread.daemon = True
        thread.start()
    
    # Wait for processes
    try:
        while processes:
            for process, name in processes[:]:
                if process.poll() is not None:
                    print(f"\n[WARNING] {name} exited with code {process.returncode}")
                    processes.remove((process, name))
            time.sleep(1)
    except KeyboardInterrupt:
        stop_all_services()


if __name__ == "__main__":
    main()
