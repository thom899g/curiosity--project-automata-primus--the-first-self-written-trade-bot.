"""
Observer & Executor Module
Responsible for market perception and trade execution
"""
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np
from web3 import Web3
from google.cloud.firestore_v1 import Client as FirestoreClient

from utils import init_logging, retry_with_backoff

class ObserverExecutor:
    """
    Market observer and trade executor with multi-RPC fallback logic
    """
    
    def __init__(self, firestore_client: FirestoreClient):
        self.logger = init_logging(__name__)
        self.firestore = firestore_client
        
        # Testnet configurations
        self.rpc_endpoints = [
            'https://eth-sepolia.g.alchemy.com/v2/demo',
            'https://sepolia.infura.io/v3/9aa3d95b3bc440fa88ea12eaa4456161',
            'https://rpc.sepolia.org'
        ]
        
        # Initialize Web3 connections
        self.web3_connections = []
        for endpoint in self.rpc_endpoints:
            try:
                w3 = Web3(Web3.HTTPProvider(endpoint, request_kwargs={'timeout': 10}))
                if w3.is_connected():
                    self.web3_connections.append(w3)
                    self.logger.info(f"✅ Connected to RPC: {endpoint.split('//')[-1][:30]}...")
            except Exception as e:
                self.logger.warning(f"Failed to connect to {endpoint}: {str(e)}")
        
        if not self.web3_connections:
            raise ConnectionError("No working RPC endpoints available")
        
        # Trading parameters
        self.test_token_address = "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"  # UNI on Sepolia
        self.wallet_address = "0x742d35Cc6634C0532925a3b844Bc9eE2a43bE0C3"  # Test address
        self.min_profit_threshold = 0.001  # 0.1% minimum profit
        
        # Local SQLite for high-frequency data
        self.init_local_storage()
    
    def init_local_storage(self):
        """Initialize local SQLite database for market data"""
        import sqlite3
        self.db_conn = sqlite3.connect('agent_memory.db', check_same_thread=False)
        self._create_tables()
    
    def _create_tables(self):
        """Create necessary tables in local SQLite"""
        cursor = self.db_conn.cursor()
        
        # Market data table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_ticks (
                timestamp DATETIME PRIMARY KEY,
                price REAL,
                volume REAL,
                source TEXT,
                latency_ms INTEGER
            )
        ''')
        
        # Trade log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_log (
                id TEXT PRIMARY KEY,
                timestamp DATETIME,
                strategy_id TEXT,
                action TEXT,
                price REAL,
                amount REAL,
                simulated_slippage REAL,
                execution_status TEXT,
                gas_used INTEGER,
                profit_loss REAL
            )
        ''')
        
        self.db_conn.commit()
    
    @retry_with_backoff(max_retries=3)
    async def get_market_data(self) -> Dict[str, Any]:
        """Fetch market data with multiple RPC fallback"""
        for w3 in self.web3_connections:
            try:
                start_time = datetime.utcnow()
                
                # Get current block and gas price
                block = w3.eth.get_block('latest')
                gas_price = w3.eth.gas_price
                
                # Get token price (simplified - would use Uniswap V3 oracle in production)
                # For prototype, simulate price based on block number
                simulated_price = 1000 + (block.number % 1000) * 0.1
                
                latency = (datetime.utcnow() - start_time).total_seconds() * 1000
                
                market_data = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'block_number': block.number,
                    'gas_price_gwei': w3.from_wei(gas_price, 'gwei'),
                    'price': simulated_price,
                    'volume': float(block.gas_used) / 1_000_000,  # Simulated volume
                    'source': str(w3.provider),
                    'latency_ms': latency,
                    'fresh': latency < 5000  # 5 second freshness threshold
                }
                
                # Store in local SQLite
                self._store_market_tick(market_data)
                
                return market_data
                
            except Exception as e:
                self.logger.warning(f"RPC failed: {str(e)}")
                continue
        
        raise ConnectionError("All RPC endpoints failed")
    
    def _store_market_tick(self, market_data: Dict):
        """Store market data in local SQLite"""
        cursor = self.db_conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO market_ticks 
            (timestamp, price, volume, source, latency_ms)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            market_data['timestamp'],
            market_data['price'],
            market_data['volume'],
            market_data['source'],
            market_data['latency_ms']
        ))
        self.db_conn.commit()
    
    async def get_current_strategy(self) -> Optional[Dict]:
        """Fetch current active strategy from Firestore"""
        try:
            strategy_ref = self.firestore.collection('strategies').document('active')
            strategy_doc = strategy_ref.get()
            
            if strategy_doc.exists:
                strategy = strategy_doc.to_dict()
                strategy['id'] = strategy_doc.id
                return strategy
            
            # If no active strategy, create default
            default_strategy = {
                'logic_type': 'grid',
                'params': {
                    'grid_levels': 5,
                    'spread_percent': 0.5,
                    'base_price': 1000.0
                },
                'created_at': datetime.utcnow().isoformat(),
                'performance_score': 0.0,
                'generation': 0
            }
            
            await strategy_ref.set(default_strategy)
            return {**default_strategy, 'id': 'active'}
            
        except Exception as e:
            self.logger.error(f"Failed to get strategy: {str(e)}")
            return None
    
    def compute_action(self, strategy: Dict, market_data: Dict) -> Dict:
        """Compute trading action based on strategy logic"""
        action = {
            'action': 'HOLD',
            'reason': 'No trigger conditions met',
            'confidence': 0.0,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            if strategy['logic_type'] == 'grid':
                action = self._grid_strategy(strategy['params'], market_data)
            elif strategy['logic_type'] == 'ma_crossover':
                action = self._ma_crossover_strategy(strategy['params'], market_data)
            elif strategy['logic_type'] == 'mean_reversion':
                action = self._mean_reversion_strategy(strategy['params'], market_data)
            
        except Exception as e:
            self