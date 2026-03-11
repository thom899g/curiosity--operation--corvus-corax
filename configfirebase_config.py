"""
Firebase configuration and initialization module.
Centralizes all Firestore operations with proper error handling and singleton pattern.
Architectural Choice: Singleton ensures we don't create multiple Firebase app instances.
"""
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, asdict
import json

import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin.exceptions import FirebaseError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Initialize logger
logger = logging.getLogger(__name__)

@dataclass
class Event:
    """Structured event for Firestore event stream"""
    event_type: str  # crawl_discovery, analysis_complete, etc.
    payload: Dict[str, Any]
    priority: int = 5
    source_agent: str = "unknown"
    timestamp: Optional[datetime] = None
    
    def to_firestore_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        data = asdict(self)
        if not data['timestamp']:
            data['timestamp'] = firestore.SERVER_TIMESTAMP
        return data

class FirebaseManager:
    """Singleton manager for Firebase operations with retry logic"""
    _instance: Optional['FirebaseManager'] = None
    _initialized: bool = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseManager, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._init_firebase()
            self._initialized = True
    
    def _init_firebase(self) -> None:
        """Initialize Firebase with environment-based credentials"""
        try:
            # Priority order: env variable -> default path
            cred_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', './config/serviceAccountKey.json')
            
            if os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                logger.info(f"Firebase initialized with service account: {cred_path}")
            else:
                # Try to use Application Default Credentials (for Cloud environments)
                firebase_admin.initialize_app()
                logger.info("Firebase initialized with Application Default Credentials")
            
            self.db = firestore.client()
            self._test_connection()
            
        except (ValueError, FirebaseError) as e:
            logger.error(f"Firebase initialization failed: {str(e)}")
            raise RuntimeError(f"Firebase initialization failed: {str(e)}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((FirebaseError, ConnectionError))
    )
    def _test_connection(self) -> None:
        """Test Firestore connection with retry logic"""
        try:
            # Simple read operation to test connection
            doc_ref = self.db.collection('system_health').document('connection_test')
            doc_ref.set({'test_time': firestore.SERVER_TIMESTAMP})
            doc_ref.delete()
            logger.info("Firestore connection test successful")
        except Exception as e:
            logger.error(f"Firestore connection test failed: {str(e)}")
            raise
    
    def publish_event(self, agent_id: str, event: Event) -> str:
        """Publish event to Firestore event stream with error handling"""
        try:
            event.timestamp = None  # Let Firestore set server timestamp
            doc_ref = self.db.collection(f'events/{agent_id}').document()
            doc_ref.set(event.to_firestore_dict())
            logger.debug(f"Event published: {event.event_type} from {agent_id}")
            return doc_ref.id
        except FirebaseError as e:
            logger.error(f"Failed to publish event: {str(e)}")
            raise
    
    def update_agent_status(self, agent_id: str, status: Dict[str, Any]) -> None:
        """Update agent status in registry"""
        try:
            doc_ref = self.db.collection('agents').document(agent_id)
            status['last_heartbeat'] = firestore.SERVER_TIMESTAMP
            doc_ref.set(status, merge=True)
        except FirebaseError as e:
            logger.error(f"Failed to update agent status: {str(e)}")

# Global instance
firebase_manager = FirebaseManager()