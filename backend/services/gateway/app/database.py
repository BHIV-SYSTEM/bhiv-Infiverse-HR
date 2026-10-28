"""
MongoDB Connection Module for Gateway Service
Uses Motor (async MongoDB driver) for FastAPI async endpoints
"""
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from motor.motor_asyncio import AsyncIOMotorCollection
from typing import Optional
import os
import logging

logger = logging.getLogger(__name__)

# Global MongoDB client and database instances
_mongo_client: Optional[AsyncIOMotorClient] = None
_mongo_db: Optional[AsyncIOMotorDatabase] = None


class DatabaseNotConfiguredError(ValueError):
    """Raised when DATABASE_URL/MONGODB_URI is not set (no-database dev mode)."""


def get_mongo_client() -> AsyncIOMotorClient:
    """
    Get or create MongoDB client (async)
    Uses singleton pattern for connection reuse
    """
    global _mongo_client
    
    if _mongo_client is None:
        mongodb_uri = os.getenv("DATABASE_URL") or os.getenv("MONGODB_URI")
        
        if not mongodb_uri:
            raise DatabaseNotConfiguredError(
                "DATABASE_URL or MONGODB_URI environment variable is required"
            )
        
        try:
            _mongo_client = AsyncIOMotorClient(
                mongodb_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=10,
                minPoolSize=2,
                connectTimeoutMS=10000,
                socketTimeoutMS=20000,
            )
            logger.info("MongoDB client (async) initialized")
        except Exception as e:
            logger.error(f"Failed to initialize MongoDB client: {e}")
            raise
    
    return _mongo_client


async def get_mongo_db() -> AsyncIOMotorDatabase:
    """
    Get or create MongoDB database instance (async)
    """
    global _mongo_db
    
    if _mongo_db is None:
        client = get_mongo_client()
        db_name = os.getenv("MONGODB_DB_NAME", "bhiv_hr")
        _mongo_db = client[db_name]
        
        # Test connection
        try:
            await _mongo_client.admin.command('ping')
            logger.info(f"Connected to MongoDB database: {db_name}")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise
    
    return _mongo_db


async def close_mongo_connections():
    """
    Close MongoDB connections (useful for cleanup/g testing)
    """
    global _mongo_client, _mongo_db
    
    if _mongo_client:
        _mongo_client.close()
        _mongo_client = None
        logger.info("MongoDB client closed")
    
    _mongo_db = None


def get_collection(collection_name: str) -> AsyncIOMotorCollection:
    """
    Helper function to get a collection (returns collection object, not async)
    Note: Actual operations on collection are async
    """
    # This will be called from async context, so we can await get_mongo_db()
    # But for typing, we'll return the collection type
    # Actual usage: db = await get_mongo_db(); collection = db[collection_name]
    pass
