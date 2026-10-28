from langgraph.graph import StateGraph, END
# MongoDB migration: Using custom MongoDB checkpointer instead of PostgresSaver
from .mongodb_checkpointer import MongoDBSaver
from .state import CandidateApplicationState
from .agents import (
    application_screener_agent,
    notification_agent,
    hr_update_agent,
    feedback_collection_agent
)
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings
import logging

logger = logging.getLogger(__name__)

def should_send_notification(state: CandidateApplicationState) -> str:
    """Conditional edge: Determine if notification should be sent"""
    if state["application_status"] in ["shortlisted", "rejected"]:
        logger.info(f"✅ Routing to notifications (status: {state['application_status']})")
        return "notify"
    logger.info(f"⏭️ Skipping notifications (status: {state['application_status']})")
    return "skip_notify"

def create_application_workflow():
    """Creates the candidate application processing workflow"""
    
    try:
        logger.info("🏗️ Creating application workflow graph...")
        
        # Initialize state graph
        workflow = StateGraph(CandidateApplicationState)
        
        # Add nodes (agents)
        logger.info("Adding agent nodes...")
        workflow.add_node("screen_application", application_screener_agent)
        workflow.add_node("send_notifications", notification_agent)
        workflow.add_node("update_hr_dashboard", hr_update_agent)
        workflow.add_node("collect_feedback", feedback_collection_agent)
        
        # Set entry point
        workflow.set_entry_point("screen_application")
        logger.info("Entry point set: screen_application")
        
        # Conditional routing after screening
        workflow.add_conditional_edges(
            "screen_application",
            should_send_notification,
            {
                "notify": "send_notifications",
                "skip_notify": "update_hr_dashboard"
            }
        )
        logger.info("Conditional routing configured")
        
        # Sequential edges
        workflow.add_edge("send_notifications", "update_hr_dashboard")
        workflow.add_edge("update_hr_dashboard", "collect_feedback")
        workflow.add_edge("collect_feedback", END)
        logger.info("Sequential edges configured")
        
        # Setup MongoDB checkpointer for state persistence (migrated from PostgresSaver)
        try:
            if settings.database_url:
                checkpointer = MongoDBSaver.from_conn_string(settings.database_url)
                logger.info("✅ MongoDB checkpointer configured")
            else:
                checkpointer = None
                logger.info(
                    "MongoDB checkpointer skipped — DATABASE_URL not set "
                    "(workflow state stays in-memory). Set it in backend/.env to persist."
                )
        except Exception as e:
            logger.warning(f"⚠️ MongoDB checkpointer failed: {e}")
            checkpointer = None
        
        # Compile graph
        if checkpointer:
            app = workflow.compile(checkpointer=checkpointer)
        else:
            app = workflow.compile()
        
        logger.info("✅ Application workflow created successfully")
        return app
    
    except Exception as e:
        logger.error(f"❌ Error creating workflow: {str(e)}")
        raise