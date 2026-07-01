import logging
import sys

def setup_logger():
    logger = logging.getLogger("script-server")
    logger.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)
    
    if not logger.handlers:
        logger.addHandler(console_handler)
        
    return logger

logger = setup_logger()
