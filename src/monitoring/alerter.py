"""
Observability and alerting system for live production issues.
"""
import time
import requests
from src.monitoring.logger import get_logger

log = get_logger("alerter")

class AlertManager:
    def __init__(self, webhook_url: str = None):
        self.webhook_url = webhook_url
        self.last_alert_time = {}
        
    def send_alert(self, title: str, message: str, level: str = "ERROR", cooldown: int = 300):
        """
        Send an alert, respecting the cooldown for the same title to avoid spam.
        """
        now = time.time()
        if title in self.last_alert_time:
            if now - self.last_alert_time[title] < cooldown:
                return  # Throttled
                
        self.last_alert_time[title] = now
        
        full_message = f"[{level}] {title}: {message}"
        log.error("system_alert", title=title, message=message, level=level)
        
        if self.webhook_url:
            try:
                # Basic slack-compatible payload
                payload = {"text": full_message}
                requests.post(self.webhook_url, json=payload, timeout=5)
            except Exception as e:
                log.error("webhook_failed", error=str(e))
                
# Global instance to be initialized if needed, or imported directly
alerter = AlertManager()
