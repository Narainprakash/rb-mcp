import os
import yaml
import json
from dotenv import load_dotenv

load_dotenv()

class SystemConfig:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SystemConfig, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.yaml")
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file not found at {config_path}")
        
        with open(config_path, "r") as f:
            self.settings = yaml.safe_load(f)

    @property
    def polling(self):
        return self.settings.get('polling', {})
    
    @property
    def dashboard(self):
        return self.settings.get('dashboard', {})

    @property
    def gateway(self):
        return self.settings.get('gateway', {})

    @property
    def discord_webhook_url(self):
        return os.getenv("DISCORD_WEBHOOK_URL")

    @property
    def twitter_bearer_token(self):
        return os.getenv("TWITTER_BEARER_TOKEN")

    @property
    def twitter_api_key(self):
        return os.getenv("TWITTER_API_KEY")
        
    @property
    def twitter_api_secret(self):
        return os.getenv("TWITTER_API_SECRET")
        
    @property
    def twitter_access_token(self):
        return os.getenv("TWITTER_ACCESS_TOKEN")
        
    @property
    def twitter_access_token_secret(self):
        return os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

# Global singleton for system-wide configuration
system_config = SystemConfig()

class UserConfigManager:
    """Manages retrieving and merging user-specific configurations from the database."""
    
    def __init__(self, user_id):
        self.user_id = user_id
        self._system_defaults = system_config.settings
        self._user_settings = self._load_user_settings()

    def _load_user_settings(self):
        # Local import to avoid circular dependency
        from src.core.db import get_connection
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT config_json FROM user_configs WHERE user_id = ?", (self.user_id,))
            row = cursor.fetchone()
            if row and row['config_json']:
                return json.loads(row['config_json'])
            return {}
        finally:
            conn.close()

    def _merge(self, section):
        default = self._system_defaults.get(section, {})
        user_override = self._user_settings.get(section, {})
        merged = default.copy()
        merged.update(user_override)
        return merged

    @property
    def decision(self):
        return self._merge('decision')
    
    @property
    def execution(self):
        return self._merge('execution')
    
    @property
    def notifications(self):
        return self._merge('notifications')
        
    @property
    def summary(self):
        return self._merge('summary')

def get_user_config(user_id):
    """Factory function to get a UserConfigManager for a specific user."""
    return UserConfigManager(user_id)

