"""
Base class for all autonomous agents in the Raven Nexus.
Implements common lifecycle, logging, and Firestore integration.
Architectural Choice: Template Method pattern for consistent agent behavior.
"""
import abc
import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass
import signal
import sys

from config.firebase_config import firebase_manager, Event

@dataclass
class AgentConfig:
    """Configuration for agent initialization"""
    agent_id: str
    agent_type: str
    max_retries: int = 3
    heartbeat_interval: int = 60  # seconds
    shutdown_timeout: int = 30  # seconds

class BaseAgent(abc.ABC):
    """Abstract base agent with lifecycle management"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.logger = logging.getLogger(f"agent.{config.agent_type}.{config.agent_id}")
        self.is_running = False
        self._shutdown_event = asyncio.Event()
        
        # Initialize agent status
        self._status = {
            'agent_id': config.agent_id,
            'agent_type': config.agent_type,
            'status': 'initializing',
            'start_time': datetime.utcnow().isoformat(),
            'error_count': 0,
            'processed_events': 0
        }
        
        # Set up graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}, initiating shutdown...")
        self._shutdown_event.set()
    
    async def _heartbeat_loop(self) -> None:
        """Periodic status updates to Firestore"""
        while not self._shutdown_event.is_set():
            try:
                self._status['last_heartbeat'] = datetime.utcnow().isoformat()
                firebase_manager.update_agent_status(self.config.agent_id, self._status)
                self.logger.debug("Heartbeat sent")
            except Exception as e:
                self.logger.error(f"Heartbeat failed: {str(e)}")
            
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), 
                                     timeout=self.config.heartbeat_interval)
            except asyncio.TimeoutError:
                continue
    
    def publish_event(self, event_type: str, payload: Dict[str, Any], priority: int = 5) -> str:
        """Publish event to Firestore event stream"""
        event = Event(
            event_type=event_type,
            payload=payload,
            priority=priority,
            source_agent=self.config.agent_id
        )
        return firebase_manager.publish_event(self.config.agent_id, event)
    
    def update_status(self, **kwargs) -> None:
        """Update agent status fields"""
        self._status.update(kwargs)
        self._status['last_update'] = datetime.utcnow().isoformat()
    
    async def run(self) -> None:
        """Main agent lifecycle with error handling"""
        self.is_running = True
        self.update_status(status='running')
        
        try:
            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            # Run agent-specific main loop
            await self._main_loop()
            
            # Wait for shutdown or heartbeat completion
            await self._shutdown_event.wait()
            
            # Cancel heartbeat
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
                
        except Exception as e:
            self.logger.error(f"Agent crashed: {str(e)}", exc_info=True)
            self.update_status(status='crashed', last_error=str(e))
        finally:
            self.is_running = False
            self.update_status(status='stopped', end_time=datetime.utcnow().isoformat())
            self.logger.info("Agent stopped")
    
    @abc.abstractmethod
    async def _main_loop(self) -> None:
        """Agent-specific main loop to be implemented by subclasses"""
        pass