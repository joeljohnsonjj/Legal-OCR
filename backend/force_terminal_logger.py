"""
Force Terminal Logger - Bypasses all logging systems to ensure terminal visibility
"""
import sys
import datetime
import os

class ForceTerminalLogger:
    """Logger that forces output to terminal regardless of other configurations"""
    
    def __init__(self):
        self.enabled = True
        # Force stdout to be unbuffered
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(line_buffering=True)
    
    def _write_message(self, level: str, message: str):
        """Write one line to the terminal (avoid duplicate print + stdout)."""
        if not self.enabled:
            return

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_msg = f"[{timestamp}] {level}: {message}"

        try:
            print(formatted_msg, flush=True)
        except UnicodeEncodeError:
            safe_message = message.encode("ascii", "ignore").decode("ascii")
            safe_formatted = f"[{timestamp}] {level}: {safe_message}"
            print(safe_formatted, flush=True)
        
    def info(self, message: str):
        """Log info message"""
        self._write_message("INFO", message)
        
    def warning(self, message: str):
        """Log warning message"""
        self._write_message("WARNING", message)
        
    def error(self, message: str):
        """Log error message"""
        self._write_message("ERROR", message)
        
    def debug(self, message: str):
        """Log debug message"""
        self._write_message("DEBUG", message)

# Global instance
force_logger = ForceTerminalLogger()

def test_logger():
    """Test function to verify the logger works"""
    force_logger.info("🧪 FORCE LOGGER TEST - This should appear in terminal")
    force_logger.warning("🚨 FORCE LOGGER WARNING TEST")
    force_logger.error("❌ FORCE LOGGER ERROR TEST")
    return "Force logger test completed"

if __name__ == "__main__":
    test_logger()