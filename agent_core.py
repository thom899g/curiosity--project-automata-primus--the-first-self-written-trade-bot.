"""
PROJECT AUTOMATA PRIMUS: The Persistent Cerebrum
Core compute layer running continuous evolution cycle
"""
import asyncio
import logging
import sys
import traceback
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional
import firebase_admin
from firebase_admin import firestore, credentials
from google.cloud.firestore_v1 import Client as FirestoreClient

# Local imports
from observer_executor import ObserverExecutor
from strategy_generator import StrategyGenerator
from learner_deployer import LearnerDeployer
from utils import init_logging, ExponentialBackoff

class SystemState(Enum):
    """Atomic state machine for the core loop"""
    OBSERVING = "OBSERVING"
    GENERATING = "GENERATING"
    BACKTESTING = "BACKTESTING"
    DEPLOYING = "DEPLOYING"
    ERROR = "ERROR"
    INITIALIZING = "INITIALIZING"

class AgentCore:
    """
    Persistent core process implementing the Trinity Framework.
    Maintains state, coordinates modules, and ensures crash recovery.
    """
    
    def __init__(self, project_id: str = "automata-primus"):
        # Initialize robust logging
        self.logger = init_logging(__name__)
        self.project_id = project_id
        
        # Initialize components
        self.firestore_client = None
        self.observer = None
        self.generator = None
        self.learner = None
        
        # State tracking
        self.current_state = SystemState.INITIALIZING
        self.last_cycle_time = None
        self.cycle_count = 0
        self.error_backoff = ExponentialBackoff(max_delay=300)
        
        # Configuration
        self.config = {
            'observation_interval': 60,  # seconds
            'generation_interval': 10,   # cycles between generations
            'max_retries': 3,
            'simulation_days': 30
        }
        
    async def initialize(self) -> bool:
        """Initialize all components with comprehensive error handling"""
        try:
            self.logger.info(f"🚀 Initializing AUTOMATA PRIMUS v0.1")
            
            # Initialize Firebase
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {
                'projectId': self.project_id
            })
            self.firestore_client = firestore.client()
            self.logger.info("✅ Firebase initialized")
            
            # Initialize components
            self.observer = ObserverExecutor(self.firestore_client)
            self.generator = StrategyGenerator(self.firestore_client)
            self.learner = LearnerDeployer(self.firestore_client)
            
            # Verify system state document exists
            await self._ensure_system_state()
            
            self.current_state = SystemState.OBSERVING
            self.logger.info("🎯 System initialized and ready")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Initialization failed: {str(e)}")
            self.logger.debug(traceback.format_exc())
            return False
    
    async def _ensure_system_state(self):
        """Ensure system state document exists in Firestore"""
        state_ref = self.firestore_client.collection('system_state').document('core')
        
        if not state_ref.get().exists:
            await state_ref.set({
                'current_state': SystemState.INITIALIZING.value,
                'last_updated': firestore.SERVER_TIMESTAMP,
                'cycle_count': 0,
                'active_strategy_id': None,
                'version': '0.1',
                'health_checks': []
            })
    
    async def update_system_state(self, state: SystemState, metadata: Optional[Dict] = None):
        """Atomically update system state in Firestore"""
        try:
            update_data = {
                'current_state': state.value,
                'last_updated': firestore.SERVER_TIMESTAMP,
                'cycle_count': self.cycle_count
            }
            
            if metadata:
                update_data['metadata'] = metadata
            
            await self.firestore_client.collection('system_state').document('core').update(update_data)
            self.current_state = state
            self.logger.debug(f"System state updated to {state.value}")
            
        except Exception as e:
            self.logger.error(f"Failed to update system state: {str(e)}")
            raise
    
    async def run_cycle(self):
        """Execute one complete cycle of the agent's operation"""
        cycle_start = datetime.utcnow()
        self.cycle_count += 1
        
        try:
            # PHASE 1: OBSERVE AND EXECUTE
            await self.update_system_state(SystemState.OBSERVING)
            observation_result = await self.observer.execute_cycle()
            
            if observation_result.get('trade_executed'):
                self.logger.info(f"💰 Trade executed: {observation_result}")
            
            # PHASE 2: GENERATE NEW STRATEGIES (periodically)
            if self.cycle_count % self.config['generation_interval'] == 0:
                await self.update_system_state(SystemState.GENERATING)
                generation_result = await self.generator.generate_new_strategies()
                
                if generation_result.get('candidates_generated', 0) > 0:
                    # PHASE 3: LEARN AND DEPLOY
                    await self.update_system_state(SystemState.BACKTESTING)
                    deployment_result = await self.learner.evaluate_and_deploy()
                    
                    if deployment_result.get('strategy_deployed'):
                        await self.update_system_state(SystemState.DEPLOYING, {
                            'new_strategy_id': deployment_result['strategy_id']
                        })
                        self.logger.info(f"🔄 New strategy deployed: {deployment_result['strategy_id']}")
            
            # Reset error backoff on successful cycle
            self.error_backoff.reset()
            
            # Log cycle completion
            cycle_duration = (datetime.utcnow() - cycle_start).total_seconds()
            self.logger.debug(f"Cycle {self.cycle_count} completed in {cycle_duration:.2f}s")
            
        except Exception as e:
            self.logger.error(f"❌ Cycle {self.cycle_count} failed: {str(e)}")
            self.logger.debug(traceback.format_exc())
            
            await self.update_system_state(SystemState.ERROR, {
                'error': str(e),
                'cycle': self.cycle_count
            })
            
            # Implement exponential backoff
            delay = self.error_backoff.next_delay()
            self.logger.warning(f"⏸️ Backing off for {delay:.1f}s")
            await asyncio.sleep(delay)
    
    async def run_continuously(self):
        """Main execution loop with graceful shutdown"""
        self.logger.info("Starting continuous operation...")
        
        if not await self.initialize():
            self.logger.error("Failed to initialize. Exiting.")
            return
        
        try:
            while True:
                await self.run_cycle()
                await asyncio.sleep(self.config['observation_interval'])
                
        except KeyboardInterrupt:
            self.logger.info("🛑 Shutdown signal received")
        except Exception as e:
            self.logger.critical(f"Fatal error: {str(e)}")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Graceful shutdown procedure"""
        self.logger.info("Shutting down...")
        
        # Update system state
        try:
            await self.update_system_state(SystemState.ERROR, {
                'shutdown': True,
                'timestamp': datetime.utcnow().isoformat()
            })
        except:
            pass
        
        self.logger.info("Shutdown complete")

async def main():
    """Entry point for the persistent agent"""
    agent = AgentCore()
    await agent.run_continuously()

if __name__ == "__main__":
    asyncio.run(main())