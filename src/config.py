"""
配置加载器模块
负责加载和管理系统配置
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv


class Config:
    """配置管理类"""

    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}

    def __new__(cls) -> 'Config':
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._config:
            self._load_config()

    def _load_config(self) -> None:
        """加载配置文件和环境变量"""
        # 加载 .env 文件
        project_root = Path(__file__).parent.parent
        env_path = project_root / '.env'
        load_dotenv(env_path)

        # 加载 config.yaml
        config_path = project_root / 'config' / 'config.yaml'
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
        else:
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        # 加载 API 密钥
        self._config['api'] = {
            'key': os.getenv('BINANCE_API_KEY', ''),
            'secret': os.getenv('BINANCE_API_SECRET', ''),
            'testnet': os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'
        }

    def reload(self) -> None:
        """重新加载配置"""
        self._config = {}
        self._load_config()

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持点号分隔的嵌套路径
        例如: config.get('risk.position_size')
        """
        keys = key.split('.')
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    @property
    def market(self) -> Dict[str, Any]:
        """市场配置"""
        return self._config.get('market', {})

    @property
    def blacklist(self) -> List[str]:
        """黑名单币种"""
        return self.market.get('blacklist', [])

    @property
    def thresholds(self) -> Dict[str, Any]:
        """阈值配置"""
        return self._config.get('thresholds', {})

    @property
    def risk(self) -> Dict[str, Any]:
        """风险管理配置"""
        return self._config.get('risk', {})

    @property
    def take_profit(self) -> Dict[str, Any]:
        """止盈配置"""
        return self._config.get('take_profit', {})

    @property
    def market_protection(self) -> Dict[str, Any]:
        """大盘保护配置"""
        return self._config.get('market_protection', {})

    @property
    def rate_limit(self) -> Dict[str, Any]:
        """限频配置"""
        return self._config.get('rate_limit', {})

    @property
    def data_settings(self) -> Dict[str, Any]:
        """数据设置"""
        return self._config.get('data', {})

    @property
    def mode(self) -> Dict[str, Any]:
        """运行模式"""
        return self._config.get('mode', {})

    @property
    def logging_config(self) -> Dict[str, Any]:
        """日志配置"""
        return self._config.get('logging', {})

    @property
    def futures(self) -> Dict[str, Any]:
        """合约配置"""
        return self._config.get('futures', {})

    @property
    def api_key(self) -> str:
        """API Key"""
        return self._config.get('api', {}).get('key', '')

    @property
    def api_secret(self) -> str:
        """API Secret"""
        return self._config.get('api', {}).get('secret', '')

    @property
    def is_testnet(self) -> bool:
        """是否使用测试网络"""
        return self._config.get('api', {}).get('testnet', False)

    @property
    def is_paper_trading(self) -> bool:
        """是否为模拟交易模式"""
        return self.mode.get('paper_trading', True)

    @property
    def is_debug(self) -> bool:
        """是否为调试模式"""
        return self.mode.get('debug', False)


# 全局配置实例
config = Config()
