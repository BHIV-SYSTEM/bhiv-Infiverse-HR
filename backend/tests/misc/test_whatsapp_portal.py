#!/usr/bin/env python3
"""
Test WhatsApp Integration via Portal System
"""

import sys
import os
# Resolve the service path relative to this file; pytest is usually invoked
# from the repository root, where the old relative sys.path entry was empty.
_SERVICE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services",
    "portal",
)
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

from email_automation import trigger_interview_notification, trigger_shortlist_notification

def test_whatsapp_notifications():
    """Test WhatsApp notifications through portal automation"""
    
    print("Testing WhatsApp notifications via portal system...")
    
    # Test data
    candidate_data = {
        "name": "John Doe",
        "email": "john.doe@example.com", 
        "phone": "+919284967526",  # Your verified number
        "job_title": "Software Engineer"
    }
    
    # Test 1: Interview notification (Email + WhatsApp)
    print("\nTesting interview notification...")
    result1 = trigger_interview_notification(
        candidate_name=candidate_data["name"],
        candidate_email=candidate_data["email"],
        candidate_phone=candidate_data["phone"],
        job_title=candidate_data["job_title"],
        interview_date="2024-12-01",
        interview_time="10:00 AM",
        interviewer="HR Manager"
    )
    print(f"Interview notification result: {result1}")
    
    # Test 2: Shortlist notification (Email + WhatsApp)
    print("\nTesting shortlist notification...")
    result2 = trigger_shortlist_notification(
        candidate_name=candidate_data["name"],
        candidate_email=candidate_data["email"],
        candidate_phone=candidate_data["phone"],
        job_title=candidate_data["job_title"]
    )
    print(f"Shortlist notification result: {result2}")
    
    print("\nWhatsApp integration test completed!")
    print("Check your WhatsApp for messages from +14155238886")

if __name__ == "__main__":
    test_whatsapp_notifications()