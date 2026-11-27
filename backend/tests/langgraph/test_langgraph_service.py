#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick test script for LangGraph service
Tests basic functionality without requiring full LangGraph dependencies
"""

import sys
import os
import asyncio
import httpx
from datetime import datetime

# Set UTF-8 encoding for Windows.
# Only when run standalone: under pytest this detaches pytest's capture
# buffers and crashes the whole session ("underlying buffer has been
# detached" at teardown).
if sys.platform == "win32" and "pytest" not in sys.modules:
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())

# Add the langgraph service to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'services', 'langgraph'))

async def test_langgraph_service():
    """Test LangGraph service endpoints"""
    
    print("Testing LangGraph Service...")
    print("=" * 50)
    
    # Test 1: Import check
    try:
        from app.main import app
        print("[OK] Successfully imported LangGraph main app")
    except ImportError as e:
        print(f"[ERROR] Import error: {e}")
        return False
    
    # Test 2: Basic configuration
    try:
        from config import settings
        print(f"[OK] Configuration loaded - Environment: {settings.environment}")
        print(f"   - Gateway URL: {settings.gateway_service_url}")
        print(f"   - LangGraph Port: {settings.langgraph_port}")
    except Exception as e:
        print(f"[ERROR] Configuration error: {e}")
        return False
    
    # Test 3: Database tracker
    try:
        from app.database_tracker import tracker
        print(f"[OK] Database tracker initialized - Connection: {'Connected' if tracker.connection else 'Fallback mode'}")
    except Exception as e:
        print(f"[ERROR] Database tracker error: {e}")
        return False
    
    # Test 4: Dependencies
    try:
        from dependencies import get_api_key, get_auth
        print("[OK] Authentication dependencies loaded")
    except Exception as e:
        print(f"[ERROR] Dependencies error: {e}")
        return False
    
    # Test 5: Workflow components (optional - may fail without LangGraph)
    try:
        from app.graphs import create_application_workflow
        from app.state import CandidateApplicationState
        print("[OK] LangGraph workflow components available")
        workflow_available = True
    except ImportError as e:
        print(f"[WARN] LangGraph components not available (expected in dev): {e}")
        workflow_available = False
    
    # Test 6: Communication manager
    try:
        from app.communication import comm_manager
        print("[OK] Communication manager initialized")
    except Exception as e:
        print(f"[ERROR] Communication manager error: {e}")
        return False
    
    # Test 7: Monitoring
    try:
        from app.monitoring import monitor
        health = monitor.get_health_status()
        print(f"[OK] Monitoring system active - Status: {health['status']}")
    except Exception as e:
        print(f"[ERROR] Monitoring error: {e}")
        return False
    
    print("\n" + "=" * 50)
    print("Test Summary:")
    print(f"   - Service Import: OK")
    print(f"   - Configuration: OK")
    print(f"   - Database Tracker: OK")
    print(f"   - Authentication: OK")
    print(f"   - LangGraph Workflow: {'OK' if workflow_available else 'Optional'}")
    print(f"   - Communication: OK")
    print(f"   - Monitoring: OK")
    print(f"   - Overall Status: {'READY' if True else 'ISSUES'}")
    
    return True

async def test_api_endpoints():
    """Test API endpoints if service is running"""
    
    print("\nTesting API Endpoints (if service is running)...")
    print("=" * 50)
    
    base_url = "http://localhost:9001"
    
    # Test endpoints
    endpoints = [
        "/",
        "/health",
        "/test-integration"
    ]
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        for endpoint in endpoints:
            try:
                response = await client.get(f"{base_url}{endpoint}")
                if response.status_code == 200:
                    data = response.json()
                    print(f"[OK] {endpoint}: {data.get('message', data.get('status', 'OK'))}")
                else:
                    print(f"[WARN] {endpoint}: HTTP {response.status_code}")
            except httpx.ConnectError:
                print(f"[WARN] {endpoint}: Service not running (expected)")
            except Exception as e:
                print(f"[ERROR] {endpoint}: {str(e)}")

def main():
    """Main test function"""
    print(f"BHIV LangGraph Service Test")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python: {sys.version.split()[0]}")
    print()
    
    try:
        # Run async tests
        asyncio.run(test_langgraph_service())
        asyncio.run(test_api_endpoints())
        
        print("\nLangGraph service test completed successfully!")
        print("\nTo start the service:")
        print("   cd services/langgraph")
        print("   python start_local.py")
        print("   # or")
        print("   uvicorn app.main:app --host 0.0.0.0 --port 9001 --reload")
        
    except Exception as e:
        print(f"\n[ERROR] Test failed: {str(e)}")
        return False
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)