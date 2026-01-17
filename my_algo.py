"""
Student Trading Algorithm Template
===================================
Connect to the exchange simulator, receive market data, submit orders, and GRAPH LIVE DATA.

    python student_algorithm.py --host ip:host --scenario normal_market --name your_name --password your_password --secure

YOUR TASK:
    Modify the `decide_order()` method to implement your trading strategy.
"""

import json
import websocket
import threading
import argparse
import time
import requests
import ssl
import urllib3
import matplotlib.pyplot as plt  # <--- NEW IMPORT
from typing import Dict, Optional

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TradingBot:
    """
    A trading bot that connects to the exchange simulator.
    """
    
    def __init__(self, student_id: str, host: str, scenario: str, password: str = None, secure: bool = False):
        self.student_id = student_id
        self.host = host
        self.scenario = scenario
        self.password = password
        self.secure = secure
        
        # Protocol configuration
        self.http_proto = "https" if secure else "http"
        self.ws_proto = "wss" if secure else "ws"
        
        # Session info (set after registration)
        self.token = None
        self.run_id = None
        
        # Trading state - track your position
        self.inventory = 0      # Current position (positive = long, negative = short)
        self.cash_flow = 0.0    # Cumulative cash from trades (negative when buying)
        self.pnl = 0.0          # Mark-to-market PnL (cash_flow + inventory * mid_price)
        self.current_step = 0   # Current simulation step
        self.orders_sent = 0    # Number of orders sent
        
        # Market data
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.last_mid = 0.0

        # --- NEW: Data storage for plotting ---
        self.history_steps = []
        self.history_mids = []
        self.history_bids = []
        self.history_asks = []
        # --------------------------------------
        
        # WebSocket connections
        self.market_ws = None
        self.order_ws = None
        self.running = True
        
        # Latency measurement
        self.last_done_time = None          # When we sent DONE
        self.step_latencies = []            # Time between DONE and next market data
        self.order_send_times = {}          # order_id -> time sent
        self.fill_latencies = []            # Time between order and fill
    
    # =========================================================================
    # REGISTRATION - Get a token to start trading
    # =========================================================================
    
    def register(self) -> bool:
        """Register with the server and get an auth token."""
        print(f"[{self.student_id}] Registering for scenario '{self.scenario}'...")
        try:
            url = f"{self.http_proto}://{self.host}/api/replays/{self.scenario}/start"
            headers = {"Authorization": f"Bearer {self.student_id}"}
            if self.password:
                headers["X-Team-Password"] = self.password
            resp = requests.get(
                url,
                headers=headers,
                timeout=10,
                verify=not self.secure  # Disable SSL verification for self-signed certs
            )
            
            if resp.status_code != 200:
                print(f"[{self.student_id}] Registration FAILED: {resp.text}")
                return False
            
            data = resp.json()
            self.token = data.get("token")
            self.run_id = data.get("run_id")
            
            if not self.token or not self.run_id:
                print(f"[{self.student_id}] Missing token or run_id")
                return False
            
            print(f"[{self.student_id}] Registered! Run ID: {self.run_id}")
            return True
            
        except Exception as e:
            print(f"[{self.student_id}] Registration error: {e}")
            return False
    
    # =========================================================================
    # CONNECTION - Connect to WebSocket streams
    # =========================================================================
    
    def connect(self) -> bool:
        """Connect to market data and order entry WebSockets."""
        try:
            # SSL options for self-signed certificates
            sslopt = {"cert_reqs": ssl.CERT_NONE} if self.secure else None
            
            # Market Data WebSocket
            market_url = f"{self.ws_proto}://{self.host}/api/ws/market?run_id={self.run_id}"
            self.market_ws = websocket.WebSocketApp(
                market_url,
                on_message=self._on_market_data,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=lambda ws: print(f"[{self.student_id}] Market data connected")
            )
            
            # Order Entry WebSocket
            order_url = f"{self.ws_proto}://{self.host}/api/ws/orders?token={self.token}&run_id={self.run_id}"
            self.order_ws = websocket.WebSocketApp(
                order_url,
                on_message=self._on_order_response,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=lambda ws: print(f"[{self.student_id}] Order entry connected")
            )
            
            # Start WebSocket threads
            threading.Thread(
                target=lambda: self.market_ws.run_forever(sslopt=sslopt),
                daemon=True
            ).start()
            
            threading.Thread(
                target=lambda: self.order_ws.run_forever(sslopt=sslopt),
                daemon=True
            ).start()
            
            # Wait for connections
            time.sleep(1)
            return True
            
        except Exception as e:
            print(f"[{self.student_id}] Connection error: {e}")
            return False
    
    # =========================================================================
    # MARKET DATA HANDLER - Called when new market data arrives
    # =========================================================================
    
    def _on_market_data(self, ws, message: str):
        """Handle incoming market data snapshot."""
        try:
            recv_time = time.time()
            data = json.loads(message)
            
            # Skip connection confirmation messages
            if data.get("type") == "CONNECTED":
                return
            
            # Measure step latency (time since we sent DONE)
            if self.last_done_time is not None:
                step_latency = (recv_time - self.last_done_time) * 1000  # ms
                self.step_latencies.append(step_latency)
            
            # Extract market data
            self.current_step = data.get("step", 0)
            self.last_bid = data.get("bid", 0.0)
            self.last_ask = data.get("ask", 0.0)
            
            # Log progress every 500 steps with latency stats
            if self.current_step % 500 == 0 and self.step_latencies:
                avg_lat = sum(self.step_latencies[-100:]) / min(len(self.step_latencies), 100)
                print(f"[{self.student_id}] Step {self.current_step} | Orders: {self.orders_sent} | Inv: {self.inventory} | Avg Latency: {avg_lat:.1f}ms")
            
            # Calculate mid price
            if self.last_bid > 0 and self.last_ask > 0:
                self.last_mid = (self.last_bid + self.last_ask) / 2
            elif self.last_bid > 0:
                self.last_mid = self.last_bid
            elif self.last_ask > 0:
                self.last_mid = self.last_ask
            else:
                self.last_mid = 0
            
            # --- NEW: Append data for graphing ---
            self.history_steps.append(self.current_step)
            self.history_mids.append(self.last_mid)
            self.history_bids.append(self.last_bid)
            self.history_asks.append(self.last_ask)
            # -------------------------------------
            
            # =============================================
            # YOUR STRATEGY LOGIC GOES HERE
            # =============================================
            order = self.decide_order(self.last_bid, self.last_ask, self.last_mid)
            
            if order and self.order_ws and self.order_ws.sock:
                self._send_order(order)
            
            # Signal DONE to advance to next step
            self._send_done()
            
        except Exception as e:
            print(f"[{self.student_id}] Market data error: {e}")
    
    # =========================================================================
    # YOUR STRATEGY - MODIFY THIS METHOD!
    # =========================================================================
    
    def decide_order(self, bid: float, ask: float, mid: float) -> Optional[Dict]:
        """
        Input: bid, ask, mid
        Return: {"side": "BUY"|"SELL", "price": X, "qty": N} or None
        """
        
        if mid <= 0 or bid <= 0 or ask <= 0:
            return None
        
        # Only trade every 50 steps
        if self.current_step % 50 != 0:
            return None
        
        # Strategy Logic
        if self.inventory > 200:
            return {"side": "SELL", "price": round(bid, 2), "qty": 100}
        elif self.inventory < -200:
            return {"side": "BUY", "price": round(ask, 2), "qty": 100}
        elif (self.current_step // 50) % 2 == 0:
            return {"side": "BUY", "price": round(ask, 2), "qty": 100}
        else:
            return {"side": "SELL", "price": round(bid, 2), "qty": 100}
    
    # =========================================================================
    # ORDER HANDLING
    # =========================================================================
    
    def _send_order(self, order: Dict):
        """Send an order to the exchange."""
        order_id = f"ORD_{self.student_id}_{self.current_step}_{self.orders_sent}"
        
        msg = {
            "order_id": order_id,
            "side": order["side"],
            "price": order["price"],
            "qty": order["qty"]
        }
        
        try:
            self.order_send_times[order_id] = time.time()
            self.order_ws.send(json.dumps(msg))
            self.orders_sent += 1
        except Exception as e:
            print(f"[{self.student_id}] Send order error: {e}")
    
    def _send_done(self):
        """Signal DONE to advance to the next simulation step."""
        try:
            self.order_ws.send(json.dumps({"action": "DONE"}))
            self.last_done_time = time.time()
        except:
            pass
    
    def _on_order_response(self, ws, message: str):
        """Handle order responses and fills."""
        try:
            recv_time = time.time()
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "AUTHENTICATED":
                print(f"[{self.student_id}] Authenticated - ready to trade!")
            
            elif msg_type == "FILL":
                qty = data.get("qty", 0)
                price = data.get("price", 0)
                side = data.get("side", "")
                order_id = data.get("order_id", "")
                
                if order_id in self.order_send_times:
                    fill_latency = (recv_time - self.order_send_times[order_id]) * 1000
                    self.fill_latencies.append(fill_latency)
                    del self.order_send_times[order_id]
                
                if side == "BUY":
                    self.inventory += qty
                    self.cash_flow -= qty * price
                else:
                    self.inventory -= qty
                    self.cash_flow += qty * price
                
                self.pnl = self.cash_flow + self.inventory * self.last_mid
                
                print(f"[{self.student_id}] FILL: {side} {qty} @ {price:.2f} | Inventory: {self.inventory} | PnL: {self.pnl:.2f}")
            
            elif msg_type == "ERROR":
                print(f"[{self.student_id}] ERROR: {data.get('message')}")
                
        except Exception as e:
            print(f"[{self.student_id}] Order response error: {e}")
    
    # =========================================================================
    # ERROR HANDLING
    # =========================================================================
    
    def _on_error(self, ws, error):
        if self.running:
            print(f"[{self.student_id}] WebSocket error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        self.running = False
        print(f"[{self.student_id}] Connection closed (status: {close_status_code})")
    
    # =========================================================================
    # MAIN RUN LOOP (Modified for Graphing)
    # =========================================================================
    
    def run(self):
        """Main entry point - register, connect, and run."""
        # Step 1: Register
        if not self.register():
            return
        
        # Step 2: Connect
        if not self.connect():
            return
        
        # Step 3: Run until complete
        print(f"[{self.student_id}] Running... Press Ctrl+C to stop")
        
        # --- GRAPH SETUP ---
        plt.ion()  # Interactive mode
        fig, ax = plt.subplots(figsize=(10, 6))
        
        # Initialize lines
        line_mid, = ax.plot([], [], 'g-', label='Mid Price', alpha=0.9)
        line_bid, = ax.plot([], [], 'b--', label='Bid', alpha=0.3)
        line_ask, = ax.plot([], [], 'r--', label='Ask', alpha=0.3)
        
        ax.set_title(f"Real-time Market Data: {self.scenario}")
        ax.set_xlabel("Step")
        ax.set_ylabel("Price")
        ax.legend(loc='upper left')
        ax.grid(True)
        # -------------------

        try:
            while self.running:
                # --- UPDATE GRAPH ---
                if len(self.history_steps) > 1:
                    # Limit to last 500 points for performance
                    steps = self.history_steps[-500:]
                    mids = self.history_mids[-500:]
                    bids = self.history_bids[-500:]
                    asks = self.history_asks[-500:]

                    line_mid.set_data(steps, mids)
                    line_bid.set_data(steps, bids)
                    line_ask.set_data(steps, asks)
                    
                    ax.relim()
                    ax.autoscale_view()
                    
                    plt.pause(0.5) # Update plot every 0.5s
                else:
                    time.sleep(1)
                # --------------------
                
        except KeyboardInterrupt:
            print(f"\n[{self.student_id}] Stopped by user")
        finally:
            self.running = False
            if self.market_ws:
                self.market_ws.close()
            if self.order_ws:
                self.order_ws.close()
            
            print(f"\n[{self.student_id}] Final Results:")
            print(f"  Orders Sent: {self.orders_sent}")
            print(f"  Inventory: {self.inventory}")
            print(f"  PnL: {self.pnl:.2f}")
            
            if self.step_latencies:
                print(f"\n  Step Latency (ms):")
                print(f"    Avg: {sum(self.step_latencies)/len(self.step_latencies):.1f}")
            
            # Keep plot open after finish
            print("[Info] Close plot window to exit.")
            plt.ioff()
            plt.show()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Student Trading Algorithm",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--name", required=True, help="Your team name")
    parser.add_argument("--password", required=True, help="Your team password")
    parser.add_argument("--scenario", default="normal_market", help="Scenario to run")
    parser.add_argument("--host", default="localhost:8080", help="Server host:port")
    parser.add_argument("--secure", action="store_true", help="Use HTTPS/WSS (for deployed servers)")
    args = parser.parse_args()
    
    bot = TradingBot(
        student_id=args.name,
        host=args.host,
        scenario=args.scenario,
        password=args.password,
        secure=args.secure
    )
    
    bot.run()