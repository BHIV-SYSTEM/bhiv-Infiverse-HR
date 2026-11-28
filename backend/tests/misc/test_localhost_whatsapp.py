#!/usr/bin/env python3
"""
Test WhatsApp on Localhost with Real Credentials
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

async def test_localhost_whatsapp():
    """Test WhatsApp directly on localhost with real credentials"""
    
    print("Testing WhatsApp on localhost with real credentials...")
    print(f"Twilio client: {comm_manager.twilio_client}")
    print(f"Gmail email: {comm_manager.gmail_email}")
    
    # Test WhatsApp directly
    result = await comm_manager.send_whatsapp(
        phone="+919284967526",
        message="🧪 LOCALHOST TEST: WhatsApp working with real credentials!"
    )
    
    print(f"WhatsApp result: {result}")
    
    # Test multi-channel
    payload = {
        "candidate_name": "John Doe",
        "candidate_email": "john.doe@example.com", 
        "candidate_phone": "+919284967526",
        "job_title": "Software Engineer",
        "message": "Your interview has been scheduled for tomorrow at 10 AM.",
        "application_status": "interview_scheduled"
    }
    
    multi_result = await comm_manager.send_multi_channel(payload, ["whatsapp"])
    print(f"Multi-channel result: {multi_result}")

if __name__ == "__main__":
    asyncio.run(test_localhost_whatsapp())