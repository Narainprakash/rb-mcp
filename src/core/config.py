import os
import yaml
from dotenv import load_dotenv

load_dotenv()

class Config:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
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
    def decision(self):
        return self.settings.get('decision', {})
    
    @property
    def execution(self):
        return self.settings.get('execution', {})
    
    @property
    def notifications(self):
        return self.settings.get('notifications', {})
    
    @property
    def dashboard(self):
        return self.settings.get('dashboard', {})

    @property
    def gateway(self):
        return self.settings.get('gateway', {})
        
    @property
    def summary(self):
        return self.settings.get('summary', {})

    @property
    def discord_webhook_url(self):
        return os.getenv("DISCORD_WEBHOOK_URL")

    @property
    def twitter_bearer_token(self):
        return os.getenv("TWITTER_BEARER_TOKEN")

config = Config()
