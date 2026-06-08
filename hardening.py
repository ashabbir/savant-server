"""
Production hardening utilities for Savant workspace system
Includes: retry logic, validation, rate limiting, error handling
"""
import time
import logging
import functools
import re
from datetime import datetime
from typing import Callable, Dict, Optional
from collections import defaultdict
from threading import Lock
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

# Rate limiting storage
_rate_limit_data = defaultdict(list)
_rate_limit_lock = Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# RETRY LOGIC WITH EXPONENTIAL BACKOFF
# ═══════════════════════════════════════════════════════════════════════════════

def retry_with_backoff(max_retries: int = 3, initial_delay: float = 0.5, max_delay: float = 10.0):
    """Retry operations with exponential backoff"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {e}")
                        time.sleep(min(delay, max_delay))
                        delay *= 2
                    else:
                        logger.error(f"{func.__name__} failed after {max_retries} attempts: {e}")
            
            raise last_exception
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_required_fields(data: Dict, required_fields: list) -> tuple:
    """Validate required fields are present and non-empty"""
    for field in required_fields:
        if field not in data:
            return False, f"Missing required field: {field}"
        
        value = data[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            return False, f"Field '{field}' cannot be empty"
    
    return True, None


def validate_string_length(value: str, field_name: str, max_length: int = 1000) -> tuple:
    """Validate string field length"""
    if len(value) > max_length:
        return False, f"Field '{field_name}' exceeds maximum length of {max_length} characters"
    return True, None


def validate_enum(value: str, field_name: str, allowed_values: list) -> tuple:
    """Validate enum values"""
    if value not in allowed_values:
        return False, f"Field '{field_name}' must be one of: {', '.join(allowed_values)}"
    return True, None


def sanitize_text(text: str) -> str:
    """Sanitize text input to prevent XSS"""
    if not isinstance(text, str):
        return str(text)
    
    # Remove HTML tags
    text = re.sub(r'<[^>]*>', '', text)
    
    # Remove script injections
    text = re.sub(r'(javascript:|data:|vbscript:)', '', text, flags=re.IGNORECASE)
    
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════════

def check_rate_limit(ip: str, max_requests: int = 10, window_seconds: int = 1) -> tuple:
    """Check if IP has exceeded rate limit"""
    try:
        from flask import current_app
        if current_app and (current_app.testing or current_app.config.get("TESTING")):
            return True, None
    except Exception:
        pass

    with _rate_limit_lock:
        now = time.time()
        cutoff = now - window_seconds
        
        # Remove old entries
        _rate_limit_data[ip] = [t for t in _rate_limit_data[ip] if t > cutoff]
        
        # Check limit
        if len(_rate_limit_data[ip]) >= max_requests:
            return False, f"Rate limit exceeded: {max_requests} requests per {window_seconds} second(s)"
        
        # Add current request
        _rate_limit_data[ip].append(now)
        
        return True, None


def rate_limit(max_requests: int = 100, window_seconds: int = 60):
    """Decorator for rate limiting endpoints (default: 100 req/min)"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            allowed, error_msg = check_rate_limit(ip, max_requests, window_seconds)
            
            if not allowed:
                return jsonify({"error": error_msg}), 429
            
            return func(*args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST VALIDATION DECORATOR
# ═══════════════════════════════════════════════════════════════════════════════

def validate_request(required_fields: list = None, max_length: Dict[str, int] = None, 
                     enum_fields: Dict[str, list] = None):
    """Decorator for validating request data"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                data = request.get_json(force=True, silent=True) or {}
            except Exception:
                return jsonify({"error": "Invalid JSON in request body"}), 400
            
            # Validate required fields
            if required_fields:
                valid, error = validate_required_fields(data, required_fields)
                if not valid:
                    return jsonify({"error": error}), 400
            
            # Validate max lengths
            if max_length:
                for field, max_len in max_length.items():
                    if field in data and isinstance(data[field], str):
                        valid, error = validate_string_length(data[field], field, max_len)
                        if not valid:
                            return jsonify({"error": error}), 400
            
            # Validate enum values
            if enum_fields:
                for field, allowed in enum_fields.items():
                    if field in data:
                        valid, error = validate_enum(data[field], field, allowed)
                        if not valid:
                            return jsonify({"error": error}), 400
            
            # Sanitize text fields
            for key, value in data.items():
                if isinstance(value, str):
                    data[key] = sanitize_text(value)
            
            # Store validated data
            g.validated_data = data
            
            return func(*args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# QUERY SAFETY
# ═══════════════════════════════════════════════════════════════════════════════

def safe_limit(requested_limit: Optional[int], default: int = 100, maximum: int = 1000) -> int:
    """Ensure query limit is within safe bounds"""
    if requested_limit is None:
        return default
    
    return min(max(1, requested_limit), maximum)
