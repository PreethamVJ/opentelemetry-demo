"""The 10 practice incidents: how to inject them, the expected answer and the allowed fix.

Expected answers live only here (scoreboard data). The agent never imports this module.
"""
from __future__ import annotations

import os
import subprocess
import sys

FAULTCTL = os.getenv("FAULTCTL", r"D:\ON-Call\agent\devtools\faultctl.py")

SCENARIOS = {
    "payment_flag": {
        "inject": ["inject", "flag", "paymentFailure", "100%"],
        "service": "payment", "fault_class": "app_error",
        "fix": "toggle_flag paymentFailure off",
        "symptom": "customers cannot complete checkout",
    },
    "cart_flag": {
        "inject": ["inject", "flag", "cartFailure", "50%"],
        "service": "cart", "fault_class": "app_error",
        "fix": "toggle_flag cartFailure off",
        "symptom": "some customers get errors when using the cart",
    },
    "product_catalog_flag": {
        "inject": ["inject", "flag", "productCatalogFailure", "on"],
        "service": "product-catalog", "fault_class": "app_error",
        "fix": "toggle_flag productCatalogFailure off",
        "symptom": "a product page fails to load",
    },
    "recommendation_leak": {
        "inject": ["inject", "flag", "recommendationCacheFailure", "on"],
        "service": "recommendation", "fault_class": "app_error",
        "fix": "toggle_flag recommendationCacheFailure off, or restart_service recommendation",
        "symptom": "recommendations are getting slower over time",
    },
    "ad_high_cpu": {
        "inject": ["inject", "flag", "adHighCpu", "on"],
        "service": "ad", "fault_class": "cpu_throttle",
        "fix": "toggle_flag adHighCpu off",
        "symptom": "the shop is slow",
    },
    "traffic_flood": {
        "inject": ["inject", "flag", "loadGeneratorFloodHomepage", "on"],
        "service": "frontend", "fault_class": "traffic_flood",
        "fix": "throttle_load",
        "symptom": "the homepage is slow and overloaded",
    },
    "stopped_service": {
        "inject": ["inject", "stop", "shipping"],
        "service": "shipping", "fault_class": "container_down",
        "fix": "restart_service shipping",
        "symptom": "customers cannot complete checkout",
    },
    "cache_down": {
        "inject": ["inject", "stop", "valkey-cart"],
        "service": "valkey-cart", "fault_class": "container_down",
        "fix": "restart_service valkey-cart",
        "symptom": "the cart is failing for every customer",
    },
    "network_latency": {
        "inject": None,
        "service": "unknown", "fault_class": "network_fault",
        "fix": "none on the allowlist: escalate with findings",
        "symptom": "the shop is slow between some services",
    },
    "cpu_starved": {
        "inject": ["inject", "cpu", "product-catalog", "0.05"],
        "service": "product-catalog", "fault_class": "cpu_throttle",
        "fix": "restart_service product-catalog, or escalate",
        "symptom": "the shop is slow",
    },
}


def faultctl(*args: str) -> str:
    result = subprocess.run([sys.executable, FAULTCTL, *args], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"faultctl {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()
