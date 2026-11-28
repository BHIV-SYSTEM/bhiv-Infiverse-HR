#!/usr/bin/env python3
"""
Test Direct Notification Without Authentication
"""

import sys
import os
# Import as a package: communication.py uses relative imports and cannot be
# loaded as a top-level module (pytest collection failed with
# "attempted relative import with no known parent package").
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from services.langgraph.app.communication import comm_manager
import asyncio

async def test_direct_communication():
    """Test communication manager directly"""
    
    print("Testing communication manager directly...")
    
    # Test WhatsApp
    result = await comm_manager.send_whatsapp(
        phone="+919284967526",
        message="Direct test: LangGraph communication working!"
    )
    print(f"WhatsApp result: {result}")
    
    # Test multi-channel
    payload = {
        "candidate_name": "Direct Test User",
        "candidate_email": "test@example.com",
        "candidate_phone": "+919284967526",
        "job_title": "Test Position",
        "message": "Direct multi-channel test message",
        "application_status": "test"
    }
    
    multi_result = await comm_manager.send_multi_channel(payload, ["email", "whatsapp"])
    print(f"Multi-channel result: {multi_result}")

if __name__ == "__main__":
    asyncio.run(test_direct_communication())